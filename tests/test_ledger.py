"""The ledger's guarantees: one live attempt per issue, and durable control requests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from chargehand import ledger as ledger_mod
from chargehand.ledger import Ledger
from chargehand.sources import Event, WorkItem


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


@pytest.mark.parametrize("write", ["update", "update_if_unchanged"])
@pytest.mark.parametrize("state", [ledger_mod.IDLE, ledger_mod.RUNNING, ledger_mod.BLOCKED,
                                   ledger_mod.PAUSED, ledger_mod.CANCELLED])
def test_a_state_write_that_leaves_parked_drops_the_reset_time(ledger, write, state):
    parked = ledger.update(make(ledger), state=ledger_mod.PARKED, reset_at=1_800_000_000.0)
    # A write without a state, such as a hook's record, keeps it.
    parked = ledger.update(parked, last_hook="StopFailure", last_hook_at=10.0)
    assert ledger.get(parked.id).reset_at == 1_800_000_000.0

    written = getattr(ledger, write)(parked, state=state)

    assert written.reset_at is None
    assert ledger.get(parked.id).reset_at is None


def test_a_write_of_parked_keeps_its_reset_time(ledger):
    worker = ledger.update(make(ledger), state=ledger_mod.RUNNING)

    parked = ledger.update_if_unchanged(worker, state=ledger_mod.PARKED, reset_at=1_800_000_000.0)
    assert ledger.get(worker.id).reset_at == 1_800_000_000.0
    # A write of `parked` that names no reset time keeps the one the row has.
    ledger.update(parked, state=ledger_mod.PARKED)
    assert ledger.get(worker.id).reset_at == 1_800_000_000.0


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


# ----- version 3: subscriptions, the inbox, seen events, cursors ----------------

V2_SCHEMA = """
CREATE TABLE workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'source', start_rule TEXT,
    runtime TEXT NOT NULL DEFAULT 'claude', item TEXT, item_id TEXT, url TEXT,
    repository TEXT NOT NULL, repo TEXT NOT NULL, worktree TEXT NOT NULL, branch TEXT NOT NULL,
    session_id TEXT, session_uuid TEXT, state TEXT NOT NULL, label_state TEXT,
    step INTEGER NOT NULL DEFAULT 0, launch_attempts INTEGER NOT NULL DEFAULT 1,
    session_state TEXT, waiting_for TEXT, notified_state TEXT, working_since REAL,
    watchdog_notified INTEGER NOT NULL DEFAULT 0, last_error TEXT, pr_url TEXT,
    gc_done INTEGER NOT NULL DEFAULT 0, wakes INTEGER NOT NULL DEFAULT 0, last_wake_at REAL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL
);
CREATE UNIQUE INDEX workers_one_live_per_role_item ON workers (role, item)
    WHERE origin = 'source' AND state IN ('launching', 'running', 'blocked', 'paused');
CREATE INDEX workers_state ON workers (state);
CREATE INDEX workers_worktree ON workers (worktree);
CREATE INDEX workers_item ON workers (item);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers (id),
    session_id TEXT, session_uuid TEXT, kind TEXT NOT NULL, started_at REAL NOT NULL,
    ended_at REAL
);
CREATE INDEX sessions_worker ON sessions (worker_id);
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT,
    args TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, applied_at REAL, ok INTEGER,
    message TEXT
);
CREATE INDEX requests_pending ON requests (applied_at);
CREATE TABLE runner_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
PRAGMA user_version = 2;
"""


def test_a_version_two_ledger_gains_the_routing_tables_in_place(tmp_path):
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at) VALUES (12, 'implementer', 'ABC-1', 'id-1', "
        "'test', '/repo', '/w/ABC-1', 'b', 'running', 4, 1.0, 2.0)"
    )
    conn.execute(
        "INSERT INTO sessions (id, worker_id, session_id, kind, started_at) "
        "VALUES (5, 12, 's1', 'initial', 1.0)"
    )
    conn.execute("INSERT INTO runner_state VALUES ('notify-pending:12', 'x', 1.0)")
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = ledger.get(12)
        assert (worker.role, worker.item, worker.state) == ("implementer", "ABC-1", "running")
        # Every worker before version 3 was started by the queue label, so it keeps it.
        assert worker.owns_label == 1
        assert [(s.id, s.session_id) for s in ledger.sessions_of(worker)] == [(5, "s1")]
        assert ledger.get_state("notify-pending:12") == "x"
        tables = {
            row[0] for row in ledger._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"subscriptions", "inbox", "events", "cursors"} <= tables
        ledger.subscribe(worker, WorkItem("fake", "ABC-1", "id-1"), ["comment"], primary=True)
        assert ledger.primary_subscription(worker).kinds == ("comment",)
        # The next worker continues after the kept ids.
        assert make(ledger, item="ABC-2", worktree="/w/ABC-2").id == 13

    # Opening it again is a no-op.
    with Ledger(path) as ledger:
        assert ledger.get(12).item == "ABC-1"


@pytest.mark.parametrize("state", [ledger_mod.IDLE, ledger_mod.QUEUED])
def test_the_migrated_one_live_worker_index_covers_the_new_live_states(tmp_path, state):
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.close()

    with Ledger(path) as ledger:
        first = make(ledger, item="ABC-1", worktree="/w/ABC-1")
        ledger.update(first, state=state)
        with pytest.raises(sqlite3.IntegrityError):
            make(ledger, item="ABC-1", worktree="/w/ABC-1-again")


def test_each_wake_is_its_own_session_row_and_counts_within_the_window(ledger):
    worker = make(ledger)
    ledger.record_session(worker, kind=ledger_mod.SESSION_INITIAL, session_id="s1", session_uuid="u1")

    ledger.record_wake(worker, session_id="s1", session_uuid="u1", now=100.0)
    ledger.record_wake(worker, session_id="s1", session_uuid="u1", now=200.0)

    assert [s.kind for s in ledger.sessions_of(worker)] == ["initial", "wake", "wake"]
    assert ledger.wakes_since(worker, 150.0) == 1
    assert ledger.wakes_since(worker, 0.0) == 2


def event(kind="comment", item="ABC-1", dedupe_id="e1", source="fake", **extra) -> Event:
    return Event(
        source=source, kind=kind, item=WorkItem(source, item, f"id-{item}", hint="test"),
        actor=extra.pop("actor", "someone"), occurred_at=1.0, dedupe_id=dedupe_id,
        pointer=f"https://t.invalid/{item}#{dedupe_id}", **extra,
    )


def test_subscribing_again_replaces_the_kinds_and_keeps_the_primary_flag(ledger):
    worker = make(ledger)
    item = WorkItem("fake", "ABC-1", "id-ABC-1")

    ledger.subscribe(worker, item, ["comment"], primary=True)
    ledger.subscribe(worker, item, ["comment", "review"])

    (subscription,) = ledger.subscriptions_of(worker)
    assert subscription.kinds == ("comment", "review")
    assert subscription.is_primary
    assert ledger.subscribed_items("fake") == [WorkItem("fake", "ABC-1", "id-ABC-1")]


def test_only_live_workers_are_subscribers(ledger):
    live = make(ledger)
    gone = make(ledger, role="pr-reviewer", worktree="/w/pr-reviewer/ABC-1")
    for worker in (live, gone):
        ledger.subscribe(worker, WorkItem("fake", "ABC-1", "id-ABC-1"), ["comment"])
    ledger.update(gone, state=ledger_mod.DONE)

    assert [w.id for _, w in ledger.live_subscriptions("fake", "ABC-1")] == [live.id]
    ledger.update(live, state=ledger_mod.CANCELLED)
    assert ledger.subscribed_items("fake") == []


def test_an_event_enters_one_inbox_once(ledger):
    worker = make(ledger)

    assert ledger.enqueue(worker.id, event()) is True
    assert ledger.enqueue(worker.id, event()) is False

    (entry,) = ledger.inbox(worker)
    assert (entry.kind, entry.item, entry.dedupe_id, entry.delivered_at) == ("comment", "ABC-1", "e1", None)


def test_a_pending_start_keeps_its_event_until_it_is_settled(ledger):
    original = event(kind="review_requested")

    ledger.record_event(original, start_pending=True)

    assert ledger.event_seen("fake", "e1")
    assert ledger.pending_start_events() == [original]
    ledger.settle_start(original)
    assert ledger.pending_start_events() == []
    assert ledger.event_seen("fake", "e1")


def test_old_events_and_long_deferred_starts_are_forgotten(ledger):
    ledger.record_event(event(dedupe_id="old"), now=time.time() - 40 * 24 * 3600)
    ledger.record_event(event(dedupe_id="stuck"), start_pending=True, now=time.time() - 8 * 24 * 3600)
    ledger.record_event(event(dedupe_id="fresh"), start_pending=True)

    assert ledger.prune_events() == (2, 1)
    assert [e.dedupe_id for e in ledger.pending_start_events()] == ["fresh"]


def test_a_cursor_is_kept_per_source(ledger):
    assert ledger.get_cursor("fake") is None
    ledger.set_cursor("fake", "3")
    ledger.set_cursor("fake", "5")
    ledger.set_cursor("other", None)

    assert (ledger.get_cursor("fake"), ledger.get_cursor("other")) == ("5", None)



# ----- version 4: parked, the session hooks, permission prompts -----------------

# A version 3 ledger: version 2 plus the label owner and the routing tables.
V3_SCHEMA = V2_SCHEMA.replace("PRAGMA user_version = 2;", "") + """
ALTER TABLE workers ADD COLUMN owns_label INTEGER NOT NULL DEFAULT 1;
DROP INDEX workers_one_live_per_role_item;
CREATE UNIQUE INDEX workers_one_live_per_role_item ON workers (role, item)
    WHERE origin = 'source' AND state IN ('launching', 'running', 'idle', 'queued', 'blocked', 'paused');
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers (id),
    source TEXT NOT NULL, item TEXT NOT NULL, item_ref TEXT NOT NULL,
    kinds TEXT NOT NULL DEFAULT '[]', is_primary INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, UNIQUE (worker_id, source, item)
);
CREATE TABLE inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers (id),
    source TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, item_ref TEXT NOT NULL,
    actor TEXT NOT NULL, occurred_at REAL NOT NULL, dedupe_id TEXT NOT NULL,
    pointer TEXT NOT NULL, queued_at REAL NOT NULL, delivered_at REAL,
    UNIQUE (worker_id, source, dedupe_id)
);
CREATE TABLE events (
    source TEXT NOT NULL, dedupe_id TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL,
    item_ref TEXT NOT NULL, item_url TEXT, item_hint TEXT, actor TEXT NOT NULL,
    occurred_at REAL NOT NULL, pointer TEXT NOT NULL, marked INTEGER NOT NULL DEFAULT 0,
    seen_at REAL NOT NULL, start_pending INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, dedupe_id)
);
CREATE TABLE cursors (source TEXT PRIMARY KEY, value TEXT, updated_at REAL NOT NULL);
PRAGMA user_version = 3;
"""


def test_a_version_three_ledger_gains_the_hook_columns_and_the_prompts_in_place(tmp_path):
    path = tmp_path / "v3.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V3_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at, owns_label) VALUES (7, 'reviewer', 'PR-1', 'r1', "
        "'test', '/repo', '/w/PR-1', 'b', 'idle', 4, 1.0, 2.0, 2)"
    )
    conn.execute(
        "INSERT INTO subscriptions (id, worker_id, source, item, item_ref, kinds, is_primary, "
        "created_at) VALUES (3, 7, 'gh', 'PR-1', 'r1', '[\"comment\"]', 1, 1.0)"
    )
    conn.execute(
        "INSERT INTO inbox (id, worker_id, source, kind, item, item_ref, actor, occurred_at, "
        "dedupe_id, pointer, queued_at) VALUES (9, 7, 'gh', 'comment', 'PR-1', 'r1', 'a', 1.0, "
        "'d1', 'p', 1.0)"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = ledger.get(7)
        assert (worker.role, worker.item, worker.state, worker.owns_label) == ("reviewer", "PR-1", "idle", 2)
        assert (worker.reset_at, worker.last_hook, worker.last_answer) == (None, None, None)
        assert [s.id for s in ledger.subscriptions_of(worker)] == [3]
        assert [e.id for e in ledger.inbox(worker)] == [9]
        prompt_id = ledger.record_permission_prompt(worker, tool_name="Bash", tool_input="{}")
        assert [p.id for p in ledger.open_permission_prompts(worker)] == [prompt_id]
        # `parked` is live, so the index holds a parked worker to one per role and item.
        ledger.update(worker, state=ledger_mod.PARKED)
        with pytest.raises(sqlite3.IntegrityError):
            make(ledger, item="PR-1", worktree="/w/PR-1-again", role="reviewer")
        assert make(ledger, item="PR-2", worktree="/w/PR-2", role="reviewer").id == 8

    with Ledger(path) as ledger:
        assert ledger.get(7).state == ledger_mod.PARKED


def test_a_version_two_ledger_is_migrated_through_version_three(tmp_path):
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.close()

    with Ledger(path) as ledger:
        worker = make(ledger)
        ledger.update(worker, last_hook="Stop", last_hook_at=5.0, reset_at=9.0)
        assert ledger.get(worker.id).reset_at == 9.0
        assert ledger.open_permission_prompts() == []


def test_a_state_write_is_refused_after_a_hook_or_a_state_change(ledger):
    worker = make(ledger)

    after_hook = ledger.update(worker, last_hook_at=10.0)
    assert ledger.update_if_unchanged(worker, state=ledger_mod.IDLE) is None
    moved = ledger.update_if_unchanged(after_hook, state=ledger_mod.RUNNING)
    assert moved is not None and ledger.get(worker.id).state == ledger_mod.RUNNING
    assert ledger.update_if_unchanged(after_hook, state=ledger_mod.IDLE) is None
    # Other columns may change between the read and the write.
    ledger.update(moved, pr_url="https://example.invalid/pr/1")
    assert ledger.update_if_unchanged(moved, state=ledger_mod.IDLE).state == ledger_mod.IDLE


def test_a_live_worker_is_found_by_its_session_uuid(ledger):
    worker = ledger.update(make(ledger), session_uuid="u-1")
    assert ledger.live_worker_by_session_uuid("u-1").id == worker.id
    ledger.update(worker, state=ledger_mod.DONE)
    assert ledger.live_worker_by_session_uuid("u-1") is None


# ----- version 5: where a control request came from, handles --------------------------

# A version 4 ledger: version 3 plus the hook columns and the permission prompts.
V4_SCHEMA = V3_SCHEMA.replace("PRAGMA user_version = 3;", "") + """
ALTER TABLE workers ADD COLUMN reset_at REAL;
ALTER TABLE workers ADD COLUMN last_hook TEXT;
ALTER TABLE workers ADD COLUMN last_hook_at REAL;
ALTER TABLE workers ADD COLUMN last_answer TEXT;
ALTER TABLE workers ADD COLUMN transcript_path TEXT;
CREATE TABLE permission_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers (id),
    tool_name TEXT NOT NULL, tool_input TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
    resolved_at REAL
);
PRAGMA user_version = 4;
"""


def test_a_version_four_ledger_records_the_origin_of_requests(tmp_path):
    path = tmp_path / "v4.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V4_SCHEMA)
    conn.execute(
        "INSERT INTO requests (id, kind, target, args, created_at) "
        "VALUES (3, 'cancel', 'ABC-1', '{}', 1.0)"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        (old,) = ledger.pending_requests()
        assert (old["id"], old["kind"], old["origin"]) == (3, "cancel", ledger_mod.REQUEST_CLI)
        new = ledger.record_request("stop", "x/#1", origin=ledger_mod.REQUEST_BOARD)
        assert ledger.get_request(new)["origin"] == ledger_mod.REQUEST_BOARD


@pytest.mark.parametrize("version_schema", ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA"])
def test_every_older_ledger_gains_the_request_origin(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        request_id = ledger.record_request("pause", origin=ledger_mod.REQUEST_BOARD)
        assert ledger.get_request(request_id)["origin"] == "board"


def test_a_request_origin_must_be_known(ledger):
    with pytest.raises(ValueError, match="origin"):
        ledger.record_request("pause", origin="elsewhere")


def test_an_applied_request_is_recorded_complete(ledger):
    request_id = ledger.record_applied_request(
        "attach", "implementer/ABC-1", origin=ledger_mod.REQUEST_BOARD, ok=True, message="opened",
        terminal="iterm2",
    )

    assert ledger.pending_requests() == []
    request = ledger.get_request(request_id)
    assert (request["ok"], request["message"], request["args"]) == (1, "opened", {"terminal": "iterm2"})


def by_hand(ledger, item="ABC-1", role="implementer", index=0):
    return ledger.create_worker(
        role=role, item=item, item_id=f"id-{item}" if item else None, url=None, repository="test",
        repo=Path("/repo"), worktree=Path(f"/w/{item}-{index}"), branch=f"b{index}",
        origin=ledger_mod.ORIGIN_BOARD,
    )


def test_a_worker_started_by_hand_has_a_handle_with_its_id(ledger):
    source = make(ledger)
    first, second = by_hand(ledger, index=1), by_hand(ledger, index=2)

    assert source.handle == "implementer/ABC-1" and source.name == "ABC-1"
    assert first.handle == f"implementer/ABC-1#{first.id}" == first.name
    assert second.handle == f"implementer/ABC-1#{second.id}"


def test_find_worker_takes_an_item_a_handle_or_a_handle_with_an_id(ledger):
    source = make(ledger)
    first, second = by_hand(ledger, index=1), by_hand(ledger, index=2)
    reviewer = make(ledger, item="ABC-2", worktree="/w/r", role="reviewer")

    assert ledger.find_worker("ABC-2")[0].id == reviewer.id
    assert ledger.find_worker(f"implementer/ABC-1#{first.id}")[0].id == first.id
    assert ledger.find_worker(f"#{second.id}")[0].id == second.id
    assert ledger.find_worker("reviewer/ABC-2")[0].id == reviewer.id
    ledger.update(first, state=ledger_mod.CANCELLED)
    ledger.update(second, state=ledger_mod.CANCELLED)
    assert ledger.find_worker("implementer/ABC-1")[0].id == source.id

    worker, problem = ledger.find_worker(f"reviewer/ABC-1#{first.id}")
    assert worker is None and first.handle in problem
    worker, problem = ledger.find_worker("#999")
    assert worker is None and problem == "no worker #999"
    worker, problem = ledger.find_worker("reviewer/ABC-2", "implementer")
    assert worker is None and "role reviewer" in problem


def test_find_worker_asks_for_a_handle_when_one_role_has_several_live_workers(ledger):
    first, second = by_hand(ledger, index=1), by_hand(ledger, index=2)

    worker, problem = ledger.find_worker("ABC-1")

    assert worker is None
    assert first.handle in problem and second.handle in problem


def test_a_handle_names_its_start_rule_worker_before_any_other_worker(ledger):
    source = make(ledger)
    board = by_hand(ledger, index=1)
    reviewer_board = by_hand(ledger, role="reviewer", index=2)

    # Rule 2: `role/item`, and the bare item with --role, are the start-rule worker's handle.
    for target, role in (("implementer/ABC-1", None), ("ABC-1", "implementer")):
        assert ledger.find_worker(target, role)[0].id == source.id
    # Rule 3: the bare item alone names the one start-rule worker among the live ones,
    # also when a board worker of another role is live on the item.
    assert ledger.find_worker("ABC-1")[0].id == source.id
    assert ledger.find_worker("ABC-1", "reviewer")[0].id == reviewer_board.id
    assert ledger.find_worker(board.handle)[0].id == board.id

    # The handle wins over a live board worker once the start-rule worker has finished.
    ledger.update(source, state=ledger_mod.CANCELLED)
    assert ledger.find_worker("implementer/ABC-1")[0].id == source.id
    # The bare item has no handle of its own: the live workers decide, here two roles.
    worker, problem = ledger.find_worker("ABC-1")
    assert worker is None and "several roles" in problem
    assert board.id_handle in problem and reviewer_board.id_handle in problem


LIVE, FINISHED = "live", "finished"


@pytest.mark.parametrize("earlier", [False, True], ids=["first", "after-a-finished-one"])
@pytest.mark.parametrize("source_state", [None, LIVE, FINISHED])
@pytest.mark.parametrize("board_state", [None, LIVE, FINISHED])
@pytest.mark.parametrize("board_role", ["implementer", "reviewer"])
@pytest.mark.parametrize("second_board", [False, True])
def test_every_handle_chargehand_prints_resolves_back_to_its_worker(
    ledger, earlier, source_state, board_state, board_role, second_board
):
    """Start-rule and board workers, live or finished, in one or two roles, on one item."""
    workers = []

    def add(worker, state):
        if state == FINISHED:
            worker = ledger.update(worker, state=ledger_mod.CANCELLED)
        else:
            worker = ledger.update(worker, state=ledger_mod.RUNNING)
        workers.append(worker)

    if earlier:
        add(make(ledger, worktree="/w/old"), FINISHED)
    if source_state:
        add(make(ledger), source_state)
    if board_state:
        add(by_hand(ledger, role=board_role, index=1), board_state)
        if second_board:
            add(by_hand(ledger, role=board_role, index=2), LIVE)
    if not workers:
        pytest.skip("no worker in this combination")

    for worker in workers:
        printed = {worker.target, ledger.target_for(worker), worker.id_handle}
        if not worker.is_terminal:
            printed.add(worker.handle)
        for name in printed:
            found, problem = ledger.find_worker(name)
            assert found is not None and found.id == worker.id, (name, problem)
        assert worker.target == (
            worker.handle if not worker.is_terminal else worker.id_handle
        )


def test_a_bare_item_is_refused_when_it_could_mean_a_finished_start_rule_worker(ledger):
    """A notification about the start-rule worker carried the bare item; a live board
    worker on the item must not take a command meant for the finished one."""
    source = ledger.update(make(ledger), state=ledger_mod.CANCELLED)
    board = ledger.update(by_hand(ledger, index=1), state=ledger_mod.IDLE)

    worker, problem = ledger.find_worker("ABC-1")

    assert worker is None
    assert source.id_handle in problem and board.id_handle in problem
    # The handles still resolve, each to its own worker.
    assert ledger.find_worker(source.id_handle)[0].id == source.id
    assert ledger.find_worker(board.id_handle)[0].id == board.id
    assert ledger.find_worker("implementer/ABC-1")[0].id == source.id


def test_errors_show_a_handle_as_typed(ledger):
    _, problem = ledger.find_worker("implementer/NOPE-1")
    assert problem == "no worker recorded for implementer/NOPE-1 in role implementer"


# ----- version 6: the board's own state -------------------------------------------------

# A version 5 ledger: version 4 plus the request origin.
V5_SCHEMA = V4_SCHEMA.replace("PRAGMA user_version = 4;", "") + """
ALTER TABLE requests ADD COLUMN origin TEXT NOT NULL DEFAULT 'cli';
PRAGMA user_version = 5;
"""


def test_a_version_five_ledger_gains_the_board_state_in_place(tmp_path):
    path = tmp_path / "v5.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V5_SCHEMA)
    conn.execute(
        "INSERT INTO requests (id, kind, target, args, created_at, origin) "
        "VALUES (4, 'stop', 'x/#1', '{}', 1.0, 'board')"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        (old,) = ledger.pending_requests()
        assert (old["id"], old["origin"]) == (4, "board")
        assert ledger.board_state() == []
        ledger.set_board_state(ledger_mod.BOARD_REPOSITORY, "test", "hidden", "1")
        assert [row["value"] for row in ledger.board_state()] == ["1"]


@pytest.mark.parametrize("version_schema", ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA", "V4_SCHEMA"])
def test_every_older_ledger_gains_the_board_state(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        ledger.set_board_state(ledger_mod.BOARD_WORKER, "1", "pinned", "1")
        assert [row["field"] for row in ledger.board_state()] == ["pinned"]
        assert ledger.get_request(ledger.record_request("pause"))["origin"] == "cli"


# ----- version 7: prompts as records, and the per-worker rules ----------------------------

# A version 6 ledger: version 5 plus the board's own state.
V6_SCHEMA = V5_SCHEMA.replace("PRAGMA user_version = 5;", "") + """
CREATE TABLE board_state (
    kind TEXT NOT NULL, key TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (kind, key, field)
);
PRAGMA user_version = 6;
"""


def test_a_version_six_ledger_turns_its_prompts_into_records(tmp_path):
    path = tmp_path / "v6.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V6_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at) VALUES (7, 'implementer', 'ABC-1', 'i1', "
        "'test', '/repo', '/w/ABC-1', 'b', 'blocked', 4, 1.0, 2.0)"
    )
    conn.execute(
        "INSERT INTO permission_prompts (id, worker_id, tool_name, tool_input, created_at, "
        "resolved_at) VALUES (1, 7, 'Bash', '{}', 1.0, 2.0), (2, 7, 'Edit', '{}', 3.0, NULL)"
    )
    conn.execute(
        "INSERT INTO board_state (kind, key, field, value, updated_at) "
        "VALUES ('worker', '7', 'pinned', '1', 1.0)"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        old, current = ledger.get_permission_prompt(1), ledger.get_permission_prompt(2)
        assert (old.state, old.resolved_by, old.released_at, old.kind) == ("cancelled", "session", 2.0, "permission")
        assert (current.state, current.decision, current.released_at) == ("open", None, None)
        assert [p.id for p in ledger.open_permission_prompts()] == [2]
        assert [row["value"] for row in ledger.board_state()] == ["1"]
        worker = ledger.get(7)
        ledger.set_prompt_digest(2, "d" * 64)
        assert ledger.answer_permission_prompt(2, "allow", by="board", rule=True)
        assert ledger.has_permission_rule(worker, "Edit", "d" * 64)
        assert not ledger.answer_permission_prompt(2, "deny", by="board")


@pytest.mark.parametrize(
    "version_schema", ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA", "V4_SCHEMA", "V5_SCHEMA"]
)
def test_every_older_ledger_gains_the_prompt_records_and_the_rules(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        worker = make(ledger, item="ABC-9", worktree="/w/ABC-9")
        prompt_id = ledger.record_permission_prompt(
            worker, tool_name="Bash", tool_input="{}", input_digest="e" * 64
        )
        assert ledger.get_permission_prompt(prompt_id).state == "open"
        assert ledger.answer_permission_prompt(prompt_id, "allow", by="board", rule=True)
        assert ledger.has_permission_rule(worker, "Bash", "e" * 64)


def test_first_writer_wins_between_an_answer_a_release_and_a_cancel(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as ledger:
        worker = make(ledger, item="ABC-1", worktree="/w/ABC-1")
        released = ledger.record_permission_prompt(worker, tool_name="Bash", tool_input="{}")
        assert ledger.release_permission_prompt(released)
        assert not ledger.answer_permission_prompt(released, "allow", by="board")
        assert not ledger.release_permission_prompt(released)

        answered = ledger.record_permission_prompt(worker, tool_name="Bash", tool_input="{}")
        assert ledger.answer_permission_prompt(answered, "deny", by="board")
        assert not ledger.release_permission_prompt(answered)

        # Leaving `blocked` cancels the open prompts, the released one included.
        assert ledger.resolve_permission_prompts(worker) == 1
        assert ledger.get_permission_prompt(released).state == "cancelled"
        assert ledger.get_permission_prompt(answered).state == "resolved"


def test_an_open_prompt_is_cancelled_when_the_worker_leaves_blocked_or_stops_not_when_its_launch_ends(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as ledger:
        worker = make(ledger, item="ABC-1", worktree="/w/ABC-1")
        launching = ledger.update(worker, state=ledger_mod.LAUNCHING)
        prompt = ledger.record_permission_prompt(launching, tool_name="Bash", tool_input="{}")

        running = ledger.update(launching, state=ledger_mod.RUNNING)
        assert ledger.get_permission_prompt(prompt).state == "open"
        blocked = ledger.update(running, state=ledger_mod.BLOCKED)
        assert ledger.get_permission_prompt(prompt).state == "open"
        ledger.update(blocked, state=ledger_mod.RUNNING)
        assert ledger.get_permission_prompt(prompt).state == "cancelled"

        other = ledger.record_permission_prompt(running, tool_name="Bash", tool_input="{}")
        ledger.update(ledger.get(worker.id), state=ledger_mod.PAUSED)
        assert ledger.get_permission_prompt(other).state == "cancelled"

        # A prompt recorded while paused does not survive the respawn of a continue, which
        # read the row after the hook touched it.
        stale = hook(ledger, worker)
        ledger.update(ledger.get(worker.id), state=ledger_mod.RUNNING)
        assert ledger.get_permission_prompt(stale).state == "cancelled"

        # Nor one recorded while idle when a wake starts a turn.
        ledger.update(ledger.get(worker.id), state=ledger_mod.IDLE)
        woken = hook(ledger, worker)
        ledger.update(ledger.get(worker.id), state=ledger_mod.RUNNING)
        assert ledger.get_permission_prompt(woken).state == "cancelled"


def hook(ledger, worker, now=None):
    """What a PermissionRequest hook writes: the row's last hook, then the prompt."""
    stamp = time.time() if now is None else now
    ledger.update(ledger.get(worker.id), last_hook="PermissionRequest", last_hook_at=stamp)
    return ledger.record_permission_prompt(worker, tool_name="Bash", tool_input="{}", now=stamp)


@pytest.mark.parametrize("before", [ledger_mod.IDLE, ledger_mod.QUEUED, ledger_mod.PAUSED, ledger_mod.PARKED])
def test_a_prompt_from_the_new_turn_survives_the_write_of_the_wake_or_continue_that_started_it(tmp_path, before):
    with Ledger(tmp_path / "l.sqlite") as ledger:
        worker = make(ledger, item="ABC-1", worktree="/w/ABC-1")
        older = hook(ledger, ledger.update(worker, state=before))
        # The wake or the continue reads the row, then resumes or respawns the session ...
        snapshot = ledger.get(worker.id)
        time.sleep(0.01)
        # ... whose new turn asks for permission before the action writes `running`.
        newer = hook(ledger, worker)
        ledger.update(snapshot, state=ledger_mod.RUNNING)

        assert ledger.get_permission_prompt(newer).state == "open"
        assert ledger.get_permission_prompt(older).state == "cancelled"


# ----- version 8: jobs, and when a worker was archived ------------------------------------

# A version 7 ledger: version 6 with the prompts as records and the per-worker rules.
V7_SCHEMA = V6_SCHEMA.replace("PRAGMA user_version = 6;", "") + """
ALTER TABLE permission_prompts ADD COLUMN kind TEXT NOT NULL DEFAULT 'permission';
ALTER TABLE permission_prompts ADD COLUMN options TEXT;
ALTER TABLE permission_prompts ADD COLUMN input_digest TEXT;
ALTER TABLE permission_prompts ADD COLUMN state TEXT NOT NULL DEFAULT 'open';
ALTER TABLE permission_prompts ADD COLUMN decision TEXT;
ALTER TABLE permission_prompts ADD COLUMN resolved_by TEXT;
ALTER TABLE permission_prompts ADD COLUMN released_at REAL;
CREATE TABLE permission_rules (
    worker_id INTEGER NOT NULL REFERENCES workers (id), tool_name TEXT NOT NULL,
    input_digest TEXT NOT NULL, prompt_id INTEGER REFERENCES permission_prompts (id),
    created_at REAL NOT NULL, PRIMARY KEY (worker_id, tool_name, input_digest)
);
PRAGMA user_version = 7;
"""


def test_a_version_seven_ledger_gains_the_jobs_and_the_archive_time(tmp_path):
    path = tmp_path / "v7.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V7_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at) VALUES (7, 'implementer', 'ABC-1', 'i1', "
        "'test', '/repo', '/w/ABC-1', 'b', 'cancelled', 4, 1.0, 2.0)"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = ledger.get(7)
        assert (worker.state, worker.archived_at) == ("cancelled", None)
        assert [w.id for w in ledger.archivable_workers("test")] == [7]
        job = ledger.create_job(ledger_mod.JOB_ARCHIVE, "7", result_path="/a.zip", force=True)
        assert (job.state, job.force, job.is_active) == ("queued", True, True)
        assert ledger.update_job(job, expect=ledger_mod.JOB_RUNNING, state=ledger_mod.JOB_DONE) is None
        done = ledger.update_job(job, expect=ledger_mod.JOB_QUEUED, state=ledger_mod.JOB_DONE)
        assert done.finished_at is not None and not done.is_active
        ledger.update(worker, archived_at=3.0)
        assert ledger.get(7).archived_at == 3.0
        assert ledger.archivable_workers("test") == []


@pytest.mark.parametrize(
    "version_schema", ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA", "V4_SCHEMA", "V5_SCHEMA", "V6_SCHEMA"]
)
def test_every_older_ledger_gains_the_jobs_and_the_archive_time(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        worker = make(ledger, item="ABC-9", worktree="/w/ABC-9")
        assert ledger.update(worker, archived_at=1.0).archived_at == 1.0
        job = ledger.create_job(ledger_mod.JOB_ARCHIVE, str(worker.id))
        assert ledger.active_job(ledger_mod.JOB_ARCHIVE, str(worker.id)) == job


# ----- version 9: what a clone keeps ---------------------------------------------------

# A version 8 ledger: version 7 with the jobs and the worker's archive time.
V8_SCHEMA = V7_SCHEMA.replace("PRAGMA user_version = 7;", "") + """
ALTER TABLE workers ADD COLUMN archived_at REAL;
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL, progress TEXT, result_path TEXT,
    error TEXT, created_at REAL NOT NULL, started_at REAL, updated_at REAL NOT NULL,
    finished_at REAL
);
PRAGMA user_version = 8;
"""


def test_a_version_eight_ledger_gains_the_clone_columns(tmp_path):
    path = tmp_path / "v8.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V8_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at, archived_at) VALUES (7, 'implementer', 'ABC-1', "
        "'i1', 'test', '/repo', '/w/ABC-1', 'b', 'idle', 4, 1.0, 2.0, NULL)"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        original = ledger.get(7)
        assert (original.parent_id, original.clone_mode, original.prompt) == (None, None, None)
        assert (original.model, original.effort) == (None, None)
        clone = make(ledger, item="ABC-2", worktree="/w/ABC-2-8")
        clone = ledger.update(
            clone, origin=ledger_mod.ORIGIN_CLONE, parent_id=7, clone_mode=ledger_mod.CLONE_FULL, prompt="go on in {worktree}",
            model="sonnet", effort="low",
        )
        assert (clone.id, clone.parent_id, clone.clone_mode) == (8, 7, "full")
        assert (clone.prompt, clone.model, clone.effort) == ("go on in {worktree}", "sonnet", "low")

    with Ledger(path) as ledger:
        assert ledger.get(8).model == "sonnet"


@pytest.mark.parametrize(
    "version_schema",
    ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA", "V4_SCHEMA", "V5_SCHEMA", "V6_SCHEMA", "V7_SCHEMA"],
)
def test_every_older_ledger_gains_the_clone_columns(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = make(ledger, item="ABC-9", worktree="/w/ABC-9")
        updated = ledger.update(worker, parent_id=1, clone_mode="fresh", prompt="p", model="m", effort="low")
        assert (updated.parent_id, updated.clone_mode, updated.prompt) == (1, "fresh", "p")


V9_SCHEMA = V8_SCHEMA.replace("PRAGMA user_version = 8;", "") + """
ALTER TABLE workers ADD COLUMN parent_id INTEGER;
ALTER TABLE workers ADD COLUMN clone_mode TEXT;
ALTER TABLE workers ADD COLUMN prompt TEXT;
ALTER TABLE workers ADD COLUMN model TEXT;
ALTER TABLE workers ADD COLUMN effort TEXT;
PRAGMA user_version = 9;
"""


def test_a_version_nine_ledger_gains_the_outbox_and_the_mod_columns(tmp_path):
    path = tmp_path / "v9.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V9_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at, model) VALUES (7, 'implementer', 'ABC-1', "
        "'i1', 'test', '/repo', '/w/ABC-1', 'b', 'idle', 4, 1.0, 2.0, 'sonnet')"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = ledger.get(7)
        assert (worker.model, worker.mod_session, worker.mod_seen_at) == ("sonnet", None, None)
        assert (worker.context_tokens, worker.context_percent) == (None, None)
        worker = ledger.update(worker, mod_session="uuid-7", mod_seen_at=3.0, context_tokens=1200,
                               context_percent=1.5)
        message = ledger.put_outbox(worker, session_uuid="uuid-7", text="wake", entries=[], now=4.0)
        assert message is not None and message.state == ledger_mod.OUTBOX_PENDING

    with Ledger(path) as ledger:
        assert ledger.get(7).mod_session == "uuid-7"
        assert ledger.outbox_message(ledger.get(7)).text == "wake"


@pytest.mark.parametrize(
    "version_schema",
    ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA", "V4_SCHEMA", "V5_SCHEMA", "V6_SCHEMA", "V7_SCHEMA",
     "V8_SCHEMA"],
)
def test_every_older_ledger_gains_the_outbox_and_the_mod_columns(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = make(ledger, item="ABC-9", worktree="/w/ABC-9")
        updated = ledger.update(worker, mod_session="u", context_tokens=5)
        assert (updated.mod_session, updated.context_tokens) == ("u", 5)
        assert ledger.put_outbox(updated, session_uuid="u", text="t", entries=[1]) is not None


# ----- version 11: reports, handoff notes and timers -----------------------------------

V10_SCHEMA = V9_SCHEMA.replace("PRAGMA user_version = 9;", "") + """
ALTER TABLE workers ADD COLUMN mod_session TEXT;
ALTER TABLE workers ADD COLUMN mod_seen_at REAL;
ALTER TABLE workers ADD COLUMN context_tokens INTEGER;
ALTER TABLE workers ADD COLUMN context_percent REAL;
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers (id),
    session_uuid TEXT NOT NULL, text TEXT NOT NULL, entries TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    taken_at REAL, submitted_at REAL
);
PRAGMA user_version = 10;
"""


def test_a_version_ten_ledger_gains_the_report_the_handoff_and_the_due_time(tmp_path):
    path = tmp_path / "v10.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V10_SCHEMA)
    conn.execute(
        "INSERT INTO workers (id, role, item, item_id, repository, repo, worktree, branch, "
        "state, step, created_at, updated_at, mod_session) VALUES (7, 'implementer', 'ABC-1', "
        "'i1', 'test', '/repo', '/w/ABC-1', 'b', 'idle', 4, 1.0, 2.0, 'uuid-7')"
    )
    conn.execute(
        "INSERT INTO inbox (worker_id, source, kind, item, item_ref, actor, occurred_at, "
        "dedupe_id, pointer, queued_at) VALUES (7, 'fake', 'comment', 'ABC-1', 'i1', 'x', 1.0, "
        "'e1', 'https://example.invalid/1', 1.0)"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = ledger.get(7)
        assert (worker.mod_session, worker.report_state, worker.handoff_note) == ("uuid-7", None, None)
        (entry,) = ledger.inbox(worker)
        assert entry.due_at is None
        ledger.update(worker, report_state="done", report_summary="s", reported_at=3.0,
                      handoff_note="n", handoff_at=3.0)
        ledger.put_timer(worker, due_at=time.time() + 60, item="ABC-1", item_ref="i1")
        assert len(ledger.inbox(worker)) == 1

    with Ledger(path) as ledger:
        assert (ledger.get(7).report_state, ledger.get(7).handoff_note) == ("done", "n")
        assert ledger.pending_timer(ledger.get(7)) is not None


@pytest.mark.parametrize(
    "version_schema",
    ["V1_SCHEMA", "V2_SCHEMA", "V3_SCHEMA", "V4_SCHEMA", "V5_SCHEMA", "V6_SCHEMA", "V7_SCHEMA",
     "V8_SCHEMA", "V9_SCHEMA"],
)
def test_every_older_ledger_gains_the_report_the_handoff_and_the_due_time(tmp_path, version_schema):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(globals()[version_schema])
    conn.close()

    with Ledger(path) as ledger:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        worker = make(ledger, item="ABC-9", worktree="/w/ABC-9")
        updated = ledger.update(worker, report_state="working", handoff_note="n")
        assert (updated.report_state, updated.handoff_note) == ("working", "n")
        ledger.put_timer(updated, due_at=time.time() - 1, item="ABC-9", item_ref="ABC-9")
        assert [e.kind for e in ledger.inbox(updated)] == [ledger_mod.TIMER_KIND]


# ----- timers and questions ---------------------------------------------------------


def test_a_timer_waits_in_the_inbox_until_it_is_due(ledger):
    worker = make(ledger)

    ledger.put_timer(worker, due_at=100.0, item="ABC-1", item_ref="id-ABC-1", now=10.0)

    assert ledger.inbox(worker, now=99.0) == []
    (entry,) = ledger.inbox(worker, now=100.0)
    assert (entry.source, entry.kind, entry.pointer, entry.due_at) == (
        ledger_mod.TIMER_SOURCE, ledger_mod.TIMER_KIND, ledger_mod.TIMER_POINTER, 100.0,
    )
    # The whole inbox shows it too.
    assert len(ledger.inbox(worker, pending_only=False)) == 1
    # A new timer replaces the one that waits, and a delivered one stays as it was.
    ledger.put_timer(worker, due_at=200.0, item="ABC-1", item_ref="id-ABC-1", now=11.0)
    assert [e.due_at for e in ledger.inbox(worker, now=300.0)] == [200.0]
    ledger.mark_delivered(ledger.inbox(worker, now=300.0), now=300.0)
    ledger.put_timer(worker, due_at=400.0, item="ABC-1", item_ref="id-ABC-1", now=301.0)
    assert len(ledger.inbox(worker, pending_only=False)) == 2


def test_a_question_is_answered_once(ledger):
    worker = make(ledger)
    first = ledger.record_question(worker, "Which colour?", ["red", "blue"], now=1.0)
    second = ledger.record_question(worker, "Which shade?", [], now=2.0)

    assert ledger.get_permission_prompt(first).state == ledger_mod.PROMPT_CANCELLED
    assert ledger.answer_question(second, by=ledger_mod.RESOLVED_BY_BOARD, now=3.0) is True
    assert ledger.answer_question(second, by=ledger_mod.RESOLVED_BY_BOARD, now=4.0) is False
    resolved = ledger.get_permission_prompt(second)
    assert (resolved.state, resolved.resolved_by, resolved.resolved_at) == (
        ledger_mod.PROMPT_RESOLVED, ledger_mod.RESOLVED_BY_BOARD, 3.0,
    )
    # A permission prompt is not a question.
    prompt = ledger.record_permission_prompt(worker, tool_name="Bash", tool_input="ls")
    assert ledger.answer_question(prompt, by=ledger_mod.RESOLVED_BY_BOARD) is False


# ----- the outbox ------------------------------------------------------------------


def test_one_open_outbox_message_per_worker_takes_the_new_events(ledger):
    worker = make(ledger)

    first = ledger.put_outbox(worker, session_uuid="u1", text="one", entries=[1], now=10.0)
    again = ledger.put_outbox(worker, session_uuid="u1", text="two", entries=[1, 2], now=11.0)

    assert again.id == first.id
    assert (again.text, again.entries, again.state) == ("two", (1, 2), ledger_mod.OUTBOX_PENDING)
    assert again.created_at == 10.0 and again.updated_at == 11.0
    assert ledger.outbox_workers() == {worker.id}


def test_a_taken_message_is_not_rewritten_and_is_handed_out_once(ledger):
    worker = make(ledger)
    ledger.put_outbox(worker, session_uuid="u1", text="one", entries=[1], now=10.0)

    taken = ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=12.0)

    assert (taken.state, taken.taken_at, taken.text) == (ledger_mod.OUTBOX_TAKEN, 12.0, "one")
    assert ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=13.0) is None
    # New events wait for the next wake.
    assert ledger.put_outbox(worker, session_uuid="u1", text="two", entries=[2], now=14.0) is None
    assert ledger.outbox_message(worker).text == "one"
    # A mod that was reloaded before its submit gets it again, but only after a while.
    again = ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=72.0)
    assert again.id == taken.id and again.taken_at == 72.0


def test_only_the_session_of_the_message_takes_it(ledger):
    worker = make(ledger)
    ledger.put_outbox(worker, session_uuid="u1", text="one", entries=[], now=10.0)

    assert ledger.take_outbox(worker, session_uuid="u2", retake_after=60, now=11.0) is None
    # A message for a new session replaces the old session's.
    newer = ledger.put_outbox(worker, session_uuid="u2", text="two", entries=[], now=12.0)
    assert newer.session_uuid == "u2"
    assert ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=13.0) is None


def test_a_submit_counts_once_and_a_drop_frees_the_worker(ledger):
    worker = make(ledger)
    message = ledger.put_outbox(worker, session_uuid="u1", text="one", entries=[], now=10.0)

    assert ledger.submit_outbox(message.id, now=11.0) is None  # not taken yet
    ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=11.0)
    submitted = ledger.submit_outbox(message.id, now=12.0)
    assert (submitted.state, submitted.submitted_at) == (ledger_mod.OUTBOX_SUBMITTED, 12.0)
    assert ledger.submit_outbox(message.id, now=13.0) is None
    assert ledger.outbox_message(worker) is None and ledger.outbox_workers() == set()

    second = ledger.put_outbox(worker, session_uuid="u1", text="two", entries=[], now=14.0)
    assert second.id != message.id
    assert ledger.drop_outbox(second.id, "the session changed", now=15.0)
    assert not ledger.drop_outbox(second.id, "again", now=16.0)
    assert ledger.get_outbox(second.id).reason == "the session changed"
    assert ledger.outbox_message(worker) is None


def test_a_poll_of_an_empty_outbox_writes_nothing(ledger):
    worker = make(ledger)
    changes = ledger._conn.total_changes

    assert ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=10.0) is None
    assert ledger._conn.total_changes == changes
    assert not ledger._conn.in_transaction


def test_a_take_loses_to_a_change_made_after_it_read_the_message(ledger, monkeypatch):
    worker = make(ledger)
    ledger.put_outbox(worker, session_uuid="u1", text="one", entries=[1], now=10.0)
    real = ledger.outbox_message

    def read_then_change(target):
        read = real(target)
        # New events join the pending message between the read and the take.
        ledger._conn.execute("UPDATE outbox SET updated_at = 11.0, text = 'two'")
        return read

    monkeypatch.setattr(ledger, "outbox_message", read_then_change)
    assert ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=12.0) is None
    monkeypatch.undo()
    assert ledger.take_outbox(worker, session_uuid="u1", retake_after=60, now=13.0).text == "two"
