"""The launch-time deny rules, and the model of Claude Code's matcher they rely on.

Nothing here runs Claude Code. It pins two things: that the shipped rules cover the
forms a session could reach the control plane through, and that the matcher these
assertions are written against still agrees with the documented examples. The live
arms of ``probes/deny_rules.py`` are what confirm the model matches reality; these
tests are what stop the rule list quietly narrowing between probe runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from chargehand.config import DEFAULT_DENY_RULES
from deny_rules import (
    ALLOWED,
    BLOCKED,
    INCONCLUSIVE,
    Attempt,
    Outcome,
    battery,
    build_scratch,
    first_matching_rule,
    rule_matches,
)

# The rows of the documented wildcard table. If Claude Code's syntax ever changes,
# these are what should fail first.
DOCUMENTED = (
    ("Bash(npm run build)", "npm run build", True),
    ("Bash(npm run build)", "npm run build --watch", False),
    ("Bash(npm run *)", "npm run build", True),
    ("Bash(npm run *)", "npm run", True),
    ("Bash(npm run *)", "npm install", False),
    ("Bash(git log * main)", "git log --oneline main", True),
    ("Bash(git log * main)", "git log main", False),
    ("Bash(git * main)", "git push origin main", True),
    ("Bash(git * main)", "git log", False),
    ("Bash(* --version)", "node --version", True),
    ("Bash(* --version)", "bash -c 'echo hi' --version", True),
    ("Bash(* --version)", "node -v", False),
    ("Bash(ls *)", "ls -la", True),
    ("Bash(ls *)", "ls", True),
    ("Bash(ls *)", "lsof", False),
    ("Bash(ls*)", "lsof", True),
    ("Bash(* --help *)", "npm --help x", True),
    ("Bash(* --help *)", "npm --help", False),
)


@pytest.mark.parametrize("rule,command,expected", DOCUMENTED)
def test_the_matcher_agrees_with_the_documented_examples(rule, command, expected):
    assert rule_matches(rule, command) is expected


def test_the_colon_star_suffix_is_a_trailing_wildcard():
    assert rule_matches("Bash(ls:*)", "ls -la")
    assert rule_matches("Bash(ls:*)", "ls")
    assert not rule_matches("Bash(ls:*)", "lsof")


def test_a_deny_rule_matches_any_subcommand():
    assert rule_matches("Bash(*chargehand *)", "cd /tmp && chargehand cancel ABC-1")


def test_a_deny_rule_matches_past_a_wrapper_and_an_assignment():
    assert rule_matches("Bash(*chargehand *)", "timeout 30 chargehand cancel ABC-1")
    assert rule_matches("Bash(*chargehand *)", "FOO=bar chargehand cancel ABC-1")


# ----- the shipped rule set -------------------------------------------------


def test_every_attempt_in_the_battery_is_covered_or_is_the_control(tmp_path):
    for attempt in battery(tmp_path):
        matched = first_matching_rule(DEFAULT_DENY_RULES, attempt.command)
        if attempt.expect_blocked:
            assert matched, f"no rule covers {attempt.name}: {attempt.command}"
        else:
            assert matched is None, f"the control attempt is denied by {matched}"


def test_the_control_survives_a_scratch_path_that_mentions_the_tool(tmp_path):
    """The probe's own working directory must not trip a rule.

    A scratch directory named after the tool would deny the control command, the probe
    would call itself broken, and the real verdicts would never be reached.
    """
    scratch = tmp_path / "chargehand-scratch"
    scratch.mkdir()
    control = next(a for a in battery(scratch) if not a.expect_blocked)
    assert first_matching_rule(DEFAULT_DENY_RULES, control.command) is None


@pytest.mark.parametrize(
    "command",
    (
        "chargehand cancel ABC-1",
        "/usr/local/bin/chargehand cancel ABC-1",
        "sh -c 'chargehand cancel ABC-1'",
        "python3 -m chargehand cancel ABC-1",
    ),
)
def test_the_cli_is_denied_however_it_is_spelled(command):
    assert first_matching_rule(DEFAULT_DENY_RULES, command)


def test_an_anchored_rule_would_have_missed_the_path_and_the_wrapper():
    """Why the shipped patterns lead with a wildcard.

    Claude Code matches the command text it writes, not the program behind it, so an
    anchored rule stops the spelling a session usually produces and no other. This is
    the gap the leading wildcard closes, and the reason it is not cosmetic.
    """
    anchored = ("Bash(chargehand:*)",)
    assert first_matching_rule(anchored, "chargehand cancel ABC-1")
    assert first_matching_rule(anchored, "/usr/local/bin/chargehand cancel ABC-1") is None
    assert first_matching_rule(anchored, "sh -c 'chargehand cancel ABC-1'") is None


def test_stop_is_denied_under_its_kill_alias():
    """`claude kill` is `claude stop`, so denying one and not the other denies nothing."""
    assert first_matching_rule(DEFAULT_DENY_RULES, "claude kill abc123")


def test_a_session_cannot_launch_an_unrestricted_sibling():
    for command in (
        "claude -p --dangerously-skip-permissions 'go'",
        "claude --allow-dangerously-skip-permissions",
        "claude --bg --permission-mode bypassPermissions 'go'",
    ):
        assert first_matching_rule(DEFAULT_DENY_RULES, command), command


# ----- the harness ----------------------------------------------------------


def test_the_decoys_record_that_they_ran(tmp_path):
    bin_dir = build_scratch(tmp_path)
    marker = tmp_path / "marker.log"
    assert not marker.exists()

    subprocess.run([str(bin_dir / "chargehand"), "cancel", "ABC-1"], check=True)

    assert "cancel ABC-1" in marker.read_text()
    assert os.access(bin_dir / "claude", os.X_OK)


def test_the_module_form_records_that_it_ran(tmp_path):
    """`python3 -m chargehand` never reaches the script on PATH.

    Without a decoy package it fails for its own reasons in both arms, which reads as
    an inconclusive verdict rather than the unobservable attempt it actually is.
    """
    build_scratch(tmp_path)
    marker = tmp_path / "marker.log"

    subprocess.run(
        [sys.executable, "-m", "chargehand", "cancel", "ABC-1"],
        check=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "pymod")},
    )

    assert "cancel ABC-1" in marker.read_text()


def _outcome(*, without: bool, with_: bool, expect_blocked: bool = True) -> Outcome:
    attempt = Attempt("x", "cmd", "why", expect_blocked=expect_blocked)
    return Outcome(attempt, ran_without_rules=without, ran_with_rules=with_)


def test_a_verdict_needs_both_arms():
    assert _outcome(without=True, with_=False).verdict == BLOCKED
    assert _outcome(without=True, with_=True).verdict == ALLOWED
    # Refused in both arms means the model declined before any rule applied, which
    # says nothing about the rule.
    assert _outcome(without=False, with_=False).verdict == INCONCLUSIVE


def test_the_probe_ships_outside_the_wheel():
    """It drives a real Claude Code install, so it is a repository tool, not a feature."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "probes" / "deny_rules.py").exists()
    assert not (root / "src" / "chargehand" / "deny_rules.py").exists()


# ----- configuration --------------------------------------------------------


def test_a_malformed_rule_is_rejected_when_the_config_is_read():
    """A dropped settings payload launches sessions with no rules and says nothing.

    Claude Code ignores a settings file that fails validation without a word in a
    non-interactive run, and every session the runner starts is non-interactive.
    """
    from chargehand.config import MachineConfig
    from chargehand.errors import ConfigError

    data = {
        "route": [{"repo": "/x", "tracker": {"type": "linear", "team": "ABC"}}],
        "deny_rules": ["Bash(*claude stop*"],
    }
    with pytest.raises(ConfigError, match="not a permission rule"):
        MachineConfig.parse(data)


def test_the_shipped_rules_pass_their_own_validation():
    from chargehand.config import validate_deny_rule

    for rule in DEFAULT_DENY_RULES:
        validate_deny_rule(rule)


def test_the_template_ships_the_same_rules_the_runner_passes():
    """The template is what an operator copies into their own settings; a template that
    has drifted from the code hands out a weaker rule set than the runner uses."""
    import json

    from chargehand import paths

    shipped = json.loads((paths.templates_dir() / "launch-deny-rules.json").read_text())
    assert shipped["permissions"]["deny"] == list(DEFAULT_DENY_RULES)

