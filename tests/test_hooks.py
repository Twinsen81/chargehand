"""The hook receiver: posts from worker sessions move their workers at once.

The daemon here serves on 127.0.0.1 on a port the kernel picks, runs no loop, and is
shut down in the test. Sessions are the fake `claude`'s.
"""

from __future__ import annotations

import dataclasses
import json
import logging
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

from chargehand import claude, hooks, ledger as ledger_mod, worker_tokens
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


def test_a_launch_carries_a_hook_per_event_with_the_port_and_the_sessions_own_token(
    harness, tmp_path
):
    _with_token(harness, tmp_path)

    worker = launch(harness)

    token_file = worker_tokens.token_file_for(worker.worktree_path)
    token = worker_tokens.read(worker.worktree_path)
    assert token and token != "the-daemon-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    hooks_settings = _settings_of_last_launch(harness)["hooks"]
    assert set(hooks_settings) == {
        "SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "Notification",
        "PermissionRequest",
    }
    for event, groups in hooks_settings.items():
        [group] = groups
        [hook] = group["hooks"]
        if event == "PermissionRequest":
            # Held for the board's answer: long, but finite.
            assert hook["timeout"] == claude.PERMISSION_TIMEOUT_SECS
        else:
            assert 0 < hook["timeout"] <= 30
        if event == "SessionStart":
            continue
        assert hook["type"] == "http"
        assert hook["url"] == f"http://127.0.0.1:4567/hooks/{event}"
        assert hook["headers"] == {"Authorization": f"Bearer {token}"}
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
    # The deny rules travel in the same file, with the ones for chargehand's own secrets.
    deny = json.loads(path.read_text())["permissions"]["deny"]
    assert deny[:len(harness.config.deny_rules) + 1] == [
        *harness.config.deny_rules, "AskUserQuestion",
    ]
    assert all(rule.startswith(("Read(/", "Edit(/"))
               for rule in deny[len(harness.config.deny_rules) + 1:])
    # The session gets its own token; the board's stays with the operator.
    assert "the-daemon-token" not in path.read_text()


def _denied(deny, path):
    """Whether `Read` and `Edit` deny rules cover *path*, by its name or a directory's `/**`."""

    def covered(tool):
        for rule in deny:
            if not rule.startswith(f"{tool}(/") or not rule.endswith(")"):
                continue
            target = rule[len(tool) + 2:-1]
            if target == str(path) or (
                target.endswith("/**") and str(path).startswith(target[:-2])
            ):
                return True
        return False

    return covered("Read") and covered("Edit")


def _deny_of(worker):
    return json.loads(claude.settings_file_for(worker.worktree_path).read_text())[
        "permissions"]["deny"]


def test_each_session_is_denied_the_board_token_the_settings_and_every_token(
    harness, tmp_path
):
    board_token = _with_token(harness, tmp_path)
    first = launch(harness, "ABC-1")

    second = launch(harness, "ABC-2")

    deny = _deny_of(second)
    assert _denied(deny, worker_tokens.token_file_for(first.worktree_path))
    # Every settings file holds a session's token, its own included; no tool reads one.
    assert _denied(deny, claude.settings_file_for(first.worktree_path))
    assert _denied(deny, claude.settings_file_for(second.worktree_path))
    # The session needs no board token any more.
    assert _denied(deny, board_token)
    # A Write rule never matches in Claude Code: the Edit rule covers it.
    assert not any(rule.startswith("Write(") and "worker-tokens" in rule for rule in deny)
    assert worker_tokens.read(first.worktree_path) != worker_tokens.read(second.worktree_path)


def test_a_worker_launched_earlier_is_denied_the_token_file_of_a_worker_launched_later(
    harness, tmp_path
):
    _with_token(harness, tmp_path)
    first = launch(harness, "ABC-1")

    second = launch(harness, "ABC-2")

    later = worker_tokens.token_file_for(second.worktree_path)
    assert later.exists()
    assert _denied(_deny_of(first), later)
    # The whole directory, its own file included: the mod reads its own token with its
    # own file read, which a Read deny rule does not bind.
    assert _denied(_deny_of(first), worker_tokens.token_file_for(first.worktree_path))


def test_a_launch_whose_settings_file_cannot_be_written_still_denies_the_board_token(
    harness, tmp_path, monkeypatch
):
    board_token = _with_token(harness, tmp_path)

    def refuse(path, settings):
        raise OSError("read-only")

    monkeypatch.setattr(claude, "write_settings_file", refuse)

    worker = launch(harness)

    deny = _settings_of_last_launch(harness)["permissions"]["deny"]
    assert "hooks" not in _settings_of_last_launch(harness)
    assert _denied(deny, board_token)
    assert _denied(deny, worker_tokens.token_file_for(worker.worktree_path))


def test_a_release_ends_the_sessions_token(harness, tmp_path):
    _with_token(harness, tmp_path)
    worker = launch(harness)
    assert worker_tokens.read(worker.worktree_path)

    claude.ClaudeRuntime().release(worker.worktree_path)

    assert not worker_tokens.token_file_for(worker.worktree_path).exists()
    assert not claude.settings_file_for(worker.worktree_path).exists()


def _as(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_a_hook_with_a_workers_token_acts_as_that_worker(daemon, harness):
    first = launch(harness, "ABC-1")
    second = launch(harness, "ABC-2")
    token = worker_tokens.issue(first.worktree_path)

    # The working directory of the second worker, the first one's token and session.
    status, answer = post(daemon, "Stop", {
        "session_id": first.session_uuid, "cwd": second.worktree,
        "last_assistant_message": "from the first",
    }, headers=_as(token))

    assert (status, answer) == (200, {})
    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    assert harness.worker("ABC-1").last_answer == "from the first"
    assert harness.worker("ABC-2").state == ledger_mod.RUNNING


@pytest.mark.parametrize("session", ["second", "a copy"])
def test_a_hook_from_another_session_with_a_workers_token_changes_nothing(
    daemon, harness, session
):
    first = launch(harness, "ABC-1")
    second = launch(harness, "ABC-2")
    token = worker_tokens.issue(first.worktree_path)
    before = (harness.ledger.get(first.id), harness.ledger.get(second.id))
    # The second worker's session, or a copy that a resume started in the first worktree
    # with the same settings and so the same token.
    session_id = second.session_uuid if session == "second" else "copy-of-the-first"

    for event, extra in (
        ("Stop", {"last_assistant_message": "from elsewhere", "transcript_path": "/t/x.jsonl"}),
        ("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
    ):
        status, answer = post(daemon, event, {
            "session_id": session_id, "cwd": first.worktree, **extra,
        }, headers=_as(token))
        assert (status, answer) == (200, {})

    assert (harness.ledger.get(first.id), harness.ledger.get(second.id)) == before
    assert harness.ledger.open_permission_prompts() == []


def test_a_hook_during_the_launch_is_taken_with_the_workers_token(daemon, harness):
    worker = launch(harness)
    token = worker_tokens.issue(worker.worktree_path)
    harness.ledger.update(worker, session_uuid=None)

    post(daemon, "Stop", {"session_id": "the-new-session"}, headers=_as(token))

    assert harness.worker("ABC-1").state == ledger_mod.IDLE


def test_a_hook_with_an_unknown_token_is_refused(daemon, harness):
    worker = launch(harness)
    before = harness.ledger.get(worker.id)

    status, _ = post(daemon, "Stop", {"session_id": worker.session_uuid}, headers=_as("not-one"))

    assert status == 401
    assert harness.ledger.get(worker.id) == before


def test_the_token_of_a_worker_that_ended_changes_nothing(daemon, harness):
    worker = launch(harness)
    token = worker_tokens.issue(worker.worktree_path)
    ended = harness.ledger.update(worker, state=ledger_mod.CANCELLED)

    status, answer = post(daemon, "Stop", {"session_id": worker.session_uuid}, headers=_as(token))

    assert (status, answer) == (200, {})
    assert harness.ledger.get(worker.id) == ended


def test_a_session_from_before_its_own_token_still_posts_with_the_boards(
    daemon, harness, caplog, monkeypatch
):
    # The fake's session ids repeat across tests, and the daemon says it once per process.
    monkeypatch.setattr(hooks, "_BOARD_TOKEN_SESSIONS", {})
    worker = launch(harness)

    with caplog.at_level(logging.INFO, logger="chargehand.hooks"):
        for _ in range(2):
            status, _ = post(daemon, "Notification", {"session_id": worker.session_uuid})
            assert status == 200
        post(daemon, "Stop", {"session_id": worker.session_uuid})

    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    said = [r.getMessage() for r in caplog.records if "the board's token" in r.getMessage()]
    assert len(said) == 1 and worker.session_uuid[:8] in said[0]


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
    # A turn end that waits for events is on the board, not in a notification.
    assert harness.notifications() == []
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


def test_the_daemon_logs_each_change_a_hook_makes_as_a_tick_line(harness, tmp_path, caplog):
    """The daemon writes a hook's changes into its log in the tick report's own form."""
    from chargehand import cli

    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(
        config, notifier=harness.notifier, interval_secs=3600, report=cli._log_report,
    )
    with support.running(built, loop=False):
        worker = launch(harness)
        caplog.clear()
        caplog.set_level(logging.INFO, logger="chargehand.tick")

        post(built, "Stop", {"session_id": worker.session_uuid})
        # Hooks that change nothing log no report line.
        post(built, "Stop", {"session_id": worker.session_uuid})
        post(built, "Notification", {"session_id": worker.session_uuid, "notification_type": "idle_prompt"})

    lines = [r.getMessage() for r in caplog.records
             if r.name == "chargehand.tick" and not r.getMessage().startswith(("implementer/ABC-1: marked"))]
    assert lines == ["transition: implementer/ABC-1: running -> idle"], lines


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

    report = TickReport()
    runner._diff_and_notify(report)

    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    assert report.transitions == []
    assert harness.notifications() == []


# ----- a background shell after the turn ended ----------------------------------------


# Claude Code lists a session whose ended turn left a background shell running as busy,
# with the state working or, more often, done.
BUSY_LISTINGS = pytest.mark.parametrize("listed_state", ["working", "done"])


def _turn_ended_with_a_background_shell(daemon, harness, tmp_path, listed_state="working"):
    """A worker launched with the turn-start hook, whose turn ended while a background
    shell runs."""
    _with_token(harness, tmp_path)
    worker = launch(harness)
    post(daemon, "Stop", {"session_id": worker.session_uuid})
    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    harness.claude_state.set_session_state(worker.session_id, listed_state, status="busy")
    return worker


@BUSY_LISTINGS
def test_a_busy_listing_after_the_turn_ended_keeps_the_worker_idle_with_a_background_shell(
    daemon, harness, tmp_path, listed_state
):
    _turn_ended_with_a_background_shell(daemon, harness, tmp_path, listed_state)

    for _ in range(3):
        report = harness.next_tick()
        worker = harness.worker("ABC-1")
        assert worker.state == ledger_mod.IDLE
        assert worker.session_state == ledger_mod.SESSION_BACKGROUND
        assert report.transitions == []
    assert harness.notifications() == []
    shown = state_of(daemon, "ABC-1")
    assert (shown["state"], shown["session_state"]) == ("idle", "background")


@BUSY_LISTINGS
def test_a_background_shell_that_ends_leaves_the_worker_idle(
    daemon, harness, tmp_path, listed_state
):
    worker = _turn_ended_with_a_background_shell(daemon, harness, tmp_path, listed_state)
    harness.next_tick()

    harness.claude_state.set_session_state(worker.session_id, "done", status="idle")
    report = harness.next_tick()

    worker = harness.worker("ABC-1")
    assert (worker.state, worker.session_state) == (ledger_mod.IDLE, "done")
    assert report.transitions == []
    assert harness.notifications() == []


def test_a_new_turn_after_the_turn_end_makes_the_worker_running(daemon, harness, tmp_path):
    worker = _turn_ended_with_a_background_shell(daemon, harness, tmp_path)
    harness.next_tick()

    # The shell ended and the session started a turn about it, or the operator typed one.
    answer = post(daemon, "UserPromptSubmit", {"session_id": worker.session_uuid, "prompt": "go on"})

    # Nothing that could add context to the turn or block its prompt.
    assert answer == (200, {})
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING
    harness.next_tick()
    worker = harness.worker("ABC-1")
    assert (worker.state, worker.session_state) == (ledger_mod.RUNNING, "working")
    # The next turn end, with the shell still running, is the same idle state again.
    post(daemon, "Stop", {"session_id": worker.session_uuid})
    harness.next_tick()
    worker = harness.worker("ABC-1")
    assert (worker.state, worker.session_state) == (ledger_mod.IDLE, ledger_mod.SESSION_BACKGROUND)


def test_a_wake_after_the_turn_end_is_a_new_turn(daemon, harness, tmp_path):
    worker = _turn_ended_with_a_background_shell(daemon, harness, tmp_path)
    harness.next_tick()

    harness.ledger.update(harness.worker("ABC-1"), last_wake_at=time.time())
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


@BUSY_LISTINGS
def test_a_background_shell_past_the_limit_notifies_once_and_the_worker_stays_idle(
    daemon, harness, tmp_path, listed_state
):
    _turn_ended_with_a_background_shell(daemon, harness, tmp_path, listed_state)
    harness.config = dataclasses.replace(harness.config, background_shell_mins=0.001)
    harness.next_tick()
    time.sleep(0.1)

    report = harness.next_tick()
    harness.next_tick()

    worker = harness.worker("ABC-1")
    assert (worker.state, worker.session_state) == (ledger_mod.IDLE, ledger_mod.SESSION_BACKGROUND)
    assert harness.notifications() == [("ABC-1", "idle")]
    [line] = report.notified
    assert "a background shell still runs" in line
    assert harness.worker("ABC-1").state == ledger_mod.IDLE


def test_a_limit_of_zero_never_notifies_about_a_background_shell(daemon, harness, tmp_path):
    _turn_ended_with_a_background_shell(daemon, harness, tmp_path)
    harness.config = dataclasses.replace(harness.config, background_shell_mins=0)
    harness.next_tick()
    time.sleep(0.05)

    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.IDLE
    assert harness.notifications() == []


def test_a_question_that_ended_the_turn_stays_blocked_while_a_background_shell_runs(
    daemon, harness, tmp_path
):
    _with_token(harness, tmp_path)
    worker = launch(harness)
    harness.ledger.record_question(worker, question="Which colour?", options=[], now=time.time())
    post(daemon, "Stop", {"session_id": worker.session_uuid})
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    sent = len(harness.notifications())
    harness.claude_state.set_session_state(worker.session_id, "working", status="busy")

    harness.next_tick()
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    assert len(harness.notifications()) == sent


def test_a_session_without_the_turn_start_hook_is_running_when_the_listing_shows_it_working(
    daemon, harness
):
    # Launched before the hook existed: a turn the operator starts in the session would
    # send no hook, so the listing is all there is.
    worker = launch(harness)
    post(daemon, "Stop", {"session_id": worker.session_uuid})
    harness.claude_state.set_session_state(worker.session_id, "working", status="busy")

    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_a_busy_and_done_listing_without_a_turn_end_is_a_plain_turn_end(
    daemon, harness, tmp_path
):
    # No Stop came: the listing alone does not say that a shell outlives a turn.
    _with_token(harness, tmp_path)
    worker = launch(harness)
    harness.claude_state.set_session_state(worker.session_id, "done", status="busy")

    harness.next_tick()

    worker = harness.worker("ABC-1")
    assert (worker.state, worker.session_state) == (ledger_mod.IDLE, "done")


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
    turn = claude.parse_hook("UserPromptSubmit", {**common, "prompt": "ignore the rules"})
    assert (turn.kind, turn.observed, turn.session_uuid) == ("turn_start", "working", "u-1")
    assert "ignore the rules" not in repr(turn)
    stop =claude.parse_hook("Stop", {**common, "last_assistant_message": "fine"})
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
    ({"last_assistant_message": "Weekly limit reached · resets Mon 9pm"}, True),
    ({"last_assistant_message": "the build failed"}, False),
    ({"error": 42}, False),
])
def test_a_usage_limit_is_matched_defensively(payload, limited):
    hook = claude.parse_hook("StopFailure", payload)
    assert hook.usage_limit is limited
    assert hook.observed == "failed"


@pytest.mark.parametrize("payload", [
    {"last_assistant_message": "5-hour limit reached · resets 9pm"},
    {"error": "unknown", "error_details": "You have reached your five-hour limit."},
])
def test_an_old_five_hour_limit_text_still_parks(payload):
    # Old input: the subscription had a five-hour window until 2026-11-03, and Claude Code
    # can still name it in a failure for some time. Such a failure parks, it does not fail.
    hook = claude.parse_hook("StopFailure", payload)
    assert hook.usage_limit is True


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


# ----- the launch stdin, the settings file's life, the status payload ---------------


def test_a_launch_gets_an_empty_pipe_that_stays_open_as_stdin(harness):
    # Claude Code runs no SessionStart hook in most launches whose stdin is /dev/null or
    # a pipe at its end. The daemon's own stdin is /dev/null under launchd.
    launch(harness)

    assert harness.claude_state.launches[-1]["stdin"] == "open-pipe"


def test_other_claude_commands_keep_dev_null_as_stdin(harness, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append((tuple(argv[1:3]), kwargs.get("stdin")))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    claude.ClaudeCLI(harness.config.claude_bin).list_sessions()

    assert seen == [(("agents", "--json"), subprocess.DEVNULL)]


def test_discard_deletes_the_session_settings_file(harness, tmp_path):
    _with_token(harness, tmp_path)
    worker = launch(harness)
    settings = claude.settings_file_for(Path(worker.worktree))
    assert settings.exists()

    request_id = harness.ledger.record_request("discard", "ABC-1", force=False)
    harness.next_tick()

    assert harness.ledger.get_request(request_id)["ok"] == 1
    assert not settings.exists()


def test_garbage_collection_deletes_the_session_settings_file(harness, tmp_path):
    _with_token(harness, tmp_path)
    worker = launch(harness)
    settings = claude.settings_file_for(Path(worker.worktree))
    # Finished, and its session gone: a listed session without a live owner is adopted.
    harness.claude_state.drop_sessions()
    harness.ledger.update(harness.worker("ABC-1"), state=ledger_mod.DONE)

    harness.board.close("ABC-1")
    report = harness.next_tick()

    assert "implementer/ABC-1" in report.collected
    assert not settings.exists()


def test_retry_releases_the_settings_of_the_session_it_removes(harness, tmp_path, monkeypatch):
    # The retry launches again in the same worktree, which writes the file anew, so the
    # release itself is what this looks at.
    _with_token(harness, tmp_path)
    worker = launch(harness)
    harness.ledger.record_request("cancel", "ABC-1")
    harness.next_tick()
    released = []
    real_release = claude.ClaudeRuntime.release

    def spy(self, worktree):
        released.append((Path(worktree), claude.settings_file_for(Path(worktree)).exists()))
        real_release(self, worktree)

    monkeypatch.setattr(claude.ClaudeRuntime, "release", spy)

    request_id = harness.ledger.record_request("retry", "ABC-1")
    harness.next_tick()

    assert harness.ledger.get_request(request_id)["ok"] == 1
    assert released == [(Path(worker.worktree), True)]


def test_a_session_still_listed_after_its_removal_keeps_its_settings_file(harness, tmp_path):
    _with_token(harness, tmp_path)
    worker = launch(harness)
    settings = claude.settings_file_for(Path(worker.worktree))
    harness.claude_state.set(rm_returncode=1)

    assert harness.fresh_runner()._remove_session(harness.worker("ABC-1")) is False

    assert settings.exists()


def test_the_settings_file_stays_while_another_live_worker_has_the_worktree(harness, tmp_path):
    # An adopted session gets a new row in the worktree of the row it replaces.
    _with_token(harness, tmp_path)
    old = launch(harness)
    settings = claude.settings_file_for(Path(old.worktree))
    harness.ledger.update(old, state=ledger_mod.CANCELLED)
    newer = harness.ledger.create_worker(
        role=old.role, item=old.item, item_id=old.item_id, url=old.url,
        repository=old.repository, repo=Path(old.repo), worktree=Path(old.worktree),
        branch=old.branch, origin=ledger_mod.ORIGIN_BOARD,
    )
    harness.ledger.update(newer, state=ledger_mod.RUNNING)
    runner = harness.fresh_runner()

    assert runner._remove_session(harness.ledger.get(old.id)) is True
    assert settings.exists()

    # Once no live worker has the worktree, the file goes with the session.
    harness.ledger.update(harness.ledger.get(newer.id), state=ledger_mod.CANCELLED)
    assert runner._remove_session(harness.ledger.get(old.id)) is True
    assert not settings.exists()


def test_a_cancelled_worker_keeps_the_settings_file_of_its_kept_session(harness, tmp_path):
    # A kept session can still be attached or respawned, which reads the file again.
    _with_token(harness, tmp_path)
    worker = launch(harness)

    harness.ledger.record_request("cancel", "ABC-1")
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.CANCELLED
    assert claude.settings_file_for(Path(worker.worktree)).exists()


def test_the_status_payload_names_the_last_hook_but_not_its_text(daemon, harness):
    from chargehand import cli

    worker = launch(harness)
    post(daemon, "StopFailure", {
        "session_id": worker.session_uuid, "error": "rate_limit",
        "error_details": f"Claude usage limit reached|{int(time.time()) + 3600}",
        "last_assistant_message": "secret session text",
    })

    worker = harness.worker("ABC-1")
    payload = cli._worker_dict(worker, verbose=False)

    assert payload["state"] == ledger_mod.PARKED
    assert payload["last_hook"] == "StopFailure"
    assert payload["last_hook_at"] == worker.last_hook_at is not None
    assert payload["reset_at"] == worker.reset_at is not None
    assert "secret session text" not in json.dumps(payload)
