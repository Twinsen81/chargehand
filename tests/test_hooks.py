"""The hook receiver: posts from worker sessions move their workers at once.

The daemon here serves on 127.0.0.1 on a port the kernel picks, runs no loop, and is
shut down in the test. Sessions are the fake `claude`'s.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chargehand import claude, hooks, ledger as ledger_mod
from chargehand.config import ServeConfig
from chargehand.runtime import Hook
from chargehand.sources import Event, WorkItem
from chargehand.tick import Runner, TickReport
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    worker = harness.worker(identifier)
    assert worker.state == ledger_mod.RUNNING
    assert worker.session_uuid
    return worker


@pytest.fixture
def daemon(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def post(daemon, event, payload, *, headers=AUTH):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    status, raw = support.request(daemon.port, "POST", f"/hooks/{event}", body=body, headers=headers)
    return status, json.loads(raw) if raw else None


def state_of(daemon, item):
    status, raw = support.request(daemon.port, "GET", "/api/state")
    assert status == 200
    return next(w for w in json.loads(raw)["workers"] if w["item"] == item)


# ----- the launch settings ----------------------------------------------------------


def _settings_of_last_launch(harness) -> dict:
    argv = harness.claude_state.launches[-1]["argv"]
    if "--settings" not in argv:
        return {}
    value = argv[argv.index("--settings") + 1]
    return json.loads(value) if value.startswith("{") else json.loads(Path(value).read_text())


def _with_token(harness, tmp_path, port=4567):
    token_file = tmp_path / "hooks.token"
    token_file.write_text("the-daemon-token\n")
    token_file.chmod(0o600)
    harness.config = dataclasses.replace(
        harness.config, serve=ServeConfig(port=port, token_file=token_file)
    )
    harness.fresh_runner()
    return token_file


def test_a_launch_carries_a_hook_per_event_with_the_port_and_the_token(harness, tmp_path):
    token_file = _with_token(harness, tmp_path)

    launch(harness)

    hooks_settings = _settings_of_last_launch(harness)["hooks"]
    assert set(hooks_settings) == {"SessionStart", "Stop", "StopFailure", "Notification", "PermissionRequest"}
    for event, groups in hooks_settings.items():
        [group] = groups
        [hook] = group["hooks"]
        assert 0 < hook["timeout"] <= 30
        if event == "SessionStart":
            continue
        assert hook["type"] == "http"
        assert hook["url"] == f"http://127.0.0.1:4567/hooks/{event}"
        assert hook["headers"] == {"Authorization": "Bearer the-daemon-token"}
    assert hooks_settings["PermissionRequest"][0]["matcher"] == "*"
    # Claude Code runs only command hooks on SessionStart. The command names the token
    # file, never the token.
    [start] = hooks_settings["SessionStart"][0]["hooks"]
    assert start["type"] == "command"
    command = shlex.split(start["command"])
    assert command[1:3] == ["-I", str(claude.HOOK_POST_SCRIPT)]
    assert command[3:] == ["--port", "4567", "--token-file", str(token_file), "SessionStart"]
    assert "the-daemon-token" not in start["command"]


def test_the_hook_settings_are_in_a_private_file_and_not_on_the_command_line(harness, tmp_path):
    _with_token(harness, tmp_path)

    launch(harness)

    argv = harness.claude_state.launches[-1]["argv"]
    assert not any("the-daemon-token" in arg for arg in argv)
    path = Path(argv[argv.index("--settings") + 1])
    assert path == claude.settings_file_for(Path(harness.worker("ABC-1").worktree))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # The deny rules travel in the same file.
    assert json.loads(path.read_text())["permissions"]["deny"] == list(harness.config.deny_rules)


def _run_session_start_hook(harness, payload, *, timeout=10):
    """Run the SessionStart command of the last launch as Claude Code would: a shell, stdin."""
    [start] = _settings_of_last_launch(harness)["hooks"]["SessionStart"][0]["hooks"]
    began = time.monotonic()
    result = subprocess.run(
        start["command"], shell=True, input=json.dumps(payload).encode(), capture_output=True,
        timeout=timeout, cwd=harness.worker("ABC-1").worktree,
    )
    return result, time.monotonic() - began


def test_the_session_start_command_posts_the_payload_to_the_daemon(harness, tmp_path):
    token_file = tmp_path / "hooks.token"
    token_file.write_text(support.TOKEN + "\n")
    token_file.chmod(0o600)
    config = support.daemon_config(harness.config, token_file)
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        harness.config = dataclasses.replace(
            harness.config, serve=ServeConfig(port=built.port, token_file=token_file)
        )
        harness.fresh_runner()
        worker = launch(harness)
        harness.ledger.update(worker, state=ledger_mod.IDLE)

        result, _ = _run_session_start_hook(
            harness, {"session_id": worker.session_uuid, "hook_event_name": "SessionStart", "source": "resume"}
        )

        assert result.returncode == 0
        # Claude Code gives a SessionStart command's output to the model.
        assert result.stdout == b""
        after = harness.worker("ABC-1")
        assert (after.state, after.last_hook) == (ledger_mod.RUNNING, "SessionStart")


def test_the_session_start_command_exits_at_once_when_no_daemon_answers(harness, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    _with_token(harness, tmp_path, port=free_port)
    worker = launch(harness)

    result, took = _run_session_start_hook(harness, {"session_id": worker.session_uuid})

    assert (result.returncode, result.stdout) == (0, b"")
    assert took < 5


@pytest.mark.parametrize("argv", [[], ["--port", "x", "Stop"], ["--port", "1", "--token-file", "/none", "Stop"]])
def test_the_poster_exits_zero_on_any_input(argv, monkeypatch):
    import io

    from chargehand import hook_post

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert hook_post.main(argv) == 0


def test_a_launch_before_the_daemon_wrote_its_token_carries_no_hooks(harness, tmp_path):
    harness.config = dataclasses.replace(
        harness.config, serve=ServeConfig(port=4567, token_file=tmp_path / "missing.token")
    )
    harness.fresh_runner()

    launch(harness)

    assert "hooks" not in _settings_of_last_launch(harness)


def test_a_token_file_others_can_read_gives_no_hooks(harness, tmp_path):
    token_file = tmp_path / "open.token"
    token_file.write_text("the-daemon-token\n")
    token_file.chmod(0o644)
    harness.config = dataclasses.replace(harness.config, serve=ServeConfig(token_file=token_file))
    harness.fresh_runner()

    launch(harness)

    assert "hooks" not in _settings_of_last_launch(harness)


# ----- mapping a post to a worker ------------------------------------------------------


def test_a_turn_end_moves_the_worker_to_idle_by_session_id(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "Stop", {
        "session_id": worker.session_uuid, "cwd": "/somewhere/else", "hook_event_name": "Stop",
        "last_assistant_message": "all done", "transcript_path": "/t/session.jsonl",
    })

    assert (status, answer) == (200, {})
    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.IDLE
    assert (after.last_hook, after.last_answer, after.transcript_path) == ("Stop", "all done", "/t/session.jsonl")
    assert harness.notifications().count(("ABC-1", "idle")) == 1
    entry = state_of(daemon, "ABC-1")
    assert entry["state"] == "idle"
    assert entry["last_answer"] == "all done"
    assert entry["last_hook"] == "Stop"


def test_a_worker_without_a_session_id_is_found_by_its_worktree(daemon, harness, tmp_path):
    worker = launch(harness)
    harness.ledger.update(worker, session_uuid=None)
    # Another spelling of the same directory, as /tmp and /private/tmp are on macOS.
    alias = tmp_path / "alias"
    alias.symlink_to(Path(worker.worktree).parent)

    post(daemon, "Stop", {"cwd": str(alias / Path(worker.worktree).name)})

    assert harness.worker("ABC-1").state == ledger_mod.IDLE


def test_another_session_in_the_worktree_is_not_the_workers(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "Stop", {"session_id": "someone-else", "cwd": worker.worktree})

    assert (status, answer) == (200, {})
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_a_post_for_no_live_worker_writes_nothing(daemon, harness):
    worker = launch(harness)
    before = harness.ledger.get(worker.id)

    assert post(daemon, "Stop", {"session_id": "unknown", "cwd": "/nowhere"}) == (200, {})
    assert post(daemon, "PermissionRequest", {"session_id": "unknown", "tool_name": "Bash"}) == (200, {})

    assert harness.ledger.get(worker.id) == before
    assert harness.ledger.open_permission_prompts() == []


@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},
    {"Authorization": "Bearer not-the-token", "Content-Type": "application/json"},
])
def test_a_post_without_the_token_changes_nothing(daemon, harness, headers):
    worker = launch(harness)
    before = harness.ledger.get(worker.id)

    for event, payload in (
        ("Stop", {"session_id": worker.session_uuid}),
        ("StopFailure", {"session_id": worker.session_uuid, "error": "rate_limit"}),
        ("PermissionRequest", {"session_id": worker.session_uuid, "tool_name": "Bash"}),
    ):
        status, _ = post(daemon, event, payload, headers=headers)
        assert status == 401

    assert harness.ledger.get(worker.id) == before
    assert harness.ledger.open_permission_prompts() == []


def test_an_unknown_event_is_not_found(daemon, harness):
    worker = launch(harness)

    assert post(daemon, "PreToolUse", {"session_id": worker.session_uuid})[0] == 404
    assert post(daemon, "Stop/again", {"session_id": worker.session_uuid})[0] == 404
    assert post(daemon, "", {"session_id": worker.session_uuid})[0] == 404
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_a_body_that_is_not_a_json_object_is_refused(daemon, harness):
    launch(harness)

    assert post(daemon, "Stop", b"not json")[0] == 400
    assert post(daemon, "Stop", [1, 2])[0] == 400


# ----- the events ------------------------------------------------------------------


def test_a_usage_limit_failure_parks_the_worker_with_its_reset_time(daemon, harness):
    worker = launch(harness)
    resets = time.time() + 3 * 3600

    post(daemon, "StopFailure", {
        "session_id": worker.session_uuid, "error": "rate_limit",
        "error_details": f"Claude usage limit reached|{int(resets)}",
    })

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.PARKED
    assert after.reset_at == pytest.approx(int(resets))
    assert after.last_error == "usage limit: rate_limit"
    assert harness.notifications().count(("ABC-1", "parked")) == 1
    entry = state_of(daemon, "ABC-1")
    assert (entry["state"], entry["reset_at"]) == ("parked", after.reset_at)
    # A parked worker shows as blocked on its tracker.
    assert harness.labels("ABC-1") == ("chargehand-blocked",)


def test_a_usage_limit_without_a_named_category_is_read_from_the_message(daemon, harness):
    worker = launch(harness)

    post(daemon, "StopFailure", {
        "session_id": worker.session_uuid,
        "last_assistant_message": "You've hit your limit · resets 3pm (Europe/Paris)",
    })

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.PARKED
    assert after.reset_at is not None
    reset = datetime.fromtimestamp(after.reset_at, timezone.utc)
    assert reset.minute == 0 and after.reset_at > time.time()


def test_another_failure_blocks_the_worker(daemon, harness):
    worker = launch(harness)

    post(daemon, "StopFailure", {
        "session_id": worker.session_uuid, "error": "model_not_found",
        "last_assistant_message": "There's an issue with the selected model",
    })

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.BLOCKED
    assert after.reset_at is None
    assert after.last_error == "turn failed: model_not_found"
    assert harness.notifications().count(("ABC-1", "blocked")) == 1


def test_a_permission_prompt_is_recorded_and_gets_no_decision(daemon, harness):
    worker = launch(harness)

    status, answer = post(daemon, "PermissionRequest", {
        "session_id": worker.session_uuid, "tool_name": "Bash",
        "tool_input": {"command": "touch /tmp/probe"},
    })
    post(daemon, "Notification", {"session_id": worker.session_uuid, "notification_type": "permission_prompt"})

    assert (status, answer) == (200, {})
    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.BLOCKED
    [prompt] = harness.ledger.open_permission_prompts(after)
    assert (prompt.tool_name, json.loads(prompt.tool_input)) == ("Bash", {"command": "touch /tmp/probe"})
    assert harness.notifications().count(("ABC-1", "blocked")) == 1
    [shown] = state_of(daemon, "ABC-1")["permission_prompts"]
    assert shown["tool_name"] == "Bash"

    # The operator answers in the session, and the turn ends.
    post(daemon, "Stop", {"session_id": worker.session_uuid})

    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    assert harness.ledger.open_permission_prompts() == []
    assert state_of(daemon, "ABC-1")["permission_prompts"] == []


def test_a_stop_resolves_the_open_prompt(daemon, harness):
    worker = launch(harness)
    post(daemon, "PermissionRequest", {"session_id": worker.session_uuid, "tool_name": "Bash"})
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED

    request_id = harness.ledger.record_request("stop", "ABC-1")
    harness.next_tick()

    assert harness.ledger.get_request(request_id)["ok"] == 1
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED
    assert harness.ledger.open_permission_prompts() == []
    assert state_of(daemon, "ABC-1")["permission_prompts"] == []


def test_a_session_start_after_a_prompt_moves_the_worker_back_to_running(daemon, harness):
    worker = launch(harness)
    post(daemon, "Stop", {"session_id": worker.session_uuid})

    post(daemon, "SessionStart", {"session_id": worker.session_uuid, "source": "resume"})

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.RUNNING
    assert after.last_hook == "SessionStart"


def test_an_idle_prompt_notification_changes_no_state(daemon, harness):
    worker = launch(harness)
    post(daemon, "Stop", {"session_id": worker.session_uuid})
    sent = len(harness.notifications())

    post(daemon, "Notification", {"session_id": worker.session_uuid, "notification_type": "idle_prompt"})

    after = harness.worker("ABC-1")
    assert (after.state, after.last_hook) == (ledger_mod.IDLE, "Notification")
    assert len(harness.notifications()) == sent


def test_a_paused_worker_is_not_moved_by_a_hook(daemon, harness):
    worker = launch(harness)
    harness.ledger.update(worker, state=ledger_mod.PAUSED)

    post(daemon, "SessionStart", {"session_id": worker.session_uuid})
    post(daemon, "Stop", {"session_id": worker.session_uuid})

    assert harness.worker("ABC-1").state == ledger_mod.PAUSED


def test_payload_text_is_stored_without_control_sequences(daemon, harness):
    worker = launch(harness)
    text = "ok \x1b[31m<img src=x onerror=alert(1)>\x1b]0;title\x07 ‮done"

    post(daemon, "Stop", {"session_id": worker.session_uuid, "last_assistant_message": text})
    post(daemon, "PermissionRequest", {
        "session_id": worker.session_uuid, "tool_name": "Bash\x1b[2J",
        "tool_input": {"command": "echo \x1b[31mred"},
    })

    stored = harness.worker("ABC-1")
    assert stored.last_answer == "ok <img src=x onerror=alert(1)> done"
    [prompt] = harness.ledger.open_permission_prompts(stored)
    assert "\x1b" not in prompt.tool_name + prompt.tool_input
    status, raw = support.request(daemon.port, "GET", "/api/state")
    assert b"\x1b" not in raw and b"\\u001b" not in raw
    # The markup stays inert data in JSON; the page escapes it when it renders.
    assert state_of(daemon, "ABC-1")["last_answer"] == "ok <img src=x onerror=alert(1)> done"


def test_a_long_answer_is_capped(daemon, harness):
    worker = launch(harness)

    post(daemon, "Stop", {"session_id": worker.session_uuid, "last_assistant_message": "x" * 100_000})

    assert len(harness.worker("ABC-1").last_answer) == claude.LAST_ANSWER_LIMIT


def test_a_hook_that_fails_inside_the_daemon_still_gets_an_empty_answer(daemon, harness, monkeypatch):
    worker = launch(harness)

    def broken(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Runner, "apply_hook", broken)

    assert post(daemon, "Stop", {"session_id": worker.session_uuid}) == (200, {})
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


# ----- the session diff as the fallback --------------------------------------------


def test_the_diff_catches_up_on_a_turn_end_no_hook_reported(harness):
    launch(harness)
    harness.claude_state.set_session_state("ABC-1", "done", status="idle")

    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.IDLE


def test_the_diff_does_not_undo_a_parked_worker(daemon, harness):
    worker = launch(harness)
    post(daemon, "StopFailure", {"session_id": worker.session_uuid, "error": "rate_limit"})
    harness.claude_state.set_session_state("ABC-1", "failed")
    sent = len(harness.notifications())

    harness.next_tick()
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.PARKED
    assert len(harness.notifications()) == sent


def test_a_listing_older_than_a_hook_does_not_undo_it(daemon, harness):
    worker = launch(harness)
    runner = harness.fresh_runner()
    # The tick lists the sessions while the turn still runs ...
    runner.sessions("claude", refresh=True)
    # ... and the turn ends before its supervise phase.
    post(daemon, "Stop", {"session_id": worker.session_uuid})

    runner._diff_and_notify(TickReport())

    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    assert harness.notifications().count(("ABC-1", "idle")) == 1


def test_a_tick_write_between_the_read_and_the_write_of_a_hook_is_not_overwritten(harness):
    worker = launch(harness)
    runner = harness.fresh_runner()
    stale = harness.ledger.get(worker.id)
    # The tick moves the worker after the hook read its row.
    harness.ledger.update(stale, state=ledger_mod.PAUSED)

    assert harness.ledger.update_if_unchanged(stale, state=ledger_mod.IDLE) is None
    result = runner.apply_hook(stale, Hook(kind="turn_end", event="Stop", observed="done"))

    assert result.state == ledger_mod.PAUSED
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED


def test_delivery_never_wakes_a_parked_worker(daemon, harness):
    worker = launch(harness)
    post(daemon, "StopFailure", {"session_id": worker.session_uuid, "error": "rate_limit"})
    harness.ledger.enqueue(worker.id, Event(
        source="fake", kind="comment", item=WorkItem("fake", "ABC-1", "id-ABC-1"),
        actor="someone", occurred_at=time.time(), dedupe_id="c1", pointer="https://example.invalid/c1",
    ))

    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.PARKED
    assert harness.claude_state.read().get("resumes", []) == []
    assert len(harness.ledger.inbox(worker)) == 1


def test_continue_resumes_a_parked_worker(daemon, harness):
    worker = launch(harness)
    post(daemon, "StopFailure", {"session_id": worker.session_uuid, "error": "rate_limit"})
    harness.claude_state.set_session_state("ABC-1", "failed")
    harness.next_tick()
    harness.claude_state.set_session_state("ABC-1", "stopped")

    request_id = harness.ledger.record_request("continue", "ABC-1")
    harness.next_tick()

    assert harness.ledger.get_request(request_id)["ok"] == 1
    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.RUNNING
    assert after.reset_at is None


# ----- reading the payloads ------------------------------------------------------------


def test_the_claude_runtime_reads_each_event():
    common = {"session_id": "u-1", "cwd": "/w", "transcript_path": "/t.jsonl"}

    start = claude.parse_hook("SessionStart", {**common, "source": "startup"})
    assert (start.kind, start.observed, start.source, start.session_uuid) == ("session_start", "working", "startup", "u-1")
    stop = claude.parse_hook("Stop", {**common, "last_assistant_message": "fine"})
    assert (stop.kind, stop.observed, stop.last_answer) == ("turn_end", "done", "fine")
    note = claude.parse_hook("Notification", {**common, "notification_type": "idle_prompt"})
    assert (note.kind, note.observed) == ("notification", None)
    prompt = claude.parse_hook("Notification", {**common, "notification_type": "permission_prompt"})
    assert prompt.observed == "blocked"
    ask = claude.parse_hook("PermissionRequest", {**common, "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert (ask.kind, ask.observed, ask.tool_name, ask.tool_input) == ("permission", "blocked", "Bash", '{"command": "ls"}')
    assert claude.parse_hook("PreToolUse", common) is None
    assert claude.parse_hook("Stop", ["not", "a", "mapping"]) is None


@pytest.mark.parametrize("payload, limited", [
    ({"error": "rate_limit"}, True),
    ({"error": "usage_limit_reached"}, True),
    ({"error": "model_not_found", "last_assistant_message": "usage limit"}, False),
    ({"error": "unknown", "error_details": "Claude AI usage limit reached|1900000000"}, True),
    ({"last_assistant_message": "5-hour limit reached · resets 9pm"}, True),
    ({"last_assistant_message": "the build failed"}, False),
    ({"error": 42}, False),
])
def test_a_usage_limit_is_matched_defensively(payload, limited):
    hook = claude.parse_hook("StopFailure", payload)
    assert hook.usage_limit is limited
    assert hook.observed == "failed"


def test_the_reset_time_is_read_from_several_spellings():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc).timestamp()

    assert claude.reset_time([f"limit reached|{int(now) + 600}"], now) == now + 600
    assert claude.reset_time(["resets at 2026-10-28T15:30:00Z"], now) == now + 3.5 * 3600
    assert claude.reset_time(["resets 3pm (UTC)"], now) == now + 3 * 3600
    # A clock time that has passed today is tomorrow's.
    assert claude.reset_time(["resets 11am (UTC)"], now) == now + 23 * 3600
    assert claude.reset_time(["resets at 14:15 (UTC)"], now) == now + 2.25 * 3600
    for text in ("resets soon", "resets 5", "resets 13pm", f"limit|{int(now) - 7200}", "resets 2027-01-01T00:00:00Z"):
        assert claude.reset_time([text], now) is None, text


def test_only_runtimes_with_session_hooks_receive_posts():
    assert [runtime.name for runtime in hooks.hook_runtimes("Stop")] == ["claude"]
    assert hooks.hook_runtimes("PreToolUse") == []
