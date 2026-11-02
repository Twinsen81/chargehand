"""Permission prompts answered on the board: the hook waits in the daemon for the answer.

The daemon serves on 127.0.0.1 on a port the kernel picks and runs no loop. Sessions are
the fake `claude`'s; a held hook is a post on a thread of the test, and the board's answer
a post with the token, as the page sends it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from chargehand import board, claude, hooks, ledger as ledger_mod
from chargehand.claude import ClaudeRuntime
from chargehand.runtime import LaunchSpec
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}
ALLOWED = claude.ClaudeRuntime.permission_decision("allow")
DENIED = claude.ClaudeRuntime.permission_decision("deny")


@pytest.fixture
def daemon(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def launch(harness, identifier="ABC-1", *, timeout=120):
    """A running worker whose session waits *timeout* seconds for its permission hook."""
    harness.board.add(identifier)
    harness.tick()
    worker = harness.worker(identifier)
    assert worker.state == ledger_mod.RUNNING
    settings = {"hooks": claude.hook_settings(1, "t", Path("/t"), permission_timeout_secs=timeout)}
    claude.write_settings_file(claude.settings_file_for(Path(worker.worktree)), settings)
    # How `claude agents` lists a session while its permission hook waits.
    harness.claude_state.set_session_state(identifier, "blocked", waitingFor="permission prompt")
    return worker


def post_json(daemon, path, payload, *, headers=AUTH):
    status, raw = support.request(
        daemon.port, "POST", path, body=json.dumps(payload).encode(), headers=headers
    )
    return status, json.loads(raw) if raw else None


class Held:
    """A permission post on its own thread, as a session's http hook waits for its answer."""

    def __init__(self, daemon, worker, command="date > probe.txt", tool="Bash", description=None):
        self.result: tuple[int, object] | None = None
        tool_input = {"command": command}
        if description is not None:
            tool_input["description"] = description
        payload = {
            "session_id": worker.session_uuid, "cwd": worker.worktree,
            "hook_event_name": "PermissionRequest", "tool_name": tool,
            "tool_input": tool_input,
        }
        self._thread = threading.Thread(
            target=lambda: setattr(self, "result", post_json(daemon, "/hooks/PermissionRequest", payload)),
            daemon=True,
        )
        self._thread.start()

    def answered(self, timeout: float = 5.0) -> tuple[int, object] | None:
        self._thread.join(timeout)
        return self.result


def held_prompt(daemon, harness, worker) -> ledger_mod.PermissionPrompt:
    """The worker's open prompt, once its hook waits for the board."""
    def find():
        prompts = harness.ledger.open_permission_prompts(harness.ledger.get(worker.id))
        return next((p for p in prompts if hooks.is_held(daemon, p.id)), None)

    prompt = support.wait_for(find)
    assert prompt is not None, "the hook was not held"
    return prompt


def answer(daemon, worker, prompt_id, choice, **kwargs):
    return post_json(daemon, f"/api/workers/{worker.id}/answer",
                     {"prompt": prompt_id, "answer": choice}, **kwargs)


def card_prompt(daemon, item="ABC-1"):
    status, raw = support.request(daemon.port, "GET", "/api/board")
    assert status == 200
    for column in json.loads(raw)["columns"]:
        for card in column["cards"]:
            if card["item"] == item:
                return card["permission_prompt"]
    raise AssertionError(f"no card for {item}")


# ----- the decision -------------------------------------------------------------------


def test_the_hook_waits_until_the_board_allows(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    assert hook.answered(timeout=0.3) is None
    shown = card_prompt(daemon)
    assert (shown["id"], shown["tool_name"], shown["answers"]) == (
        prompt.id, "Bash", ["allow", "deny", "always"],
    )
    assert json.loads(shown["tool_input"]) == {"command": "date > probe.txt"}
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED

    assert answer(daemon, worker, prompt.id, "allow") == (
        200, {"prompt": prompt.id, "answer": "allow", "ok": True, "message": "allowed"},
    )

    assert hook.answered() == (200, ALLOWED)
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.state, row.decision, row.resolved_by) == ("resolved", "allow", "board")
    assert card_prompt(daemon) is None
    assert harness.ledger.permission_rules(worker) == []


def test_the_board_denies(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    assert answer(daemon, worker, prompt.id, "deny")[0] == 200

    assert hook.answered() == (200, DENIED)
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.state, row.decision, row.resolved_by) == ("resolved", "deny", "board")


def test_the_decision_is_claude_codes_permission_request_output():
    assert ALLOWED == {
        "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}
    }
    verdict = DENIED["hookSpecificOutput"]["decision"]
    assert verdict["behavior"] == "deny"
    assert verdict["message"] == claude.DENIED_MESSAGE
    with pytest.raises(ValueError):
        ClaudeRuntime.permission_decision("maybe")


def test_always_allow_answers_the_same_request_by_itself_next_time(daemon, harness):
    worker = launch(harness)
    first = Held(daemon, worker, description="Write the date")
    prompt = held_prompt(daemon, harness, worker)
    assert answer(daemon, worker, prompt.id, "always")[0] == 200
    assert first.answered() == (200, ALLOWED)
    [rule] = harness.ledger.permission_rules(worker)
    assert (rule["tool_name"], rule["prompt_id"]) == ("Bash", prompt.id)
    sent = len(harness.notifications())

    # The same tool with the same input: allowed at once, no card, no notification. The
    # model words the description of the call anew each time.
    again = Held(daemon, worker, description="Write the date again")
    assert again.answered(timeout=3) == (200, ALLOWED)
    assert harness.ledger.open_permission_prompts() == []
    assert card_prompt(daemon) is None
    assert len(harness.notifications()) == sent
    newest = harness.ledger.get_permission_prompt(prompt.id + 1)
    assert (newest.state, newest.decision, newest.resolved_by) == ("resolved", "allow", "rule")

    # Another input of the same tool still asks.
    other = Held(daemon, worker, command="rm -rf build")
    other_prompt = held_prompt(daemon, harness, worker)
    assert json.loads(other_prompt.tool_input) == {"command": "rm -rf build"}
    assert answer(daemon, worker, other_prompt.id, "deny")[0] == 200
    assert other.answered() == (200, DENIED)


def test_a_rule_belongs_to_one_worker(daemon, harness):
    worker = launch(harness, "ABC-1")
    other = launch(harness, "ABC-2")
    hook = Held(daemon, worker)
    assert answer(daemon, worker, held_prompt(daemon, harness, worker).id, "always")[0] == 200
    assert hook.answered() == (200, ALLOWED)

    elsewhere = Held(daemon, other)
    prompt = held_prompt(daemon, harness, other)

    assert elsewhere.answered(timeout=0.3) is None
    assert answer(daemon, other, prompt.id, "allow")[0] == 200
    assert elsewhere.answered() == (200, ALLOWED)


def test_a_rule_never_matches_an_input_that_differs_after_the_display_cap():
    head = "x" * claude.TOOL_INPUT_LIMIT
    one = claude.parse_hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": head + "a"}})
    two = claude.parse_hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": head + "b"}})
    three = claude.parse_hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "x\x1b"}})
    four = claude.parse_hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "x"}})

    assert one.tool_input == two.tool_input
    assert one.tool_input_digest != two.tool_input_digest
    assert three.tool_input == four.tool_input
    assert three.tool_input_digest != four.tool_input_digest


def test_the_description_of_a_bash_call_is_not_part_of_what_a_rule_matches():
    def digest(tool, **tool_input):
        return claude.parse_hook("PermissionRequest", {"tool_name": tool, "tool_input": tool_input}).tool_input_digest

    # The model writes a new description for each call of the same command.
    assert digest("Bash", command="date", description="a") == digest("Bash", command="date", description="b")
    assert digest("Bash", command="date", description="a") != digest("Bash", command="date -u", description="a")
    assert digest("Bash", command="date", timeout=1) != digest("Bash", command="date", timeout=2)
    assert digest("Other", command="date", description="a") != digest("Other", command="date", description="b")


# ----- first writer wins ----------------------------------------------------------------


def test_a_second_click_changes_nothing(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)
    assert answer(daemon, worker, prompt.id, "deny")[0] == 200
    assert hook.answered() == (200, DENIED)

    status, payload = answer(daemon, worker, prompt.id, "always")

    assert (status, payload["error"]) == (409, hooks.ALREADY_RESOLVED)
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.decision, row.resolved_by) == ("deny", "board")
    assert harness.ledger.permission_rules(worker) == []


def test_an_answer_in_the_session_cancels_the_card_and_releases_the_hook(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    # The operator answered in `claude attach`; the turn goes on and ends.
    post_json(daemon, "/hooks/Stop", {"session_id": worker.session_uuid})

    assert hook.answered() == (200, {})
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.state, row.resolved_by, row.decision) == ("cancelled", "session", None)
    assert card_prompt(daemon) is None
    status, payload = answer(daemon, worker, prompt.id, "allow")
    assert (status, payload["error"]) == (409, hooks.ALREADY_RESOLVED)


def test_a_click_after_an_answer_in_the_session_is_refused_and_cancels_the_card(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)
    # The operator chose No in `claude attach`: the session lists as working at once, while
    # its hook connection stays open and no other hook comes for a while.
    harness.claude_state.set_session_state("ABC-1", "working", waitingFor=None)

    status, payload = answer(daemon, worker, prompt.id, "allow")

    assert (status, payload["error"]) == (409, hooks.ANSWERED_IN_SESSION)
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.state, row.decision, row.resolved_by) == ("cancelled", None, "session")
    assert hook.answered() == (200, {})
    assert card_prompt(daemon) is None
    assert harness.ledger.permission_rules(worker) == []


@pytest.mark.parametrize("listed", [
    {"session_state": "blocked", "waitingFor": "question"},
    {"session_state": "idle", "waitingFor": None},
])
def test_a_click_is_refused_unless_the_session_shows_a_permission_dialog(daemon, harness, listed):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)
    harness.claude_state.set_session_state("ABC-1", listed["session_state"], waitingFor=listed["waitingFor"])

    assert answer(daemon, worker, prompt.id, "deny")[0] == 409

    assert harness.ledger.get_permission_prompt(prompt.id).state == "cancelled"
    assert hook.answered() == (200, {})


def test_a_click_is_refused_while_the_session_cannot_be_read(daemon, harness, monkeypatch):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)
    real = ClaudeRuntime.permission_waits
    unreadable = [True]
    monkeypatch.setattr(
        ClaudeRuntime, "permission_waits",
        lambda self: None if unreadable[0] else real(self),
    )

    status, payload = answer(daemon, worker, prompt.id, "allow")

    assert (status, payload["error"]) == (409, hooks.NOT_CONFIRMED)
    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"
    assert hook.answered(timeout=0.3) is None
    unreadable[0] = False
    assert answer(daemon, worker, prompt.id, "allow")[0] == 200
    assert hook.answered() == (200, ALLOWED)


def test_an_answer_in_the_session_clears_the_card_without_a_click(daemon, harness, monkeypatch):
    monkeypatch.setattr(hooks, "SESSION_CHECK_SECS", 0.2)
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)
    # The held hook sees the session show the dialog first.
    assert hook.answered(timeout=0.6) is None

    # The operator answers in `claude attach` in a long turn: no hook comes, no tick runs.
    harness.claude_state.set_session_state("ABC-1", "working", waitingFor=None)

    assert hook.answered(timeout=3) == (200, {})
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.state, row.resolved_by, row.decision) == ("cancelled", "session", None)
    assert card_prompt(daemon) is None


def test_a_listing_that_never_showed_the_dialog_cancels_nothing(daemon, harness, monkeypatch):
    monkeypatch.setattr(hooks, "SESSION_CHECK_SECS", 0.2)
    worker = launch(harness)
    # The listing lags behind the hook: it still shows the turn running.
    harness.claude_state.set_session_state("ABC-1", "working", waitingFor=None)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    assert hook.answered(timeout=1.0) is None
    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"
    harness.claude_state.set_session_state("ABC-1", "blocked", waitingFor="permission prompt")
    assert answer(daemon, worker, prompt.id, "deny")[0] == 200
    assert hook.answered() == (200, DENIED)


def test_held_hooks_share_one_listing_per_interval(daemon, harness, monkeypatch):
    interval = 0.3
    monkeypatch.setattr(hooks, "SESSION_CHECK_SECS", interval)
    calls = []
    real = ClaudeRuntime.permission_waits

    def counted(self):
        calls.append(time.monotonic())
        return real(self)

    workers = [launch(harness, item) for item in ("ABC-1", "ABC-2", "ABC-3")]
    held = [Held(daemon, worker) for worker in workers]
    prompts = [held_prompt(daemon, harness, worker) for worker in workers]
    monkeypatch.setattr(ClaudeRuntime, "permission_waits", counted)
    began = time.monotonic()
    time.sleep(2.0)
    looked = len(calls)
    elapsed = time.monotonic() - began

    # About one listing per interval for all three, not one per hook.
    assert 2 <= looked <= elapsed / interval + 2
    for worker, hook, prompt in zip(workers, held, prompts):
        assert answer(daemon, worker, prompt.id, "allow")[0] == 200
        assert hook.answered() == (200, ALLOWED)


def test_a_click_after_the_hook_let_go_says_to_answer_in_the_session(daemon, harness, monkeypatch):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)
    real = ledger_mod.Ledger.answer_permission_prompt

    def timeout_first(self, prompt_id, *args, **kwargs):
        # The hold reached its timeout between the checks of the click and its write.
        self.release_permission_prompt(prompt_id)
        return real(self, prompt_id, *args, **kwargs)

    monkeypatch.setattr(ledger_mod.Ledger, "answer_permission_prompt", timeout_first)

    status, payload = answer(daemon, worker, prompt.id, "allow")

    assert (status, payload["error"]) == (409, hooks.NOT_HELD)
    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"
    assert hook.answered() == (200, {})


def test_a_prompt_that_comes_during_the_launch_survives_the_end_of_the_launch(daemon, harness):
    worker = launch(harness)
    harness.ledger.update(harness.ledger.get(worker.id), state=ledger_mod.LAUNCHING)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    # The tick confirms the launch after the session showed its first dialog.
    harness.ledger.update(harness.ledger.get(worker.id), state=ledger_mod.RUNNING)

    assert hook.answered(timeout=0.6) is None
    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"
    assert answer(daemon, worker, prompt.id, "allow")[0] == 200
    assert hook.answered() == (200, ALLOWED)


def test_the_listing_diff_also_cancels_a_held_prompt(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    harness.claude_state.set_session_state("ABC-1", "working")
    harness.next_tick()

    assert hook.answered() == (200, {})
    assert harness.ledger.get_permission_prompt(prompt.id).state == "cancelled"


# ----- when the hook stops waiting ------------------------------------------------------


def test_on_a_timeout_the_hook_returns_no_decision_and_the_card_points_to_the_session(
    daemon, harness, monkeypatch
):
    monkeypatch.setattr(ClaudeRuntime, "permission_hold_secs", classmethod(lambda cls, worktree: 0.5))
    worker = launch(harness)

    hook = Held(daemon, worker)

    assert hook.answered() == (200, {})
    [prompt] = harness.ledger.open_permission_prompts()
    assert prompt.released_at is not None
    assert card_prompt(daemon)["answers"] == []
    status, payload = answer(daemon, worker, prompt.id, "allow")
    assert (status, payload["error"]) == (409, hooks.NOT_HELD)
    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"


def test_a_daemon_stop_releases_every_held_hook(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    worker = launch(harness)
    with support.running(built, loop=False):
        hook = Held(built, worker)
        prompt = held_prompt(built, harness, worker)

        built.stop()

        assert hook.answered() == (200, {})
    row = harness.ledger.get_permission_prompt(prompt.id)
    assert (row.state, row.released_at is not None) == ("open", True)
    assert not hooks.is_held(built, prompt.id)


def test_a_slow_listing_does_not_keep_a_held_hook_from_a_stop(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "FIRST_SESSION_CHECK_SECS", 0.05)
    monkeypatch.setattr(hooks, "SESSION_CHECK_SECS", 0.1)
    listing = threading.Event()

    def slow(self):
        listing.set()
        time.sleep(5)
        return None

    monkeypatch.setattr(ClaudeRuntime, "permission_waits", slow)
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    worker = launch(harness)
    with support.running(built, loop=False):
        hook = Held(built, worker)
        held_prompt(built, harness, worker)
        assert listing.wait(3)

        built.stop()

        assert hook.answered(timeout=1.5) == (200, {})


def test_a_session_with_the_old_short_timeout_is_not_held(daemon, harness):
    worker = launch(harness, timeout=claude.HOOK_TIMEOUT_SECS)

    assert Held(daemon, worker).answered(timeout=3) == (200, {})
    [prompt] = harness.ledger.open_permission_prompts()
    assert prompt.released_at is not None


def test_the_hold_comes_from_the_settings_file_of_the_launch(tmp_path):
    worktree = tmp_path / "w"
    assert ClaudeRuntime.permission_hold_secs(worktree) is None
    path = claude.settings_file_for(worktree)
    claude.write_settings_file(path, {"hooks": claude.hook_settings(1, "t", Path("/t"), permission_timeout_secs=30)})
    assert ClaudeRuntime.permission_hold_secs(worktree) == 30 - claude.PERMISSION_ANSWER_MARGIN_SECS
    path.write_text("not json")
    assert ClaudeRuntime.permission_hold_secs(worktree) is None


# ----- the endpoint ---------------------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},
    {"Authorization": "Bearer not-the-token", "Content-Type": "application/json"},
])
def test_an_answer_needs_the_token(daemon, harness, headers):
    worker = launch(harness)
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    assert answer(daemon, worker, prompt.id, "allow", headers=headers)[0] == 401

    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"
    assert answer(daemon, worker, prompt.id, "deny")[0] == 200
    assert hook.answered() == (200, DENIED)


def test_an_answer_names_a_prompt_of_the_worker_and_a_known_answer(daemon, harness):
    worker = launch(harness, "ABC-1")
    other = launch(harness, "ABC-2")
    hook = Held(daemon, worker)
    prompt = held_prompt(daemon, harness, worker)

    assert answer(daemon, other, prompt.id, "allow")[0] == 404
    assert answer(daemon, worker, prompt.id, "yes")[0] == 400
    assert answer(daemon, worker, str(prompt.id), "allow")[0] == 400
    assert answer(daemon, worker, prompt.id + 100, "allow")[0] == 404
    assert harness.ledger.get_permission_prompt(prompt.id).state == "open"

    assert answer(daemon, worker, prompt.id, "allow")[0] == 200
    assert hook.answered() == (200, ALLOWED)


def test_the_card_shows_the_tool_input_without_control_sequences(daemon, harness):
    worker = launch(harness)
    hook = Held(daemon, worker, command="echo \x1b[31mred\x1b[0m <b>", tool="Bash\x1b[2J")
    prompt = held_prompt(daemon, harness, worker)

    status, raw = support.request(daemon.port, "GET", "/api/board")
    assert b"\x1b" not in raw and b"\\u001b" not in raw and b"<b>" not in raw
    shown = card_prompt(daemon)
    assert shown["tool_name"] == "Bash"
    assert "red" in shown["tool_input"]

    assert answer(daemon, worker, prompt.id, "deny")[0] == 200
    assert hook.answered() == (200, DENIED)


def test_a_question_record_has_room_for_its_own_options(harness):
    worker = launch(harness)
    prompt_id = harness.ledger.record_permission_prompt(worker, tool_name="ask", tool_input="{}")
    harness.ledger._conn.execute(
        "UPDATE permission_prompts SET kind = ?, options = ? WHERE id = ?",
        (ledger_mod.PROMPT_QUESTION, json.dumps(["main", "release"]), prompt_id),
    )

    prompt = harness.ledger.get_permission_prompt(prompt_id)

    assert (prompt.kind, json.loads(prompt.options), prompt.state) == ("question", ["main", "release"], "open")
    # The board answers no question until the tool that asks one exists.
    assert board.permission_prompt(prompt, lambda prompt_id: True)["answers"] == []


def test_a_role_sets_how_long_its_sessions_wait_for_the_board(tmp_path):
    token_file = tmp_path / "hooks.token"
    token_file.write_text("the-daemon-token\n")
    token_file.chmod(0o600)
    runtime = ClaudeRuntime(claude.ClaudeCLI("claude"), hook_port=4567, token_file=token_file)
    options = ClaudeRuntime.parse_role_options({"permission_timeout_secs": 45}, "role r")

    path = runtime._settings_for(
        LaunchSpec(worktree=tmp_path / "w", name="n", prompt="p", options=options), None
    )

    settings = json.loads(Path(path).read_text())
    assert claude.permission_timeout_of(settings) == 45
    # The other hooks keep their short timeout.
    assert settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] == claude.HOOK_TIMEOUT_SECS
    assert ClaudeRuntime.permission_hold_secs(tmp_path / "w") == 45 - claude.PERMISSION_ANSWER_MARGIN_SECS
    assert ClaudeRuntime.parse_role_options({}, "role r")["permission_timeout_secs"] == (
        claude.PERMISSION_TIMEOUT_SECS
    )
