"""The chargehand mod: it ships with the package, every launch loads it, and it does only what it says.

The mod's own behaviour is tested with `claude plugin test` in its folder. Here: what
chargehand passes to it, how its posts read, and a static check of what the module hooks
and calls, so that this suite needs no `claude`.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chargehand import claude, ledger as ledger_mod, runtime as runtime_mod
from chargehand.config import ServeConfig

MODULE = claude.MOD_DIR / "hooks" / "register.ts"

# What the module may hook and call: nothing that approves a tool call or writes a prompt
# of its own. A change here is a change of the threat model.
EXPECTED_HOOKS = {"session.start", "turn.complete", "session.end", "tool.call"}
EXPECTED_CALLS = {
    "$.clock.every", "$.env.get", "$.fs.read", "$.http.fetch", "$.prompt.submit",
    "$.session.cwd", "$.session.id", "$.session.usage", "$.tool.register",
}
EXPECTED_ENV = {claude.MOD_PORT_ENV, claude.MOD_TOKEN_FILE_ENV}


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    worker = harness.worker(identifier)
    assert worker.state == ledger_mod.RUNNING
    return worker


def settings_of_last_launch(harness) -> dict:
    argv = harness.claude_state.launches[-1]["argv"]
    value = argv[argv.index("--settings") + 1]
    return json.loads(value) if value.startswith("{") else json.loads(Path(value).read_text())


def with_token(harness, tmp_path, port=4567) -> Path:
    token_file = tmp_path / "hooks.token"
    token_file.write_text("the-daemon-token\n")
    token_file.chmod(0o600)
    harness.config = dataclasses.replace(harness.config, serve=ServeConfig(port=port, token_file=token_file))
    harness.fresh_runner()
    return token_file


# ----- the package and the launch ---------------------------------------------------


def test_the_mod_ships_with_the_package_under_its_plugin_name():
    manifest = json.loads((claude.MOD_DIR / ".claude-plugin" / "plugin.json").read_text())
    hooks = json.loads((claude.MOD_DIR / "hooks" / "hooks.json").read_text())

    assert manifest["name"] == claude.MOD_NAME == "chargehand"
    assert hooks == {"modules": ["./register.ts"]}
    assert MODULE.is_file()
    assert claude.MOD_TOOL_RULE == "mcp__chargehand__*"


def test_the_wheel_takes_the_mod_with_its_dot_directory():
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text()

    # The templates glob takes `.claude-plugin` too. The types that Claude Code writes beside
    # the manifest stay out of git and of the wheel: an artifact is taken even when ignored.
    assert (
        'artifacts = ["src/chargehand/templates/**", '
        '"!src/chargehand/templates/mod/.claude-plugin/types/**"]'
    ) in text
    ignored = (Path(__file__).parents[1] / ".gitignore").read_text()
    assert "src/chargehand/templates/mod/.claude-plugin/types/" in ignored


def test_every_launch_loads_the_mod_and_allows_its_tools(harness, tmp_path):
    token_file = with_token(harness, tmp_path)

    launch(harness)

    argv = harness.claude_state.launches[-1]["argv"]
    assert argv[argv.index("--plugin-dir") + 1] == str(claude.MOD_DIR)
    settings = settings_of_last_launch(harness)
    assert claude.MOD_TOOL_RULE in settings["permissions"]["allow"]
    # The mod finds the daemon where the hooks post; the file names the token file only.
    assert settings["env"] == {
        claude.MOD_PORT_ENV: "4567", claude.MOD_TOKEN_FILE_ENV: str(token_file),
    }
    assert "the-daemon-token" not in json.dumps(settings["env"])


def test_without_a_token_the_mod_gets_no_address(harness, tmp_path):
    harness.config = dataclasses.replace(
        harness.config, serve=ServeConfig(port=4567, token_file=tmp_path / "missing.token")
    )
    harness.fresh_runner()

    launch(harness)

    settings = settings_of_last_launch(harness)
    assert "env" not in settings and "hooks" not in settings
    assert claude.MOD_TOOL_RULE in settings["permissions"]["allow"]


def test_the_role_allow_rules_come_first_and_stay(tmp_path):
    settings = claude.launch_settings(
        {"allow": ("Bash(gh pr *)",)}, (), mod=True, mod_env=claude.mod_env(7979, tmp_path / "t")
    )

    assert settings["permissions"]["allow"] == ["Bash(gh pr *)", claude.MOD_TOOL_RULE]
    assert settings["env"] == {
        claude.MOD_PORT_ENV: "7979", claude.MOD_TOKEN_FILE_ENV: str(tmp_path / "t"),
    }
    assert claude.launch_settings({}, ()) is None


def test_a_missing_mod_folder_is_not_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(claude, "MOD_DIR", tmp_path / "gone")

    assert claude.ClaudeRuntime().plugin_dirs() == ()


# ----- the lifecycle posts ------------------------------------------------------------


def test_a_session_start_post_reads_as_the_session_start_hook():
    hook = claude.parse_mod_post("session_start", {"session_id": "u1", "cwd": "/w"})

    assert (hook.kind, hook.observed, hook.session_uuid, hook.cwd) == (
        runtime_mod.HOOK_SESSION_START, runtime_mod.WORKING, "u1", "/w",
    )
    assert hook.event == "mod:session_start"


def test_a_turn_that_answered_reads_as_the_stop_hook_with_a_clean_answer():
    hook = claude.parse_mod_post(
        "turn_complete", {"session_id": "u1", "cwd": "/w", "reason": "answer", "answer": "done\x1b[2J" + "x" * 9000},
    )

    assert (hook.kind, hook.observed) == (runtime_mod.HOOK_TURN_END, runtime_mod.DONE)
    assert "\x1b" not in hook.last_answer and len(hook.last_answer) <= claude.LAST_ANSWER_LIMIT


@pytest.mark.parametrize("reason", ["error", "refusal", "aborted", None])
def test_any_other_turn_end_and_the_session_end_change_no_state(reason):
    # The settings hooks report a failure with what the mod does not see.
    turn = claude.parse_mod_post("turn_complete", {"session_id": "u1", "reason": reason})
    end = claude.parse_mod_post("session_end", {"session_id": "u1", "reason": "other"})

    assert turn.observed is None and end.observed is None


def test_an_unknown_event_or_payload_is_not_the_mods():
    assert claude.parse_mod_post("tool_call", {}) is None
    assert claude.parse_mod_post("session_start", ["not", "a", "mapping"]) is None
    assert claude.ClaudeRuntime.channel_events == claude.MOD_EVENTS
    assert runtime_mod.Runtime.parse_channel("session_start", {}) is None


# ----- what the module hooks and calls ------------------------------------------------


def _module_source() -> str:
    # Comments name calls in prose; only code counts.
    text = MODULE.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] if not line.lstrip().startswith("//") else ""
                     for line in text.splitlines())


def test_the_module_hooks_and_calls_only_what_it_is_allowed_to():
    source = _module_source()

    hooks = set(re.findall(r"\bon\(\s*'([a-z.]+)'", source))
    calls = set(re.findall(r"\$\.[a-z]+\.[a-zA-Z]+", source))
    env = set(re.findall(r"\$\.env\.get\(\s*'([A-Z_]+)'", source))

    assert hooks == EXPECTED_HOOKS
    assert calls == EXPECTED_CALLS
    assert env == EXPECTED_ENV
    # It never answers a permission question, never writes the environment, never runs a
    # process, and never fills or rewrites a prompt.
    for forbidden in ("tool.check", "permission", "prompt.fill", "env.set", "process.", "fs.write"):
        assert forbidden not in source
    # The only prompt it submits is the outbox message's text.
    assert re.findall(r"\$\.prompt\.submit\(([^)]*)\)", source) == ["{ text, asUser: true }"]
    # The one tool.call hook serves the mod's own tools.
    assert re.findall(r"on\(\s*'tool\.call',\s*\{\s*tool:\s*/([^/]+)/", source) == ["^mcp__chargehand__"]
    # No default address: the port comes from the launch settings or nothing is posted.
    assert "7878" not in source and "localhost" not in source


@pytest.mark.skipif(shutil.which("claude") is None, reason="needs the claude binary")
def test_claude_plugin_validate_lists_only_the_expected_hooks_and_calls():
    result = subprocess.run(
        ["claude", "plugin", "validate", "--json", str(claude.MOD_DIR)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    report = json.loads(result.stdout)
    (module,) = report["contents"]
    notes = module["notes"]

    assert report["success"] is True and module["errors"] == []
    hooked = next(n for n in notes if n.startswith("./register.ts hooks: "))
    assert {re.sub(r"\{.*\}$", "", h.strip()) for h in hooked.split(": ", 1)[1].split(", ")} == EXPECTED_HOOKS
    called = next(n for n in notes if n.startswith("./register.ts calls: "))
    assert {re.sub(r" \(via .*\)$", "", c.strip()) for c in called.split(": ", 1)[1].split(", ")} == EXPECTED_CALLS
    assert next(n for n in notes if n.startswith("./register.ts env writes: ")).endswith("nothing")
    assert [gate["hasCatch"] for gate in module["gatingHooks"]] == [True]
