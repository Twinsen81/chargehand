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


def test_the_tracker_cursor_is_kept_per_repository(harness):
    harness.board.add("ABC-1")

    harness.tick()

    rows = harness.ledger._conn.execute("SELECT source FROM cursors").fetchall()
    assert [row["source"] for row in rows] == ["tracker:test"]
