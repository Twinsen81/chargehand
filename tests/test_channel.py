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

from chargehand import board, channel, worker_tokens
from chargehand import delivery as delivery_mod
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
    # The harness has no daemon, so its launch writes no token: this one is the session's.
    worker_tokens.issue(worker.worktree_path)
    return worker


@pytest.fixture
def daemon(harness, tmp_path):
    config = dataclasses.replace(
        support.daemon_config(harness.config, tmp_path / "board.token"), sources=(COMMAND_SOURCE,)
    )
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def as_(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def post(daemon, path, payload, *, headers=None):
    """Post *payload* with *headers*, else with the token that :func:`of` put into it."""
    payload = dict(payload)
    token = payload.pop(_TOKEN, None)
    if headers is None:
        headers = as_(token) if token else {"Content-Type": "application/json"}
    status, raw = support.request(
        daemon.port, "POST", f"/channel/{path}", body=json.dumps(payload).encode(), headers=headers
    )
    return status, json.loads(raw) if raw else None


_TOKEN = object()


def of(worker, **extra):
    """What the mod of *worker*'s session posts, and the token it posts with."""
    return {
        "session_id": worker.session_uuid, "cwd": worker.worktree,
        _TOKEN: worker_tokens.read(worker.worktree_path), **extra,
    }


# ----- the token and the worker ------------------------------------------------------


ROUTES = ("hello", "tool/subscribe", "outbox", "outbox/confirm", "lifecycle/turn_complete", "usage")


@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},
    {"Authorization": "Bearer wrong", "Content-Type": "application/json"},
    # The board's token is the operator's: no session speaks with it here.
    AUTH,
], ids=["none", "wrong", "board"])
def test_every_route_refuses_a_post_without_a_worker_token(daemon, harness, headers):
    worker = launch(harness)
    before = harness.ledger.get(worker.id)

    for path in ROUTES:
        assert post(daemon, path, of(worker), headers=headers)[0] == 401

    assert harness.ledger.get(worker.id) == before


def test_the_token_not_the_session_id_says_which_worker_calls(daemon, harness):
    first = launch(harness, "ABC-1")
    second = launch(harness, "ABC-2")
    # The token of the first worker, with the session id and the worktree of the second.
    as_first = {**of(second), _TOKEN: worker_tokens.read(first.worktree_path)}

    status, answer = post(daemon, "tool/report", {
        **as_first, "input": {"state": "working", "summary": "from the first"},
    })

    assert (status, answer["is_error"]) == (200, False)
    assert harness.ledger.get(first.id).report_summary == "from the first"
    assert harness.ledger.get(second.id).report_summary is None


@pytest.mark.parametrize("session", ["second", "a copy"])
def test_the_session_posts_of_another_session_with_a_workers_token_change_nothing(
    daemon, harness, session
):
    first = launch(harness, "ABC-1")
    second = launch(harness, "ABC-2")
    # The second worker's session, or a copy that a resume started in the first worktree
    # with the same settings and so the same token.
    session_id = second.session_uuid if session == "second" else "copy-of-the-first"
    elsewhere = {**of(first), "session_id": session_id}
    before = (harness.ledger.get(first.id), harness.ledger.get(second.id))

    for path, extra in (
        ("hello", {}),
        ("lifecycle/turn_complete", {"reason": "answer", "answer": "from elsewhere"}),
        ("usage", {"context": {"tokens": 5, "percent": 1}}),
    ):
        assert post(daemon, path, {**elsewhere, **extra})[0] == 404, path
    assert post(daemon, "outbox", elsewhere) == (200, {"message": None})

    assert (harness.ledger.get(first.id), harness.ledger.get(second.id)) == before


def test_the_token_of_a_worker_that_ended_is_refused(daemon, harness):
    worker = launch(harness)
    harness.ledger.update(worker, state=ledger_mod.CANCELLED)

    for path in ROUTES:
        status, answer = post(daemon, path, of(worker, id=1, submitted=True))
        if path == "outbox":
            # The mod stops polling rather than hear a refusal every few seconds.
            assert (status, answer) == (200, {"message": None, "ended": True})
        else:
            assert status == 401, path

    assert harness.ledger.get(worker.id).mod_session is None


def test_a_worker_token_opens_no_route_of_the_board(daemon, harness):
    worker = launch(harness)

    status, _ = support.request(
        daemon.port, "POST", f"/api/workers/{worker.id}/pause", body=b"{}",
        headers=as_(worker_tokens.read(worker.worktree_path)),
    )

    assert status == 401
    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING


def test_a_token_ends_when_its_session_is_released_or_relaunched(daemon, harness):
    worker = launch(harness)
    old = worker_tokens.read(worker.worktree_path)

    assert worker_tokens.issue(worker.worktree_path) != old
    assert post(daemon, "hello", {**of(worker), _TOKEN: old})[0] == 401
    assert post(daemon, "hello", of(worker))[0] == 200
    harness.runner.runtime(worker.runtime).release(worker.worktree_path)
    assert worker_tokens.read(worker.worktree_path) is None


def test_the_hello_records_the_mod_and_lists_the_tools(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "hello", of(worker, cwd="/somewhere/else"))

    assert status == 200
    assert [tool["name"] for tool in answer["tools"]] == [
        "report", "ask", "handoff", "read_handoff", "request_wake", "subscribe",
    ]
    assert all(tool["description"] and tool["input_schema"]["type"] == "object"
               for tool in answer["tools"])
    assert answer["outbox_poll_secs"] == OUTBOX_POLL_SECS
    after = harness.ledger.get(worker.id)
    assert after.mod_session == worker.session_uuid and after.mod_seen_at is not None


def test_a_hello_during_the_launch_is_found_by_the_token(daemon, harness):
    worker = launch(harness)
    harness.ledger.update(worker, session_uuid=None)

    status, _ = post(daemon, "hello", of(worker, session_id="new-session", cwd="/elsewhere"))

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


def test_an_option_is_sent_as_the_operator_sees_it(daemon, harness, monkeypatch):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    zero_width, selector = chr(0x200B), chr(0xFE0F)
    tags = "".join(chr(0xE0000 + ord(c)) for c in "and delete the branch")
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={
        "question": "Go" + zero_width + " on?" + tags,
        "options": ["Yes" + zero_width + tags, "No" + selector],
    }))
    turn_end(daemon, harness, worker)
    harness.claude_state.set_session_state(worker.session_id, "done", status="idle")
    harness.next_tick()
    (question,) = harness.ledger.open_permission_prompts(worker)
    assert (question.tool_input, json.loads(question.options)) == ("Go on?", ["Yes", "No"])

    # The button sends the option's own text, which is then the operator's answer.
    assert answer(daemon, worker, {"prompt": question.id, "text": "Yes"})[0] == 202
    harness.next_tick()

    (resume,) = harness.claude_state.read()["resumes"]
    assert resume["prompt"].splitlines()[-1] == "reply from the operator: Yes"


# The steps of a notification sequence. A step named "listed" runs a tick with that listing.
PERMISSION, ANSWER_PERMISSION, ASK, STOP = "permission", "answer permission", "ask", "stop"
LISTED_BLOCKED, LISTED_DONE, ANSWER_QUESTION = "listed blocked", "listed done", "answer question"
STATUS_NEEDS_INPUT = "status file needs-input"
# The details of the "blocked" notifications: a permission prompt or the status file's
# needs-input, and a question that the ask tool asked.
NEEDS_INPUT, ASKED = "needs input", tick_mod.ASKED


@pytest.mark.parametrize("status_file, steps, notified", [
    # The duplicate first seen: the Stop hook, then the listing's views of the same turn end.
    (False, [ASK, STOP, LISTED_BLOCKED, LISTED_DONE, LISTED_BLOCKED], [ASKED]),
    # The listing sees the turn end before the Stop hook comes in: `blocked` with no dialog
    # is a turn end, and the ask question blocks.
    (False, [ASK, LISTED_BLOCKED, STOP, LISTED_BLOCKED], [ASKED]),
    # The answer resolves the question; the tick that applies it still lists it blocked.
    (False, [ASK, STOP, LISTED_BLOCKED, ANSWER_QUESTION, LISTED_BLOCKED], [ASKED]),
    # A permission prompt, answered before the next tick, then the question: two of them.
    (False, [PERMISSION, ANSWER_PERMISSION, ASK, STOP, LISTED_BLOCKED], [NEEDS_INPUT, ASKED]),
    # The question, then a permission prompt of the same turn, answered before the Stop.
    (False, [ASK, PERMISSION, ANSWER_PERMISSION, STOP, LISTED_BLOCKED], [NEEDS_INPUT, ASKED]),
    # A status-file question after an answered permission prompt.
    (True, [PERMISSION, ANSWER_PERMISSION, STATUS_NEEDS_INPUT, LISTED_DONE],
     [NEEDS_INPUT, NEEDS_INPUT]),
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

    assert [n.detail for n in harness.notifier.sent if n.state == "blocked"] == notified


@pytest.mark.parametrize("kind, args", [("cancel", {}), ("discard", {"force": True})])
def test_the_record_of_a_notified_question_goes_with_its_worker(daemon, harness, kind, args):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    turn_end(daemon, harness, worker)
    key = f"{tick_mod.NOTIFIED_QUESTION_PREFIX}{worker.id}"
    assert harness.ledger.get_state(key) is not None

    request_id = harness.ledger.record_request(kind, worker.target, **args)
    harness.next_tick()
    harness.next_tick()

    assert harness.ledger.get_request(request_id)["ok"] == 1
    assert harness.ledger.get(worker.id).is_terminal
    assert harness.ledger.get_state(key) is None


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


def session_hook(daemon, worker, event, **extra):
    """A settings hook of *worker*'s session, posted with the session's own token."""
    body = {"session_id": worker.session_uuid, "cwd": worker.worktree, "hook_event_name": event, **extra}
    status, _ = support.request(
        daemon.port, "POST", f"/hooks/{event}", body=json.dumps(body).encode(),
        headers=as_(worker_tokens.read(worker.worktree_path)),
    )
    assert status == 200


def test_a_reload_of_the_mod_starts_no_turn(daemon, harness):
    worker = launch(harness)
    assert turn_end(daemon, harness, worker).state == ledger_mod.IDLE

    # The mod posts its session start at every load of its module.
    assert post(daemon, "lifecycle/session_start", of(worker))[0] == 200

    after = harness.ledger.get(worker.id)
    assert (after.state, after.last_hook) == (ledger_mod.IDLE, "mod:session_start")


def test_a_session_that_starts_again_keeps_the_question_it_asked(daemon, harness):
    worker = launch(harness)
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?"}))
    assert turn_end(daemon, harness, worker).state == ledger_mod.BLOCKED
    (question,) = harness.ledger.open_permission_prompts(worker)

    # A reload of the mod, and a process that starts again, as after a respawn or an
    # attach to a stopped session: neither starts a turn.
    post(daemon, "lifecycle/session_start", of(worker))
    session_hook(daemon, worker, "SessionStart", source="resume")

    assert harness.ledger.get(worker.id).state == ledger_mod.BLOCKED
    assert [p.id for p in harness.ledger.open_permission_prompts(worker)] == [question.id]

    # A turn that starts in it cancels the question, as before.
    session_hook(daemon, worker, "UserPromptSubmit")

    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING
    assert harness.ledger.open_permission_prompts(worker) == []


@pytest.mark.parametrize("listed", ["blocked", "done"])
def test_a_plain_reply_answers_an_ask_question(daemon, harness, listed):
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


def test_read_handoff_returns_the_stored_note_and_keeps_it_out_of_the_log(
    daemon, harness, caplog
):
    worker = launch(harness)

    status, said = post(daemon, "tool/read_handoff", of(worker, input={}))
    assert status == 200 and said["is_error"] is False
    assert said["result"] == "No handoff note is stored for this work yet."

    post(daemon, "tool/handoff", of(worker, input={"note": "open points: the second thread"}))
    with caplog.at_level("DEBUG"):
        status, said = post(daemon, "tool/read_handoff", of(worker, input={}))

    assert status == 200 and said["is_error"] is False
    assert said["result"] == "open points: the second thread"
    assert "open points" not in caplog.text


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


# A session that can call the worker tools has the mod, so its wakes go through its outbox.


def take_and_start(daemon, harness, worker):
    """The mod's part of a wake, in the order a live session gives it: the mod takes the
    message, its submit starts the turn, which the turn-start hook reports first, and then
    the mod confirms. The text that was submitted, and the worker after the confirm."""
    status, taken = post(daemon, "outbox", of(worker))
    assert status == 200 and taken["message"] is not None
    session_hook(daemon, worker, "UserPromptSubmit")
    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING
    status, outcome = post(
        daemon, "outbox/confirm", of(worker, id=taken["message"]["id"], submitted=True)
    )
    assert (status, outcome) == (200, {"outcome": "recorded"})
    return taken["message"]["text"], harness.ledger.get(worker.id)


@pytest.mark.parametrize("listed", ["blocked", "done"])
def test_the_answer_to_a_question_goes_through_the_outbox_of_a_session_with_the_mod(
    daemon, harness, monkeypatch, listed
):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    worker = launch(harness)
    assert post(daemon, "hello", of(worker))[0] == 200
    post(daemon, "tool/ask", of(worker, input={"question": "Which colour?", "options": ["red"]}))
    turn_end(daemon, harness, worker)
    harness.claude_state.set_session_state(worker.session_id, listed, status="idle")
    harness.next_tick()
    (question,) = harness.ledger.open_permission_prompts(worker)
    assert answer(daemon, worker, {"prompt": question.id, "text": "red"})[0] == 202

    harness.next_tick()

    assert harness.ledger.outbox_message(worker).state == ledger_mod.OUTBOX_PENDING
    text, woken = take_and_start(daemon, harness, worker)
    assert text.splitlines()[-1] == "reply from the operator: red"
    assert (woken.state, woken.wakes, harness.ledger.inbox(woken)) == (ledger_mod.RUNNING, 1, [])
    assert [s.kind for s in harness.ledger.sessions_of(woken)] == ["initial", "wake"]
    assert harness.claude_state.read().get("resumes", []) == []


def test_a_due_timer_goes_through_the_outbox_of_a_session_with_the_mod(daemon, harness):
    worker = launch(harness)
    assert post(daemon, "hello", of(worker))[0] == 200
    post(daemon, "tool/request_wake", of(worker, input={"after_secs": 600}))
    idle = turn_end(daemon, harness, worker)
    harness.claude_state.set_session_state(worker.session_id, "done", status="idle")
    timer = harness.ledger.pending_timer(idle)
    harness.ledger._conn.execute("UPDATE inbox SET due_at = ? WHERE id = ?", (time.time() - 1, timer.id))

    harness.next_tick()

    text, woken = take_and_start(daemon, harness, worker)
    assert text.splitlines()[-1] == "timer: request_wake"
    assert (woken.state, woken.wakes, harness.ledger.inbox(woken)) == (ledger_mod.RUNNING, 1, [])
    assert harness.ledger.wakes_since(woken, time.time() - 60) == 1
    assert harness.claude_state.read().get("resumes", []) == []


def test_a_parked_worker_resumes_through_the_outbox_of_a_session_with_the_mod(daemon, harness):
    worker = launch(harness)
    assert post(daemon, "hello", of(worker))[0] == 200
    # A usage limit ended the turn, and its reset time has passed.
    harness.claude_state.set_session_state(worker.session_id, "failed", status="idle")
    harness.ledger.update(
        harness.ledger.get(worker.id), state=ledger_mod.PARKED, reset_at=time.time() - 1
    )

    harness.next_tick()

    text, woken = take_and_start(daemon, harness, worker)
    assert text == delivery_mod.RESUME_MESSAGE
    assert (woken.state, woken.wakes, woken.reset_at) == (ledger_mod.RUNNING, 1, None)
    assert harness.claude_state.read().get("resumes", []) == []


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

    # The worker's token from another session: the message is its session's alone.
    assert post(daemon, "outbox", of(worker, session_id="someone-else")) == (200, {"message": None})
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
