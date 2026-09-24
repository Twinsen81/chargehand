"""The control surface. Mutating commands record a request; only a tick acts."""

from __future__ import annotations

import json

import pytest

from chargehand import __version__, ledger as ledger_mod
from chargehand.cli import EXIT_OK, EXIT_REFUSED, main


def run(argv, config_file):
    return main(["--config", str(config_file), *argv])


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help(capsys):
    assert main([]) == EXIT_OK
    assert "usage: chargehand" in capsys.readouterr().out


def test_tick_launches_a_queued_issue(capsys, config_file, harness):
    harness.board.add("ABC-1")

    assert run(["tick"], config_file) == EXIT_OK

    assert "launched: ABC-1" in capsys.readouterr().out
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_status_json_carries_no_issue_titles(capsys, config_file, harness):
    harness.board.add("ABC-1", title="secret internal title")
    run(["tick"], config_file)
    capsys.readouterr()

    assert run(["status", "--json"], config_file) == EXIT_OK

    payload = capsys.readouterr().out
    assert "secret internal title" not in payload
    status = json.loads(payload)
    assert status["attempts"][0]["issue"] == "ABC-1"
    assert status["runner"]["working"] == 1
    assert status["routes"][0]["queued"] == 0


def test_verbose_titles_are_opt_in(capsys, config_file, harness):
    harness.board.add("ABC-1", title="a title")

    run(["status", "--json", "--verbose-titles"], config_file)

    assert "a title" in capsys.readouterr().out


def test_status_renders_a_table_without_free_text(capsys, config_file, harness):
    harness.board.add("ABC-1", title="untrusted \x1b[31mtitle\x1b[0m")
    run(["tick"], config_file)
    capsys.readouterr()

    run(["status"], config_file)

    out = capsys.readouterr().out
    assert "ABC-1" in out
    assert "running" in out
    assert "\x1b[31m" not in out
    assert "untrusted" not in out


def test_a_blocked_run_shows_how_to_attach(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    harness.claude_state.set_session_state("ABC-1", "blocked", waitingFor="input needed")
    run(["tick"], config_file)
    capsys.readouterr()

    run(["status"], config_file)

    assert "claude attach" in capsys.readouterr().out


def test_logs_strip_terminal_control_sequences(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    harness.claude_state.set(logs="\x1b[2J\x1b]0;pwned\x07agent said something\n")
    capsys.readouterr()

    assert run(["logs", "ABC-1"], config_file) == EXIT_OK

    out = capsys.readouterr().out
    assert "agent said something" in out
    assert "\x1b" not in out
    assert "pwned" not in out


def test_cancel_goes_through_a_tick(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    capsys.readouterr()

    assert run(["cancel", "ABC-1"], config_file) == EXIT_OK

    assert harness.attempt("ABC-1").state == ledger_mod.CANCELLED
    assert "worktree kept" in capsys.readouterr().out


def test_a_refused_request_reports_a_non_zero_exit(capsys, config_file, harness):
    assert run(["cancel", "NOPE-1"], config_file) == EXIT_REFUSED
    assert "no attempt recorded" in capsys.readouterr().err


def test_discard_requires_an_explicit_yes(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    capsys.readouterr()

    assert run(["discard", "ABC-1"], config_file) == EXIT_REFUSED

    assert "Re-run with --yes" in capsys.readouterr().err
    assert (harness.worktree_root / "ABC-1").exists()


def test_discard_with_yes_removes_the_worktree(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)

    assert run(["discard", "ABC-1", "--yes"], config_file) == EXIT_OK

    assert not (harness.worktree_root / "ABC-1").exists()


def test_no_wait_only_records_the_request(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    capsys.readouterr()

    assert run(["cancel", "ABC-1", "--no-wait"], config_file) == EXIT_OK

    assert "recorded" in capsys.readouterr().out
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING
    assert [r["kind"] for r in harness.ledger.pending_requests()] == ["cancel"]


def test_pause_stops_the_next_tick_from_launching(capsys, config_file, harness):
    run(["pause"], config_file)
    harness.board.add("ABC-1")

    run(["tick"], config_file)

    assert harness.attempt("ABC-1") is None


def test_the_fault_injection_flag_is_reachable_from_the_cli(capsys, config_file, harness):
    harness.board.add("ABC-1")

    assert run(["tick", "--crash-after-step", "2"], config_file) != EXIT_OK

    assert "launch aborted after step 2" in capsys.readouterr().err
    assert harness.attempt("ABC-1").step == 2
    assert harness.attempt("ABC-1").state == ledger_mod.LAUNCHING


def test_a_second_tick_cannot_run_while_one_holds_the_lock(capsys, config_file, harness):
    from chargehand.tick import tick_lock

    with tick_lock():
        assert run(["tick"], config_file) != EXIT_OK

    assert "another chargehand tick is running" in capsys.readouterr().err


def test_a_broken_config_is_reported_not_traced(capsys, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("max_concurrent = 1\n")

    assert main(["--config", str(bad), "status"]) != EXIT_OK

    assert "at least one" in capsys.readouterr().err


def test_templates_can_be_listed_and_printed(capsys):
    assert main(["templates"]) == EXIT_OK
    listing = capsys.readouterr().out
    assert "chargehand.toml" in listing
    assert "skill/SKILL.md" in listing

    assert main(["templates", "assistant-settings.json"]) == EXIT_OK
    settings = json.loads(capsys.readouterr().out)
    assert "Bash(*chargehand cancel *)" in settings["permissions"]["ask"]
    assert "Bash(chargehand status:*)" in settings["permissions"]["allow"]


def test_status_json_does_not_carry_session_waiting_text(capsys, config_file, harness):
    """The starter skill allow-lists `status --json` and describes it as free of free text."""
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    harness.claude_state.set_session_state(
        "ABC-1", "blocked", waitingFor="permission: run `psql prod-acme`"
    )
    run(["tick"], config_file)
    capsys.readouterr()

    run(["status", "--json", "--no-queue"], config_file)

    payload = capsys.readouterr().out
    assert "prod-acme" not in payload
    status = json.loads(payload)
    assert status["attempts"][0]["waiting"] is True
    assert "waiting_for" not in status["attempts"][0]


def test_verbose_titles_reveal_the_waiting_text(capsys, config_file, harness):
    harness.board.add("ABC-1")
    run(["tick"], config_file)
    harness.claude_state.set_session_state("ABC-1", "blocked", waitingFor="needs a decision")
    run(["tick"], config_file)
    capsys.readouterr()

    run(["status", "--json", "--no-queue", "--verbose-titles"], config_file)

    assert "needs a decision" in capsys.readouterr().out
