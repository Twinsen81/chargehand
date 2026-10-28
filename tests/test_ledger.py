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
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
