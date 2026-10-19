"""The ledger: the write-ahead record that makes a launch crash-safe.

The row is written before any external side effect and its ``step`` advances after
each one, so a tick that dies mid-launch leaves enough behind for the next tick to
adopt, resume, or fail the worker loudly. Control commands also land here: a
mutating command records a request and the tick applies it, which keeps a single
writer of side effects.

A *worker* is one member of a role. A role may have any number of workers. A worker
usually has one work item, the pull request or issue it was created for, and it owns a
worktree and, over its life, one or more agent sessions; the ``sessions`` table keeps
them. A worker created by a start rule has ``origin`` ``source``; one created by hand
has ``board`` or ``clone``, and may have no work item at all.

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

SCHEMA_VERSION = 2

# Worker lifecycle. A worker that a start rule created occupies its (role, item) pair
# until it reaches a terminal state.
LAUNCHING = "launching"
RUNNING = "running"
BLOCKED = "blocked"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

NON_TERMINAL = (LAUNCHING, RUNNING, BLOCKED, PAUSED)
TERMINAL = (DONE, FAILED, CANCELLED)
WORKER_STATES = NON_TERMINAL + TERMINAL

# Session states as reported by Claude Code, plus `missing` for a session that has
# disappeared from the listing entirely.
SESSION_WORKING = "working"
SESSION_BLOCKED = "blocked"
SESSION_DONE = "done"
SESSION_FAILED = "failed"
SESSION_STOPPED = "stopped"
SESSION_MISSING = "missing"

LAST_STEP = 4

# Why a session row exists: the launch, an adoption, a continuation after a refresh,
# a retry, or a clone. Rows copied from a version 1 ledger carry `migrated`.
SESSION_INITIAL = "initial"
SESSION_ADOPTED = "adopted"
SESSION_MIGRATED = "migrated"

# How a worker came to exist. Only `source` workers are held to one per role and item.
ORIGIN_SOURCE = "source"
ORIGIN_BOARD = "board"
ORIGIN_CLONE = "clone"
ORIGINS = (ORIGIN_SOURCE, ORIGIN_BOARD, ORIGIN_CLONE)

_WORKER_COLUMNS = (
    "id",
    "role",
    "origin",
    "start_rule",
    "runtime",
    "item",
    "item_id",
    "url",
    "repository",
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
    "wakes",
    "last_wake_at",
    "created_at",
    "updated_at",
    "finished_at",
)

_LIVE = ", ".join(repr(s) for s in NON_TERMINAL)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS workers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    role             TEXT    NOT NULL,
    origin           TEXT    NOT NULL DEFAULT '{ORIGIN_SOURCE}',
    start_rule       TEXT,
    runtime          TEXT    NOT NULL DEFAULT 'claude',
    item             TEXT,
    item_id          TEXT,
    url              TEXT,
    repository       TEXT    NOT NULL,
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
    wakes            INTEGER NOT NULL DEFAULT 0,
    last_wake_at     REAL,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    finished_at      REAL
);

-- The structural guarantee against a duplicate launch: a start event arrives again on
-- every poll, so at most one live worker that a start rule created per role and work
-- item, enforced by the database rather than by a check the caller may skip. Workers
-- created by hand are not limited.
CREATE UNIQUE INDEX IF NOT EXISTS workers_one_live_per_role_item
    ON workers (role, item)
    WHERE origin = '{ORIGIN_SOURCE}' AND state IN ({_LIVE});

CREATE INDEX IF NOT EXISTS workers_state ON workers (state);
CREATE INDEX IF NOT EXISTS workers_worktree ON workers (worktree);
CREATE INDEX IF NOT EXISTS workers_item ON workers (item);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    INTEGER NOT NULL REFERENCES workers (id),
    session_id   TEXT,
    session_uuid TEXT,
    kind         TEXT    NOT NULL,
    started_at   REAL    NOT NULL,
    ended_at     REAL
);

CREATE INDEX IF NOT EXISTS sessions_worker ON sessions (worker_id);

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

# Version 1 kept one row per attempt at an issue. Every attempt becomes a worker in the
# `implementer` role bound to the issue, with its row id preserved so runner-state keys
# that name an id keep pointing at the right row. The old table is kept under another
# name for one release rather than dropped.
_MIGRATE_V1_TO_V2 = f"""
ALTER TABLE attempts RENAME TO attempts_v1;
{_SCHEMA}
INSERT INTO workers (id, role, origin, start_rule, runtime, item, item_id, url, repository,
                     repo, worktree, branch,
                     session_id, session_uuid, state, label_state, step, launch_attempts,
                     session_state, waiting_for, notified_state, working_since,
                     watchdog_notified, last_error, pr_url, gc_done, wakes, last_wake_at,
                     created_at, updated_at, finished_at)
SELECT id, 'implementer', '{ORIGIN_SOURCE}', 'queue', 'claude', identifier, issue_id, url,
       route, repo, worktree, branch,
       session_id, session_uuid, state, label_state, step, launch_attempts,
       session_state, waiting_for, notified_state, working_since,
       watchdog_notified, last_error, pr_url, gc_done, 0, NULL,
       created_at, updated_at, finished_at
FROM attempts_v1 ORDER BY id;
INSERT INTO sessions (worker_id, session_id, session_uuid, kind, started_at)
SELECT id, session_id, session_uuid, '{SESSION_MIGRATED}', created_at
FROM workers WHERE session_id IS NOT NULL;
"""


@dataclass(frozen=True)
class Worker:
    id: int
    role: str
    origin: str
    start_rule: str | None
    runtime: str
    item: str | None
    item_id: str | None
    url: str | None
    repository: str
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
    wakes: int
    last_wake_at: float | None
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

    @property
    def handle(self) -> str:
        """How a worker is named in logs, in reports, and as its session's display name."""
        return f"{self.role}/{self.item}" if self.item else f"{self.role}/#{self.id}"


@dataclass(frozen=True)
class SessionRecord:
    id: int
    worker_id: int
    session_id: str | None
    session_uuid: str | None
    kind: str
    started_at: float
    ended_at: float | None


def _row_to_worker(row: sqlite3.Row) -> Worker:
    return Worker(**{column: row[column] for column in _WORKER_COLUMNS})


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
        if version == 1:
            self._conn.executescript(_MIGRATE_V1_TO_V2)
        else:
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

    # ----- workers ----------------------------------------------------------

    def create_worker(
        self,
        *,
        role: str,
        item: str | None,
        item_id: str | None,
        url: str | None,
        repository: str,
        repo: Path,
        worktree: Path,
        branch: str,
        origin: str = ORIGIN_SOURCE,
        start_rule: str | None = None,
        runtime: str = "claude",
        now: float | None = None,
    ) -> Worker:
        """Step 0 of the launch sequence: the row exists before any side effect."""
        if origin not in ORIGINS:
            raise ValueError(f"unknown worker origin {origin!r}")
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            """
            INSERT INTO workers (role, origin, start_rule, runtime, item, item_id, url,
                                 repository, repo, worktree, branch, state, step,
                                 launch_attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (role, origin, start_rule, runtime, item, item_id, url, repository, str(repo),
             str(worktree), branch, LAUNCHING, stamp, stamp),
        )
        worker = self.get(int(cursor.lastrowid))
        assert worker is not None
        return worker

    def get(self, worker_id: int) -> Worker | None:
        row = self._conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return _row_to_worker(row) if row else None

    def live_worker_for(self, item: str, role: str | None = None) -> Worker | None:
        """The live worker on *item*, in *role* when given, else in any role."""
        workers = self.live_workers_for(item)
        if role is not None:
            workers = [w for w in workers if w.role == role]
        return workers[0] if workers else None

    def live_workers_for(self, item: str) -> list[Worker]:
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE item = ? AND state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) ORDER BY id",
            (item, *NON_TERMINAL),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def latest_worker_for(self, item: str, role: str | None = None) -> Worker | None:
        if role is None:
            row = self._conn.execute(
                "SELECT * FROM workers WHERE item = ? ORDER BY id DESC LIMIT 1", (item,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM workers WHERE item = ? AND role = ? ORDER BY id DESC LIMIT 1",
                (item, role),
            ).fetchone()
        return _row_to_worker(row) if row else None

    def live_workers(self) -> list[Worker]:
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE state IN ({', '.join('?' * len(NON_TERMINAL))}) "
            f"ORDER BY id",
            NON_TERMINAL,
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def workers_in_state(self, states: Sequence[str]) -> list[Worker]:
        if not states:
            return []
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE state IN ({', '.join('?' * len(states))}) ORDER BY id",
            tuple(states),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def workers_needing_gc(self) -> list[Worker]:
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE gc_done = 0 AND state IN "
            f"({', '.join('?' * len(TERMINAL))}) ORDER BY id",
            TERMINAL,
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def terminal_workers_since(self, seconds: float = 7 * 24 * 3600) -> list[Worker]:
        """Recently finished workers, for retrying a label write that did not land."""
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE state IN ({', '.join('?' * len(TERMINAL))}) "
            f"AND (finished_at IS NULL OR finished_at >= ?) ORDER BY id",
            (*TERMINAL, time.time() - seconds),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def recent_workers(self, limit: int = 50) -> list[Worker]:
        rows = self._conn.execute(
            "SELECT * FROM workers ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def worker_by_session_id(self, session_id: str) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
        return _row_to_worker(row) if row else None

    def worker_by_worktree(self, worktree: str) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE worktree = ? ORDER BY id DESC LIMIT 1", (worktree,)
        ).fetchone()
        return _row_to_worker(row) if row else None

    def live_count_by_role(self) -> dict[str, int]:
        rows = self._conn.execute(
            f"SELECT role, COUNT(*) AS n FROM workers WHERE state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) GROUP BY role",
            NON_TERMINAL,
        ).fetchall()
        return {row["role"]: int(row["n"]) for row in rows}

    def update(self, worker: Worker, **changes: Any) -> Worker:
        """Write *changes* to the worker's row and return the updated value."""
        if not changes:
            return worker
        unknown = set(changes) - set(_WORKER_COLUMNS)
        if unknown:
            raise ValueError(f"unknown worker column(s): {', '.join(sorted(unknown))}")
        if "state" in changes and changes["state"] not in WORKER_STATES:
            raise ValueError(f"unknown worker state: {changes['state']!r}")
        changes.setdefault("updated_at", time.time())
        if changes.get("state") in TERMINAL and "finished_at" not in changes:
            changes["finished_at"] = changes["updated_at"]
        assignments = ", ".join(f"{column} = ?" for column in changes)
        self._conn.execute(
            f"UPDATE workers SET {assignments} WHERE id = ?",
            (*changes.values(), worker.id),
        )
        return replace(worker, **changes)

    # ----- sessions ---------------------------------------------------------

    def record_session(
        self,
        worker: Worker,
        *,
        kind: str,
        session_id: str | None,
        session_uuid: str | None,
        now: float | None = None,
    ) -> None:
        """Remember a session the worker owned. Idempotent per session id."""
        if session_id and self._conn.execute(
            "SELECT 1 FROM sessions WHERE worker_id = ? AND session_id = ?",
            (worker.id, session_id),
        ).fetchone():
            return
        self._conn.execute(
            "INSERT INTO sessions (worker_id, session_id, session_uuid, kind, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (worker.id, session_id, session_uuid, kind, time.time() if now is None else now),
        )

    def sessions_of(self, worker: Worker) -> list[SessionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE worker_id = ? ORDER BY id", (worker.id,)
        ).fetchall()
        return [SessionRecord(**dict(row)) for row in rows]

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

    def is_paused(self, repository: str | None = None) -> bool:
        if self.get_state(self.GLOBAL_PAUSE_KEY) == "1":
            return True
        if repository is not None:
            return self.get_state(f"paused:{repository}") == "1"
        return False

    def set_paused(self, paused: bool, repository: str | None = None) -> None:
        key = self.GLOBAL_PAUSE_KEY if repository is None else f"paused:{repository}"
        if paused:
            self.set_state(key, "1")
        else:
            self.delete_state(key)

    def paused_repositories(self) -> list[str]:
        return sorted(
            key.split(":", 1)[1]
            for key, value in self.all_state().items()
            if key.startswith("paused:") and value == "1"
        )


def open_ledger(path: Path | None = None) -> Ledger:
    return Ledger(path)


def summarize(workers: Iterable[Worker]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for worker in workers:
        counts[worker.state] = counts.get(worker.state, 0) + 1
    return counts


__all__ = [
    "Worker",
    "SessionRecord",
    "Ledger",
    "open_ledger",
    "summarize",
    "closing",
    *WORKER_STATES,
]
