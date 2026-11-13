"""The usage policy: the reserves, parking at a usage limit, and the resume after the reset."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timezone

import pytest

from chargehand import board as board_mod
from chargehand import claude
from chargehand import delivery as delivery_mod
from chargehand import ledger as ledger_mod
from chargehand import sanitize
from chargehand import usage as usage_mod
from chargehand.config import MachineConfig, UsageConfig
from chargehand.delivery import (
    QUEUE_REASON_PREFIX,
    RESUME_MESSAGE,
    UsageReading,
    UsageWindow,
    new_worker_admits,
)
from chargehand.errors import ConfigError
from chargehand.tick import MODEL_LIMIT_KEY, TickReport, reset_clock
from conftest import FAKE_CLAUDE
from test_delivery import REVIEWER, build, emit_three, end_turn, resumes, start_reviewer, write_role

MINIMAL = {"route": [{"repo": "~/code/app", "tracker": {"type": "linear", "team": "ABC"}}]}


def reading(read_at: float, **used: float) -> UsageReading:
    return UsageReading(
        "claude", {kind: UsageWindow(kind, percent) for kind, percent in used.items()}, read_at
    )


def write_reading(
    harness, *, age_secs: float = 0.0, resets_in: float = 3600, **used: float
) -> None:
    """Write the usage file the production runner reads, as the status line wrapper does."""
    now = time.time()
    usage_mod.write_usage(
        harness.config.serve.usage_file,
        {
            kind: {"used_percent": value, "resets_at": now + resets_in}
            for kind, value in used.items()
        },
        now=now - age_secs,
    )


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_reason(harness, worker) -> str | None:
    return harness.ledger.get_state(f"{QUEUE_REASON_PREFIX}{worker.id}")


def card(harness, worker) -> dict:
    model = board_mod.build(harness.config, harness.ledger, health={})
    return next(
        entry for column in model["columns"] for entry in column["cards"] if entry["id"] == worker.id
    )


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    worker = harness.worker(identifier)
    assert worker.state == ledger_mod.RUNNING
    return worker


def stop_failure(harness, worker, **payload):
    hook = claude.parse_hook("StopFailure", {"session_id": worker.session_uuid, **payload})
    return harness.runner.apply_hook(harness.ledger.get(worker.id), hook)


def park(harness, worker, reset_at: float | None):
    """A usage limit that the StopFailure hook reports, with *reset_at* in its message."""
    details = f"Claude usage limit reached|{int(reset_at)}" if reset_at else "usage limit reached"
    parked = stop_failure(harness, worker, error="rate_limit", error_details=details)
    assert parked.state == ledger_mod.PARKED
    # The session lists its failed turn until it runs again.
    harness.claude_state.set_session_state(worker.item, "failed")
    return parked


# ----- configuration -----------------------------------------------------------


def test_the_reserves_default_to_twenty_and_thirty_percent():
    config = MachineConfig.parse(MINIMAL)

    assert config.usage == UsageConfig(wake_reserve_percent=20.0, new_worker_reserve_percent=30.0)
    assert config.usage.wake_reserve_percent == delivery_mod.DEFAULT_WAKE_RESERVE_PERCENT
    assert config.usage.new_worker_reserve_percent == delivery_mod.DEFAULT_NEW_WORKER_RESERVE_PERCENT


def test_the_usage_table_sets_both_reserves():
    config = MachineConfig.parse(
        {**MINIMAL, "usage": {"wake_reserve_percent": 10, "new_worker_reserve_percent": 45.5}}
    )

    assert config.usage == UsageConfig(wake_reserve_percent=10.0, new_worker_reserve_percent=45.5)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"wake_reserve_percent": 101}, r"usage.wake_reserve_percent: must be <= 100"),
        ({"new_worker_reserve_percent": -1}, r"usage.new_worker_reserve_percent: must be >= 0"),
        ({"wake_reserve_percent": "20"}, r"usage.wake_reserve_percent: expected a number"),
        ({"wake_reserve_percent": True}, r"usage.wake_reserve_percent: expected a number"),
        ({"reserve": 20}, r"usage: unknown key\(s\): reserve"),
        ("20", r"usage: expected a table"),
    ],
)
def test_a_bad_usage_table_is_an_error(table, message):
    with pytest.raises(ConfigError, match=message):
        MachineConfig.parse({**MINIMAL, "usage": table})


def test_the_bundled_configuration_names_both_reserves():
    from chargehand import paths

    text = (paths.templates_dir() / "config.toml").read_text()

    assert "[usage]" in text
    assert "wake_reserve_percent = 20" in text and "new_worker_reserve_percent = 30" in text


# ----- the thresholds ----------------------------------------------------------


def test_the_wake_reserve_reads_the_weekly_window_only():
    now = 50_000.0

    assert delivery_mod.usage_admits(reading(now, seven_day=80.0), now=now)
    assert not delivery_mod.usage_admits(reading(now, seven_day=80.5), now=now)
    assert delivery_mod.usage_admits(reading(now, seven_day=89.0), reserve_percent=10, now=now)
    # A reading can carry other kinds; no reserve reads them.
    assert delivery_mod.usage_admits(reading(now, other=99.0, seven_day=10.0), now=now)
    assert delivery_mod.usage_admits(reading(now, other=99.0), now=now)


def test_the_new_worker_reserve_reads_the_weekly_window_only():
    now = 50_000.0

    assert new_worker_admits(reading(now, seven_day=70.0), now=now)
    assert not new_worker_admits(reading(now, seven_day=71.0), now=now)
    assert new_worker_admits(reading(now, other=99.0, seven_day=10.0), now=now)
    assert new_worker_admits(reading(now, other=99.0), now=now)
    assert new_worker_admits(reading(now, seven_day=85.0), reserve_percent=10, now=now)


def test_no_reading_is_no_reserve_and_an_old_one_counts_however_old():
    now = 50_000.0
    old_high = reading(now - 3 * 86400, seven_day=99.0)
    old_low = reading(now - 3 * 86400, seven_day=10.0)

    assert new_worker_admits(None, now=now) and delivery_mod.usage_admits(None, now=now)
    assert not new_worker_admits(old_high, now=now)
    assert not delivery_mod.usage_admits(old_high, now=now)
    assert new_worker_admits(old_low, now=now) and delivery_mod.usage_admits(old_low, now=now)


def test_the_usage_file_is_a_reading_of_the_claude_provider_only(tmp_path):
    path = tmp_path / "usage.json"
    now = 50_000.0

    assert usage_mod.provider_reading(path, "claude", now=now) is None
    usage_mod.write_usage(path, {
        "seven_day": {"used_percent": 85, "resets_at": now + 600},
    }, now=now - 7200)

    found = usage_mod.provider_reading(path, "claude", now=now)
    assert found.provider == "claude" and found.read_at == now - 7200
    assert found.windows == {"seven_day": UsageWindow("seven_day", 85.0, now + 600)}
    # A window whose reset time has passed belongs to the window before.
    assert usage_mod.provider_reading(path, "claude", now=now + 600).windows == {}
    assert usage_mod.provider_reading(path, "other", now=now) is None
    path.write_text("{not json")
    assert usage_mod.provider_reading(path, "claude", now=now) is None


# ----- the wake reserve --------------------------------------------------------


def test_the_production_runner_holds_a_wake_by_the_usage_file_with_its_reason(
    tmp_path, repo, claude_state, board
):
    write_role(repo, "reviewer", REVIEWER)
    harness = build(tmp_path, repo, claude_state, board)
    reviewer = start_reviewer(harness)
    end_turn(harness)
    emit_three()
    write_reading(harness, seven_day=85.0)

    report = harness.next_tick()

    assert resumes(harness) == []
    held = harness.ledger.get(reviewer.id)
    assert held.state == ledger_mod.QUEUED
    reason = "wake held by the usage reserve (claude seven_day at 85%, above 80%)"
    assert f"reviewer/PR-7: {reason}" in report.skipped
    assert queue_reason(harness, held) == reason
    shown = card(harness, held)
    assert (shown["column"], shown["queue_reason"]) == ("queued", reason)

    write_reading(harness, seven_day=50.0)
    report = harness.next_tick()

    assert report.woken == ["reviewer/PR-7"]
    assert queue_reason(harness, held) is None
    assert card(harness, harness.ledger.get(reviewer.id))["queue_reason"] is None
    harness.ledger.close()


def test_the_wake_reserve_follows_the_configured_percentage(tmp_path, repo, claude_state, board):
    write_role(repo, "reviewer", REVIEWER)
    harness = build(tmp_path, repo, claude_state, board)
    harness.config = dataclasses.replace(harness.config, usage=UsageConfig(wake_reserve_percent=5))
    reviewer = start_reviewer(harness)
    end_turn(harness)
    emit_three()
    write_reading(harness, seven_day=90.0)

    assert harness.next_tick().woken == ["reviewer/PR-7"]
    assert harness.ledger.get(reviewer.id).state == ledger_mod.RUNNING
    harness.ledger.close()


def test_an_old_reading_holds_a_wake_until_its_reset_time(tmp_path, repo, claude_state, board):
    write_role(repo, "reviewer", REVIEWER)
    harness = build(tmp_path, repo, claude_state, board)
    reviewer = start_reviewer(harness)
    end_turn(harness)
    emit_three()
    write_reading(harness, age_secs=3 * 3600, seven_day=99.0)

    assert harness.next_tick().woken == []
    assert harness.ledger.get(reviewer.id).state == ledger_mod.QUEUED

    write_reading(harness, age_secs=3 * 3600, resets_in=-1, seven_day=99.0)
    assert harness.next_tick().woken == ["reviewer/PR-7"]
    harness.ledger.close()


# ----- the reserve for new workers ---------------------------------------------


def test_a_start_waits_while_the_week_is_above_the_reserve(harness):
    write_reading(harness, seven_day=75.0)
    harness.board.add("ABC-1")

    report = harness.next_tick()

    assert harness.worker("ABC-1") is None
    assert not harness.worktree("ABC-1").exists()
    assert any(
        "ABC-1: held by the usage reserve (claude seven_day at 75%, above 70%)" in line
        for line in report.skipped
    ), report.skipped

    write_reading(harness, seven_day=60.0)
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_an_old_reading_holds_new_workers_until_its_reset_and_no_file_holds_none(harness):
    write_reading(harness, age_secs=3 * 3600, seven_day=99.0)
    harness.board.add("ABC-1")

    report = harness.next_tick()

    assert harness.worker("ABC-1") is None
    assert any(
        "ABC-1: held by the usage reserve (claude seven_day at 99%, above 70%)" in line
        for line in report.skipped
    ), report.skipped

    write_reading(harness, age_secs=3 * 3600, resets_in=-1, seven_day=99.0)
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING

    write_reading(harness, age_secs=3 * 3600, seven_day=99.0)
    harness.board.add("ABC-2")
    harness.next_tick()
    assert harness.worker("ABC-2") is None
    harness.config.serve.usage_file.unlink()
    harness.next_tick()
    assert harness.worker("ABC-2").state == ledger_mod.RUNNING


def test_a_new_worker_from_the_board_is_refused_by_the_reserve_until_the_weekly_reset(harness):
    write_reading(harness, seven_day=75.0, resets_in=2 * 86400)
    reset = usage_mod.provider_reading(harness.config.serve.usage_file, "claude").windows["seven_day"].resets_at
    harness.board.add("ABC-1", labels=())
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer", item="ABC-1"
    )

    harness.next_tick()

    # A refused request is not tried again, so its answer says so and when to try again.
    answer = harness.ledger.get_request(request_id)
    assert answer["ok"] == 0
    assert answer["message"] == (
        f"ABC-1: the usage reserve allows no new worker until the weekly window resets on "
        f"{reset_clock(reset)} (claude seven_day at 75%, above 70%)"
    )
    assert harness.ledger.live_workers() == []
    assert list(harness.worktree_root.rglob("*")) == []

    write_reading(harness, seven_day=60.0)
    harness.next_tick()
    assert harness.ledger.live_workers() == []
    assert harness.ledger.get_request(request_id)["ok"] == 0


# ----- parking -----------------------------------------------------------------


def test_a_usage_limit_in_the_hook_parks_once_and_uses_no_retry(harness):
    worker = launch(harness)
    reset = time.time() + 3600

    parked = park(harness, worker, reset)
    harness.next_tick()
    harness.next_tick()

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.PARKED
    assert after.reset_at == pytest.approx(int(reset))
    assert after.launch_attempts == worker.launch_attempts == parked.launch_attempts
    assert harness.notifications().count(("ABC-1", "parked")) == 1
    assert [n for n in harness.notifications() if n[0] == "ABC-1" and n[1] != "parked"] == []
    shown = card(harness, after)
    assert (shown["column"], shown["reset_at"]) == ("parked", after.reset_at)


def test_a_usage_limit_in_the_session_listing_parks_the_worker(harness):
    worker = launch(harness)
    reset = int(time.time()) + 7200
    harness.claude_state.set_session_state(
        "ABC-1", "failed", error="rate_limit", errorMessage=f"Usage limit reached, resets {iso(reset)}"
    )

    harness.next_tick()
    harness.next_tick()

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.PARKED
    assert after.reset_at == pytest.approx(reset)
    assert after.launch_attempts == worker.launch_attempts
    assert after.last_error.startswith("usage limit")
    assert harness.notifications().count(("ABC-1", "parked")) == 1


def test_a_failed_listing_without_a_usage_limit_still_blocks(harness):
    launch(harness)
    harness.claude_state.set_session_state("ABC-1", "failed", error="api_error",
                                           errorMessage="Internal server error")

    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED


def test_the_listing_reads_the_failure_of_a_session():
    reset = 1_900_000_000
    assert claude.session_failure({"error": "rate_limit"}, now=reset - 60) == (True, None, None)
    assert claude.session_failure(
        {"error": {"type": "error", "message": f"Opus weekly limit reached, resets {iso(reset)}"}},
        now=reset - 60,
    ) == (True, reset, "opus")
    assert claude.session_failure(
        {"lastError": "You've hit your limit · resets at 1700000000"}, now=1_699_999_000
    ) == (True, 1_700_000_000, None)
    assert claude.session_failure({"error": "model_not_found"}) == (False, None, None)
    assert claude.session_failure({"message": "usage limit reached"}) == (False, None, None)
    session = claude.parse_session({"id": "a1", "state": "failed", "error": "rate_limit"})
    assert session.usage_limit is True
    assert claude.parse_session({"id": "a1", "state": "done", "error": "rate_limit"}).usage_limit is False


def test_the_listing_places_a_clock_time_after_the_failure_it_names():
    failed = 1_700_000_000
    clock = datetime.fromtimestamp(failed + 3600, tz=timezone.utc)
    entry = {"id": "a1", "state": "failed", "error": "rate_limit",
             "errorMessage": f"Usage limit reached, resets {clock:%H:%M} (UTC)"}

    for listed in (iso(failed), failed * 1000):
        session = claude.parse_session({**entry, "lastTerminalAt": listed})
        assert session.ended_at == failed
        assert session.reset_at == pytest.approx(failed + 3600, abs=60)
    # Without that time the clock time is the next one after now.
    assert claude.parse_session(entry).ended_at is None
    assert claude.parse_session(entry).reset_at > time.time()
    # A time ahead of this machine's clock is not read.
    ahead = claude.parse_session({**entry, "lastTerminalAt": iso(time.time() + 3600)})
    assert ahead.ended_at is None


def test_a_parked_worker_without_a_reset_time_waits_for_continue(harness):
    worker = launch(harness)
    park(harness, worker, None)

    harness.next_tick()
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.PARKED
    assert harness.claude_state.read().get("resumes", []) == []
    assert card(harness, harness.worker("ABC-1"))["reset_at"] is None


# ----- the resume after the reset ----------------------------------------------


def test_a_parked_worker_resumes_in_the_same_session_after_the_reset(harness):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)

    report = harness.next_tick()

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.RUNNING
    assert after.session_id == worker.session_id and after.session_uuid == worker.session_uuid
    assert after.reset_at is None and after.last_error is None
    assert after.wakes == worker.wakes + 1
    assert after.launch_attempts == worker.launch_attempts
    (resume,) = harness.claude_state.read()["resumes"]
    assert resume["argv"][:3] == ["--resume", worker.session_uuid, "--bg"]
    assert resume["prompt"] == RESUME_MESSAGE
    assert [s.kind for s in harness.ledger.sessions_of(after)] == ["initial", "wake"]
    assert "implementer/ABC-1: resume after the usage reset -> running" in report.transitions


def test_parked_workers_resume_oldest_first_under_the_global_cap(harness):
    harness.config = dataclasses.replace(harness.config, max_concurrent=1)
    harness.fresh_runner()
    first = launch(harness, "ABC-1")
    harness.claude_state.set_session_state("ABC-1", "done", status="idle")
    harness.next_tick()
    second = launch(harness, "ABC-2")
    park(harness, second, time.time() + 3600)
    park(harness, first, time.time() + 3600)
    now = time.time()
    harness.ledger.update(harness.ledger.get(second.id), reset_at=now - 20)
    harness.ledger.update(harness.ledger.get(first.id), reset_at=now - 10)

    report = harness.next_tick()

    # The one whose limit reset first goes first; the cap holds the other.
    assert report.woken == ["implementer/ABC-2"]
    held = harness.ledger.get(first.id)
    assert held.state == ledger_mod.PARKED
    assert queue_reason(harness, held) == "wake held by the global cap (1/1 working)"
    assert card(harness, held)["queue_reason"] == "wake held by the global cap (1/1 working)"

    harness.claude_state.set_session_state("ABC-2", "done", status="idle")
    report = harness.next_tick()

    assert report.woken == ["implementer/ABC-1"]
    assert harness.ledger.get(first.id).state == ledger_mod.RUNNING
    assert queue_reason(harness, held) is None


def test_a_parked_session_that_runs_again_is_not_interrupted(harness):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    # The operator went on in the session before the reset.
    harness.claude_state.set_session_state("ABC-1", "working")

    report = harness.next_tick()

    assert harness.claude_state.read().get("resumes", []) == []
    assert harness.worker("ABC-1").state == ledger_mod.PARKED
    assert "implementer/ABC-1: resume held, the session has a turn or a prompt" in report.skipped


def test_a_parked_session_listed_blocked_without_a_dialog_resumes_after_the_reset(harness):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    # Claude Code lists some ended turns as blocked, with no dialog open.
    harness.claude_state.set_session_state("ABC-1", "blocked", status="idle")

    report = harness.next_tick()

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.RUNNING
    (resume,) = harness.claude_state.read()["resumes"]
    assert resume["prompt"] == RESUME_MESSAGE
    assert "implementer/ABC-1: resume after the usage reset -> running" in report.transitions


def test_a_parked_session_with_an_open_dialog_holds_the_resume(harness):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    harness.claude_state.set_session_state(
        "ABC-1", "blocked", status="waiting", waitingFor="permission prompt"
    )

    report = harness.next_tick()

    assert harness.claude_state.read().get("resumes", []) == []
    assert "implementer/ABC-1: resume held, the session has a turn or a prompt" in report.skipped


@pytest.mark.parametrize("listed_as, reason", [
    ("working", "the session has a turn or a prompt"),
    ("hibernating", "the session state is not recognised"),
    (None, "session {session} is not in the listing"),
])
def test_the_card_says_why_the_listing_holds_a_resume(harness, listed_as, reason):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    if listed_as is None:
        harness.claude_state.drop_sessions()
    else:
        harness.claude_state.set_session_state("ABC-1", listed_as)

    report = harness.next_tick()

    expected = f"resume held, {reason.format(session=worker.session_id)}"
    held = harness.worker("ABC-1")
    assert held.state == ledger_mod.PARKED
    assert harness.claude_state.read().get("resumes", []) == []
    assert f"implementer/ABC-1: {expected}" in report.skipped
    assert queue_reason(harness, held) == expected
    assert card(harness, held)["queue_reason"] == expected


def test_a_resume_held_by_a_failed_listing_reports_the_error(harness, monkeypatch):
    from chargehand.tick import Runner

    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    real = Runner._route_trackers

    def listing_fails_after(self, report):
        # An action of this tick, such as a stop, dropped the listing, and a new one fails.
        real(self, report)
        harness.claude_state.set(agents_fail=True)
        self.invalidate_sessions()

    monkeypatch.setattr(Runner, "_route_trackers", listing_fails_after)
    report = harness.next_tick()

    held = harness.worker("ABC-1")
    assert held.state == ledger_mod.PARKED
    reason = queue_reason(harness, held)
    prefix = "resume held, the session listing failed: "
    assert reason.startswith(prefix)
    # The error names the binary, so its length depends on the checkout path: compare
    # with the same cap that delivery applies.
    error = f"`{FAKE_CLAUDE} agents --json --all` exited 1: fake claude: agents unavailable"
    assert reason == prefix + sanitize.one_line(error, limit=200)
    assert len(reason) <= len(prefix) + 200
    assert f"implementer/ABC-1: {reason.removeprefix('resume held, ')}" in report.errors
    # The card caps a line as it caps every line.
    assert card(harness, held)["queue_reason"] == sanitize.one_line(reason, limit=board_mod.LINE_LIMIT)


def test_a_resume_held_by_a_runtime_this_build_lacks_says_so(harness):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1, runtime="nope")

    report = harness.next_tick()

    held = harness.worker("ABC-1")
    assert held.state == ledger_mod.PARKED
    reason = queue_reason(harness, held)
    assert reason.startswith("resume held, the runtime cannot be set up: unknown runtime 'nope'")
    assert f"implementer/ABC-1: {reason}" in report.skipped


def test_a_turn_that_starts_while_a_resume_prepares_is_not_stopped(harness, monkeypatch):
    """The check and the stop use one listing: the operator can start a turn in between."""
    from chargehand.tick import Runner

    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)

    def operator_starts_a_turn(self, parked):
        harness.claude_state.set_session_state("ABC-1", "working")
        return None

    monkeypatch.setattr(Runner, "_refresh_item_checkout", operator_starts_a_turn)
    harness.next_tick()

    assert ["stop", worker.session_id] not in harness.claude_state.commands
    assert harness.claude_state.read().get("resumes", []) == []
    held = harness.worker("ABC-1")
    assert held.state == ledger_mod.PARKED
    # The card says why the resume waits now, not an older reason.
    assert queue_reason(harness, held) == "wake held, session s1 is working, not idle"


def test_a_new_limit_during_the_resume_keeps_its_reset_and_is_notified(harness, monkeypatch):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    later = int(time.time()) + 5 * 3600
    real = claude.ClaudeRuntime.wake

    def wake_then_limit(self, session_id, message, **kwargs):
        result = real(self, session_id, message, **kwargs)
        stop_failure(harness, worker, error="rate_limit",
                     error_details=f"Claude usage limit reached|{later}")
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "wake", wake_then_limit)
    harness.next_tick()

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.PARKED
    assert after.reset_at == pytest.approx(later)
    assert after.wakes == worker.wakes + 1
    assert harness.notifications().count(("ABC-1", "parked")) == 2


def test_a_new_limit_during_a_continue_keeps_its_reset(harness, monkeypatch):
    worker = launch(harness)
    park(harness, worker, None)
    later = int(time.time()) + 5 * 3600
    hook = claude.parse_hook("StopFailure", {
        "session_id": worker.session_uuid, "error": "rate_limit",
        "error_details": f"Claude usage limit reached|{later}",
    })

    after = continue_with(harness, worker, monkeypatch, hook)

    assert after.state == ledger_mod.PARKED
    assert after.reset_at == pytest.approx(later)


def test_a_limit_the_listing_shows_late_keeps_the_reset_of_its_turn(harness):
    """A clock time is placed after the start of the turn, not after the tick."""
    worker = launch(harness)
    now = time.time()
    harness.ledger.update(harness.worker("ABC-1"), working_since=now - 2 * 3600)
    passed = datetime.fromtimestamp(now - 3600, tz=timezone.utc)
    harness.claude_state.set_session_state(
        "ABC-1", "failed", error="rate_limit",
        errorMessage=f"Usage limit reached, resets {passed:%H:%M} (UTC)",
    )

    report = harness.next_tick()

    # Parked by the diff, and due at once, so delivery resumes it in the same tick.
    assert any("running -> parked" in line for line in report.transitions), report.transitions
    assert "implementer/ABC-1: resume after the usage reset -> running" in report.transitions
    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.RUNNING and after.session_id == worker.session_id
    (resume,) = harness.claude_state.read()["resumes"]
    assert resume["prompt"] == RESUME_MESSAGE


def test_a_limit_the_listing_shows_hours_late_is_placed_after_the_failure(harness):
    """The daemon was down and the hook was lost: the listing names when the turn failed."""
    worker = launch(harness)
    now = time.time()
    harness.ledger.update(harness.worker("ABC-1"), working_since=now - 8 * 3600)
    failed = now - 6 * 3600
    # More than FAILURE_LOOKBACK_SECS ago: placed after the lookback, it would be tomorrow.
    resets = datetime.fromtimestamp(failed + 3600, tz=timezone.utc)
    harness.claude_state.set_session_state(
        "ABC-1", "failed", error="rate_limit", lastTerminalAt=iso(failed),
        errorMessage=f"Usage limit reached, resets {resets:%H:%M} (UTC)",
    )

    report = harness.next_tick()

    assert any("running -> parked" in line for line in report.transitions), report.transitions
    assert "implementer/ABC-1: resume after the usage reset -> running" in report.transitions
    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.RUNNING and after.session_id == worker.session_id
    (resume,) = harness.claude_state.read()["resumes"]
    assert resume["prompt"] == RESUME_MESSAGE


def test_a_parked_worker_whose_turn_ends_keeps_no_reset_time(harness):
    """The operator went on in the session before the reset, and that turn ended."""
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    turn_end = claude.parse_hook("Stop", {
        "session_id": worker.session_uuid, "last_assistant_message": "Done.",
    })

    after = harness.runner.apply_hook(harness.ledger.get(worker.id), turn_end)

    assert after.state == ledger_mod.IDLE
    assert after.reset_at is None
    assert harness.ledger.get(worker.id).reset_at is None
    assert card(harness, after)["reset_at"] is None


def test_the_resume_waits_for_the_wake_reserve(harness):
    worker = launch(harness)
    park(harness, worker, time.time() + 3600)
    harness.ledger.update(harness.worker("ABC-1"), reset_at=time.time() - 1)
    write_reading(harness, seven_day=95.0)

    harness.next_tick()

    after = harness.worker("ABC-1")
    assert after.state == ledger_mod.PARKED
    assert "usage reserve" in queue_reason(harness, after)


# ----- the model-limit badge -----------------------------------------------------


def test_a_per_model_limit_shows_a_badge_until_the_reset(harness):
    worker = launch(harness)
    reset = int(time.time()) + 3600
    assert board_mod.build(harness.config, harness.ledger, health={})["model_limit"] is None

    stop_failure(harness, worker, error="rate_limit",
                 error_details=f"Opus weekly limit reached, resets {iso(reset)}")

    badge = board_mod.build(harness.config, harness.ledger, health={})["model_limit"]
    assert badge["model"] == "opus" and badge["provider"] == "claude"
    assert badge["reset_at"] == pytest.approx(reset)
    later = board_mod.build(harness.config, harness.ledger, health={}, now=reset + 1)
    assert later["model_limit"] is None


def test_a_limit_of_the_account_shows_no_model_badge(harness):
    worker = launch(harness)

    stop_failure(harness, worker, error="rate_limit", error_details="Claude usage limit reached")

    assert harness.ledger.get_state(MODEL_LIMIT_KEY) is None


def test_the_badge_without_a_reset_time_shows_for_a_fixed_time(harness):
    at = time.time()
    harness.ledger.set_state(MODEL_LIMIT_KEY, json.dumps(
        {"provider": "claude", "model": "sonnet", "at": at, "reset_at": None}
    ))

    assert board_mod.model_limit(harness.ledger, at + 3600)["model"] == "sonnet"
    assert board_mod.model_limit(harness.ledger, at + board_mod.MODEL_LIMIT_SHOWN_SECS + 1) is None
    harness.ledger.set_state(MODEL_LIMIT_KEY, "not json")
    assert board_mod.model_limit(harness.ledger, at) is None


def test_the_hook_names_only_a_known_model_family():
    hook = claude.parse_hook("StopFailure", {
        "session_id": "s", "error": "rate_limit",
        "error_details": "Claude Sonnet 4 usage limit reached",
    })
    assert (hook.usage_limit, hook.model_limit) == (True, "sonnet")
    plain = claude.parse_hook("StopFailure", {"session_id": "s", "error": "rate_limit"})
    assert plain.model_limit is None
    other = claude.parse_hook("StopFailure", {
        "session_id": "s", "error": "model_not_found", "error_details": "Opus limit",
    })
    assert (other.usage_limit, other.model_limit) == (False, None)


# ----- a hook of the new turn during a wake or a continue ----------------------


def prompt_hook(worker):
    return claude.parse_hook("PermissionRequest", {
        "session_id": worker.session_uuid, "tool_name": "Bash", "tool_input": {"command": "date"},
    })


def test_a_permission_prompt_during_a_wake_keeps_the_worker_blocked(
    tmp_path, repo, claude_state, board, monkeypatch
):
    write_role(repo, "reviewer", REVIEWER)
    harness = build(tmp_path, repo, claude_state, board)
    reviewer = start_reviewer(harness)
    end_turn(harness)
    harness.next_tick()
    emit_three()
    real = claude.ClaudeRuntime.wake

    def wake_then_prompt(self, session_id, message, **kwargs):
        result = real(self, session_id, message, **kwargs)
        harness.runner.apply_hook(harness.ledger.get(reviewer.id), prompt_hook(reviewer))
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "wake", wake_then_prompt)
    report = harness.next_tick()

    assert report.woken == ["reviewer/PR-7"]
    after = harness.ledger.get(reviewer.id)
    assert after.state == ledger_mod.BLOCKED
    assert after.notified_state == claude.BLOCKED
    assert after.wakes == 1 and after.last_wake_at is not None
    assert harness.ledger.inbox(after) == []

    # The listing shows the dialog too; the next diff sends no second notification.
    monkeypatch.undo()
    harness.claude_state.set_session_state("PR-7", "blocked")
    harness.next_tick()
    assert harness.notifications().count(("PR-7", "blocked")) == 1
    harness.ledger.close()


def continue_with(harness, worker, monkeypatch, hook):
    """Apply a continue whose respawn posts *hook* before it returns; the state it wrote."""
    real = claude.ClaudeRuntime.respawn

    def respawn_then_hook(self, session_id):
        result = real(self, session_id)
        harness.runner.apply_hook(harness.ledger.get(worker.id), hook)
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "respawn", respawn_then_hook)
    runner = harness.fresh_runner()
    runner.sessions("claude", refresh=True)
    ok, message = runner._apply_request("continue", "ABC-1", {}, TickReport())
    monkeypatch.undo()
    assert ok, message
    return harness.ledger.get(worker.id)


@pytest.mark.parametrize("state", [ledger_mod.BLOCKED, ledger_mod.PARKED])
def test_a_permission_prompt_during_a_continue_keeps_the_worker_blocked(
    harness, monkeypatch, state
):
    worker = launch(harness)
    # Only a parked worker has a reset time.
    reset = {"reset_at": time.time() + 60} if state == ledger_mod.PARKED else {}
    harness.ledger.update(worker, state=state, **reset)

    after = continue_with(harness, worker, monkeypatch, prompt_hook(worker))

    assert after.state == ledger_mod.BLOCKED
    assert after.reset_at is None
    [prompt] = harness.ledger.open_permission_prompts(after)
    assert prompt.tool_name == "Bash"


def test_a_paused_worker_that_a_session_start_reports_is_running_after_continue(
    harness, monkeypatch
):
    worker = launch(harness)
    harness.ledger.update(worker, state=ledger_mod.PAUSED)
    hook = claude.parse_hook("SessionStart", {"session_id": worker.session_uuid, "source": "resume"})

    after = continue_with(harness, worker, monkeypatch, hook)

    assert after.state == ledger_mod.RUNNING


def test_a_paused_worker_that_asks_during_continue_is_blocked(harness, monkeypatch):
    worker = launch(harness)
    harness.ledger.update(worker, state=ledger_mod.PAUSED)

    after = continue_with(harness, worker, monkeypatch, prompt_hook(worker))

    assert after.state == ledger_mod.BLOCKED
    assert len(harness.ledger.open_permission_prompts(after)) == 1


def test_a_limit_the_listing_shows_after_a_long_turn_keeps_its_reset(harness):
    """A turn start days ago does not push an absolute reset time out of reach."""
    launch(harness)
    now = time.time()
    harness.ledger.update(harness.worker("ABC-1"), working_since=now - 10 * 86400)
    reset = int(now) + 2 * 3600
    harness.claude_state.set_session_state(
        "ABC-1", "failed", error="rate_limit", errorMessage=f"Usage limit reached, resets {iso(reset)}"
    )

    harness.next_tick()

    parked = harness.worker("ABC-1")
    assert parked.state == ledger_mod.PARKED
    assert parked.reset_at == pytest.approx(reset)


def test_a_clock_time_is_not_placed_days_before_the_failure(harness):
    launch(harness)
    now = time.time()
    harness.ledger.update(harness.worker("ABC-1"), working_since=now - 3 * 86400)
    ahead = datetime.fromtimestamp(now + 2 * 3600, tz=timezone.utc)
    harness.claude_state.set_session_state(
        "ABC-1", "failed", error="rate_limit",
        errorMessage=f"Usage limit reached, resets {ahead:%H:%M} (UTC)",
    )

    harness.next_tick()

    parked = harness.worker("ABC-1")
    assert parked.state == ledger_mod.PARKED
    assert parked.reset_at == pytest.approx(now + 2 * 3600, abs=60)
    assert harness.claude_state.read().get("resumes", []) == []
