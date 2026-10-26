"""The runtime interface: the registry, capabilities, runtime-owned role keys, and the claude runtime."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from chargehand import claude as claude_mod
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


# Pids of old processes that a test says are still running. The fake binary's pids are
# above any real pid, so the default liveness check would always say "gone".
ALIVE: set[int] = set()


@pytest.fixture
def claude_runtime(claude_state) -> ClaudeRuntime:
    ALIVE.clear()
    return ClaudeRuntime(
        ClaudeCLI(str(FAKE_CLAUDE)),
        deny_rules=DEFAULT_DENY_RULES,
        stop_wait_secs=0.3,
        stop_poll_secs=0.05,
        pid_alive=ALIVE.__contains__,
    )


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


UUID = "11111111-2222-3333-4444-555555555555"


def session(state="working", **extra):
    """A listing entry in the shape of Claude Code 2.1.310."""
    return {"id": "s1", "sessionId": UUID, "name": "r/ABC-1", "kind": "background",
            "state": state, **extra}


def live(state="done", pid=86470):
    """An entry whose process runs: `pid`, and `status` idle or busy."""
    return session(state, pid=pid, status="busy" if state == "working" else "idle")


def stopped_job():
    """The entry after `claude stop` of an idle session: still `done`, no `pid`, no `status`."""
    return session("done")


def test_the_claude_stop_rule_reads_the_live_fields():
    runtime = ClaudeRuntime(ClaudeCLI(str(FAKE_CLAUDE)))
    parse = claude_mod.parse_session

    # Only the live fields decide; the state is the last turn's.
    for state in ("done", "blocked", "failed", "stopped", "working"):
        assert runtime.is_stopped(parse(session(state))), state
        assert not runtime.is_stopped(parse(live(state))), state
        assert not runtime.is_stopped(parse(session(state, status="idle"))), state
        assert not runtime.is_stopped(parse(session(state, pid=86470))), state
    # A state chargehand does not recognise fails closed, even without the live fields.
    assert not runtime.is_stopped(parse(session("reticulating-splines")))


def test_a_wake_of_an_idle_session_stops_it_and_resumes_by_the_full_session_id(
    claude_runtime, claude_state
):
    # The real shape: after the stop the entry still says done, without pid and status.
    claude_state.set(sessions=[live("done")])

    result = claude_runtime.wake("s1", "new events: 2")

    assert result.ok and result.copy_id is None
    assert claude_state.commands == [["stop", "s1"]]
    resume = claude_state.read()["resumes"][0]
    assert resume["target"] == UUID
    assert resume["prompt"] == "new events: 2"
    assert claude_runtime.list_sessions()[0].state == runtime_mod.WORKING


def test_a_wake_of_a_stopped_job_does_not_stop_it_again(claude_runtime, claude_state):
    claude_state.set(sessions=[stopped_job()])

    assert claude_runtime.wake("s1", "hello").ok
    assert claude_state.commands == []
    assert claude_state.read()["resumes"][0]["target"] == UUID


def test_a_wake_waits_for_the_old_process_to_exit(claude_runtime, claude_state):
    # The listing drops pid about a second before the old process ends.
    claude_state.set(sessions=[live("done", pid=86470)])
    ALIVE.add(86470)
    checks = []

    def alive(pid):
        checks.append(pid)
        if len(checks) >= 3:
            ALIVE.discard(pid)
        return pid in ALIVE

    claude_runtime.pid_alive = alive

    assert claude_runtime.wake("s1", "hello").ok
    assert checks[:3] == [86470, 86470, 86470]
    assert len(claude_state.read()["resumes"]) == 1


def test_a_wake_does_not_resume_while_the_old_process_lives(claude_runtime, claude_state):
    claude_state.set(sessions=[live("done", pid=86470)])
    ALIVE.add(86470)

    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert "process 86470 is still running" in result.detail
    assert "resumes" not in claude_state.read()


def test_a_wake_does_not_resume_an_entry_that_keeps_its_live_fields(claude_runtime, claude_state):
    # A stop that reports success while the listing keeps pid and status: the process runs.
    claude_state.set(sessions=[live("done")], stop_state="done")

    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert "did not stop" in result.detail
    assert claude_state.commands == [["stop", "s1"]]
    assert "resumes" not in claude_state.read()


def test_a_wake_that_cannot_stop_a_working_session_does_not_resume(claude_runtime, claude_state):
    claude_state.set(sessions=[live("working")], stop_state="working")

    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert "did not stop" in result.detail
    assert "resumes" not in claude_state.read()


def test_a_wake_does_not_resume_when_the_stop_fails(claude_runtime, claude_state):
    claude_state.set(sessions=[live("working")], stop_returncode=1)

    result = claude_runtime.wake("s1", "hello")

    assert not result.ok
    assert "exited 1" in result.detail
    assert "resumes" not in claude_state.read()


def test_a_wake_of_a_failed_turn_with_a_live_process_stops_it_first(claude_runtime, claude_state):
    # A turn that ended in an error can leave its process idle and alive.
    claude_state.set(sessions=[live("failed")])

    result = claude_runtime.wake("s1", "hello")

    assert result.ok
    assert claude_state.commands == [["stop", "s1"]]
    assert claude_state.read()["resumes"][0]["target"] == UUID


def test_a_wake_of_a_blocked_session_stops_it_and_resumes(claude_runtime, claude_state):
    claude_state.set(sessions=[live("blocked")])

    result = claude_runtime.wake("s1", "the operator answered: yes")

    assert result.ok
    assert claude_state.commands == [["stop", "s1"]]
    assert claude_state.read()["resumes"][0]["prompt"] == "the operator answered: yes"


def test_a_wake_of_a_failed_session_resumes_without_a_stop(claude_runtime, claude_state):
    claude_state.set(sessions=[session("failed")])

    assert claude_runtime.wake("s1", "hello").ok
    assert claude_state.commands == []


def test_a_wake_that_starts_a_copy_is_a_fault(claude_runtime, claude_state):
    claude_state.set(sessions=[stopped_job()], resume_copy_id="9abc0def")

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


def test_the_claude_health_checks_cover_the_binary_the_listing_and_the_settings_flag(
    claude_runtime, claude_state
):
    checks = {c.name: c for c in claude_runtime.health_checks()}

    assert checks["claude"].status == runtime_mod.CHECK_OK
    assert checks["claude agents"].status == runtime_mod.CHECK_OK
    assert checks["launch deny rules"].status == runtime_mod.CHECK_OK


def test_a_missing_claude_binary_is_one_failed_check(tmp_path):
    runtime = ClaudeRuntime(ClaudeCLI(str(tmp_path / "no-such-claude")))

    (check,) = runtime.health_checks()

    assert (check.name, check.status) == ("claude", runtime_mod.CHECK_FAIL)


def _cancel(harness, item):
    request_id = harness.ledger.record_request("cancel", item)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


def test_the_tick_confirms_a_stop_with_the_runtimes_rule(harness):
    # An idle session after a real `claude stop`: still done, without pid and status.
    harness.board.add("ABC-1")
    harness.tick()
    harness.claude_state.set_session_state("ABC-1", "done")

    result = _cancel(harness, "ABC-1")

    assert result["ok"] == 1
    entry = harness.claude_state.sessions[0]
    assert entry["state"] == "done" and "pid" not in entry and "status" not in entry


def test_the_tick_does_not_take_done_with_a_live_process_as_stopped(harness):
    harness.board.add("ABC-1")
    harness.tick()
    harness.claude_state.set_session_state("ABC-1", "done")
    harness.claude_state.set(stop_state="done")

    result = _cancel(harness, "ABC-1")

    assert result["ok"] == 0
    assert "did not stop" in result["message"]


# ----- the fake runtime -----------------------------------------------------


def test_the_default_health_check_reports_the_listing():
    world = fake_runtime.world()
    world.add_session(name="r/ABC-1", cwd="/tmp/x", state="working")

    assert FakeRuntime().health_checks() == [
        runtime_mod.HealthCheck("fake sessions", runtime_mod.CHECK_OK, "1 session(s) listed")
    ]
    world.set(list_fails=True)
    assert FakeRuntime().health_checks()[0].status == runtime_mod.CHECK_FAIL


def test_the_fake_runtime_wakes_only_a_session_at_rest():
    runtime = FakeRuntime()
    world = fake_runtime.world()
    world.add_session(name="r/ABC-1", cwd="/tmp/x", state="working")

    assert not runtime.wake("f1", "hello").ok
    world.set_session_state("f1", "done")
    assert runtime.wake("f1", "hello").ok
    assert runtime.list_sessions()[0].state == runtime_mod.WORKING


# ----- only the runtime modules know a command line -------------------------


@pytest.mark.parametrize("module", ["tick.py", "cli.py", "roles.py", "install.py"])
def test_the_tick_the_cli_and_the_roles_reach_claude_code_only_through_the_interface(module):
    text = (SRC / module).read_text()

    assert not re.search(r"ClaudeCLI|ClaudeRuntime|claude_mod|chargehand\.claude|from chargehand import claude", text)
    assert '== "claude"' not in text
