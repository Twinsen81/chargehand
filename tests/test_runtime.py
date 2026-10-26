"""The runtime interface: the registry, capabilities, runtime-owned role keys, and the claude runtime."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from chargehand import runtime as runtime_mod
from chargehand.claude import ClaudeCLI, ClaudeRuntime
from chargehand.config import DEFAULT_DENY_RULES
from chargehand.errors import ConfigError
from chargehand.roles import Role, load_role
from conftest import FAKE_CLAUDE, make_config
from support import fake_runtime
from support.fake_runtime import FakeRuntime

SRC = Path(__file__).resolve().parent.parent / "src" / "chargehand"


# ----- the registry ---------------------------------------------------------


def test_the_registry_finds_runtimes_by_name():
    assert runtime_mod.get("claude") is ClaudeRuntime
    assert runtime_mod.get("fake") is FakeRuntime
    assert {"claude", "fake"} <= set(runtime_mod.names())
    assert runtime_mod.DEFAULT_RUNTIME == "claude"


def test_an_unknown_runtime_is_a_configuration_error():
    with pytest.raises(ConfigError, match="unknown runtime 'codex'; expected one of .*claude"):
        runtime_mod.get("codex")


def test_the_claude_runtime_is_set_up_from_the_machine_configuration(tmp_path):
    config = make_config(tmp_path, tmp_path / "worktrees")

    runtime = runtime_mod.create("claude", config)

    assert isinstance(runtime, ClaudeRuntime)
    assert runtime.cli.binary == str(FAKE_CLAUDE)
    assert runtime.deny_rules == tuple(config.deny_rules)
    assert runtime.trust_worktrees is config.trust_worktrees

    quiet = dataclasses.replace(config, pass_deny_rules_at_launch=False)
    assert runtime_mod.create("claude", quiet).deny_rules == ()


# ----- capabilities ---------------------------------------------------------


def test_claude_declares_every_capability():
    caps = ClaudeRuntime.capabilities

    assert caps.as_dict() == {
        "live_attach": True,
        "in_place_wake": True,
        "board_permissions": True,
        "usage_readings": True,
        "fork": True,
        "external_supervisor": True,
    }


def test_the_fake_runtime_declares_fewer_capabilities():
    caps = FakeRuntime.capabilities

    assert not caps.live_attach
    assert not caps.in_place_wake
    assert not caps.board_permissions
    assert not caps.external_supervisor
    assert caps.fork


# ----- runtime-owned role keys ----------------------------------------------


def test_a_claude_role_keeps_the_claude_keys():
    role = Role.parse(
        "r",
        {"prompt": "{item}", "agent": "reviewer", "permission_mode": "acceptEdits",
         "allow": ["Bash(gh pr *)"]},
    )

    assert role.runtime == "claude"
    assert role.options == {
        "agent": "reviewer", "permission_mode": "acceptEdits", "allow": ("Bash(gh pr *)",)
    }
    assert (role.agent, role.permission_mode, role.allow) == (
        "reviewer", "acceptEdits", ("Bash(gh pr *)",)
    )


def test_a_claude_role_without_its_keys_gets_the_defaults():
    role = Role.parse("r", {"prompt": "{item}"})

    assert role.options == {"agent": None, "permission_mode": "auto", "allow": ()}


def test_the_fake_runtime_rejects_the_claude_keys():
    with pytest.raises(ConfigError, match=r"runtime 'fake'.*unknown key\(s\): agent"):
        Role.parse("r", {"runtime": "fake", "prompt": "{item}", "agent": "reviewer"})


def test_the_claude_runtime_rejects_another_runtimes_keys():
    with pytest.raises(ConfigError, match=r"unknown key\(s\): sandbox"):
        Role.parse("r", {"prompt": "{item}", "sandbox": True})


def test_the_fake_runtime_checks_its_own_keys():
    role = Role.parse("r", {"runtime": "fake", "prompt": "{item}", "sandbox": False})

    assert role.options == {"sandbox": False}
    assert (role.agent, role.permission_mode, role.allow) == (None, "", ())
    with pytest.raises(ConfigError, match="sandbox"):
        Role.parse("r", {"runtime": "fake", "prompt": "{item}", "sandbox": "yes"})


def test_a_role_on_an_unknown_runtime_is_rejected():
    with pytest.raises(ConfigError, match="runtime: unknown runtime 'codex'"):
        Role.parse("r", {"runtime": "codex", "prompt": "{item}"})


def test_only_the_claude_runtime_looks_for_an_agent_file(tmp_path):
    role_dir = tmp_path / ".chargehand" / "roles"
    role_dir.mkdir(parents=True)
    (role_dir / "claude-role.toml").write_text('prompt = "{item}"\nagent = "nobody"\n')
    (role_dir / "fake-role.toml").write_text('runtime = "fake"\nprompt = "{item}"\n')

    with pytest.raises(ConfigError, match="agent 'nobody' not found"):
        load_role(tmp_path, "claude-role")
    assert load_role(tmp_path, "fake-role").runtime == "fake"


# ----- the claude runtime ---------------------------------------------------


@pytest.fixture
def claude_runtime(claude_state) -> ClaudeRuntime:
    return ClaudeRuntime(ClaudeCLI(str(FAKE_CLAUDE)), deny_rules=DEFAULT_DENY_RULES)


def test_a_claude_launch_passes_the_roles_keys_and_the_settings(claude_runtime, claude_state, tmp_path):
    spec = runtime_mod.LaunchSpec(
        worktree=tmp_path,
        name="pr-reviewer/ABC-1",
        prompt="review",
        options={"agent": "reviewer", "permission_mode": "acceptEdits", "allow": ("Bash(gh pr *)",)},
        model="opus",
        effort="high",
    )

    claude_runtime.launch(spec)

    argv = claude_state.launches[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--agent") + 1] == "reviewer"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    assert '"allow": ["Bash(gh pr *)"]' in argv[argv.index("--settings") + 1]
    assert claude_runtime.list_sessions()[0].name == "pr-reviewer/ABC-1"


def session(state="working", **extra):
    return {"id": "s1", "uuid": "11111111-2222-3333-4444-555555555555", "name": "r/ABC-1",
            "state": state, **extra}


def test_a_wake_stops_the_live_process_and_resumes_by_the_full_session_id(claude_runtime, claude_state):
    claude_state.set(sessions=[session("done")])

    result = claude_runtime.wake("s1", "new events: 2")

    assert result.ok and result.copy_id is None
    assert claude_state.commands == [["stop", "s1"]]
    resume = claude_state.read()["resumes"][0]
    assert resume["target"] == "11111111-2222-3333-4444-555555555555"
    assert resume["prompt"] == "new events: 2"
    assert claude_runtime.list_sessions()[0].state == runtime_mod.WORKING


def test_a_wake_of_a_stopped_session_does_not_stop_it_again(claude_runtime, claude_state):
    claude_state.set(sessions=[session("stopped")])

    assert claude_runtime.wake("s1", "hello").ok
    assert claude_state.commands == []


def test_a_wake_that_cannot_stop_the_session_does_not_resume(claude_runtime, claude_state):
    claude_state.set(sessions=[session("working")], stop_state="working")

    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert "did not stop" in result.detail
    assert "resumes" not in claude_state.read()


def test_a_wake_that_starts_a_copy_is_a_fault(claude_runtime, claude_state):
    claude_state.set(sessions=[session("stopped")], resume_copy_id="9abc0def")

    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert result.copy_id == "9abc0def"


def test_a_wake_of_a_session_that_is_gone_is_a_fault(claude_runtime, claude_state):
    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert "not in the listing" in result.detail


def test_a_fork_resumes_a_copy_with_the_new_model_and_effort(claude_runtime, claude_state):
    claude_state.set(sessions=[session("done")])

    result = claude_runtime.fork(
        "s1", "carry on", session_uuid="11111111-2222-3333-4444-555555555555",
        model="sonnet", effort="low",
    )

    assert result.returncode == 0
    argv = claude_state.read()["forks"][0]["argv"]
    assert argv == [
        "--resume", "11111111-2222-3333-4444-555555555555", "--fork-session", "--bg",
        "--model", "sonnet", "--effort", "low", "carry on",
    ]
    assert len(claude_runtime.list_sessions()) == 2


def test_the_attach_command_names_the_session(claude_runtime):
    assert claude_runtime.attach_command("s1") == "claude attach s1"


# ----- the fake runtime -----------------------------------------------------


def test_the_fake_runtime_wakes_only_a_session_at_rest():
    runtime = FakeRuntime()
    world = fake_runtime.world()
    world.add_session(name="r/ABC-1", cwd="/tmp/x", state="working")

    assert not runtime.wake("f1", "hello").ok
    world.set_session_state("f1", "done")
    assert runtime.wake("f1", "hello").ok
    assert runtime.list_sessions()[0].state == runtime_mod.WORKING


# ----- only the runtime modules know a command line -------------------------


@pytest.mark.parametrize("module", ["tick.py", "cli.py", "roles.py"])
def test_the_tick_the_cli_and_the_roles_reach_claude_code_only_through_the_interface(module):
    text = (SRC / module).read_text()

    assert not re.search(r"ClaudeCLI|ClaudeRuntime|claude_mod|chargehand\.claude|from chargehand import claude", text)
    assert '== "claude"' not in text
