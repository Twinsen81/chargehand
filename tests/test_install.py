"""The launchd job, `doctor`, and `init`. Nothing here loads or starts anything."""

from __future__ import annotations

import json
import plistlib
import sys

import pytest

from chargehand import install, paths
from chargehand.cli import EXIT_OK, EXIT_REFUSED, main
from chargehand.config import load_machine_config
from chargehand.errors import ChargehandError


def test_the_plist_runs_a_tick_on_a_timer_with_an_explicit_path():
    data = install.build_plist(interval_secs=300)

    assert data["Label"] == paths.LAUNCHD_LABEL
    assert data["ProgramArguments"][-1] == "tick"
    assert data["StartInterval"] == 300
    assert data["RunAtLoad"] is True
    assert data["ProcessType"] == "Background"
    # launchd starts jobs with a minimal PATH, so the job carries its own.
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]


def test_the_plist_can_always_be_run_by_launchd(monkeypatch):
    """launchd has no shell and no PATH of its own, so the argv has to be complete.

    The module form is what a virtual environment that was never activated falls back
    to: neither `chargehand` nor its `python` is on PATH there, so naming the program
    alone would produce `python tick`, which launchd would try to run as a file.
    """
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    argv = install.build_plist()["ProgramArguments"]
    assert argv == [sys.executable, "-m", "chargehand", "tick"]

    monkeypatch.setattr(install.shutil, "which", lambda name: "/opt/bin/chargehand")
    assert install.build_plist()["ProgramArguments"] == ["/opt/bin/chargehand", "tick"]


def test_the_plist_round_trips_through_plistlib(tmp_path):
    target = install.write_plist(tmp_path / "job.plist", interval_secs=120)

    with target.open("rb") as handle:
        data = plistlib.load(handle)

    assert data["StartInterval"] == 120


def test_init_writes_a_repo_config_that_parses(tmp_path, capsys):
    assert main(["init", "--repo", str(tmp_path)]) == EXIT_OK

    from chargehand.config import load_repo_config

    config = load_repo_config(tmp_path)
    assert config.base
    assert config.prompt
    assert config.permission_mode == "auto"


def test_init_refuses_to_overwrite_without_force(tmp_path):
    install.init_repo(tmp_path)

    with pytest.raises(ChargehandError, match="--force"):
        install.init_repo(tmp_path)

    assert install.init_repo(tmp_path, force=True)


def test_init_machine_writes_a_config_and_a_notify_script(tmp_path, capsys):
    target = tmp_path / "machine" / "config.toml"

    assert main(["init", "--machine", "--output", str(target)]) == EXIT_OK

    assert load_machine_config(target).routes
    assert (target.parent / "notify.sh").exists()
    assert (target.parent / "notify.sh").stat().st_mode & 0o111


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
    assert checks["route test"]["status"] == install.OK
    assert checks["claude"]["status"] == install.OK
    assert checks["claude agents"]["status"] == install.OK
    assert checks["notify"]["status"] == install.WARN  # nothing configured


def test_doctor_flags_a_bypass_permissions_route(capsys, config_file, harness, repo):
    (repo / ".chargehand.toml").write_text(
        'base = "origin/main"\nprompt = "{issue}"\npermission_mode = "bypassPermissions"\n'
    )

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["route test"]["status"] == install.WARN
    assert "dedicated machine" in checks["route test"]["detail"]


def test_doctor_warns_when_the_launch_deny_rules_cannot_be_passed(capsys, config_file, harness):
    harness.claude_state.set(version="claude 1.0.0-fake")
    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}

    # The bundled fake advertises --settings, so this is the supported case.
    assert checks["launch deny rules"]["status"] == install.OK


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
