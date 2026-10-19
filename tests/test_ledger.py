"""The ledger's guarantees: one live attempt per issue, and durable control requests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chargehand import ledger as ledger_mod
from chargehand.ledger import Ledger


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    with Ledger(tmp_path / "ledger.sqlite") as handle:
        yield handle


def make(ledger: Ledger, item: str = "ABC-1", worktree: str = "/w/ABC-1", role: str = "implementer"):
    return ledger.create_worker(
        role=role,
        item=item,
        item_id=f"id-{item}",
        url=None,
        repository="test",
        repo=Path("/repo"),
        worktree=Path(worktree),
        branch=f"chargehand/{role}/{item}",
    )


def test_a_new_attempt_starts_at_step_zero(ledger):
    attempt = make(ledger)

    assert attempt.state == ledger_mod.LAUNCHING
    assert attempt.step == 0
    assert attempt.launch_attempts == 1
    assert attempt.label_state is None


def test_the_database_itself_prevents_a_duplicate_live_attempt(ledger):
    make(ledger)

    with pytest.raises(sqlite3.IntegrityError):
        make(ledger, worktree="/w/other")


def test_a_new_attempt_is_allowed_once_the_previous_one_ended(ledger):
    first = make(ledger)
    ledger.update(first, state=ledger_mod.FAILED)

    second = make(ledger)

    assert second.id != first.id
    assert ledger.live_worker_for("ABC-1").id == second.id
    assert ledger.latest_worker_for("ABC-1").id == second.id


def test_reaching_a_terminal_state_stamps_the_finish_time(ledger):
    attempt = ledger.update(make(ledger), state=ledger_mod.DONE)

    assert attempt.is_terminal
    assert attempt.finished_at is not None


def test_an_unknown_column_is_rejected_rather_than_silently_dropped(ledger):
    with pytest.raises(ValueError, match="unknown worker column"):
        ledger.update(make(ledger), stat="running")


def test_an_unknown_state_is_rejected(ledger):
    with pytest.raises(ValueError, match="unknown worker state"):
        ledger.update(make(ledger), state="vibing")


def test_an_attempt_can_be_found_by_its_session_id(ledger):
    attempt = ledger.update(make(ledger), session_id="s1")

    assert ledger.worker_by_session_id("s1").id == attempt.id
    assert ledger.worker_by_session_id("nope") is None


def test_requests_are_pending_until_a_tick_completes_them(ledger):
    request_id = ledger.record_request("cancel", "ABC-1", force=True)

    assert [r["kind"] for r in ledger.pending_requests()] == ["cancel"]
    assert ledger.get_request(request_id)["args"] == {"force": True}

    ledger.complete_request(request_id, ok=False, message="refused")

    assert ledger.pending_requests() == []
    assert ledger.get_request(request_id)["ok"] == 0


def test_pausing_is_global_or_per_route(ledger):
    assert not ledger.is_paused("test")

    ledger.set_paused(True, "test")
    assert ledger.is_paused("test")
    assert not ledger.is_paused("other")
    assert ledger.paused_repositories() == ["test"]

    ledger.set_paused(True)
    assert ledger.is_paused("other")

    ledger.set_paused(False)
    ledger.set_paused(False, "test")
    assert ledger.paused_repositories() == []


def test_the_ledger_survives_being_reopened(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as first:
        make(first)
    with Ledger(path) as second:
        assert second.live_worker_for("ABC-1") is not None


def test_a_newer_schema_is_refused_rather_than_corrupted(tmp_path):
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={ledger_mod.SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(RuntimeError, match="upgrade chargehand"):
        Ledger(path)


def test_applied_requests_are_pruned_but_pending_ones_are_kept(ledger):
    old = ledger.record_request("tick")
    ledger.complete_request(old, ok=True, message="")
    pending = ledger.record_request("cancel", "ABC-1")

    assert ledger.prune_requests(older_than_secs=-1) == 1
    assert [r["id"] for r in ledger.pending_requests()] == [pending]


# ----- version 2: workers, roles, sessions --------------------------------------

V1_SCHEMA = """
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT NOT NULL, identifier TEXT NOT NULL,
    url TEXT, route TEXT NOT NULL, repo TEXT NOT NULL, worktree TEXT NOT NULL,
    branch TEXT NOT NULL, session_id TEXT, session_uuid TEXT, state TEXT NOT NULL,
    label_state TEXT, step INTEGER NOT NULL DEFAULT 0, launch_attempts INTEGER NOT NULL DEFAULT 1,
    session_state TEXT, waiting_for TEXT, notified_state TEXT, working_since REAL,
    watchdog_notified INTEGER NOT NULL DEFAULT 0, last_error TEXT, pr_url TEXT,
    gc_done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE UNIQUE INDEX attempts_one_live_per_issue ON attempts (identifier)
    WHERE state IN ('launching', 'running', 'blocked', 'paused');
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT,
    args TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, applied_at REAL, ok INTEGER,
    message TEXT
);
CREATE TABLE runner_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
PRAGMA user_version = 1;
"""


def test_a_version_one_ledger_is_migrated_to_workers(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO attempts (id, issue_id, identifier, url, route, repo, worktree, branch, "
        "session_id, session_uuid, state, step, created_at, updated_at) "
        "VALUES (7, 'uuid-1', 'ABC-1', 'https://t.invalid/ABC-1', 'main', '/repo', '/w/ABC-1', "
        "'chargehand/ABC-1', 's1', 'u1', 'running', 4, 1.0, 2.0)"
    )
    conn.execute("INSERT INTO runner_state VALUES ('notify-pending:7', 'x', 1.0)")
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        worker = ledger.get(7)
        assert worker is not None
        assert (worker.role, worker.item, worker.item_id) == ("implementer", "ABC-1", "uuid-1")
        assert (worker.repository, worker.state, worker.step) == ("main", ledger_mod.RUNNING, 4)
        assert worker.wakes == 0
        assert (worker.origin, worker.start_rule, worker.runtime) == (
            ledger_mod.ORIGIN_SOURCE, "queue", "claude"
        )
        assert ledger.live_worker_for("ABC-1").id == 7
        sessions = ledger.sessions_of(worker)
        assert [(s.session_id, s.kind) for s in sessions] == [("s1", ledger_mod.SESSION_MIGRATED)]
        assert ledger.get_state("notify-pending:7") == "x"
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        # The old table is kept for one release rather than dropped.
        kept = ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'attempts_v1'"
        ).fetchone()
        assert kept is not None


def test_a_migration_that_fails_half_way_leaves_the_old_ledger_as_it_was(tmp_path, monkeypatch):
    """The rename, the copy and the version commit together, so the next open can migrate."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO attempts (id, issue_id, identifier, url, route, repo, worktree, branch, "
        "state, step, created_at, updated_at) "
        "VALUES (3, 'uuid-3', 'ABC-3', NULL, 'main', '/repo', '/w/ABC-3', 'b', 'running', 4, 1.0, 2.0)"
    )
    conn.commit()
    conn.close()
    good = ledger_mod._MIGRATE_V1_TO_V2
    monkeypatch.setattr(ledger_mod, "_MIGRATE_V1_TO_V2", good + "\nINSERT INTO no_such_table VALUES (1);\n")

    with pytest.raises(sqlite3.OperationalError):
        Ledger(path)

    conn = sqlite3.connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "attempts" in tables and "attempts_v1" not in tables and "workers" not in tables
    conn.close()

    monkeypatch.setattr(ledger_mod, "_MIGRATE_V1_TO_V2", good)
    with Ledger(path) as ledger:
        assert ledger.get(3).item == "ABC-3"


def test_two_roles_may_hold_the_same_item_but_a_role_may_not_hold_it_twice(ledger):
    make(ledger, role="implementer")
    make(ledger, role="pr-reviewer", worktree="/w/pr-reviewer/ABC-1")

    assert {w.role for w in ledger.live_workers_for("ABC-1")} == {"implementer", "pr-reviewer"}
    assert ledger.live_worker_for("ABC-1", "pr-reviewer").role == "pr-reviewer"
    assert ledger.live_count_by_role() == {"implementer": 1, "pr-reviewer": 1}
    with pytest.raises(sqlite3.IntegrityError):
        make(ledger, role="pr-reviewer", worktree="/w/other")


def test_a_session_is_recorded_once_per_id(ledger):
    worker = make(ledger)

    ledger.record_session(worker, kind=ledger_mod.SESSION_INITIAL, session_id="s1", session_uuid="u1")
    ledger.record_session(worker, kind=ledger_mod.SESSION_ADOPTED, session_id="s1", session_uuid="u1")
    ledger.record_session(worker, kind=ledger_mod.SESSION_INITIAL, session_id="s2", session_uuid="u2")

    assert [s.session_id for s in ledger.sessions_of(worker)] == ["s1", "s2"]
    assert worker.handle == "implementer/ABC-1"


def test_workers_created_by_hand_are_not_held_to_one_per_role_and_item(ledger):
    """Only a start rule needs the guard: its event arrives again on every poll."""
    make(ledger)

    for index, origin in enumerate((ledger_mod.ORIGIN_BOARD, ledger_mod.ORIGIN_CLONE, ledger_mod.ORIGIN_BOARD)):
        ledger.create_worker(
            role="implementer", item="ABC-1", item_id="id-ABC-1", url=None, repository="test",
            repo=Path("/repo"), worktree=Path(f"/w/ABC-1-{index}"), branch=f"b{index}",
            origin=origin,
        )

    assert len(ledger.live_workers_for("ABC-1")) == 4
    with pytest.raises(sqlite3.IntegrityError):
        make(ledger, worktree="/w/other")


def test_a_worker_may_have_no_work_item(ledger):
    worker = ledger.create_worker(
        role="scratch", item=None, item_id=None, url=None, repository="test",
        repo=Path("/repo"), worktree=Path("/w/scratch"), branch="chargehand/scratch",
        origin=ledger_mod.ORIGIN_BOARD,
    )

    assert worker.item is None
    assert worker.handle == f"scratch/#{worker.id}"
    assert worker.runtime == "claude"


def test_an_unknown_origin_is_rejected(ledger):
    with pytest.raises(ValueError, match="origin"):
        ledger.create_worker(
            role="implementer", item="ABC-1", item_id="id", url=None, repository="test",
            repo=Path("/repo"), worktree=Path("/w/x"), branch="b", origin="magic",
        )

