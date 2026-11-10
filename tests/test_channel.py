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

from chargehand import channel
from chargehand import ledger as ledger_mod
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
    assert "DEMO-2" in answer["result"]
    followed = {(s.source, s.item): s.kinds for s in harness.ledger.subscriptions_of(worker)}
    assert followed[("cmd", "DEMO-2")] == ("comment",)

    # Again with another kind: the first one stays.
    post(daemon, "tool/subscribe", of(worker, input={"item": "DEMO-2", "kinds": ["ci_status"]}))
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


@pytest.mark.parametrize("tool", ["report", "ask", "handoff", "request_wake"])
def test_the_other_tools_answer_that_they_record_nothing_yet(daemon, harness, tool):
    worker = launch(harness)

    status, answer = post(daemon, f"tool/{tool}", of(worker, input={"summary": "x"}))

    assert (status, answer) == (200, {"result": channel.NOT_AVAILABLE, "is_error": False})


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
    assert post(daemon, "outbox", of(worker))[1] == {"message": None}
    assert harness.ledger.outbox_message(worker).state == ledger_mod.OUTBOX_PENDING


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
