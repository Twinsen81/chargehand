"""The daemon's own usage reading: Claude Code's get_usage control request, on its own interval."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chargehand import claude
from chargehand import install
from chargehand import ledger as ledger_mod
from chargehand import usage as usage_mod
from chargehand.config import MachineConfig, UsageConfig
from chargehand.errors import ClaudeError, ConfigError
from conftest import FAKE_CLAUDE
from support import daemon as support

MINIMAL = {"route": [{"repo": "~/code/app", "tracker": {"type": "linear", "team": "ABC"}}]}
RESET = "2026-11-09T13:59:59Z"
RESET_EPOCH = datetime(2026, 11, 9, 13, 59, 59, tzinfo=timezone.utc).timestamp()


def answer(**seven_day) -> dict:
    return {
        "subscription_type": "max",
        "rate_limits_available": True,
        "rate_limits": {
            "five_hour": {"utilization": 90, "resets_at": RESET},
            "seven_day": {"utilization": 42, "resets_at": RESET, **seven_day},
            "seven_day_opus": {"utilization": 99, "resets_at": RESET},
        },
        "limits": [{"severity": "warning"}],
    }


def line(message: dict) -> bytes:
    return json.dumps(message).encode("utf-8")


class Fetch:
    """A fetch that returns or raises what the test sets, and counts its calls."""

    def __init__(self, result=None) -> None:
        self.result = result if result is not None else answer()
        self.calls: list[Path] = []

    def __call__(self, config, cwd):
        self.calls.append(cwd)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def api_state(daemon) -> dict:
    status, body = support.request(daemon.port, "GET", "/api/state")
    assert status == 200, body
    return json.loads(body)


def poller(harness, fetch, tmp_path, **kwargs) -> usage_mod.UsagePoller:
    return usage_mod.UsagePoller(
        lambda: harness.config, fetch=fetch, workdir=lambda: tmp_path / "usage-request",
        clock=lambda: RESET_EPOCH - 3600, **kwargs,
    )


# ----- the request -------------------------------------------------------------


def test_the_request_uses_print_mode_stream_json_and_no_hooks_and_never_bare():
    args = claude.usage_request_args()

    assert args[:6] == [
        "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
    ]
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--strict-mcp-config" in args and "--mcp-config" not in args
    assert "--no-session-persistence" in args
    assert "--bare" not in args
    assert json.loads(claude.usage_request_line()) == {
        "type": "control_request",
        "request_id": claude.USAGE_REQUEST_ID,
        "request": {"subtype": "get_usage", "skip_behaviors": True},
    }


def test_the_answer_is_the_control_response_with_the_request_id():
    body = answer()
    response = {"type": "control_response", "response": {
        "subtype": "success", "request_id": claude.USAGE_REQUEST_ID, "response": body,
    }}

    assert claude.usage_answer(line(response)) == body
    assert claude.usage_answer(line({**response, "response": {
        **response["response"], "request_id": "other",
    }})) is None
    assert claude.usage_answer(line({"type": "system", "subtype": "init"})) is None
    assert claude.usage_answer(b"not json") is None
    assert claude.usage_answer(b"\xff\xfe") is None


def test_an_error_answer_or_one_without_a_usage_object_raises():
    error = {"type": "control_response", "response": {
        "subtype": "error", "request_id": claude.USAGE_REQUEST_ID, "error": "nope\x1b[31m",
    }}
    with pytest.raises(ClaudeError, match="refused the usage request: nope"):
        claude.usage_answer(line(error))
    empty = {"type": "control_response", "response": {
        "subtype": "success", "request_id": claude.USAGE_REQUEST_ID, "response": [1],
    }}
    with pytest.raises(ClaudeError, match="without a usage object"):
        claude.usage_answer(line(empty))


# ----- the answer --------------------------------------------------------------


def test_a_full_answer_gives_the_weekly_window_only():
    assert usage_mod.windows_from_get_usage(answer()) == {
        "seven_day": {"used_percent": 42.0, "resets_at": RESET_EPOCH},
    }


@pytest.mark.parametrize(
    "response",
    [
        {**answer(), "rate_limits_available": False},
        {"rate_limits_available": True},
        {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 5}}},
        {"rate_limits_available": True, "rate_limits": {"seven_day": None}},
        answer(utilization=None),
        answer(resets_at=None),
        answer(utilization="42"),
        answer(resets_at="next week"),
        {"rate_limits_available": True, "rate_limits": [{"seven_day": 42}]},
        {"rate_limits_available": True, "rate_limits": {"seven_day": {"percent": 42, "reset": RESET}}},
        "unexpected",
        None,
    ],
    ids=[
        "unavailable", "no-rate-limits", "no-weekly", "weekly-null", "no-utilization",
        "no-reset", "string-utilization", "bad-reset", "list", "renamed-fields", "string", "none",
    ],
)
def test_a_missing_field_an_unavailable_or_a_changed_answer_is_no_window(response):
    assert usage_mod.windows_from_get_usage(response) is None


def test_a_percentage_outside_zero_to_a_hundred_is_clamped():
    assert usage_mod.windows_from_get_usage(answer(utilization=140))["seven_day"]["used_percent"] == 100.0


# ----- the exchange with the fake binary ---------------------------------------


def test_the_exchange_reads_past_other_lines_to_the_answer(tmp_path, claude_state):
    cwd = tmp_path / "empty"
    cwd.mkdir()

    body = claude.ClaudeCLI(str(FAKE_CLAUDE)).get_usage(cwd, timeout=10)

    assert usage_mod.windows_from_get_usage(body) == {
        "seven_day": {"used_percent": 42.0, "resets_at": RESET_EPOCH},
    }
    (request,) = claude_state.read()["usage_requests"]
    assert request["argv"] == claude.usage_request_args()
    assert Path(request["cwd"]).resolve() == cwd.resolve()
    assert json.loads(request["line"])["request"]["subtype"] == "get_usage"


def test_an_error_answer_from_the_binary_raises(tmp_path, claude_state):
    claude_state.set(usage_mode="error")

    with pytest.raises(ClaudeError, match="get_usage is not supported"):
        claude.ClaudeCLI(str(FAKE_CLAUDE)).get_usage(tmp_path, timeout=10)


def test_an_exit_without_an_answer_raises_with_the_exit_code(tmp_path, claude_state):
    claude_state.set(usage_mode="exit")

    with pytest.raises(ClaudeError, match=r"without an answer \(exit code 1\): fake claude: not signed in"):
        claude.ClaudeCLI(str(FAKE_CLAUDE)).get_usage(tmp_path, timeout=10)


def test_a_hanging_binary_times_out_and_is_killed(tmp_path, claude_state):
    claude_state.set(usage_mode="hang")
    started = time.monotonic()

    with pytest.raises(ClaudeError, match="timed out"):
        claude.ClaudeCLI(str(FAKE_CLAUDE)).get_usage(tmp_path, timeout=0.5)

    assert time.monotonic() - started < 5


def test_a_missing_binary_raises(tmp_path):
    with pytest.raises(ClaudeError, match="not found"):
        claude.ClaudeCLI(str(tmp_path / "no-such-claude")).get_usage(tmp_path, timeout=1)


# ----- the poller --------------------------------------------------------------


def test_a_poll_writes_the_reading_with_its_source(harness, tmp_path):
    fetch = Fetch()

    assert poller(harness, fetch, tmp_path).poll_once()

    reading = usage_mod.read_usage(harness.config.serve.usage_file, RESET_EPOCH - 60)
    assert reading["source"] == usage_mod.SOURCE_GET_USAGE
    assert reading["written_at"] == RESET_EPOCH - 3600
    assert reading["seven_day"]["used_percent"] == 42.0
    assert reading["seven_day"]["resets_at"] == RESET_EPOCH
    # It runs in its own private directory.
    (workdir,) = fetch.calls
    assert workdir == tmp_path / "usage-request"
    assert workdir.stat().st_mode & 0o777 == 0o700


def test_the_default_directory_is_under_the_state_directory(isolated_state):
    assert usage_mod.poll_dir().parent == isolated_state


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (ClaudeError("the usage request timed out"), "timed out"),
        (ClaudeError("Claude Code refused the usage request: no"), "refused"),
        ({**answer(), "rate_limits_available": False}, "no rate limits"),
        (answer(utilization=None), "no weekly window"),
        ({"changed": True}, "no weekly window"),
        (RuntimeError("boom"), "unexpected RuntimeError"),
    ],
)
def test_a_failed_poll_writes_nothing_and_logs_one_line(harness, tmp_path, caplog, result, reason):
    usage_file = harness.config.serve.usage_file
    usage_mod.write_usage(usage_file, {"seven_day": {"used_percent": 10, "resets_at": RESET_EPOCH}}, now=5)
    before = usage_file.read_bytes()
    caplog.set_level(logging.DEBUG, logger="chargehand.usage")

    assert not poller(harness, Fetch(result), tmp_path).poll_once()

    assert usage_file.read_bytes() == before
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and reason in warnings[0].getMessage()


def test_a_failure_warns_once_until_it_changes_and_a_recovery_says_so(harness, tmp_path, caplog):
    fetch = Fetch(ClaudeError("the usage request timed out"))
    poll = poller(harness, fetch, tmp_path)
    caplog.set_level(logging.DEBUG, logger="chargehand.usage")

    poll.poll_once()
    poll.poll_once()
    fetch.result = ClaudeError("the usage request ended without an answer (exit code 1)")
    poll.poll_once()
    fetch.result = answer()
    poll.poll_once()

    levels = [(r.levelname, r.getMessage()) for r in caplog.records if r.levelno >= logging.INFO]
    assert [level for level, _ in levels] == ["WARNING", "WARNING", "INFO"]
    assert "timed out" in levels[0][1] and "exit code 1" in levels[1][1]


def test_the_poller_polls_at_once_and_then_every_interval(harness, tmp_path):
    harness.config = dataclasses.replace(harness.config, usage=UsageConfig(poll_secs=120))
    waits: list[float] = []
    fetch = Fetch()

    def wait(secs: float) -> bool:
        waits.append(secs)
        return len(waits) >= 3

    poller(harness, fetch, tmp_path, wait=wait).run()

    assert len(fetch.calls) == 3
    assert waits == [120.0, 120.0, 120.0]


def test_a_poll_interval_of_zero_polls_nothing_and_reads_the_configuration_again(harness, tmp_path):
    harness.config = dataclasses.replace(harness.config, usage=UsageConfig(poll_secs=0))
    waits: list[float] = []
    fetch = Fetch()

    def wait(secs: float) -> bool:
        waits.append(secs)
        if len(waits) == 2:
            harness.config = dataclasses.replace(harness.config, usage=UsageConfig(poll_secs=300))
        return len(waits) >= 3

    poller(harness, fetch, tmp_path, wait=wait).run()

    assert waits == [usage_mod.POLL_OFF_RECHECK_SECS, usage_mod.POLL_OFF_RECHECK_SECS, 300.0]
    assert len(fetch.calls) == 1


def test_the_poller_runs_the_fake_binary_and_a_hang_leaves_the_window_unknown(
    harness, tmp_path, claude_state, caplog
):
    usage_file = harness.config.serve.usage_file
    claude_state.set(usage_mode="hang")
    fetch = lambda config, cwd: claude.ClaudeCLI(config.claude_bin).get_usage(cwd, timeout=0.5)
    poll = usage_mod.UsagePoller(lambda: harness.config, fetch=fetch, workdir=lambda: tmp_path / "u")

    assert not poll.poll_once()
    assert not usage_file.exists()
    assert usage_mod.read_usage(usage_file)["seven_day"]["level"] == usage_mod.GREY

    claude_state.set(usage_mode="ok", usage_response=answer(resets_at=time.time() + 86400))
    assert poll.poll_once()
    assert usage_mod.read_usage(usage_file)["source"] == usage_mod.SOURCE_GET_USAGE


# ----- the consumers -----------------------------------------------------------


def test_the_reserves_use_the_reading_of_a_poll(harness, tmp_path):
    reset = time.time() + 86400
    fetch = Fetch(answer(utilization=75, resets_at=reset))
    usage_mod.UsagePoller(lambda: harness.config, fetch=fetch, workdir=lambda: tmp_path / "u").poll_once()
    harness.board.add("ABC-1")

    report = harness.next_tick()

    assert harness.worker("ABC-1") is None
    assert any(
        "ABC-1: held by the usage reserve (claude seven_day at 75%, above 70%)" in entry
        for entry in report.skipped
    ), report.skipped
    reading = usage_mod.provider_reading(harness.config.serve.usage_file, claude.USAGE_PROVIDER)
    assert reading.windows["seven_day"].used_percent == 75.0

    fetch.result = answer(utilization=50, resets_at=reset)
    usage_mod.UsagePoller(lambda: harness.config, fetch=fetch, workdir=lambda: tmp_path / "u").poll_once()
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_the_daemon_polls_on_its_own_thread_and_a_hanging_poll_blocks_no_tick(harness, tmp_path):
    release = threading.Event()
    started = threading.Event()

    def hanging(config, cwd):
        started.set()
        release.wait(30)
        raise ClaudeError("the usage request timed out")

    config = support.daemon_config(harness.config, tmp_path / "board.token", interval=1)
    daemon = support.build(
        config,
        usage_poller=lambda d: usage_mod.UsagePoller(
            lambda: d.config, fetch=hanging, workdir=lambda: tmp_path / "u"
        ),
    )
    try:
        with support.running(daemon):
            assert started.wait(10)
            assert support.wait_for(lambda: daemon.health_data.ticks >= 2, timeout=20)
            assert daemon.health_data.failed_ticks == 0
            assert daemon.health_data.last_tick_error is None
            state = api_state(daemon)
            assert state["usage"]["known"] is False
            release.set()
    finally:
        release.set()


def test_the_daemon_shows_a_polled_reading_in_its_state(harness, tmp_path):
    reset = time.time() + 86400
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    daemon = support.build(
        config,
        usage_poller=lambda d: usage_mod.UsagePoller(
            lambda: d.config, fetch=Fetch(answer(utilization=42, resets_at=reset)),
            workdir=lambda: tmp_path / "u",
        ),
    )
    with support.running(daemon):
        assert support.wait_for(lambda: config.serve.usage_file.exists())
        state = api_state(daemon)

    assert state["usage"]["source"] == usage_mod.SOURCE_GET_USAGE
    assert state["usage"]["seven_day"]["used_percent"] == 42.0
    assert state["usage"]["seven_day"]["resets_at"] == pytest.approx(reset)
    assert daemon.usage_poller is not None and daemon.usage_poller._thread is None


def test_a_daemon_without_a_poller_starts_none(harness, tmp_path):
    daemon = support.build(support.daemon_config(harness.config, tmp_path / "board.token"))
    with support.running(daemon):
        assert support.wait_for(lambda: daemon.health_data.ticks >= 1)

    assert daemon.usage_poller is None


# ----- doctor ------------------------------------------------------------------


def test_doctor_shows_the_source_and_the_age_of_the_reading(harness):
    now = time.time()
    usage_mod.write_usage(
        harness.config.serve.usage_file,
        {"seven_day": {"used_percent": 42, "resets_at": now + 86400}},
        source=usage_mod.SOURCE_GET_USAGE, now=now - 180,
    )

    check = install.usage_check(harness.config, now)

    assert check.status == install.OK
    assert "weekly 42%" in check.detail
    assert "source get_usage" in check.detail
    assert "3 min old" in check.detail


def test_doctor_warns_about_a_reading_older_than_three_poll_intervals(harness):
    now = time.time()
    usage_mod.write_usage(
        harness.config.serve.usage_file,
        {"seven_day": {"used_percent": 42, "resets_at": now + 86400}},
        source=usage_mod.SOURCE_STATUSLINE, now=now - 2 * 3600,
    )

    check = install.usage_check(harness.config, now)

    assert check.status == install.WARN
    assert "source statusline · 2 h old" in check.detail
    assert "daemon log" in check.detail
    off = dataclasses.replace(harness.config, usage=UsageConfig(poll_secs=0))
    assert install.usage_check(off, now).status == install.OK


def test_doctor_says_when_there_is_no_reading_or_no_current_window(harness):
    check = install.usage_check(harness.config)
    assert check.status == install.INFO
    assert "no reading" in check.detail and "every 300s" in check.detail

    now = time.time()
    usage_mod.write_usage(
        harness.config.serve.usage_file,
        {"seven_day": {"used_percent": 42, "resets_at": now - 10}},
        source=usage_mod.SOURCE_SESSION, now=now - 30,
    )
    check = install.usage_check(harness.config, now)
    assert "no current weekly window · source session · less than a minute old" == check.detail


def test_doctor_lists_the_usage_check(capsys, config_file, harness):
    from chargehand.cli import main

    main(["--config", str(config_file), "doctor", "--json", "--no-tracker"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["usage"]["status"] == install.INFO


# ----- the configuration -------------------------------------------------------


def test_the_poll_interval_defaults_to_five_minutes_and_zero_turns_it_off():
    assert MachineConfig.parse(MINIMAL).usage.poll_secs == 300
    assert MachineConfig.parse({**MINIMAL, "usage": {"poll_secs": 0}}).usage.poll_secs == 0
    assert MachineConfig.parse({**MINIMAL, "usage": {"poll_secs": 60}}).usage.poll_secs == 60


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (30, r"usage.poll_secs: must be 0 \(off\) or >= 60"),
        (-1, r"usage.poll_secs: must be >= 0"),
        (300.0, r"usage.poll_secs: expected an integer"),
        (True, r"usage.poll_secs: expected an integer"),
    ],
)
def test_a_bad_poll_interval_is_an_error(value, message):
    with pytest.raises(ConfigError, match=message):
        MachineConfig.parse({**MINIMAL, "usage": {"poll_secs": value}})


def test_the_bundled_configuration_names_the_poll_interval():
    from chargehand import paths

    assert "poll_secs = 300" in (paths.templates_dir() / "config.toml").read_text()
