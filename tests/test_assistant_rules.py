"""Cover the assistant permission rules without a real Claude Code install.

The probe needs a real install, so it cannot run here. What can run here is the claim the
rules make, checked against the same model of the matcher the deny-rule tests use. Without
this, someone tidying `assistant-settings.json` back into the anchored style would
reintroduce the hole and nothing would notice until the probe was next run by hand.
"""

from __future__ import annotations

import json

import pytest

from chargehand import paths

import assistant_rules
from deny_rules import first_matching_rule, rule_matches


@pytest.fixture
def shipped():
    return json.loads(
        (paths.templates_dir() / "assistant-settings.json").read_text(encoding="utf-8")
    )


MUTATING = ("cancel", "stop", "continue", "retry", "discard", "pause", "resume", "tick",
            "install", "uninstall", "init")

# The spellings an anchored rule misses. None of them is an evasion: the second is what an
# assistant produces after running `which chargehand`, and the third is what it produces
# when it wraps a command for any ordinary reason.
SPELLINGS = (
    "chargehand {command} ABC-1",
    "/usr/local/bin/chargehand {command} ABC-1",
    "sh -c 'chargehand {command} ABC-1'",
    "cd /tmp && chargehand {command} ABC-1",
)


def test_every_mutating_command_has_an_ask_rule(shipped):
    asked = " ".join(shipped["permissions"]["ask"])
    missing = [command for command in MUTATING if f"chargehand {command}" not in asked]
    assert not missing, f"no ask rule for: {missing}"


@pytest.mark.parametrize("command", MUTATING)
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_mutating_command_is_gated_in_every_spelling(shipped, command, spelling):
    rules = tuple(shipped["permissions"]["ask"])
    text = spelling.format(command=command)

    assert first_matching_rule(rules, text) is not None, text


@pytest.mark.parametrize("command", ("pause", "resume", "tick", "install", "uninstall", "init"))
def test_a_command_with_no_argument_is_gated_too(shipped, command):
    """`chargehand pause` takes no issue, so a rule ending in a space would miss it."""
    rules = tuple(shipped["permissions"]["ask"])

    assert first_matching_rule(rules, f"chargehand {command}") is not None


def test_no_ask_rule_is_anchored_on_the_program_name(shipped):
    """An anchored rule matches the bare form only, which is the hole this file guards."""
    anchored = [
        rule for rule in shipped["permissions"]["ask"]
        if not rule.startswith("Bash(*")
    ]
    assert not anchored, f"anchored, so a path or a wrapper slips past: {anchored}"


def test_reads_are_allowed_rather_than_asked(shipped):
    allowed = " ".join(shipped["permissions"]["allow"])
    assert "chargehand status" in allowed
    assert "chargehand logs" in allowed


def test_a_read_is_not_caught_by_a_mutating_rule(shipped):
    """A prompt on every `status` would train the operator to approve without reading."""
    rules = tuple(shipped["permissions"]["ask"])

    assert first_matching_rule(rules, "chargehand status --json") is None
    assert first_matching_rule(rules, "chargehand logs ABC-1") is None


def test_uninstall_is_not_swallowed_by_the_install_rule(shipped):
    """`install` and `uninstall` are different commands and each needs its own rule."""
    rules = tuple(shipped["permissions"]["ask"])
    install_rule = first_matching_rule(rules, "chargehand install --load")
    uninstall_rule = first_matching_rule(rules, "chargehand uninstall")

    assert install_rule is not None and uninstall_rule is not None
    assert not rule_matches("Bash(*chargehand install*)", "chargehand uninstall")


def test_an_unrelated_command_is_not_gated(shipped):
    rules = tuple(shipped["permissions"]["ask"])

    assert first_matching_rule(rules, "git status") is None
    assert first_matching_rule(rules, "ls -la") is None


# ----- the probe's own verdict logic ----------------------------------------


def outcome(name, *, expect_gated=True, is_control=False, control_arm=True, shipped_arm=False):
    attempt = assistant_rules.Attempt(
        name, f"chargehand {name}", "why", expect_gated=expect_gated, is_control=is_control
    )
    result = assistant_rules.Outcome(attempt)
    result.ran_in_control_arm = control_arm
    result.ran_with_shipped_rules = shipped_arm
    return result


def test_a_mutation_that_ran_under_the_shipped_rules_is_a_failure():
    assert outcome("cancel", shipped_arm=True).verdict == assistant_rules.NOT_GATED


def test_a_mutation_that_did_not_run_is_gated():
    assert outcome("cancel", shipped_arm=False).verdict == assistant_rules.GATED


def test_a_read_that_did_not_run_is_a_failure():
    """A read that prompts is a failure of a different kind, and still a failure."""
    assert outcome("status", expect_gated=False, shipped_arm=False).verdict == \
        assistant_rules.PROMPTED


def test_a_read_that_ran_is_silent():
    assert outcome("status", expect_gated=False, shipped_arm=True).verdict == \
        assistant_rules.SILENT


def test_a_command_that_never_ran_even_when_allowed_is_inconclusive():
    """The model declining and a rule firing mean opposite things and must not be confused."""
    assert outcome("cancel", control_arm=False).verdict == assistant_rules.INCONCLUSIVE


def test_a_control_that_did_not_run_means_the_harness_is_broken():
    result = outcome("probe-control", expect_gated=False, is_control=True, control_arm=False)
    assert result.verdict == assistant_rules.BROKEN


def test_the_battery_covers_the_spellings_an_anchor_misses():
    commands = [a.command for a in assistant_rules.battery(paths.templates_dir())]
    assert any(c.startswith("sh -c ") for c in commands)
    assert any("/chargehand cancel" in c for c in commands)
    assert any(c == "chargehand pause" for c in commands)
