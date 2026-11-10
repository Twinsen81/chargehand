"""The agent channel: the posts of a worker session's chargehand mod, and the tools it serves.

The daemon here serves on 127.0.0.1 on a port the kernel picks, runs no loop, and is shut
down in the test. The posts are the ones the mod sends; sessions are the fake `claude`'s.
"""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timezone

import pytest

from chargehand import board, channel
from chargehand import ledger as ledger_mod
from chargehand import runtime as runtime_mod
from chargehand import tick as tick_mod
from chargehand.config import SourceConfig
from chargehand.delivery import OUTBOX_POLL_SECS
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}
COMMAND_SOURCE = SourceConfig(name="cmd", type="command", options={"poll": "/bin/true"})


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    worker = harness.worker(identifier)
    assert worker.state == ledger_mod.RUNNING and worker.session_uuid
    return worker


@pytest.fixture
def daemon(harness, tmp_path):
    config = dataclasses.replace(
        support.daemon_config(harness.config, tmp_path / "board.token"), sources=(COMMAND_SOURCE,)
    )
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def post(daemon, path, payload, *, headers=AUTH):
    status, raw = support.request(
        daemon.port, "POST", f"/channel/{path}", body=json.dumps(payload).encode(), headers=headers
    )
    return status, json.loads(raw) if raw else None


def of(worker, **extra):
    return {"session_id": worker.session_uuid, "cwd": worker.worktree, **extra}


# ----- the token and the worker ------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},
    {"Authorization": "Bearer wrong", "Content-Type": "application/json"},
])
def test_every_route_refuses_a_post_without_the_token(daemon, harness, headers):
    worker = launch(harness)
    before = harness.ledger.get(worker.id)

    for path in ("hello", "tool/subscribe", "outbox", "outbox/confirm", "lifecycle/turn_complete",
                 "usage"):
        assert post(daemon, path, of(worker), headers=headers)[0] == 401

    assert harness.ledger.get(worker.id) == before


def test_a_post_from_a_session_no_worker_has_changes_nothing(daemon, harness):
    worker = launch(harness)

    status, _ = post(daemon, "hello", {"session_id": "not-a-worker", "cwd": "/elsewhere"})

    assert status == 404
    assert harness.ledger.get(worker.id).mod_session is None


def test_the_hello_records_the_mod_and_lists_the_tools(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "hello", of(worker, cwd="/somewhere/else"))

    assert status == 200
    assert [tool["name"] for tool in answer["tools"]] == [
        "report", "ask", "handoff", "request_wake", "subscribe",
    ]
    assert all(tool["description"] and tool["input_schema"]["type"] == "object"
               for tool in answer["tools"])
    assert answer["outbox_poll_secs"] == OUTBOX_POLL_SECS
    after = harness.ledger.get(worker.id)
    assert after.mod_session == worker.session_uuid and after.mod_seen_at is not None


def test_a_hello_during_the_launch_is_found_by_the_worktree(daemon, harness):
    worker = launch(harness)
    harness.ledger.update(worker, session_uuid=None)

    status, _ = post(daemon, "hello", {"session_id": "new-session", "cwd": worker.worktree})

    assert status == 200
    # It counts once the tick records that session as the worker's.
    assert harness.ledger.get(worker.id).mod_session == "new-session"


# ----- the tools ---------------------------------------------------------------------


def test_subscribe_records_a_subscription_of_the_calling_worker(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "tool/subscribe", of(worker, input={"item": "DEMO-2", "kinds": ["comment"]}))

    assert status == 200 and answer["is_error"] is False
    assert answer["result"] == "You follow DEMO-2 now: a comment on it wakes you."
    followed = {(s.source, s.item): s.kinds for s in harness.ledger.subscriptions_of(worker)}
    assert followed[("cmd", "DEMO-2")] == ("comment",)

    # Again with another kind: the first one stays.
    status, answer = post(daemon, "tool/subscribe", of(worker, input={"item": "DEMO-2", "kinds": ["ci_status"]}))
    assert answer["result"] == "You follow DEMO-2 now: a comment or a ci_status on it wakes you."
    followed = {(s.source, s.item): s.kinds for s in harness.ledger.subscriptions_of(worker)}
    assert followed[("cmd", "DEMO-2")] == ("comment", "ci_status")


@pytest.mark.parametrize("arguments, says", [
    ({"item": "<script>", "kinds": ["comment"]}, "plain http(s) URL"),
    ({"item": "DEMO-2", "kinds": []}, "kinds must be"),
    ({"item": "DEMO-2", "kinds": ["Comment!"]}, "kinds must be"),
    ({"item": "https://tracker.invalid/DEMO-2", "kinds": ["comment"]}, "no configured source"),
    ({"kinds": ["comment"]}, "plain http(s) URL"),
])
def test_subscribe_refuses_what_it_cannot_follow(daemon, harness, arguments, says):
    worker = launch(harness)
    before = harness.ledger.subscriptions_of(worker)

    status, answer = post(daemon, "tool/subscribe", of(worker, input=arguments))

    assert status == 200 and answer["is_error"] is True and says in answer["result"]
    assert harness.ledger.subscriptions_of(worker) == before


def turn_end(daemon, harness, worker):
    status, _ = post(daemon, "lifecycle/turn_complete", of(worker, reason="answer", answer="ok"))
    assert status == 200
    return harness.ledger.get(worker.id)


def answer(daemon, worker, body):
    raw = json.dumps(body).encode()
    status, payload = support.request(
        daemon.port, "POST", f"/api/workers/{worker.id}/answer", body=raw, headers=AUTH
    )
    return status, json.loads(payload or b"{}")


def test_ask_records_an_open_question_and_the_turn_end_blocks_the_worker(daemon, harness):
    worker = launch(harness)

    status, said = post(daemon, "tool/ask", of(worker, input={
        "question": "Which colour?\x1b[2J", "options": ["red", "blue", "red", " "],
    }))

    assert status == 200 and said["is_error"] is False
    assert "End your turn now" in said["result"]
    (question,) = harness.ledger.open_permission_prompts(worker)
    assert (question.kind, question.tool_input) == (ledger_mod.PROMPT_QUESTION, "Which colour?")
    assert json.loads(question.options) == ["red", "blue"]
    # The turn still runs until the session ends it.
    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING

    assert turn_end(daemon, harness, worker).state == ledger_mod.BLOCKED
    assert harness.notifications()[-1] == ("ABC-1", "blocked")
    assert harness.notifier.sent[-1].detail == tick_mod.ASKED
    # A second question replaces the first.
    post(daemon, "tool/ask", of(worker, input={"question": "Which shade?"}))
    assert [p.tool_input for p in harness.ledger.open_permission_prompts(worker)] == ["Which shade?"]


# The listing after such a turn: `blocked` with `status: idle`, as for a plain-text question,
# or `done`. Either way the answer wakes the worker.
@pytest.mark.parametrize("listed", ["blocked", "done"])
def test_the_board_answers_a_question_once_and_the_answer_wakes_the_worker(
    daemon, harness, monkeypatch, listed
):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?", "options": ["red", "blue"]}))
    turn_end(daemon, harness, worker)
    harness.claude_state.set_session_state(worker.session_id, listed, status="idle")
    harness.next_tick()
    assert harness.ledger.get(worker.id).state == ledger_mod.BLOCKED
    (question,) = harness.ledger.open_permission_prompts(worker)
    shown = board.card(
        harness.ledger.get(worker.id), harness.ledger, board._Roles(harness.config),
        attach=lambda w: None, prompts=[question], now=time.time(),
    )
    assert (shown["column"], shown["permission_prompt"]["kind"]) == ("needs_you", "question")
    assert shown["permission_prompt"]["options"] == ["red", "blue"]
    assert shown["permission_prompt"]["tool_input"] == "Which colour?"

    status, recorded = answer(daemon, worker, {"prompt": question.id, "text": "red"})

    assert status == 202 and recorded["kind"] == "reply"
    assert harness.ledger.get_permission_prompt(question.id).state == ledger_mod.PROMPT_RESOLVED
    assert answer(daemon, worker, {"prompt": question.id, "text": "blue"})[0] == 409
    harness.next_tick()
    woken = harness.ledger.get(worker.id)
    assert (woken.state, woken.wakes) == (ledger_mod.RUNNING, 1)
    (resume,) = harness.claude_state.read()["resumes"]
    assert "reply from the operator: red" in resume["prompt"].splitlines()
    assert harness.ledger.pending_requests() == []


# The steps of a notification sequence. A step named "listed" runs a tick with that listing.
PERMISSION, ANSWER_PERMISSION, ASK, STOP = "permission", "answer permission", "ask", "stop"
LISTED_BLOCKED, LISTED_DONE, ANSWER_QUESTION = "listed blocked", "listed done", "answer question"
STATUS_NEEDS_INPUT = "status file needs-input"


@pytest.mark.parametrize("status_file, steps, notified", [
    # The duplicate first seen: the Stop hook, then the listing's views of the same turn end.
    (False, [ASK, STOP, LISTED_BLOCKED, LISTED_DONE, LISTED_BLOCKED], 1),
    # The listing sees the question before the Stop hook comes in.
    (False, [ASK, LISTED_BLOCKED, STOP, LISTED_BLOCKED], 1),
    # The answer resolves the question; the tick that applies it still lists it blocked.
    (False, [ASK, STOP, LISTED_BLOCKED, ANSWER_QUESTION, LISTED_BLOCKED], 1),
    # A permission prompt, answered before the next tick, then the question: two of them.
    (False, [PERMISSION, ANSWER_PERMISSION, ASK, STOP, LISTED_BLOCKED], 2),
    # The question, then a permission prompt of the same turn, answered before the Stop.
    (False, [ASK, PERMISSION, ANSWER_PERMISSION, STOP, LISTED_BLOCKED], 2),
    # A status-file question after an answered permission prompt.
    (True, [PERMISSION, ANSWER_PERMISSION, STATUS_NEEDS_INPUT, LISTED_DONE], 2),
])
def test_each_question_and_each_prompt_is_notified_once(
    daemon, harness, monkeypatch, tmp_path, status_file, steps, notified
):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    status_dir = tmp_path / "status"
    if status_file:
        status_dir.mkdir()
        (harness.repo / ".chargehand.toml").write_text(
            f'base = "origin/main"\nprompt = "{{issue}}"\n'
            f'status_file = "{status_dir}/{{issue}}.json"\n'
        )
    worker = launch(harness)

    def listed(state):
        harness.claude_state.set_session_state(worker.session_id, state, status="idle")
        harness.next_tick()

    for step in steps:
        if step == PERMISSION:
            harness.runner.apply_hook(harness.ledger.get(worker.id), runtime_mod.Hook(
                kind=runtime_mod.HOOK_PERMISSION, event="PermissionRequest",
                observed=runtime_mod.BLOCKED, tool_name="Bash", tool_input="ls",
            ))
        elif step == ANSWER_PERMISSION:
            (prompt,) = [p for p in harness.ledger.open_permission_prompts(worker)
                         if p.kind == ledger_mod.PROMPT_PERMISSION]
            assert harness.ledger.answer_permission_prompt(
                prompt.id, ledger_mod.ALLOW, by=ledger_mod.RESOLVED_BY_BOARD
            )
        elif step == ASK:
            post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
        elif step == STOP:
            turn_end(daemon, harness, worker)
        elif step == LISTED_BLOCKED:
            listed("blocked")
        elif step == LISTED_DONE:
            listed("done")
        elif step == ANSWER_QUESTION:
            (question,) = harness.ledger.open_permission_prompts(worker)
            assert answer(daemon, worker, {"prompt": question.id, "text": "red"})[0] == 202
        elif step == STATUS_NEEDS_INPUT:
            (status_dir / "ABC-1.json").write_text(json.dumps({"state": "needs-input"}))

    assert harness.notifications().count(("ABC-1", "blocked")) == notified


def test_an_answer_needs_one_line_of_text(daemon, harness):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    (question,) = harness.ledger.open_permission_prompts(worker)

    for body in ({"prompt": question.id}, {"prompt": question.id, "text": "  "},
                 {"prompt": question.id, "text": "x" * 501}):
        assert answer(daemon, worker, body)[0] == 400
    assert harness.ledger.get_permission_prompt(question.id).state == ledger_mod.PROMPT_OPEN
    assert harness.ledger.pending_requests() == []


def test_a_permission_prompt_leaves_an_open_question_open(daemon, harness):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    hook = runtime_mod.Hook(
        kind=runtime_mod.HOOK_PERMISSION, event="PermissionRequest", observed=runtime_mod.BLOCKED,
        tool_name="Bash", tool_input="ls",
    )

    harness.runner.apply_hook(harness.ledger.get(worker.id), hook)

    kinds = sorted(p.kind for p in harness.ledger.open_permission_prompts(worker))
    assert kinds == [ledger_mod.PROMPT_PERMISSION, ledger_mod.PROMPT_QUESTION]

    # The prompt is answered and the same turn goes on: the question stays, and the end of
    # that turn blocks the worker with it.
    harness.claude_state.set_session_state(worker.session_id, "working", status="busy")
    harness.next_tick()
    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING
    (question,) = harness.ledger.open_permission_prompts(worker)
    assert question.kind == ledger_mod.PROMPT_QUESTION
    blocked = turn_end(daemon, harness, worker)
    assert (blocked.state, harness.notifier.sent[-1].detail) == (ledger_mod.BLOCKED, tick_mod.ASKED)


def test_a_new_turn_after_the_question_ended_its_turn_cancels_it(daemon, harness):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    assert turn_end(daemon, harness, worker).state == ledger_mod.BLOCKED

    # The operator answers in `claude attach`, which starts a new turn.
    harness.claude_state.set_session_state(worker.session_id, "working", status="busy")
    harness.next_tick()

    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING
    assert harness.ledger.open_permission_prompts(worker) == []
    assert turn_end(daemon, harness, worker).state == ledger_mod.IDLE


@pytest.mark.parametrize("listed", ["blocked", "done"])
def test_a_reply_from_the_command_line_answers_an_ask_question(daemon, harness, listed):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    turn_end(daemon, harness, worker)
    harness.claude_state.set_session_state(worker.session_id, listed, status="idle")
    harness.next_tick()
    (question,) = harness.ledger.open_permission_prompts(worker)

    request_id = harness.ledger.record_request("reply", worker.target, text="red")
    harness.next_tick()

    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 1 and "answers the worker's question" in outcome["message"]
    resolved = harness.ledger.get_permission_prompt(question.id)
    assert (resolved.state, resolved.resolved_by) == (
        ledger_mod.PROMPT_RESOLVED, ledger_mod.RESOLVED_BY_REPLY,
    )
    woken = harness.ledger.get(worker.id)
    assert (woken.state, woken.wakes) == (ledger_mod.RUNNING, 1)
    (resume,) = harness.claude_state.read()["resumes"]
    assert "reply from the operator: red" in resume["prompt"].splitlines()


def test_a_question_is_cancelled_when_the_worker_leaves_blocked_by_another_path(daemon, harness):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    blocked = turn_end(daemon, harness, worker)

    harness.ledger.update(blocked, state=ledger_mod.PAUSED)

    assert harness.ledger.open_permission_prompts(worker) == []


@pytest.mark.parametrize("state, ended, label", [
    ("done", ledger_mod.DONE, ()),
    # As the status file's `aborted`: the label asks the operator for a look.
    ("failed", ledger_mod.FAILED, ("chargehand-blocked",)),
])
def test_a_report_of_done_or_failed_ends_the_worker_at_the_turn_end(daemon, harness, state, ended, label):
    worker = launch(harness)

    status, said = post(daemon, "tool/report", of(worker, input={
        "state": state, "summary": "chose red\x1b[31m", "links": ["https://example.invalid/x", "javascript:x"],
    }))

    assert status == 200 and said["is_error"] is False
    assert "1 link(s) were left out" in said["result"] and "End your turn now" in said["result"]
    reported = harness.ledger.get(worker.id)
    assert (reported.report_state, reported.report_summary) == (state, "chose red")
    assert json.loads(reported.report_links) == ["https://example.invalid/x"]
    assert reported.state == ledger_mod.RUNNING

    after = turn_end(daemon, harness, worker)

    assert after.state == ended
    assert harness.labels("ABC-1") == label
    assert harness.notifications()[-1] == ("ABC-1", ended)
    if ended == ledger_mod.FAILED:
        assert after.last_error == "failed: chose red"


def test_a_report_from_before_the_turn_started_does_not_count(daemon, harness):
    worker = launch(harness)
    post(daemon, "tool/report", of(worker, input={"state": "done", "summary": "old"}))
    before_wake = harness.ledger.get(worker.id)
    harness.ledger.update(
        before_wake, last_wake_at=before_wake.reported_at + tick_mod.REPORT_TURN_MARGIN_SECS + 1
    )

    assert turn_end(daemon, harness, worker).state == ledger_mod.IDLE


def test_working_and_needs_input_are_card_text_only(daemon, harness):
    worker = launch(harness)

    post(daemon, "tool/report", of(worker, input={"state": "needs-input", "summary": "a look, please"}))

    assert turn_end(daemon, harness, worker).state == ledger_mod.IDLE
    shown = board.report_of(harness.ledger.get(worker.id))
    assert (shown["state"], shown["summary"], shown["links"]) == ("needs-input", "a look, please", [])


class _PullRequests:
    """A source that has pull requests and names one by its URL."""

    has_pull_requests = True

    def item_for(self, text):
        from chargehand.sources.base import WorkItem

        if text == "https://github.com/o/r/pull/7":
            return WorkItem(source="gh", key="o/r#7", ref="o/r#7", url=text)
        return None


def test_a_pull_request_link_becomes_the_card_link_and_is_followed(daemon, harness, monkeypatch):
    from chargehand import tick

    monkeypatch.setattr(
        tick.Runner, "source", lambda self, name: _PullRequests() if name == "cmd" else None
    )
    worker = launch(harness)

    status, said = post(daemon, "tool/report", of(worker, input={
        "state": "working", "summary": "opened it", "links": ["https://github.com/o/r/pull/7"],
    }))

    assert status == 200 and "You follow this pull request now" in said["result"]
    after = harness.ledger.get(worker.id)
    assert after.pr_url == "https://github.com/o/r/pull/7"
    followed = {(s.source, s.item): s.kinds for s in harness.ledger.subscriptions_of(after)}
    assert followed[("gh", "o/r#7")] == tick.PULL_REQUEST_KINDS
    assert harness.ledger.get_state(f"{tick.FOLLOWS_PULL_REQUEST_PREFIX}{worker.id}") == "o/r#7"


def test_a_link_that_names_no_pull_request_is_not_the_card_link(daemon, harness):
    worker = launch(harness)

    post(daemon, "tool/report", of(worker, input={
        "state": "working", "summary": "x", "links": ["https://github.com/o/r/issues/7"],
    }))

    assert harness.ledger.get(worker.id).pr_url is None


def test_handoff_stores_the_note_and_keeps_it_out_of_the_log(daemon, harness, caplog):
    worker = launch(harness)
    note = "secret plan: line one\nline two\x1b[2J"

    with caplog.at_level("DEBUG"):
        status, said = post(daemon, "tool/handoff", of(worker, input={"note": note}))

    assert status == 200 and said["is_error"] is False
    stored = harness.ledger.get(worker.id)
    assert stored.handoff_note == "secret plan: line one\nline two"
    assert stored.handoff_at is not None
    assert "secret plan" not in caplog.text

    post(daemon, "tool/handoff", of(worker, input={"note": "y" * (channel.HANDOFF_LIMIT + 10)}))
    assert len(harness.ledger.get(worker.id).handoff_note) == channel.HANDOFF_LIMIT


def test_request_wake_is_delivered_once_due_and_counts_as_a_wake(daemon, harness):
    worker = launch(harness)

    status, said = post(daemon, "tool/request_wake", of(worker, input={"after_secs": 600}))

    assert status == 200 and said["is_error"] is False
    idle = turn_end(daemon, harness, worker)
    assert idle.state == ledger_mod.IDLE
    harness.claude_state.set_session_state(worker.session_id, "done")
    timer = harness.ledger.pending_timer(idle)
    assert timer is not None and timer.due_at > time.time() + 590
    assert harness.ledger.inbox(idle) == []
    harness.next_tick()
    assert harness.ledger.get(worker.id).wakes == 0

    harness.ledger._conn.execute("UPDATE inbox SET due_at = ? WHERE id = ?", (time.time() - 1, timer.id))
    harness.next_tick()

    woken = harness.ledger.get(worker.id)
    assert (woken.state, woken.wakes) == (ledger_mod.RUNNING, 1)
    assert harness.ledger.wakes_since(woken, time.time() - 60) == 1
    (resume,) = harness.claude_state.read()["resumes"]
    assert "timer: request_wake" in resume["prompt"].splitlines()


def test_a_new_request_wake_replaces_the_one_that_waits(daemon, harness):
    worker = launch(harness)

    post(daemon, "tool/request_wake", of(worker, input={"after_secs": 600}))
    post(daemon, "tool/request_wake", of(worker, input={"after_secs": 60}))

    pending = harness.ledger.inbox(worker, now=time.time() + 3600)
    assert [(e.source, e.kind) for e in pending] == [("timer", "timer")]
    assert pending[0].due_at < time.time() + 61


@pytest.mark.parametrize("tool, arguments, says", [
    ("report", {"state": "finished", "summary": "x"}, "state must be one of"),
    ("report", {"state": "done"}, "summary must be"),
    ("report", {"state": "done", "summary": "x", "links": "https://x.invalid"}, "links must be"),
    ("ask", {"question": " "}, "question must be"),
    ("ask", {"question": "q", "options": ["a"] * 11}, "options must be"),
    ("handoff", {"note": ""}, "note must be"),
    ("request_wake", {"after_secs": 0}, "after_secs must be"),
    ("request_wake", {"after_secs": True}, "after_secs must be"),
    ("request_wake", {"after_secs": channel.MAX_WAKE_AFTER_SECS + 1}, "after_secs must be"),
])
def test_the_tools_refuse_what_they_cannot_take(daemon, harness, tool, arguments, says):
    worker = launch(harness)
    before = harness.ledger.get(worker.id)

    status, said = post(daemon, f"tool/{tool}", of(worker, input=arguments))

    assert status == 200 and said["is_error"] is True and says in said["result"]
    assert harness.ledger.get(worker.id) == before
    assert harness.ledger.open_permission_prompts(worker) == []
    assert harness.ledger.pending_timer(worker) is None


def test_an_unknown_tool_or_input_is_refused(daemon, harness):
    worker = launch(harness)

    assert post(daemon, "tool/approve", of(worker, input={}))[0] == 404
    assert post(daemon, "tool/subscribe", of(worker, input=["DEMO-2"]))[0] == 400


# ----- the outbox ------------------------------------------------------------------


def test_the_mod_takes_its_message_once_and_its_confirm_records_the_wake(daemon, harness):
    worker = launch(harness)
    harness.ledger.update(worker, state=ledger_mod.IDLE)
    put = harness.ledger.put_outbox(worker, session_uuid=worker.session_uuid, text="wake", entries=[])

    status, answer = post(daemon, "outbox", of(worker))
    assert (status, answer) == (200, {"message": {"id": put.id, "text": "wake"}})
    assert post(daemon, "outbox", of(worker))[1] == {"message": None}

    status, answer = post(daemon, "outbox/confirm", of(worker, id=put.id, submitted=True))

    assert (status, answer) == (200, {"outcome": "recorded"})
    woken = harness.ledger.get(worker.id)
    assert woken.state == ledger_mod.RUNNING and woken.wakes == 1
    assert [s.kind for s in harness.ledger.sessions_of(woken)][-1] == ledger_mod.SESSION_WAKE
    assert post(daemon, "outbox/confirm", of(worker, id=put.id, submitted=True))[1] == {
        "outcome": "already recorded",
    }
    assert post(daemon, "outbox/confirm", of(worker, id=put.id + 99, submitted=True))[0] == 404
    assert post(daemon, "outbox/confirm", of(worker, id="1", submitted=True))[0] == 400


def test_another_session_or_a_paused_worker_takes_no_message(daemon, harness):
    worker = launch(harness)
    harness.ledger.put_outbox(worker, session_uuid=worker.session_uuid, text="wake", entries=[])

    assert post(daemon, "outbox", of(worker, session_id="someone-else"))[0] == 404
    harness.ledger.update(harness.ledger.get(worker.id), state=ledger_mod.PAUSED)
    # A paused worker goes on after `continue`, so its mod keeps polling.
    assert post(daemon, "outbox", of(worker))[1] == {"message": None}
    assert harness.ledger.outbox_message(worker).state == ledger_mod.OUTBOX_PENDING


@pytest.mark.parametrize("state", ["done", "failed"])
def test_the_mod_of_an_ended_worker_is_told_to_stop_polling(daemon, harness, caplog, state):
    worker = launch(harness)
    post(daemon, "tool/report", of(worker, input={"state": state, "summary": "x"}))
    assert turn_end(daemon, harness, worker).state == state

    with caplog.at_level("INFO"):
        status, answer = post(daemon, "outbox", of(worker))

    assert (status, answer) == (200, {"message": None, "ended": True})
    assert "refused" not in caplog.text and "no live worker" not in caplog.text
    assert sum("the mod stops polling" in r.getMessage() for r in caplog.records) == 1


# ----- lifecycle and usage ---------------------------------------------------------


def test_a_turn_end_from_the_mod_moves_the_worker_as_the_stop_hook_does(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "lifecycle/turn_complete", of(worker, reason="answer", answer="all done"))

    assert (status, answer) == (200, {})
    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.IDLE
    assert (after.last_hook, after.last_answer) == ("mod:turn_complete", "all done")
    # The Stop hook of the same turn end changes nothing more.
    status, _ = support.request(
        daemon.port, "POST", "/hooks/Stop", headers=AUTH,
        body=json.dumps({"session_id": worker.session_uuid, "last_assistant_message": "all done"}).encode(),
    )
    assert status == 200 and harness.worker("ABC-1").state == ledger_mod.IDLE


def test_an_unknown_lifecycle_event_is_not_found(daemon, harness):
    worker = launch(harness)

    assert post(daemon, "lifecycle/permission", of(worker))[0] == 404
    assert post(daemon, "lifecycle", of(worker))[0] == 404


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_usage_post_shows_in_the_state_with_its_source(daemon, harness):
    worker = launch(harness)
    reset = time.time() + 86400

    status, answer = post(daemon, "usage", of(worker, rate_limits=[
        {"kind": "five_hour", "percentUsed": 99, "resetsAt": _iso(time.time() + 3600)},
        {"kind": "seven_day", "percentUsed": 41.5, "resetsAt": _iso(reset)},
    ], context={"tokens": 12000, "window": 200000, "percent": 6}))

    assert (status, answer) == (200, {"recorded": True})
    status, body = support.request(daemon.port, "GET", "/api/state")
    reading = json.loads(body)["usage"]
    assert (reading["known"], reading["source"]) == (True, "session")
    assert reading["seven_day"]["used_percent"] == 41.5
    assert "five_hour" not in reading
    after = harness.ledger.get(worker.id)
    assert (after.context_tokens, after.context_percent) == (12000, 6.0)


def test_a_usage_post_without_a_weekly_window_leaves_the_reading(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "usage", of(worker, rate_limits=[
        {"kind": "five_hour", "percentUsed": 99, "resetsAt": _iso(time.time() + 3600)},
    ], context={"tokens": -1, "percent": "a lot"}))

    assert (status, answer) == (200, {"recorded": False})
    status, body = support.request(daemon.port, "GET", "/api/state")
    assert json.loads(body)["usage"]["known"] is False
    after = harness.ledger.get(worker.id)
    assert (after.context_tokens, after.context_percent) == (None, None)
