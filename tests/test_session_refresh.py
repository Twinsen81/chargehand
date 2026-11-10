"""Session continuity: each wake continues the session, and past the role's refresh limit
the next wake starts a fresh session for the same worker, which keeps the old one."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import Harness, make_config
from chargehand import board as board_mod
from chargehand import channel
from chargehand import delivery as delivery_mod
from chargehand import ledger as ledger_mod
from chargehand.config import SourceConfig
from chargehand.delivery import MESSAGE_HEADER
from chargehand.errors import DeliveryAborted
from chargehand.gitutil import Git
from chargehand.ledger import Ledger
from chargehand.notify import Notifier
from chargehand.tick import HANDOFF_TOOL, Runner, TickReport
from support import fake_source

FAKE_SOURCE = SourceConfig(name="fake", type="fake", options={"board": "default", "repository": "test"})

REVIEWER = """\
prompt = "/review {item_url}"

[[start]]
on = "fake.review_requested"
subscribe = ["comment", "ci_status"]
"""

NOTE = "Open points: answer the second review thread; rerun the flaky test."


def write_role(repo: Path, text: str) -> None:
    target = repo / ".chargehand" / "roles" / "reviewer.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def build(tmp_path, repo, claude_state, board, policy: str) -> Harness:
    write_role(repo, REVIEWER + "\n[policy]\n" + policy + "\n")
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir(exist_ok=True)
    config = make_config(
        repo, worktree_root, roles=("implementer", "reviewer"), sources=(FAKE_SOURCE,)
    )
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    return Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root, notifier)


@pytest.fixture
def after_two(tmp_path, repo, claude_state, board):
    harness = build(tmp_path, repo, claude_state, board, "refresh_after_wakes = 2")
    yield harness
    harness.ledger.close()


@pytest.fixture
def after_tokens(tmp_path, repo, claude_state, board):
    harness = build(tmp_path, repo, claude_state, board, "refresh_after_tokens = 1000")
    yield harness
    harness.ledger.close()


def start_reviewer(harness, item="PR-7"):
    harness.board.add(item, labels=())
    fake_source.feed().emit("review_requested", item)
    harness.tick()
    reviewer = harness.worker(item, "reviewer")
    assert reviewer is not None and reviewer.state == ledger_mod.RUNNING
    return reviewer


def end_turn(harness, worker):
    """The worker's current session ends its turn; its process lives on."""
    current = harness.ledger.get(worker.id)
    harness.claude_state.set_session_state(current.session_id, "done", status="idle")


def wake(harness, worker):
    """The current session ends its turn, and one comment wakes the worker."""
    end_turn(harness, worker)
    dedupe = fake_source.feed().emit("comment", "PR-7")
    report = harness.next_tick()
    return report, f"https://tracker.invalid/PR-7#{dedupe}"


def launches(harness) -> list[dict]:
    return harness.claude_state.read().get("launches", [])


def resumes(harness) -> list[dict]:
    return harness.claude_state.read().get("resumes", [])


def kinds(harness, worker) -> list[str]:
    return [s.kind for s in harness.ledger.sessions_of(worker)]


def listed(harness, session_id) -> dict | None:
    return next((s for s in harness.claude_state.sessions if s["id"] == session_id), None)


def test_each_wake_continues_the_same_session_without_a_limit(tmp_path, repo, claude_state, board):
    harness = build(tmp_path, repo, claude_state, board, "max_wakes_per_day = 12")
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)

    assert len(launches(harness)) == 1
    assert [r["target"] for r in resumes(harness)] == [reviewer.session_uuid] * 3
    assert kinds(harness, reviewer) == ["initial", "wake", "wake", "wake"]
    harness.ledger.close()


def test_the_wake_after_the_limit_starts_a_fresh_session_and_keeps_the_old_one(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    harness.ledger.update(reviewer, handoff_note=NOTE)
    wake(harness, reviewer)
    wake(harness, reviewer)
    assert len(launches(harness)) == 1

    report, pointer = wake(harness, reviewer)

    assert report.errors == []
    assert report.woken == ["reviewer/PR-7"]
    first, fresh = launches(harness)
    # A new background session in the same worktree, under the worker's name.
    assert "--bg" in fresh["argv"] and fresh["cwd"] == first["cwd"] == reviewer.worktree
    assert fresh["argv"][fresh["argv"].index("-n") + 1] == "reviewer/PR-7"
    # The old session is stopped, not removed, and the third wake did not resume it.
    assert ["stop", reviewer.session_id] in harness.claude_state.commands
    assert ["rm", reviewer.session_id] not in harness.claude_state.commands
    assert listed(harness, reviewer.session_id) is not None
    assert len(resumes(harness)) == 2
    # The first prompt: chargehand's note with the item and the tool, the role's prompt,
    # then the waiting event. The handoff note is never in it.
    prompt = fresh["argv"][-1]
    note, role_prompt, message = prompt.split("\n\n")
    assert "PR-7" in note and HANDOFF_TOOL in note and HANDOFF_TOOL in channel.TOOLS
    assert role_prompt.startswith("/review ")
    assert message.splitlines() == [MESSAGE_HEADER, f"comment: {pointer}"]
    assert NOTE not in prompt and "Open points" not in prompt

    woken = harness.ledger.get(reviewer.id)
    assert woken.state == ledger_mod.RUNNING and woken.wakes == 3
    assert woken.session_id != reviewer.session_id
    assert listed(harness, woken.session_id)["cwd"] == reviewer.worktree
    assert woken.handoff_note == NOTE
    assert kinds(harness, woken) == ["initial", "wake", "wake", "refresh"]
    assert harness.ledger.inbox(woken) == []
    # The refresh counts against the daily wake cap.
    assert harness.ledger.wakes_since(woken, 0) == 3


def test_the_fresh_session_continues_until_its_own_limit(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)
    fresh = harness.ledger.get(reviewer.id)

    wake(harness, reviewer)
    wake(harness, reviewer)

    assert len(launches(harness)) == 2
    assert [r["target"] for r in resumes(harness)[2:]] == [fresh.session_uuid] * 2
    wake(harness, reviewer)
    assert len(launches(harness)) == 3
    assert kinds(harness, reviewer) == [
        "initial", "wake", "wake", "refresh", "wake", "wake", "refresh",
    ]


def test_a_session_with_a_turn_is_not_stopped_for_a_refresh(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    end_turn(harness, reviewer)
    fake_source.feed().emit("comment", "PR-7")
    runner = harness.fresh_runner()
    # The operator starts a turn in the session after the tick's own listing.
    original = runner._refresh_session

    def operator_types_first(worker, message, report):
        harness.claude_state.set_session_state(worker.session_id, "working", status="busy")
        return original(worker, message, report)

    runner._refresh_session = operator_types_first
    before = len(harness.claude_state.commands)
    report = runner.tick()

    assert len(launches(harness)) == 1
    assert ["stop", reviewer.session_id] not in harness.claude_state.commands[before:]
    assert any("wake held, the session has a turn or a prompt" in line for line in report.skipped)
    held = harness.ledger.get(reviewer.id)
    assert held.session_id == reviewer.session_id
    assert len(harness.ledger.inbox(held)) == 1


def test_a_failed_launch_keeps_the_old_session_and_the_events(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    harness.claude_state.set(launch_fails=True)

    report, pointer = wake(harness, reviewer)

    assert any("refresh failed" in line for line in report.errors)
    kept = harness.ledger.get(reviewer.id)
    assert (kept.session_id, kept.session_uuid) == (reviewer.session_id, reviewer.session_uuid)
    assert [e.pointer for e in harness.ledger.inbox(kept)] == [pointer]
    assert kinds(harness, kept) == ["initial", "wake", "wake"]

    # Once the launch works again, the next tick refreshes with the same event.
    harness.claude_state.set(launch_fails=False)
    report = harness.next_tick()
    assert report.woken == ["reviewer/PR-7"]
    assert launches(harness)[-1]["argv"][-1].endswith(f"comment: {pointer}")
    assert kinds(harness, reviewer)[-1] == "refresh"


def test_a_crash_after_the_launch_of_the_fresh_session_loses_no_event(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    end_turn(harness, reviewer)
    dedupe = fake_source.feed().emit("comment", "PR-7")
    # The tick dies after the launch of the fresh session, before it records it.
    harness.fresh_runner()
    with pytest.raises(DeliveryAborted):
        harness.runner.tick(crash_after_wake=True)
    (fresh,) = [s for s in harness.claude_state.sessions if s["id"] != reviewer.session_id]

    harness.next_tick()

    taken = harness.ledger.get(reviewer.id)
    assert taken.session_id == fresh["id"]
    assert kinds(harness, taken) == ["initial", "wake", "wake", "refresh"]
    assert [e.pointer for e in harness.ledger.inbox(taken)] == [
        f"https://tracker.invalid/PR-7#{dedupe}"
    ]
    # Its turn ends, and the same event wakes it in the fresh session: no third session.
    end_turn(harness, taken)
    report = harness.next_tick()
    assert report.woken == ["reviewer/PR-7"]
    assert len(launches(harness)) == 2
    assert resumes(harness)[-1]["target"] == fresh["uuid"]
    assert harness.ledger.inbox(taken) == []


def test_a_fresh_session_that_appears_late_is_taken_and_woken_next(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    harness.claude_state.set(launch_invisible=True)
    report, pointer = wake(harness, reviewer)
    assert any("did not appear" in line for line in report.errors)
    assert harness.ledger.get(reviewer.id).session_id == reviewer.session_id
    # The launch's session shows in the listing only now.
    state = harness.claude_state.read()
    state["sessions"].append({
        "id": "late1", "uuid": "uuid-late1", "name": "reviewer/PR-7",
        "cwd": reviewer.worktree, "state": "done",
    })
    state["launch_invisible"] = False
    harness.claude_state.write(state)

    harness.next_tick()

    taken = harness.ledger.get(reviewer.id)
    assert taken.session_id == "late1"
    assert kinds(harness, taken)[-1] == "refresh"
    # It did not get the first prompt from this tick, so the event waits for its wake.
    assert len(launches(harness)) == 2
    report = harness.next_tick()
    assert report.woken == ["reviewer/PR-7"]
    assert resumes(harness)[-1]["target"] == "uuid-late1"
    assert resumes(harness)[-1]["prompt"].endswith(f"comment: {pointer}")


def test_the_token_limit_reads_the_context_the_mod_reported(after_tokens):
    harness = after_tokens
    reviewer = start_reviewer(harness)
    harness.ledger.update(reviewer, context_tokens=999, context_percent=10.0)
    wake(harness, reviewer)
    assert len(launches(harness)) == 1

    harness.ledger.update(harness.ledger.get(reviewer.id), context_tokens=1000)
    wake(harness, reviewer)

    assert len(launches(harness)) == 2
    fresh = harness.ledger.get(reviewer.id)
    # The fresh session's context is its own: the old figures go.
    assert (fresh.context_tokens, fresh.context_percent) == (None, None)
    assert kinds(harness, fresh) == ["initial", "wake", "refresh"]


def test_the_token_limit_reads_a_percentage_without_a_count(after_tokens):
    harness = after_tokens
    reviewer = start_reviewer(harness)
    harness.ledger.update(reviewer, context_percent=1.0)

    wake(harness, reviewer)

    assert len(launches(harness)) == 2


def test_the_token_limit_reads_the_transcript_without_the_mod(after_tokens, tmp_path):
    harness = after_tokens
    reviewer = start_reviewer(harness)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"x" * (999 * delivery_mod.BYTES_PER_TOKEN))
    harness.ledger.update(reviewer, transcript_path=str(transcript))
    wake(harness, reviewer)
    assert len(launches(harness)) == 1

    transcript.write_bytes(b"x" * (1000 * delivery_mod.BYTES_PER_TOKEN))
    wake(harness, reviewer)

    assert len(launches(harness)) == 2
    assert harness.ledger.get(reviewer.id).transcript_path is None


def test_a_session_with_the_mod_is_refreshed_and_its_open_wake_dropped(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    current = harness.ledger.get(reviewer.id)
    delivery_mod.record_mod_hello(harness.ledger, current, current.session_uuid)
    end_turn(harness, reviewer)
    fake_source.feed().emit("comment", "PR-7")

    report = harness.next_tick()

    assert report.woken == ["reviewer/PR-7"]
    assert len(launches(harness)) == 2
    fresh = harness.ledger.get(reviewer.id)
    assert harness.ledger.outbox_message(fresh) is None
    assert not delivery_mod.has_mod(fresh)


def test_discard_removes_every_session_of_the_worker(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)
    fresh = harness.ledger.get(reviewer.id)
    request = harness.ledger.record_request("discard", harness.ledger.target_for(fresh), force=True)

    harness.next_tick()

    assert harness.ledger.get_request(request)["ok"] == 1
    assert ["rm", reviewer.session_id] in harness.claude_state.commands
    assert ["rm", fresh.session_id] in harness.claude_state.commands
    assert harness.claude_state.sessions == []


def test_garbage_collection_removes_every_session_of_the_worker(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)
    fresh = harness.ledger.get(reviewer.id)
    end_turn(harness, fresh)
    report = TickReport()

    problem = harness.fresh_runner()._collect(fresh, report)

    assert problem is None
    assert harness.claude_state.sessions == []
    assert harness.ledger.get(reviewer.id).gc_done == 1


def test_the_board_lists_both_sessions_under_the_one_worker(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)
    fresh = harness.ledger.get(reviewer.id)

    page = board_mod.build(harness.config, harness.ledger, health={})

    cards = [card for column in page["columns"] for card in column["cards"]]
    assert len(cards) == 1
    assert [(s["kind"], s["id"], s["current"]) for s in cards[0]["sessions"]] == [
        ("initial", reviewer.session_id, False),
        ("refresh", fresh.session_id, True),
    ]
    assert all(isinstance(s["started_at"], float) for s in cards[0]["sessions"])


def test_a_new_worker_in_the_same_worktree_does_not_take_a_replaced_session(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)
    fresh = harness.ledger.get(reviewer.id)
    other = harness.ledger.create_worker(
        role="reviewer", item="PR-8", item_id="PR-8", url=None, repository="test",
        repo=harness.repo, worktree=Path(reviewer.worktree), branch=reviewer.branch,
    )

    owned = harness.runner._sessions_owned_by_other_workers(other)

    assert {reviewer.session_id, fresh.session_id} <= owned
