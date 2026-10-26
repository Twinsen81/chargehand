"""The repository's tracker as a source in the tick: queue starts, comments, the label owner."""

from __future__ import annotations

import dataclasses

from chargehand import ledger as ledger_mod
from chargehand.sources import AUTHORSHIP_MARKER
from test_tick_roles import harness_with, write_role


def inbox(harness, item, role="implementer"):
    worker = harness.worker(item, role)
    return [(entry.kind, entry.actor) for entry in harness.ledger.inbox(worker, pending_only=False)]


def marks(harness):
    return [call[1] for call in harness.board.calls if call[0] == "mark"]


def test_a_comment_on_an_issue_with_a_live_implementer_lands_in_its_inbox(harness):
    harness.board.add("ABC-1")
    harness.tick()
    assert harness.labels("ABC-1") == ("chargehand-running",)

    harness.board.comment("ABC-1", author="reviewer@example.invalid")
    harness.next_tick()
    harness.next_tick()

    assert inbox(harness, "ABC-1") == [("comment", "reviewer@example.invalid")]
    rows = harness.ledger._conn.execute("SELECT kind FROM inbox").fetchall()
    assert [row["kind"] for row in rows] == ["comment"]


def test_a_comment_a_worker_posted_wakes_nobody(harness):
    harness.board.add("ABC-1")
    harness.tick()

    harness.board.comment(
        "ABC-1", author=harness.board.viewer, body=f"pushed a fix\n\n{AUTHORSHIP_MARKER}"
    )
    harness.board.comment("ABC-1", author=harness.board.viewer, body="a note of my own")
    harness.next_tick()

    # Only the marker tells a worker's comment from the operator's own.
    assert inbox(harness, "ABC-1") == [("comment", harness.board.viewer)]


def test_a_comment_on_an_issue_nobody_follows_is_not_read(harness):
    harness.board.add("ABC-1", labels=())
    harness.board.comment("ABC-1")

    harness.tick()

    assert harness.ledger._conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    assert ("get", "ABC-1") not in harness.board.calls


def test_a_second_worker_on_the_issue_never_writes_the_label(tmp_path, repo, claude_state, board):
    write_role(repo, "helper", 'prompt = "look at {item}"\n\n[[start]]\non = "comment"\n')
    harness = harness_with(tmp_path, repo, claude_state, board, roles=("implementer", "helper"))
    board.add("ABC-1")
    harness.tick()
    board.comment("ABC-1")

    report = harness.next_tick()

    assert report.launched == ["ABC-1"]
    helper = harness.worker("ABC-1", "helper")
    assert helper is not None and not helper.owns_label
    assert harness.worker("ABC-1", "implementer").owns_label
    before = marks(harness)

    claude_state.set_session_state("helper/ABC-1", "blocked")
    harness.next_tick()
    claude_state.set_session_state("helper/ABC-1", "done")
    harness.next_tick()

    assert harness.worker("ABC-1", "helper").state == ledger_mod.BLOCKED
    assert marks(harness) == before
    assert harness.labels("ABC-1") == ("chargehand-running",)
    harness.ledger.close()


def test_a_waiting_queue_start_is_dropped_once_the_issue_left_the_queue(harness):
    for index in range(3):
        harness.board.add(f"ABC-{index}")
    first = harness.tick()
    assert first.launched == ["ABC-0", "ABC-1"]

    # Taken out of the queue while it waited for a slot.
    issue = harness.board.by_identifier("ABC-2")
    harness.board.issues[issue.id] = dataclasses.replace(issue, labels=())
    harness.claude_state.set_session_state("ABC-0", "blocked")
    report = harness.next_tick()

    assert report.launched == []
    assert harness.worker("ABC-2") is None
    assert harness.ledger.pending_start_events() == []


def test_a_waiting_queue_start_launches_when_a_slot_frees(harness):
    for index in range(3):
        harness.board.add(f"ABC-{index}")
    harness.tick()

    harness.claude_state.set_session_state("ABC-0", "blocked")
    report = harness.next_tick()

    assert report.launched == ["ABC-2"]
    assert harness.worker("ABC-2").owns_label
    assert harness.labels("ABC-2") == ("chargehand-running",)


def test_a_closed_issue_ends_its_worker_and_clears_the_labels(harness):
    harness.board.add("ABC-1")
    harness.tick()
    harness.claude_state.set_session_state("ABC-1", "done")
    harness.next_tick()

    harness.board.close("ABC-1")
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.DONE
    assert harness.labels("ABC-1") == ()


def test_a_queued_issue_starts_once_a_broken_role_file_is_fixed(harness):
    role_file = harness.repo / ".chargehand" / "roles" / "implementer.toml"
    role_file.parent.mkdir(parents=True)
    role_file.write_text("prompt = 1\n")
    harness.board.add("ABC-1")

    broken = harness.tick()

    assert broken.launched == []
    assert any("implementer.toml" in error for error in broken.errors)

    role_file.unlink()
    for _ in range(2):
        report = harness.next_tick()
        if report.launched:
            break

    assert report.launched == ["ABC-1"]


def test_a_queue_start_given_up_after_a_week_comes_back_from_the_queue(harness):
    for index in range(3):
        harness.board.add(f"ABC-{index}")
    harness.tick()
    assert [e.item.key for e in harness.ledger.pending_start_events()] == ["ABC-2"]

    # Held back for more than a week: the router gives the start up.
    harness.ledger._conn.execute("UPDATE events SET seen_at = seen_at - 8 * 24 * 3600")
    given_up = harness.next_tick()
    harness.claude_state.set_session_state("ABC-0", "blocked")
    report = harness.next_tick()

    assert any("gave up 1 start" in warning for warning in given_up.warnings)
    assert report.launched == ["ABC-2"]


def test_a_waiting_start_is_tried_once_per_tick(harness):
    for index in range(3):
        harness.board.add(f"ABC-{index}")
    harness.tick()

    report = harness.next_tick()

    assert [line for line in report.skipped if line.startswith("ABC-2:")] == [
        "ABC-2: launch throttle (2/2)"
    ]


def test_waiting_starts_make_no_tracker_call_while_the_poll_fails(harness, monkeypatch):
    from chargehand.errors import TrackerError
    from support import fake_tracker

    for index in range(5):
        harness.board.add(f"ABC-{index}")
    harness.tick()
    assert len(harness.ledger.pending_start_events()) == 3

    working = fake_tracker.FakeTracker.list

    def down(self, status):
        if status.value != "queued":
            return working(self, status)
        self.board.calls.append(("list", status.value))
        raise TrackerError("tracker is down")

    monkeypatch.setattr(fake_tracker.FakeTracker, "list", down)
    harness.board.calls.clear()
    harness.claude_state.set_session_state("ABC-0", "blocked")
    report = harness.next_tick()

    queue_reads = [call for call in harness.board.calls if call == ("list", "queued")]
    assert len(queue_reads) == 1
    assert [error for error in report.errors if "tracker is down" in error] == [
        "repository test: tracker is down"
    ]
    assert report.launched == []
    assert len(harness.ledger.pending_start_events()) == 3


def test_a_source_with_the_trackers_name_reads_the_followed_issues(
    tmp_path, repo, claude_state, board
):
    from chargehand.config import SourceConfig

    source = SourceConfig(name="fake", type="fake", options={"board": "default", "repository": "test"})
    harness = harness_with(tmp_path, repo, claude_state, board, sources=(source,))
    board.add("ABC-1")
    harness.tick()
    board.calls.clear()
    board.comment("ABC-1")

    harness.next_tick()

    issue_id = board.by_identifier("ABC-1").id
    assert ("get", issue_id) not in board.calls
    assert inbox(harness, "ABC-1") == []
    harness.ledger.close()


def test_the_tracker_cursor_is_kept_per_repository(harness):
    harness.board.add("ABC-1")

    harness.tick()

    rows = harness.ledger._conn.execute("SELECT source FROM cursors").fetchall()
    assert [row["source"] for row in rows] == ["tracker:test"]
