"""The lease pool: the banksman command, the runner's calls to it, `status`, and `doctor`.

Nothing here needs a real banksman. The fake command records every call, so the tests can
also show what the runner does not do: it never releases a lease itself.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import time

import pytest

from chargehand import install, ledger as ledger_mod, paths
from chargehand.cli import EXIT_OK, main
from chargehand.errors import PoolError
from chargehand.pool import BanksmanPool, NoopPool, select
from chargehand.pool.banksman import SCHEMAS
from conftest import FAKE_BANKSMAN, NO_POOL, free, leased, reaped, write_machine_config

REAP = ["reap", "--json"]


@pytest.fixture
def pool(banksman_state) -> BanksmanPool:
    return BanksmanPool(str(FAKE_BANKSMAN))


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    return harness.attempt(identifier)


def apply_request(harness, kind, target=None, **args):
    request_id = harness.ledger.record_request(kind, target, **args)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


# ----- the command ----------------------------------------------------------


def test_the_fake_speaks_the_schema_this_chargehand_reads():
    from support import fake_banksman

    assert fake_banksman.SCHEMA in SCHEMAS


def test_reap_reports_each_lease_it_ended(pool, banksman_state):
    banksman_state.set(reap=[reaped("emulator-5554"), reaped("pixel-8", "quarantined")])

    assert pool.reap() == ["emulator-5554: released", "pixel-8: quarantined"]
    assert pool.reap() == []
    assert banksman_state.calls == [REAP, REAP]


def test_status_lists_the_leases_and_not_the_free_resources(pool, banksman_state):
    banksman_state.set(resources=[
        leased("emulator-5554", "/tmp/worktrees/ABC-1", issue="ABC-1", abandoned_in=900),
        free("emulator-5556"),
    ])

    (lease,) = pool.status()

    assert lease.resource == "emulator-5554"
    assert lease.kind == "emulator"
    assert lease.state == "ready"
    assert lease.owner == "/tmp/worktrees/ABC-1"
    assert lease.issue == "ABC-1"
    assert lease.expires_in_secs == 900.0
    assert banksman_state.calls == [["status", "--json"]]


def test_a_draining_lease_has_no_expiry(pool, banksman_state):
    banksman_state.set(resources=[
        leased("emulator-5554", "/tmp/worktrees/ABC-1", state="draining", abandoned_in=None),
    ])

    (lease,) = pool.status()

    assert lease.state == "draining"
    assert lease.issue is None
    assert lease.expires_in_secs is None


def test_the_version_is_read_from_its_json(pool, banksman_state):
    banksman_state.set(version="0.2.0")

    assert pool.version() == "0.2.0"
    assert banksman_state.calls == [["version", "--json"]]


@pytest.mark.parametrize("call", ["reap", "status", "version"])
def test_a_failing_command_raises_a_pool_error(pool, banksman_state, call):
    banksman_state.set(fail=True)

    with pytest.raises(PoolError, match="exited 1: fake banksman: forced failure"):
        getattr(pool, call)()


@pytest.mark.parametrize("call", ["reap", "status", "version"])
def test_output_with_an_unknown_schema_is_refused(pool, banksman_state, call):
    banksman_state.set(schema=max(SCHEMAS) + 1, reap=[reaped("emulator-5554")])

    with pytest.raises(PoolError, match=f"schema {max(SCHEMAS) + 1}.*upgrade chargehand"):
        getattr(pool, call)()


def test_an_older_banksman_is_named_as_the_one_to_upgrade(pool, banksman_state):
    banksman_state.set(schema=min(SCHEMAS) - 1)

    with pytest.raises(PoolError, match="upgrade banksman"):
        pool.version()


@pytest.mark.parametrize(
    "output",
    ['{"reaped": []}', '{"schema": "5", "reaped": []}', '{"schema": true, "reaped": []}'],
)
def test_output_without_a_schema_number_is_refused(pool, banksman_state, output):
    banksman_state.set(output=output)

    with pytest.raises(PoolError, match="no schema number"):
        pool.reap()


@pytest.mark.parametrize(
    "output,message",
    [
        ("not json", "did not print JSON"),
        ("[]", "not an object"),
        ('{"schema": 5}', "'reaped' is not a list"),
        ('{"schema": 5, "reaped": [{"resource": "emulator-5554"}]}', "'outcome' is missing"),
    ],
)
def test_output_in_another_shape_is_refused(pool, banksman_state, output, message):
    banksman_state.set(output=output)

    with pytest.raises(PoolError, match=message):
        pool.reap()


def test_a_command_that_is_gone_raises_a_pool_error(tmp_path):
    with pytest.raises(PoolError, match="not found"):
        BanksmanPool(str(tmp_path / "banksman")).reap()


def test_the_pool_is_used_only_when_its_command_resolves(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "banksman").symlink_to(FAKE_BANKSMAN)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    chosen = select("banksman")
    assert isinstance(chosen, BanksmanPool)
    assert chosen.binary == str(bin_dir / "banksman")
    assert chosen.enabled

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert isinstance(select("banksman"), NoopPool)
    assert not select(str(NO_POOL)).enabled


# ----- the runner -----------------------------------------------------------


def test_every_tick_reaps_through_the_pool(harness, pool, banksman_state):
    harness.use_pool(pool)
    banksman_state.set(reap=[reaped("emulator-5554")])

    report = harness.next_tick()

    assert "reaped lease emulator-5554: released" in report.warnings
    assert banksman_state.calls == [REAP]


def test_a_failing_pool_does_not_stop_the_watchdog(harness, pool, banksman_state):
    attempt = launch(harness)
    harness.ledger.update(attempt, working_since=time.time() - 12 * 3600)
    harness.use_pool(pool)
    banksman_state.set(fail=True)

    report = harness.next_tick()

    assert [e for e in report.errors if e.startswith("pool: ")] == report.errors
    assert len(report.errors) == 1
    assert ("ABC-1", "watchdog-stopped") in harness.notifications()
    assert harness.attempt("ABC-1").state == ledger_mod.BLOCKED


def test_a_pool_with_an_unknown_schema_is_reported_and_the_tick_goes_on(
    harness, pool, banksman_state
):
    harness.use_pool(pool)
    banksman_state.set(schema=max(SCHEMAS) + 1)
    harness.board.add("ABC-1")

    report = harness.next_tick()

    assert any("upgrade chargehand" in error for error in report.errors)
    assert report.launched == ["ABC-1"]


def test_cancel_stop_and_discard_leave_the_leases_to_the_pool(harness, pool, banksman_state):
    """A stopped session's agent process has ended, so banksman ends its leases itself."""
    launch(harness)
    harness.use_pool(pool)

    assert apply_request(harness, "stop", "ABC-1")["ok"] == 1
    assert apply_request(harness, "continue", "ABC-1")["ok"] == 1
    assert apply_request(harness, "cancel", "ABC-1")["ok"] == 1
    assert apply_request(harness, "discard", "ABC-1")["ok"] == 1

    assert not (harness.worktree_root / "ABC-1").exists()
    assert banksman_state.calls == [REAP] * 4


def test_the_watchdog_leaves_the_leases_to_the_pool(harness, pool, banksman_state):
    attempt = launch(harness)
    harness.ledger.update(attempt, working_since=time.time() - 12 * 3600)
    harness.use_pool(pool)

    harness.next_tick()

    assert harness.attempt("ABC-1").state == ledger_mod.BLOCKED
    assert banksman_state.calls == [REAP]


def test_collection_leaves_the_leases_to_the_pool(harness, repo, tmp_path, pool, banksman_state):
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (repo / paths.REPO_CONFIG_NAME).write_text(
        f'base = "origin/main"\nprompt = "{{issue}}"\nstatus_file = "{status_dir}/{{issue}}.json"\n'
    )
    launch(harness)
    harness.use_pool(pool)
    (status_dir / "ABC-1.json").write_text(json.dumps({"state": "complete"}))
    harness.claude_state.set_session_state("ABC-1", "done")
    harness.next_tick()
    harness.board.close("ABC-1")

    report = harness.next_tick()

    assert "ABC-1" in report.collected
    assert banksman_state.calls == [REAP, REAP]


# ----- status ---------------------------------------------------------------


@pytest.fixture
def pool_config(tmp_path, repo, harness, banksman_state):
    return write_machine_config(
        tmp_path / "pool-config.toml", repo, harness.worktree_root, pool_bin=str(FAKE_BANKSMAN)
    )


def test_status_json_carries_the_leases(capsys, pool_config, banksman_state):
    banksman_state.set(resources=[
        leased("emulator-5554", "/tmp/worktrees/ABC-1", issue="ABC-1"),
        free("emulator-5556"),
    ])

    assert main(["--config", str(pool_config), "status", "--json", "--no-queue"]) == EXIT_OK

    pool = json.loads(capsys.readouterr().out)["pool"]
    assert pool["enabled"] is True
    assert pool["error"] is None
    assert pool["leases"] == [{
        "resource": "emulator-5554",
        "kind": "emulator",
        "state": "ready",
        "owner": "/tmp/worktrees/ABC-1",
        "issue": "ABC-1",
        "expires_in_secs": 1200.0,
    }]


def test_status_shows_the_leases_in_its_table(capsys, pool_config, banksman_state):
    banksman_state.set(resources=[
        leased("emulator-5554", "/tmp/worktrees/ABC-1", issue="ABC-1"),
        leased("pixel-8", "/tmp/worktrees/ABC-2", abandoned_in=None, state="draining"),
    ])

    main(["--config", str(pool_config), "status", "--no-queue"])

    out = capsys.readouterr().out
    assert "IF ABANDONED" in out
    assert re.search(r"emulator-5554\s+emulator\s+ready\s+20m\s+ABC-1", out)
    # Without an issue the holder is the worktree's directory name.
    assert re.search(r"pixel-8\s+emulator\s+draining\s+-\s+ABC-2", out)


def test_status_strips_control_sequences_from_a_lease(capsys, pool_config, banksman_state):
    banksman_state.set(resources=[leased("emulator-5554", "/tmp/worktrees/\x1b[31mred")])

    main(["--config", str(pool_config), "status", "--no-queue"])

    out = capsys.readouterr().out
    assert "emulator-5554" in out
    assert "\x1b[31m" not in out


def test_status_reports_a_failing_pool_and_still_succeeds(capsys, pool_config, banksman_state):
    banksman_state.set(fail=True)

    assert main(["--config", str(pool_config), "status", "--json", "--no-queue"]) == EXIT_OK
    pool = json.loads(capsys.readouterr().out)["pool"]
    assert pool["leases"] == []
    assert "forced failure" in pool["error"]

    main(["--config", str(pool_config), "status", "--no-queue"])
    assert "! pool: `" in capsys.readouterr().out


def test_status_without_the_pool_says_it_is_off(capsys, config_file):
    main(["--config", str(config_file), "status", "--json", "--no-queue"])

    assert json.loads(capsys.readouterr().out)["pool"] == {
        "enabled": False, "leases": [], "error": None,
    }


# ----- doctor ---------------------------------------------------------------


def _job(tmp_path, *, with_pool: bool) -> dict:
    bin_dir = tmp_path / "job-bin"
    bin_dir.mkdir(exist_ok=True)
    if with_pool and not (bin_dir / "banksman").exists():
        (bin_dir / "banksman").symlink_to(FAKE_BANKSMAN)
    path = f"/usr/bin:/bin:{bin_dir}" if with_pool else "/usr/bin:/bin"
    return {
        "ProgramArguments": [sys.executable, "-m", "chargehand", "tick"],
        "EnvironmentVariables": {"PATH": path},
    }


def _pool_check(harness, job) -> install.Check:
    config = dataclasses.replace(harness.config, pool_bin="banksman")
    (check,) = [c for c in install.job_checks(config, job) if c.name == "pool"]
    return check


def test_doctor_finds_the_pool_on_the_jobs_path(tmp_path, harness, banksman_state):
    banksman_state.set(version="0.3.1")

    check = _pool_check(harness, _job(tmp_path, with_pool=True))

    assert check.status == install.OK
    assert "banksman 0.3.1" in check.detail
    assert str(tmp_path / "job-bin" / "banksman") in check.detail


def test_doctor_reports_a_pool_missing_from_the_jobs_path_as_information(tmp_path, harness):
    check = _pool_check(harness, _job(tmp_path, with_pool=False))

    assert check.status == install.INFO
    assert "the job's PATH" in check.detail
    assert install.worst([check]) == install.OK


def test_doctor_fails_a_pool_with_an_unsupported_schema(tmp_path, harness, banksman_state):
    banksman_state.set(schema=max(SCHEMAS) + 1)

    check = _pool_check(harness, _job(tmp_path, with_pool=True))

    assert check.status == install.FAIL
    assert "upgrade chargehand" in check.detail


def test_doctor_fails_a_pool_command_that_fails(tmp_path, harness, banksman_state):
    banksman_state.set(fail=True)

    assert _pool_check(harness, _job(tmp_path, with_pool=True)).status == install.FAIL


def test_doctor_checks_the_pool_once_without_a_job(capsys, monkeypatch, config_file, tmp_path):
    monkeypatch.setattr(paths, "launch_agent_plist", lambda: tmp_path / "missing.plist")
    monkeypatch.setattr(sys, "platform", "darwin")

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    checks = [c for c in json.loads(capsys.readouterr().out) if c["name"] == "pool"]
    assert [c["status"] for c in checks] == [install.INFO]
    assert "not found on PATH" in checks[0]["detail"]


def test_doctor_prints_information_with_its_own_marker(capsys, config_file):
    main(["--config", str(config_file), "doctor", "--no-tracker"])

    assert re.search(r"\[ info \] pool ", capsys.readouterr().out)

