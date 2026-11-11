"""Session continuity: each wake continues the session, and past the role's refresh limit
the next wake starts a fresh session for the same worker, which keeps the old one."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import Harness, make_config
from chargehand import board as board_mod
from chargehand import claude
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
    # It loads the mod, whose tool gives the note.
    assert fresh["argv"][fresh["argv"].index("--plugin-dir") + 1] == (
        first["argv"][first["argv"].index("--plugin-dir") + 1]
    )
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
    harness.ledger.update(harness.ledger.get(reviewer.id), context_tokens=500,
                          context_percent=5.0, transcript_path="/t/old.jsonl")
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
    # The same switch as a refresh that ran to its end: the old figures go, and the old
    # transcript path stays on the old session's row.
    assert (taken.context_tokens, taken.context_percent, taken.transcript_path) == (
        None, None, None,
    )
    assert harness.ledger.sessions_of(taken)[0].transcript_path == "/t/old.jsonl"
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


# ----- hooks during the launch, the mod, the note, and what stays in the old session ------


def test_a_permission_prompt_during_the_refresh_launch_keeps_the_worker_blocked(
    after_two, monkeypatch
):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    real = claude.ClaudeRuntime.launch

    def launch_then_prompt(self, spec, **kwargs):
        result = real(self, spec, **kwargs)
        fresh = harness.claude_state.sessions[-1]
        hook = claude.parse_hook("PermissionRequest", {
            "session_id": fresh["uuid"], "cwd": fresh["cwd"], "tool_name": "Bash",
            "tool_input": {"command": "date"},
        })
        harness.runner.apply_hook(harness.ledger.get(reviewer.id), hook)
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "launch", launch_then_prompt)
    report, _ = wake(harness, reviewer)

    assert report.woken == ["reviewer/PR-7"]
    after = harness.ledger.get(reviewer.id)
    assert after.session_id == harness.claude_state.sessions[-1]["id"]
    assert after.state == ledger_mod.BLOCKED
    assert after.session_state == claude.BLOCKED
    assert after.wakes == 3 and harness.ledger.inbox(after) == []
    (prompt,) = harness.ledger.open_permission_prompts(after)
    assert prompt.tool_name == "Bash"
    assert kinds(harness, after)[-1] == "refresh"


def test_a_runtime_that_would_not_load_the_mod_continues_the_session(after_two, monkeypatch):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    monkeypatch.setattr(claude.ClaudeRuntime, "plugin_dirs", lambda self: ())

    report, pointer = wake(harness, reviewer)

    assert report.woken == ["reviewer/PR-7"]
    assert any("no refresh, a fresh session would not load the chargehand mod" in line
               for line in report.warnings)
    assert len(launches(harness)) == 1
    assert resumes(harness)[-1]["target"] == reviewer.session_uuid
    assert resumes(harness)[-1]["prompt"].endswith(f"comment: {pointer}")
    assert kinds(harness, reviewer) == ["initial", "wake", "wake", "wake"]


def test_a_fresh_session_without_a_stored_note_is_not_sent_to_the_tool(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)

    note = launches(harness)[-1]["argv"][-1].split("\n\n")[0]
    assert "left no handoff note" in note and HANDOFF_TOOL not in note
    assert "do not start the work again" not in note


def test_the_old_session_keeps_its_transcript_path_on_its_row(after_two, tmp_path):
    harness = after_two
    reviewer = start_reviewer(harness)
    transcript = tmp_path / "old.jsonl"
    transcript.write_text("{}\n")
    harness.ledger.update(reviewer, transcript_path=str(transcript))
    for _ in range(3):
        wake(harness, reviewer)

    rows = harness.ledger.sessions_of(reviewer)
    assert [(r.kind, r.transcript_path) for r in rows if r.kind != "wake"] == [
        ("initial", str(transcript)), ("refresh", None),
    ]
    assert all(r.transcript_path is None for r in rows if r.kind == "wake")


def test_the_resume_of_a_parked_worker_stays_in_its_session_past_the_limit(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    # A usage limit ended the last turn, and its reset time has passed.
    harness.claude_state.set_session_state(reviewer.session_id, "failed", status="idle")
    harness.ledger.update(
        harness.ledger.get(reviewer.id), state=ledger_mod.PARKED, reset_at=time.time() - 1
    )

    report = harness.next_tick()

    assert report.woken == ["reviewer/PR-7"]
    assert len(launches(harness)) == 1
    assert resumes(harness)[-1]["target"] == reviewer.session_uuid
    assert resumes(harness)[-1]["prompt"] == delivery_mod.RESUME_MESSAGE


def test_the_answer_to_a_question_stays_in_its_session_past_the_limit(after_two):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    # The last turn ended with a plain-text question.
    harness.claude_state.set_session_state(reviewer.session_id, "blocked", status="idle")
    harness.next_tick()
    assert harness.ledger.get(reviewer.id).state == ledger_mod.BLOCKED
    harness.ledger.record_request("reply", harness.ledger.target_for(reviewer), text="yes")

    report = harness.next_tick()

    assert report.woken == ["reviewer/PR-7"]
    assert len(launches(harness)) == 1
    assert resumes(harness)[-1]["target"] == reviewer.session_uuid
    assert resumes(harness)[-1]["prompt"].endswith(f"{delivery_mod.REPLY_PREFIX}yes")


# ----- a worktree root reached through a symbolic link ------------------------------------


def test_a_replaced_session_is_never_adopted_when_the_root_is_reached_through_a_link(
    tmp_path, repo, claude_state, board
):
    # /tmp on macOS: the worktree is stored under the link, and the listing names the real path.
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    write_role(repo, REVIEWER + "\n[policy]\nrefresh_after_wakes = 1\n")
    worktree_root = tmp_path / "link" / "worktrees"
    worktree_root.mkdir()
    config = make_config(
        repo, worktree_root, roles=("implementer", "reviewer"), sources=(FAKE_SOURCE,)
    )
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    harness = Harness(config, ledger, Runner(config, ledger, git=Git(), notifier=notifier),
                      claude_state, board, repo, repo, worktree_root, notifier)
    # A worker started by hand: its handle carries its id, so no live worker has its name.
    ledger.record_request("new_worker", "test", role="reviewer")
    harness.next_tick()
    (worker,) = ledger.live_workers()
    assert worker.origin == ledger_mod.ORIGIN_BOARD
    assert listed(harness, worker.session_id)["cwd"] != worker.worktree

    def reply(text):
        end_turn(harness, worker)
        harness.next_tick()
        ledger.record_request("reply", ledger.target_for(ledger.get(worker.id)), text=text)
        return harness.next_tick()

    reply("first")
    report = reply("second")
    assert report.woken == [worker.handle]
    fresh = ledger.get(worker.id)
    assert fresh.session_id != worker.session_id and len(launches(harness)) == 2

    report = harness.next_tick()
    report = harness.next_tick()

    assert not any("adopted" in line for line in report.warnings)
    assert [w.id for w in ledger.live_workers()] == [worker.id]
    assert report.adopted == []
    # The old session stays stopped and the worker's own: discard removes both.
    ledger.record_request("discard", ledger.target_for(fresh), force=True)
    harness.next_tick()
    assert harness.claude_state.sessions == []
    ledger.close()


def test_an_earlier_session_of_an_ended_worker_that_works_again_is_reported_not_adopted(
    after_two
):
    harness = after_two
    reviewer = start_reviewer(harness)
    for _ in range(3):
        wake(harness, reviewer)
    harness.ledger.record_request("cancel", harness.ledger.target_for(reviewer))
    harness.next_tick()
    assert harness.ledger.get(reviewer.id).is_terminal
    # The operator resumes the session that the refresh replaced.
    harness.claude_state.set_session_state(reviewer.session_id, "working", status="busy")

    report = harness.next_tick()

    assert report.adopted == []
    assert harness.ledger.live_workers() == []
    assert any(f"session {reviewer.session_id}, an earlier session of reviewer/PR-7" in line
               for line in report.warnings)
    # Said once.
    assert not any(reviewer.session_id in line for line in harness.next_tick().warnings)


def test_a_post_of_the_old_session_while_it_stops_does_not_hold_the_fresh_turn(
    after_two, monkeypatch
):
    harness = after_two
    reviewer = start_reviewer(harness)
    wake(harness, reviewer)
    wake(harness, reviewer)
    real = claude.ClaudeRuntime.stop

    def stop_then_post(self, session_id):
        result = real(self, session_id)
        # The old session's last post, such as the turn end its stop reports.
        hook = claude.parse_hook("Stop", {"session_id": reviewer.session_uuid,
                                          "cwd": reviewer.worktree})
        harness.runner.apply_hook(harness.ledger.get(reviewer.id), hook)
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "stop", stop_then_post)
    report, _ = wake(harness, reviewer)

    assert report.woken == ["reviewer/PR-7"]
    after = harness.ledger.get(reviewer.id)
    assert after.session_id != reviewer.session_id
    assert (after.state, after.session_state) == (ledger_mod.RUNNING, claude.WORKING)
    assert after.wakes == 3 and harness.ledger.inbox(after) == []


@pytest.mark.parametrize("working", [False, True])
def test_a_copy_in_the_worktree_of_an_ended_worker_under_a_linked_root_is_not_adopted(
    tmp_path, repo, claude_state, board, working
):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    write_role(repo, REVIEWER)
    worktree_root = tmp_path / "link" / "worktrees"
    worktree_root.mkdir()
    config = make_config(
        repo, worktree_root, roles=("implementer", "reviewer"), sources=(FAKE_SOURCE,)
    )
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    harness = Harness(config, ledger, Runner(config, ledger, git=Git(), notifier=notifier),
                      claude_state, board, repo, repo, worktree_root, notifier)
    ledger.record_request("new_worker", "test", role="reviewer")
    harness.next_tick()
    (worker,) = ledger.live_workers()
    ledger.record_request("cancel", ledger.target_for(worker))
    harness.next_tick()
    assert ledger.get(worker.id).is_terminal
    # A copy that `claude --resume` started under a new id, which no session row names.
    state = claude_state.read()
    state["sessions"].append({
        "id": "copy1", "uuid": "uuid-copy1", "name": worker.handle,
        "cwd": os.path.realpath(worker.worktree), "state": "working" if working else "done",
        **({"pid": 4242, "status": "busy"} if working else {}),
    })
    claude_state.write(state)

    report = harness.next_tick()

    assert report.adopted == []
    assert ledger.live_workers() == []
    said = [line for line in report.warnings if "copy1" in line]
    if working:
        assert said == [
            f"session copy1 of {worker.handle}, which is cancelled, is still working; "
            f"attach to it or stop it"
        ]
    else:
        assert said == []
    ledger.close()
