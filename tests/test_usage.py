"""Usage readings: the status line wrapper writes the windows, the daemon reads them."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chargehand import cli, usage
from chargehand.config import ServeConfig, load_machine_config
from chargehand.delivery import USAGE_MAX_AGE_SECS
from chargehand.errors import ConfigError
from conftest import write_machine_config
from support import daemon as support

NOW = 1_800_000_000.0


def _payload(five: float = 62, seven: float = 81, **extra) -> bytes:
    return json.dumps({
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": NOW + 3600},
            "seven_day": {"used_percentage": seven, "resets_at": NOW + 86400},
        },
        **extra,
    }).encode()


@pytest.fixture
def usage_file(tmp_path) -> Path:
    return tmp_path / "cache" / "usage.json"


# ----- the wrapper -------------------------------------------------------------


def test_the_wrapper_writes_both_windows_with_mode_0600(usage_file):
    assert usage.run_statusline(_payload(), None, usage_file, now=NOW) == 0

    data = json.loads(usage_file.read_text())
    assert data == {
        "five_hour": {"used_percent": 62.0, "resets_at": NOW + 3600},
        "seven_day": {"used_percent": 81.0, "resets_at": NOW + 86400},
        "written_at": NOW,
    }
    assert stat.S_IMODE(os.stat(usage_file).st_mode) == 0o600
    # No temporary file stays next to it.
    assert sorted(p.name for p in usage_file.parent.iterdir()) == ["usage.json"]


@pytest.mark.parametrize("stdin", [
    json.dumps({"model": {"display_name": "Opus"}}).encode(),
    json.dumps({"rate_limits": {}}).encode(),
    json.dumps({"rate_limits": {"five_hour": {"used_percentage": "a lot"}}}).encode(),
    b"not json",
    b"",
])
def test_a_payload_without_rate_limits_leaves_the_file(usage_file, stdin):
    usage.write_usage(usage_file, {"five_hour": {"used_percent": 10.0, "resets_at": None}}, now=NOW)
    before = usage_file.read_bytes()

    assert usage.run_statusline(stdin, None, usage_file, now=NOW + 60) == 0

    assert usage_file.read_bytes() == before


def test_the_user_command_gets_the_same_stdin_and_its_output_and_exit_code_pass_through(
    usage_file, capfd
):
    stdin = _payload()

    code = usage.run_statusline(stdin, "cat; echo user-line; echo to-stderr >&2; exit 7", usage_file)

    out, err = capfd.readouterr()
    assert code == 7
    assert out.encode() == stdin + b"user-line\n"
    assert err == "to-stderr\n"


def test_a_write_error_still_runs_the_command_unchanged(tmp_path, capfd):
    blocker = tmp_path / "a-file"
    blocker.write_text("")

    code = usage.run_statusline(_payload(), "cat >/dev/null; echo user-line", blocker / "usage.json")

    out, err = capfd.readouterr()
    assert (code, out, err) == (0, "user-line\n", "")


def test_percentages_are_clamped_and_reset_times_accept_iso_8601(usage_file):
    stdin = json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 140, "resets_at": "2027-01-15T10:00:00Z"},
        "seven_day": {"used_percentage": -3, "resets_at": "2027-01-15T10:00:00+02:00"},
    }}).encode()

    usage.run_statusline(stdin, None, usage_file, now=NOW)

    data = json.loads(usage_file.read_text())
    expected = datetime(2027, 1, 15, 10, tzinfo=timezone.utc).timestamp()
    assert data["five_hour"] == {"used_percent": 100.0, "resets_at": expected}
    assert data["seven_day"] == {"used_percent": 0.0, "resets_at": expected - 7200}


def test_reset_times_in_other_shapes():
    assert usage.resets_at(1_700_000_000) == 1_700_000_000.0
    assert usage.resets_at("1700000000") == 1_700_000_000.0
    # A time without a zone is UTC.
    assert usage.resets_at("2027-01-15T10:00:00") == datetime(
        2027, 1, 15, 10, tzinfo=timezone.utc
    ).timestamp()
    for value in (None, True, "", "soon", float("nan"), [1]):
        assert usage.resets_at(value) is None


def test_one_shell_word_is_the_command_and_several_are_quoted_back():
    assert usage.user_command([]) is None
    assert usage.user_command(["--"]) is None
    assert usage.user_command(["--", "cat >/dev/null; echo hi"]) == "cat >/dev/null; echo hi"
    assert usage.user_command(["echo", "a b", "$HOME"]) == "echo 'a b' '$HOME'"


def test_the_cli_wrapper_reads_the_usage_file_from_the_serve_table(
    tmp_path, usage_file, monkeypatch, capfd
):
    config = tmp_path / "config.toml"
    config.write_text(f'[serve]\nusage_file = "{usage_file}"\n')
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(_payload())))

    code = cli.main(["--config", str(config), "statusline", "--", "cat >/dev/null; echo user-line"])

    assert code == 0
    assert capfd.readouterr().out == "user-line\n"
    assert json.loads(usage_file.read_text())["five_hour"]["used_percent"] == 62.0


def test_a_broken_configuration_does_not_change_the_status_line(tmp_path, monkeypatch, capfd):
    config = tmp_path / "config.toml"
    config.write_text("[serve]\nusage_file = 5\n")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(_payload())))

    code = cli.main(["--config", str(config), "statusline", "--", "cat >/dev/null; echo user-line"])

    out, err = capfd.readouterr()
    assert (code, out, err) == (0, "user-line\n", "")


# ----- the reader --------------------------------------------------------------


def test_a_fresh_reading_has_each_window_with_its_level(usage_file):
    usage.run_statusline(_payload(62, 81), None, usage_file, now=NOW)

    reading = usage.read_usage(usage_file, now=NOW + 60)

    assert reading == {
        "known": True,
        "written_at": NOW,
        "age_secs": 60.0,
        "five_hour": {"used_percent": 62.0, "resets_at": NOW + 3600, "level": "amber"},
        "seven_day": {"used_percent": 81.0, "resets_at": NOW + 86400, "level": "red"},
    }


def test_a_reading_older_than_thirty_minutes_is_unknown(usage_file):
    usage.run_statusline(_payload(), None, usage_file, now=NOW)

    assert usage.read_usage(usage_file, now=NOW + USAGE_MAX_AGE_SECS)["known"] is True
    reading = usage.read_usage(usage_file, now=NOW + 31 * 60)

    assert reading["known"] is False
    assert reading["age_secs"] == 31 * 60
    for name in usage.WINDOWS:
        assert reading[name] == {"used_percent": None, "resets_at": None, "level": "grey"}


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"five_hour": {}}',
                                     '{"written_at": "now"}'])
def test_a_missing_or_unreadable_file_is_unknown(usage_file, content):
    if content is not None:
        usage_file.parent.mkdir(parents=True)
        usage_file.write_text(content)

    reading = usage.read_usage(usage_file, now=NOW)

    assert reading["known"] is False
    assert reading["five_hour"]["level"] == reading["seven_day"]["level"] == "grey"


def test_a_window_whose_reset_has_passed_is_unknown(usage_file):
    usage.write_usage(usage_file, {
        "five_hour": {"used_percent": 95.0, "resets_at": NOW + 60},
        "seven_day": {"used_percent": 81.0, "resets_at": NOW + 86400},
    }, now=NOW)

    reading = usage.read_usage(usage_file, now=NOW + 60)

    assert reading["known"] is True
    assert reading["five_hour"]["level"] == "grey"
    assert reading["seven_day"]["level"] == "red"


@pytest.mark.parametrize("percent, level", [
    (0, "green"), (59.9, "green"), (60, "amber"), (79.9, "amber"), (80, "red"), (100, "red"),
    (None, "grey"),
])
def test_levels(percent, level):
    assert usage.level(percent) == level


# ----- the configuration and the state -----------------------------------------


def test_the_serve_table_names_the_usage_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert ServeConfig().usage_file == tmp_path / ".cache" / "chargehand" / "usage.json"
    assert ServeConfig.parse({}).usage_file == ServeConfig().usage_file
    assert ServeConfig.parse({"usage_file": "~/u.json"}).usage_file == tmp_path / "u.json"


@pytest.mark.parametrize("value", [5, True, "", ["a"]])
def test_a_usage_file_that_is_not_a_path_is_refused(value):
    with pytest.raises(ConfigError, match="serve.usage_file"):
        ServeConfig.parse({"usage_file": value})


def test_the_usage_file_is_read_from_the_machine_configuration(tmp_path, repo, harness):
    path = write_machine_config(tmp_path / "c.toml", repo, harness.worktree_root)
    with path.open("a") as handle:
        handle.write(f'\n[serve]\nusage_file = "{tmp_path / "u.json"}"\n')

    assert load_machine_config(path).serve.usage_file == tmp_path / "u.json"


def test_the_state_carries_the_reading_and_follows_the_file(harness, tmp_path, usage_file):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    config = dataclasses.replace(
        config, serve=dataclasses.replace(config.serve, usage_file=usage_file)
    )
    daemon = support.build(config)
    with support.running(daemon, loop=False):
        status, body = support.request(daemon.port, "GET", "/api/state")
        assert status == 200
        assert json.loads(body)["usage"]["known"] is False

        now = time.time()
        usage.write_usage(usage_file, {
            "five_hour": {"used_percent": 62.0, "resets_at": now + 3600},
            "seven_day": {"used_percent": 81.0, "resets_at": now + 86400},
        }, now=now)
        status, body = support.request(daemon.port, "GET", "/api/state")
        reading = json.loads(body)["usage"]
        assert reading["known"] is True
        assert (reading["five_hour"]["used_percent"], reading["five_hour"]["level"]) == (62.0, "amber")
        assert (reading["seven_day"]["used_percent"], reading["seven_day"]["level"]) == (81.0, "red")

        usage.write_usage(usage_file, json.loads(usage_file.read_text()), now=now - 31 * 60)
        status, body = support.request(daemon.port, "GET", "/api/state")
        reading = json.loads(body)["usage"]
        assert reading["known"] is False
        assert reading["five_hour"]["level"] == "grey"
