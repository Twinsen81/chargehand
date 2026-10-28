"""The launchd job, `doctor`, and `init`. Nothing here loads or starts anything."""

from __future__ import annotations

import argparse
import dataclasses
import json
import plistlib
import subprocess
import sys

import pytest

from chargehand import install, paths
from chargehand.cli import EXIT_OK, EXIT_REFUSED, main
from chargehand.config import load_machine_config
from chargehand.errors import ChargehandError


def test_the_plist_keeps_the_daemon_running_with_an_explicit_path(monkeypatch):
    monkeypatch.setenv("PATH", "/opt/tools/bin:/usr/bin")
    data = install.build_plist()

    assert data["Label"] == paths.LAUNCHD_LABEL
    assert data["ProgramArguments"][-1] == "serve"
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True
    # The daemon ticks on its own timer; a StartInterval would start a second process.
    assert "StartInterval" not in data
    # The daemon finishes the phase in progress after SIGTERM; launchd must wait for it.
    assert data["ExitTimeOut"] >= 60
    # Sessions inherit the job's process type; Background would throttle them.
    assert data["ProcessType"] == "Interactive"
    # launchd starts jobs with a minimal PATH, so the job carries its own, merged with the
    # PATH of the shell that ran `install`.
    job_path = data["EnvironmentVariables"]["PATH"].split(":")
    assert "/opt/homebrew/bin" in job_path and "/opt/tools/bin" in job_path
    assert job_path.count("/usr/bin") == 1


def test_the_plist_can_always_be_run_by_launchd(monkeypatch):
    """launchd has no shell and no PATH of its own, so the argv has to be complete.

    The module form is what a virtual environment that was never activated falls back
    to: neither `chargehand` nor its `python` is on PATH there, so naming the program
    alone would produce `python serve`, which launchd would try to run as a file.
    """
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    argv = install.build_plist()["ProgramArguments"]
    assert argv == [sys.executable, "-m", "chargehand", "serve"]

    monkeypatch.setattr(install.shutil, "which", lambda name: "/opt/bin/chargehand")
    assert install.build_plist()["ProgramArguments"] == ["/opt/bin/chargehand", "serve"]


def test_the_plist_round_trips_through_plistlib(tmp_path):
    target = install.write_plist(tmp_path / "job.plist")

    with target.open("rb") as handle:
        data = plistlib.load(handle)

    assert data["KeepAlive"] is True
    assert data["ProgramArguments"][-1] == "serve"
    assert not install.is_timer_job(data)


def test_a_job_from_before_the_daemon_is_a_timer_job():
    assert install.is_timer_job({"ProgramArguments": ["/bin/chargehand", "tick"], "StartInterval": 180})
    assert install.is_timer_job({"ProgramArguments": ["/bin/chargehand", "tick"]})
    assert not install.is_timer_job({"ProgramArguments": ["/bin/chargehand", "serve"]})


def test_install_writes_the_daemon_job_and_does_not_load_it(monkeypatch, capsys, config_file, tmp_path):
    plist = tmp_path / "LaunchAgents" / "job.plist"
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert main(["--config", str(config_file), "install"]) == EXIT_OK

    with plist.open("rb") as handle:
        data = plistlib.load(handle)
    assert data["ProgramArguments"][-1] == "serve"
    assert data["KeepAlive"] is True and data["ProcessType"] == "Interactive"
    assert "StartInterval" not in data
    assert "Not loaded" in capsys.readouterr().out


def test_install_load_replaces_a_loaded_timer_job(monkeypatch, capsys, config_file, tmp_path):
    plist = tmp_path / "LaunchAgents" / "job.plist"
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(install, "is_loaded", lambda: True)
    monkeypatch.setattr(
        install, "launchctl",
        lambda *args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert main(["--config", str(config_file), "install", "--load"]) == EXIT_OK

    assert [call[0] for call in calls] == ["bootout", "bootstrap"]
    assert calls[1][-1] == str(plist)


def test_uninstall_unloads_the_job_and_removes_its_plist(monkeypatch, capsys, tmp_path):
    plist = tmp_path / "LaunchAgents" / "job.plist"
    install.write_plist(plist)
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    calls = []
    monkeypatch.setattr(install, "is_loaded", lambda: True)
    monkeypatch.setattr(
        install, "launchctl",
        lambda *args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert main(["uninstall", "--remove-plist"]) == EXIT_OK

    assert [call[0] for call in calls] == ["bootout"]
    assert not plist.exists()


def test_init_writes_a_repo_config_that_parses(tmp_path, capsys):
    assert main(["init", "--repo", str(tmp_path)]) == EXIT_OK

    from chargehand.config import load_repo_config

    config = load_repo_config(tmp_path)
    assert config.base
    assert config.prompt is None
    from chargehand.roles import load_role
    from chargehand.sources import AUTHORSHIP_MARKER

    role = load_role(tmp_path, "implementer", repo_config=config, tracker_type="linear")
    # The worker posts as the operator, so it must mark its comments or they wake it.
    assert "{item}" in role.prompt and AUTHORSHIP_MARKER in role.prompt
    assert role.permission_mode == "auto"
    assert role.start_rule_for("labeled", "linear") is not None


def test_init_refuses_to_overwrite_without_force(tmp_path):
    install.init_repo(tmp_path)

    with pytest.raises(ChargehandError, match="--force"):
        install.init_repo(tmp_path)

    assert install.init_repo(tmp_path, force=True)


def test_init_writes_a_starter_role_and_its_agent_file_on_request(tmp_path, capsys):
    from chargehand.config import load_repo_config
    from chargehand.roles import load_role

    config = tmp_path / ".chargehand.toml"
    config.write_text('base = "origin/main"\n')

    assert main(["init", "--repo", str(tmp_path), "--role", "pr-reviewer"]) == EXIT_OK

    # The repository configuration that exists is left alone.
    assert config.read_text() == 'base = "origin/main"\n'
    assert not (tmp_path / ".chargehand" / "roles" / "implementer.toml").exists()
    agent = tmp_path / ".claude" / "agents" / "pr-reviewer.md"
    assert agent.read_text() == (paths.templates_dir() / "agents" / "pr-reviewer.md").read_text()
    # The role finds its agent file.
    role = load_role(tmp_path, "pr-reviewer", repo_config=load_repo_config(tmp_path))
    assert role.agent == "pr-reviewer"
    assert role.start_rule_for("review_requested", "github") is not None

    with pytest.raises(ChargehandError, match="--force"):
        install.init_repo(tmp_path, roles=["pr-reviewer"])
    assert install.init_repo(tmp_path, roles=["pr-reviewer"], force=True) == [
        tmp_path / ".chargehand" / "roles" / "pr-reviewer.toml", agent,
    ]


def test_init_writes_the_repository_configuration_with_a_role_when_it_is_missing(tmp_path):
    written = install.init_repo(tmp_path, roles=["pr-reviewer", "implementer"])

    assert written == [
        tmp_path / ".chargehand.toml",
        tmp_path / ".chargehand" / "roles" / "pr-reviewer.toml",
        tmp_path / ".claude" / "agents" / "pr-reviewer.md",
        tmp_path / ".chargehand" / "roles" / "implementer.toml",
    ]


def test_init_refuses_a_role_it_has_no_template_for(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert main(["init", "--repo", str(repo), "--role", "../x"]) == EXIT_REFUSED
    assert "available: implementer, pr-reviewer" in capsys.readouterr().err
    assert list(repo.iterdir()) == []


def test_init_machine_writes_a_config_and_a_notify_script(tmp_path, capsys):
    target = tmp_path / "machine" / "config.toml"

    assert main(["init", "--machine", "--output", str(target)]) == EXIT_OK

    assert load_machine_config(target).repositories
    assert (target.parent / "notify.sh").exists()
    assert (target.parent / "notify.sh").stat().st_mode & 0o111
    assert "only prints until you enable a method" in capsys.readouterr().out


def test_init_machine_keeps_an_existing_notify_script_and_says_nothing_about_it(
    tmp_path, capsys
):
    target = tmp_path / "config.toml"
    (tmp_path / "notify.sh").write_text("#!/bin/sh\nmy own hook\n")

    assert main(["init", "--machine", "--output", str(target)]) == EXIT_OK

    assert (tmp_path / "notify.sh").read_text() == "#!/bin/sh\nmy own hook\n"
    assert "only prints" not in capsys.readouterr().out


def test_init_machine_refuses_to_overwrite(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("keep me")

    assert main(["init", "--machine", "--output", str(target)]) == EXIT_REFUSED
    assert target.read_text() == "keep me"


def test_doctor_reports_a_missing_config_without_crashing(capsys, tmp_path):
    assert main(["--config", str(tmp_path / "nope.toml"), "doctor", "--json"]) != EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == "fail"


def test_doctor_checks_every_route(capsys, config_file, harness):
    assert main(["--config", str(config_file), "doctor", "--json", "--no-tracker"]) in (0, 1)

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["repository test"]["status"] == install.OK
    assert checks["claude"]["status"] == install.OK
    assert checks["claude agents"]["status"] == install.OK
    assert checks["notify"]["status"] == install.WARN  # nothing configured


def test_doctor_flags_a_bypass_permissions_route(capsys, config_file, harness, repo):
    (repo / ".chargehand.toml").write_text(
        'base = "origin/main"\nprompt = "{issue}"\npermission_mode = "bypassPermissions"\n'
    )

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["repository test role implementer"]["status"] == install.WARN
    assert "dedicated machine" in checks["repository test role implementer"]["detail"]


def test_doctor_checks_every_runtime_a_role_uses(capsys, config_file, harness, repo):
    role = repo / ".chargehand" / "roles" / "implementer.toml"
    role.parent.mkdir(parents=True)
    role.write_text('runtime = "fake"\nprompt = "{item}"\n\n[[start]]\non = "labeled"\n')

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["claude"]["status"] == install.OK
    assert checks["fake sessions"]["status"] == install.OK
    assert checks["repository test role implementer"]["detail"] == "runtime fake"


def test_doctor_warns_when_the_launch_deny_rules_cannot_be_passed(capsys, config_file, harness):
    harness.claude_state.set(version="claude 1.0.0-fake")
    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}

    # The bundled fake advertises --settings, so this is the supported case.
    assert checks["launch deny rules"]["status"] == install.OK


def test_doctor_names_a_setting_that_is_ignored(capsys, tmp_path, repo, harness):
    from conftest import write_machine_config

    config_file = write_machine_config(
        tmp_path / "config.toml", repo, harness.worktree_root, pool_bin="banksman"
    )
    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    notes = [c for c in json.loads(capsys.readouterr().out) if c["name"] == "config"]
    assert any(c["status"] == install.INFO and "pool_bin is ignored" in c["detail"] for c in notes)


def test_worst_picks_the_most_serious_status():
    assert install.worst([install.Check("a", install.OK)]) == install.OK
    assert install.worst([install.Check("a", install.OK), install.Check("b", install.WARN)]) == install.WARN
    assert install.worst([install.Check("b", install.WARN), install.Check("c", install.FAIL)]) == install.FAIL


def test_install_is_refused_off_macos(capsys, config_file, monkeypatch):
    monkeypatch.setattr("chargehand.cli.sys", __import__("sys"))
    import sys as real_sys

    monkeypatch.setattr(real_sys, "platform", "linux")

    assert main(["--config", str(config_file), "install"]) != EXIT_OK
    assert "macOS only" in capsys.readouterr().err


def test_kickstart_does_not_kill_a_running_tick_by_default(monkeypatch):
    """`-k` would abort a tick mid-fetch and burn one of that attempt's launch retries."""
    calls = []
    monkeypatch.setattr(install, "launchctl", lambda *args: calls.append(args) or None)

    install.kickstart()
    install.kickstart(force=True)

    assert "-k" not in calls[0]
    assert "-k" in calls[1]


def test_a_control_command_never_forces_a_kickstart(monkeypatch, config_file, harness):
    forced = []
    # The suite always runs as a second instance, which is now a reason not to kick the
    # job at all. This test is about the `force` flag, so it takes that question away.
    monkeypatch.setattr("chargehand.cli._runs_the_scheduled_job", lambda args: True)
    monkeypatch.setattr(install, "is_loaded", lambda: True)
    monkeypatch.setattr("chargehand.cli.install.is_loaded", lambda: True)
    monkeypatch.setattr(
        "chargehand.cli.install.kickstart",
        lambda **kwargs: forced.append(kwargs) or None,
    )
    harness.board.add("ABC-1")
    main(["--config", str(config_file), "tick"])

    main(["--config", str(config_file), "cancel", "ABC-1", "--wait", "1"])

    assert forced, "the loaded job should have been kicked"
    assert all(not kwargs.get("force") for kwargs in forced)


def test_a_control_command_keeps_asking_while_the_scheduled_job_is_busy(
    monkeypatch, capsys, config_file, harness
):
    """launchd ignores a kickstart while the job runs, and that tick may be past requests.

    A command issued while the previous command's tick was finishing used to ask once, wait
    the full thirty seconds, and then apply the request in an inline tick: outside the job's
    environment and missing from its log.
    """
    monkeypatch.setattr("chargehand.cli._runs_the_scheduled_job", lambda args: True)
    monkeypatch.setattr("chargehand.cli.install.is_loaded", lambda: True)
    monkeypatch.setattr("chargehand.cli.KICKSTART_RETRY_SECS", 0.05)
    monkeypatch.setattr(
        "chargehand.cli.tick_lock",
        lambda **kwargs: pytest.fail("the command fell back to an inline tick"),
    )
    kicks = []

    def kickstart(**kwargs):
        kicks.append(kwargs)
        # The first two arrive while the previous tick is still running.
        if len(kicks) == 3:
            harness.next_tick()

    monkeypatch.setattr("chargehand.cli.install.kickstart", kickstart)

    assert main(["--config", str(config_file), "pause"]) == EXIT_OK

    assert len(kicks) == 3
    assert capsys.readouterr().out == "pause: all repositories paused\n"
    assert harness.ledger.is_paused()


def test_a_control_command_falls_back_when_the_scheduled_job_never_applies_it(
    monkeypatch, capsys, config_file, harness
):
    monkeypatch.setattr("chargehand.cli._runs_the_scheduled_job", lambda args: True)
    monkeypatch.setattr("chargehand.cli.install.is_loaded", lambda: True)
    monkeypatch.setattr("chargehand.cli.KICKSTART_RETRY_SECS", 0.05)
    monkeypatch.setattr("chargehand.cli.SCHEDULED_JOB_WAIT_SECS", 0.3)
    kicks = []
    monkeypatch.setattr("chargehand.cli.install.kickstart", lambda **kwargs: kicks.append(kwargs))

    assert main(["--config", str(config_file), "pause"]) == EXIT_OK

    assert len(kicks) > 1
    assert capsys.readouterr().out == "pause: all repositories paused\n"


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_doctor_checks_the_path_the_job_has_rather_than_its_own(tmp_path, harness):
    """A tool that resolves in a shell but not under launchd fails inside a tick instead."""
    bin_dir = tmp_path / "job-bin"
    program = _executable(bin_dir / "chargehand")
    claude = _executable(bin_dir / "chargehand-test-claude")
    git = _executable(bin_dir / "chargehand-test-git")
    config = dataclasses.replace(
        harness.config, claude_bin="chargehand-test-claude", git_bin="chargehand-test-git"
    )
    job = {
        "ProgramArguments": [str(program), "tick"],
        "EnvironmentVariables": {"PATH": f"/usr/bin:/bin:{bin_dir}"},
    }

    checks = {check.name: check for check in install.job_checks(config, job)}
    assert set(checks) == {"launchd PATH"}
    assert checks["launchd PATH"].status == install.OK
    assert str(claude) in checks["launchd PATH"].detail
    assert str(git) in checks["launchd PATH"].detail

    job["EnvironmentVariables"] = {"PATH": "/usr/bin:/bin"}
    checks = {check.name: check for check in install.job_checks(config, job)}
    assert checks["launchd PATH"].status == install.FAIL
    assert "chargehand-test-claude, chargehand-test-git" in checks["launchd PATH"].detail

    # A job that sets no PATH gets launchd's own, which has no room for either.
    del job["EnvironmentVariables"]
    checks = {check.name: check for check in install.job_checks(config, job)}
    assert checks["launchd PATH"].status == install.FAIL


def test_doctor_fails_a_job_whose_program_is_gone(tmp_path, harness):
    """launchd cannot start it at all, so the job's log never says anything."""
    job = {
        "ProgramArguments": [str(tmp_path / "removed-venv" / "chargehand"), "tick"],
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
    }

    checks = {check.name: check for check in install.job_checks(harness.config, job)}

    assert checks["launchd program"].status == install.FAIL
    assert "removed-venv" in checks["launchd program"].detail


def test_doctor_reads_the_installed_job(monkeypatch, capsys, config_file, harness, tmp_path):
    plist = install.write_plist(tmp_path / "job.plist", job_path=str(tmp_path / "empty"))
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    monkeypatch.setattr(sys, "platform", "darwin")

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["launchd"]["status"] == install.WARN  # written, not loaded
    assert checks["launchd PATH"]["status"] == install.FAIL
    assert "git" in checks["launchd PATH"]["detail"]


def test_doctor_reports_an_unreadable_job_definition(monkeypatch, capsys, config_file, harness, tmp_path):
    plist = tmp_path / "job.plist"
    plist.write_text("<?xml version='1.0'?><plist><dict><key>Label")
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    monkeypatch.setattr(sys, "platform", "darwin")

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    statuses = [c["status"] for c in json.loads(capsys.readouterr().out) if c["name"] == "launchd"]
    assert install.FAIL in statuses


def test_install_warns_when_the_job_cannot_reach_its_tools(monkeypatch, capsys, config_file, tmp_path):
    plist = tmp_path / "LaunchAgents" / "job.plist"
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(install, "DEFAULT_JOB_PATH", str(tmp_path / "also-empty"))

    assert main(["--config", str(config_file), "install"]) == EXIT_OK

    captured = capsys.readouterr()
    assert f"wrote {plist}" in captured.out
    assert "warning: the job's PATH does not reach git" in captured.err


def test_a_second_instance_never_kickstarts_the_scheduled_job(monkeypatch, config_file, harness):
    """The job runs the default configuration, whatever this process was pointed at.

    Kick-starting it from a sandbox would tick somebody else's ledger: launching real
    sessions for real queued issues, while this process waits for a request that tick
    never sees.
    """
    monkeypatch.setattr(install, "is_loaded", lambda: True)
    monkeypatch.setattr("chargehand.cli.install.is_loaded", lambda: True)
    monkeypatch.setattr(
        "chargehand.cli.install.kickstart",
        lambda **kwargs: pytest.fail("a second instance kick-started the scheduled job"),
    )
    harness.board.add("ABC-1")
    main(["--config", str(config_file), "tick"])

    assert main(["--config", str(config_file), "cancel", "ABC-1", "--wait", "1"]) == EXIT_OK


@pytest.mark.parametrize("override", [paths.CONFIG_ENV, paths.STATE_DIR_ENV])
def test_a_path_override_makes_this_a_second_instance(monkeypatch, tmp_path, override):
    from chargehand.cli import _runs_the_scheduled_job

    args = argparse.Namespace(config=None)
    monkeypatch.delenv(paths.CONFIG_ENV, raising=False)
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    assert _runs_the_scheduled_job(args) is True

    monkeypatch.setenv(override, str(tmp_path / "elsewhere"))
    assert _runs_the_scheduled_job(args) is False


def test_pointing_at_another_config_makes_this_a_second_instance(monkeypatch, tmp_path):
    """`--config` reaches the same place the environment variable does."""
    from chargehand.cli import _runs_the_scheduled_job

    monkeypatch.delenv(paths.CONFIG_ENV, raising=False)
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)

    assert _runs_the_scheduled_job(argparse.Namespace(config=None)) is True
    assert _runs_the_scheduled_job(argparse.Namespace(config=str(tmp_path / "c.toml"))) is False


def test_the_suite_is_insulated_from_a_real_launch_agent():
    """With chargehand installed, an unstubbed control command would start the real job."""
    assert install.is_loaded() is False
    # pytest.fail raises from BaseException, which is what makes the guard unmissable.
    with pytest.raises(BaseException, match="tried to run launchctl"):
        install.launchctl("print", "anything")


def test_a_control_command_completes_without_the_scheduled_job(capsys, config_file, harness):
    harness.board.add("ABC-1")
    main(["--config", str(config_file), "tick"])
    capsys.readouterr()

    assert main(["--config", str(config_file), "cancel", "ABC-1"]) == EXIT_OK

    assert "worktree kept" in capsys.readouterr().out


# ----- the daemon -----------------------------------------------------------


def _doctor(capsys, config_file):
    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])
    return {c["name"]: c for c in json.loads(capsys.readouterr().out)}


def test_doctor_reports_the_daemon_and_its_token(capsys, config_file, harness, tmp_path):
    from support import daemon as support

    token = tmp_path / "board.token"
    config = support.daemon_config(harness.config, token)
    daemon = support.build(config, token=None)
    with support.running(daemon, loop=False):
        with config_file.open("a") as handle:
            handle.write(f"\n[serve]\nport = {daemon.port}\ntoken_file = \"{token}\"\n")
        checks = _doctor(capsys, config_file)
        assert checks["daemon"]["status"] == install.OK
        assert f"127.0.0.1:{daemon.port}" in checks["daemon"]["detail"]
        assert checks["daemon token"]["status"] == install.OK

        token.chmod(0o644)
        checks = _doctor(capsys, config_file)
        assert checks["daemon token"]["status"] == install.FAIL
        assert "mode 644" in checks["daemon token"]["detail"]
        token.chmod(0o600)

    checks = _doctor(capsys, config_file)
    assert checks["daemon"]["status"] == install.FAIL
    assert "nothing answers" in checks["daemon"]["detail"]
    assert main(["--config", str(config_file), "doctor", "--no-tracker"]) == 1


def test_doctor_names_a_daemon_that_serves_another_ledger(capsys, config_file, harness, tmp_path):
    from support import daemon as support

    config = support.daemon_config(harness.config, tmp_path / "board.token")
    daemon = support.build(config, ledger_path=tmp_path / "other" / "ledger.sqlite")
    with support.running(daemon, loop=False):
        with config_file.open("a") as handle:
            handle.write(f"\n[serve]\nport = {daemon.port}\n")
        checks = _doctor(capsys, config_file)

    assert checks["daemon"]["status"] == install.FAIL
    assert "another ledger" in checks["daemon"]["detail"]


def test_doctor_warns_about_a_missing_token_file(capsys, config_file, harness):
    checks = _doctor(capsys, config_file)

    assert checks["daemon token"]["status"] == install.WARN
    assert "writes it when it starts" in checks["daemon token"]["detail"]


def test_doctor_warns_about_a_timer_job_from_before_the_daemon(
    monkeypatch, capsys, config_file, harness, tmp_path
):
    plist = tmp_path / "job.plist"
    data = install.build_plist()
    data.pop("KeepAlive")
    data["StartInterval"] = 180
    data["ProgramArguments"] = [*data["ProgramArguments"][:-1], "tick"]
    with plist.open("wb") as handle:
        plistlib.dump(data, handle)
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: plist)
    monkeypatch.setattr(sys, "platform", "darwin")

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    notes = [c for c in json.loads(capsys.readouterr().out) if c["name"] == "launchd"]
    assert any("timer instead of the daemon" in c["detail"] for c in notes)
