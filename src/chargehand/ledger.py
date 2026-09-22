"""The ledger: the write-ahead record that makes a launch crash-safe.

The row is written before any external side effect and its ``step`` advances after
each one, so a tick that dies mid-launch leaves enough behind for the next tick to
adopt, resume, or fail the attempt loudly. Control commands also land here: a
mutating command records a request and the tick applies it, which keeps a single
writer of side effects.

SQLite under the state directory, not /tmp, because it has to survive a reboot.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from chargehand import paths

SCHEMA_VERSION = 1

# Attempt lifecycle. An attempt occupies its issue until it reaches a terminal state.
LAUNCHING = "launching"
RUNNING = "running"
BLOCKED = "blocked"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

NON_TERMINAL = (LAUNCHING, RUNNING, BLOCKED, PAUSED)
TERMINAL = (DONE, FAILED, CANCELLED)
ATTEMPT_STATES = NON_TERMINAL + TERMINAL

# Session states as reported by Claude Code, plus `missing` for a session that has
# disappeared from the listing entirely.
SESSION_WORKING = "working"
SESSION_BLOCKED = "blocked"
SESSION_DONE = "done"
SESSION_FAILED = "failed"
SESSION_STOPPED = "stopped"
SESSION_MISSING = "missing"

LAST_STEP = 4

_ATTEMPT_COLUMNS = (
    "id",
    "issue_id",
    "identifier",
    "url",
    "route",
    "repo",
    "worktree",
    "branch",
    "session_id",
    "session_uuid",
    "state",
    "label_state",
    "step",
    "launch_attempts",
    "session_state",
    "waiting_for",
    "notified_state",
    "working_since",
    "watchdog_notified",
    "last_error",
    "pr_url",
    "gc_done",
    "created_at",
    "updated_at",
    "finished_at",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id         TEXT    NOT NULL,
    identifier       TEXT    NOT NULL,
    url              TEXT,
    route            TEXT    NOT NULL,
    repo             TEXT    NOT NULL,
    worktree         TEXT    NOT NULL,
    branch           TEXT    NOT NULL,
    session_id       TEXT,
    session_uuid     TEXT,
    state            TEXT    NOT NULL,
    -- The queue status chargehand last *verified* on the tracker. Kept apart from
    -- `state` so an ambiguous label write is retried on the next tick rather than
    -- assumed to have landed.
    label_state      TEXT,
    step             INTEGER NOT NULL DEFAULT 0,
    launch_attempts  INTEGER NOT NULL DEFAULT 1,
    session_state    TEXT,
    waiting_for      TEXT,
    notified_state   TEXT,
    working_since    REAL,
    watchdog_notified INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    pr_url           TEXT,
    gc_done          INTEGER NOT NULL DEFAULT 0,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    finished_at      REAL
);

-- The structural guarantee against a duplicate launch: at most one live attempt
-- per issue, enforced by the database rather than by a check the caller may skip.
CREATE UNIQUE INDEX IF NOT EXISTS attempts_one_live_per_issue
    ON attempts (identifier)
    WHERE state IN ({", ".join(repr(s) for s in NON_TERMINAL)});

CREATE INDEX IF NOT EXISTS attempts_state ON attempts (state);
CREATE INDEX IF NOT EXISTS attempts_worktree ON attempts (worktree);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    target      TEXT,
    args        TEXT    NOT NULL DEFAULT '{{}}',
    created_at  REAL    NOT NULL,
    applied_at  REAL,
    ok          INTEGER,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS requests_pending ON requests (applied_at);

CREATE TABLE IF NOT EXISTS runner_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Attempt:
    id: int
    issue_id: str
    identifier: str
    url: str | None
    route: str
    repo: str
    worktree: str
    branch: str
    session_id: str | None
    session_uuid: str | None
    state: str
    label_state: str | None
    step: int
    launch_attempts: int
    session_state: str | None
    waiting_for: str | None
    notified_state: str | None
    working_since: float | None
    watchdog_notified: int
    last_error: str | None
    pr_url: str | None
    gc_done: int
    created_at: float
    updated_at: float
    finished_at: float | None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def worktree_path(self) -> Path:
        return Path(self.worktree)

    @property
    def repo_path(self) -> Path:
        return Path(self.repo)


def _row_to_attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(**{column: row[column] for column in _ATTEMPT_COLUMNS})


class Ledger:
    """A thin, explicit wrapper over the SQLite file. No ORM, no lazy state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.ledger_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path}: ledger schema version {version} is newer than this "
                f"chargehand understands ({SCHEMA_VERSION}); upgrade chargehand"
            )
        if version == SCHEMA_VERSION:
            return
        # executescript() commits any open transaction, so migration runs outside one.
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ----- attempts ---------------------------------------------------------

    def create_attempt(
        self,
        *,
        issue_id: str,
        identifier: str,
        url: str | None,
        route: str,
        repo: Path,
        worktree: Path,
        branch: str,
        now: float | None = None,
    ) -> Attempt:
        """Step 0 of the launch sequence: the row exists before any side effect."""
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            """
            INSERT INTO attempts (issue_id, identifier, url, route, repo, worktree, branch,
                                  state, step, launch_attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (issue_id, identifier, url, route, str(repo), str(worktree), branch,
             LAUNCHING, stamp, stamp),
        )
        attempt = self.get(int(cursor.lastrowid))
        assert attempt is not None
        return attempt

    def get(self, attempt_id: int) -> Attempt | None:
        row = self._conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return _row_to_attempt(row) if row else None

    def live_attempt_for(self, identifier: str) -> Attempt | None:
        row = self._conn.execute(
            f"SELECT * FROM attempts WHERE identifier = ? AND state IN "
            f"({', '.join('?' * len(NON_TERMINAL))})",
            (identifier, *NON_TERMINAL),
        ).fetchone()
        return _row_to_attempt(row) if row else None

    def latest_attempt_for(self, identifier: str) -> Attempt | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE identifier = ? ORDER BY id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        return _row_to_attempt(row) if row else None

    def live_attempts(self) -> list[Attempt]:
        rows = self._conn.execute(
            f"SELECT * FROM attempts WHERE state IN ({', '.join('?' * len(NON_TERMINAL))}) "
            f"ORDER BY id",
            NON_TERMINAL,
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def attempts_in_state(self, states: Sequence[str]) -> list[Attempt]:
        if not states:
            return []
        rows = self._conn.execute(
            f"SELECT * FROM attempts WHERE state IN ({', '.join('?' * len(states))}) ORDER BY id",
            tuple(states),
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def attempts_needing_gc(self) -> list[Attempt]:
        rows = self._conn.execute(
            f"SELECT * FROM attempts WHERE gc_done = 0 AND state IN "
            f"({', '.join('?' * len(TERMINAL))}) ORDER BY id",
            TERMINAL,
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def terminal_attempts_since(self, seconds: float = 7 * 24 * 3600) -> list[Attempt]:
        """Recently finished attempts, for retrying a label write that did not land."""
        rows = self._conn.execute(
            f"SELECT * FROM attempts WHERE state IN ({', '.join('?' * len(TERMINAL))}) "
            f"AND (finished_at IS NULL OR finished_at >= ?) ORDER BY id",
            (*TERMINAL, time.time() - seconds),
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def recent_attempts(self, limit: int = 50) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def attempt_by_session_id(self, session_id: str) -> Attempt | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
        return _row_to_attempt(row) if row else None

    def attempt_by_worktree(self, worktree: str) -> Attempt | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE worktree = ? ORDER BY id DESC LIMIT 1", (worktree,)
        ).fetchone()
        return _row_to_attempt(row) if row else None

    def update(self, attempt: Attempt, **changes: Any) -> Attempt:
        """Write *changes* to the attempt's row and return the updated value."""
        if not changes:
            return attempt
        unknown = set(changes) - set(_ATTEMPT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown attempt column(s): {', '.join(sorted(unknown))}")
        if "state" in changes and changes["state"] not in ATTEMPT_STATES:
            raise ValueError(f"unknown attempt state: {changes['state']!r}")
        changes.setdefault("updated_at", time.time())
        if changes.get("state") in TERMINAL and "finished_at" not in changes:
            changes["finished_at"] = changes["updated_at"]
        assignments = ", ".join(f"{column} = ?" for column in changes)
        self._conn.execute(
            f"UPDATE attempts SET {assignments} WHERE id = ?",
            (*changes.values(), attempt.id),
        )
        return replace(attempt, **changes)

    # ----- control requests -------------------------------------------------

    def record_request(self, kind: str, target: str | None = None, **args: Any) -> int:
        cursor = self._conn.execute(
            "INSERT INTO requests (kind, target, args, created_at) VALUES (?, ?, ?, ?)",
            (kind, target, json.dumps(args, sort_keys=True), time.time()),
        )
        return int(cursor.lastrowid)

    def pending_requests(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE applied_at IS NULL ORDER BY id"
        ).fetchall()
        return [self._request_to_dict(row) for row in rows]

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._request_to_dict(row) if row else None

    def complete_request(self, request_id: int, *, ok: bool, message: str) -> None:
        self._conn.execute(
            "UPDATE requests SET applied_at = ?, ok = ?, message = ? WHERE id = ?",
            (time.time(), 1 if ok else 0, message, request_id),
        )

    @staticmethod
    def _request_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["args"] = json.loads(data["args"])
        except (TypeError, ValueError):
            data["args"] = {}
        return data

    def prune_requests(self, older_than_secs: float = 7 * 24 * 3600) -> int:
        cursor = self._conn.execute(
            "DELETE FROM requests WHERE applied_at IS NOT NULL AND applied_at < ?",
            (time.time() - older_than_secs,),
        )
        return cursor.rowcount

    # ----- runner state -----------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO runner_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM runner_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def delete_state(self, key: str) -> None:
        self._conn.execute("DELETE FROM runner_state WHERE key = ?", (key,))

    def all_state(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM runner_state").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ----- pause ------------------------------------------------------------

    GLOBAL_PAUSE_KEY = "paused"

    def is_paused(self, route: str | None = None) -> bool:
        if self.get_state(self.GLOBAL_PAUSE_KEY) == "1":
            return True
        if route is not None:
            return self.get_state(f"paused:{route}") == "1"
        return False

    def set_paused(self, paused: bool, route: str | None = None) -> None:
        key = self.GLOBAL_PAUSE_KEY if route is None else f"paused:{route}"
        if paused:
            self.set_state(key, "1")
        else:
            self.delete_state(key)

    def paused_routes(self) -> list[str]:
        return sorted(
            key.split(":", 1)[1]
            for key, value in self.all_state().items()
            if key.startswith("paused:") and value == "1"
        )


def open_ledger(path: Path | None = None) -> Ledger:
    return Ledger(path)


def summarize(attempts: Iterable[Attempt]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.state] = counts.get(attempt.state, 0) + 1
    return counts


__all__ = [
    "Attempt",
    "Ledger",
    "open_ledger",
    "summarize",
    "closing",
    *ATTEMPT_STATES,
]
