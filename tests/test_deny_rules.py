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

from chargehand.config import DEFAULT_DENY_RULES, serve_deny_rules
from deny_rules import (
    ALLOWED,
    BLOCKED,
    BODY_DIR,
    INCONCLUSIVE,
    LAUNCH_RULES,
    PR,
    RAN,
    REFUSED,
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
        matched = first_matching_rule(LAUNCH_RULES, attempt.command)
        if attempt.expect_blocked:
            assert matched, f"no rule covers {attempt.name}: {attempt.command}"
        else:
            assert matched is None, f"{attempt.name} is denied by {matched}"


def test_the_control_survives_a_scratch_path_that_mentions_the_tool(tmp_path):
    """The probe's own working directory must not trip a rule.

    A scratch directory named after the tool would deny the control command, the probe
    would call itself broken, and the real verdicts would never be reached.
    """
    scratch = tmp_path / "chargehand-scratch"
    scratch.mkdir()
    control = next(a for a in battery(scratch) if not a.expect_blocked)
    assert first_matching_rule(LAUNCH_RULES, control.command) is None


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


@pytest.mark.parametrize(
    "command",
    (
        "banksman admin discover --all --yes",
        "/opt/homebrew/bin/banksman admin release --force --resource emulator-5554",
        "sh -c 'banksman admin discover --all'",
    ),
)
def test_the_lease_tools_operator_commands_are_denied(command):
    assert first_matching_rule(DEFAULT_DENY_RULES, command) == "Bash(*banksman admin*)"


@pytest.mark.parametrize(
    "command",
    (
        "banksman acquire --where form=tablet --for 'check the layout'",
        "banksman status --json",
        "banksman touch --lease 3f2a",
        "banksman release --lease 3f2a",
        "banksman enter --resource emulator-5554 --lease 3f2a --pid 4242",
    ),
)
def test_agents_keep_the_lease_tool_commands_their_scripts_use(command):
    assert first_matching_rule(DEFAULT_DENY_RULES, command) is None


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
    assert os.access(bin_dir / "banksman", os.X_OK)


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


def test_a_form_that_must_pass_fails_when_only_the_rules_refuse_it():
    assert _outcome(without=True, with_=True, expect_blocked=False).verdict == RAN
    assert _outcome(without=True, with_=False, expect_blocked=False).verdict == REFUSED
    assert _outcome(without=False, with_=False, expect_blocked=False).verdict == INCONCLUSIVE


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
    assert shipped["permissions"]["deny"] == list(LAUNCH_RULES)


# ----- the daemon's port ----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    (
        "curl -s http://127.0.0.1:7878/api/state",
        "curl -X POST -H 'Authorization: Bearer x' http://localhost:7878/api/anything",
        "/usr/bin/curl 127.0.0.1:7878",
        "sh -c 'curl -s localhost:7878/api/health'",
        "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:7878/')\"",
    ),
)
def test_the_daemons_port_is_denied_however_it_is_reached(command):
    assert first_matching_rule(LAUNCH_RULES, command), command


@pytest.mark.parametrize(
    "command",
    (
        "curl -s http://127.0.0.1:3000/",
        "npm run dev -- --port 8080",
        "curl https://example.com/7878",
    ),
)
def test_a_session_keeps_its_own_local_servers(command):
    assert first_matching_rule(LAUNCH_RULES, command) is None


def test_the_port_rules_follow_the_configured_port():
    from chargehand.config import MachineConfig

    data = {
        "route": [{"repo": "/x", "tracker": {"type": "linear", "team": "ABC"}}],
        "serve": {"port": 9123},
    }
    rules = MachineConfig.parse(data).deny_rules

    assert first_matching_rule(rules, "curl -s http://127.0.0.1:9123/api/state")
    assert first_matching_rule(rules, "curl -s http://localhost:9123/api/state")
    assert first_matching_rule(rules, "curl -s http://127.0.0.1:7878/api/state") is None
    assert set(DEFAULT_DENY_RULES) <= set(rules)
    assert "WebFetch(domain:127.0.0.1)" in rules and "WebFetch(domain:localhost)" in rules


def test_an_operators_own_rule_list_still_gets_the_port_rules():
    """A list written before the daemon existed must not leave its port open to sessions."""
    from chargehand.config import MachineConfig

    data = {
        "route": [{"repo": "/x", "tracker": {"type": "linear", "team": "ABC"}}],
        "deny_rules": ["Bash(*chargehand *)"],
    }

    assert MachineConfig.parse(data).deny_rules == ("Bash(*chargehand *)", *serve_deny_rules(7878))


def test_the_port_rules_pass_their_own_validation():
    from chargehand.config import validate_deny_rule

    for rule in serve_deny_rules(7878):
        validate_deny_rule(rule)



@pytest.mark.parametrize(
    "command",
    (
        'gh pr comment 1 --body "Fixed.\n\n<!-- chargehand-worker -->"',
        "gh pr review https://github.com/o/r/pull/1 --comment --body 'Looks fine. <!-- chargehand-worker -->'",
        'gh pr create --base main --title "Note" --body "Test.\n\n<!-- chargehand-worker -->"',
    ),
)
def test_a_worker_may_post_a_comment_that_carries_the_marker(command):
    # Workers post from a body file, but a body that still ends up on a command line
    # must not be refused for its marker alone.
    assert first_matching_rule(DEFAULT_DENY_RULES, command) is None


def test_the_first_marker_spelling_is_refused_on_a_command_line():
    # Why the marker changed: the name followed by a space is what the rules refuse.
    assert first_matching_rule(DEFAULT_DENY_RULES, 'gh pr comment 1 --body "ok <!-- chargehand -->"')


# ----- posting from a body file ---------------------------------------------


def _shipped_role(name: str):
    import tomllib

    from chargehand import paths
    from chargehand.roles import Role

    text = (paths.templates_dir() / "roles" / f"{name}.toml").read_text(encoding="utf-8")
    return Role.parse(name, tomllib.loads(text))


@pytest.mark.parametrize(
    "role,command",
    (
        ("pr-reviewer", f"gh pr comment {PR} --body-file {BODY_DIR}/o-r-1-1.md"),
        ("pr-reviewer", f"gh pr review {PR} --comment --body-file {BODY_DIR}/o-r-1-2.md"),
        ("implementer", f"gh pr comment {PR} --body-file {BODY_DIR}/item-1.md"),
        ("implementer", f"gh pr create --base main --title 'Fix' --body-file {BODY_DIR}/item-2.md"),
        ("implementer", f"gh pr create --body-file {BODY_DIR}/item-3.md --title 'Fix'"),
    ),
)
def test_the_body_file_forms_pass_the_deny_rules_and_match_the_roles_allow_rules(role, command):
    assert first_matching_rule(LAUNCH_RULES, command) is None
    assert first_matching_rule(_shipped_role(role).allow, command), command


def test_the_body_file_forms_in_the_battery_pass_every_deny_rule(tmp_path):
    forms = [a for a in battery(tmp_path) if a.name.startswith("body-file-")]

    assert {a.name for a in forms} == {
        "body-file-comment", "body-file-review", "body-file-create", "body-file-api",
    }
    for attempt in forms:
        assert not attempt.expect_blocked
        assert first_matching_rule(LAUNCH_RULES, attempt.command) is None, attempt.command


def test_a_reply_through_gh_api_passes_the_deny_rules_and_no_allow_rule():
    """The reply goes to the classifier: no role allows `gh api`."""
    command = f"gh api repos/o/r/pulls/1/comments/2/replies -F body=@{BODY_DIR}/o-r-1-3.md"

    assert first_matching_rule(LAUNCH_RULES, command) is None
    for role in ("pr-reviewer", "implementer"):
        assert first_matching_rule(_shipped_role(role).allow, command) is None


def test_a_reply_rule_for_gh_api_would_also_allow_a_merge():
    """Why the reviewer has no `gh api` rule, not even one for the reply endpoint.

    A `*` matches spaces too, so the endpoint pattern stretches over a different call
    that carries the rest of the pattern in its arguments.
    """
    reply_rule = ("Bash(gh api repos/*/comments/*/replies -F body=@*)",)
    merge = (
        "gh api repos/o/r/pulls/1/merge -X PUT -f note=x/comments/2/replies "
        f"-F body=@{BODY_DIR}/x.md"
    )

    assert first_matching_rule(reply_rule, merge)


@pytest.mark.parametrize(
    "body",
    (
        "This changes the bypassPermissions rule.",
        "Run claude stop on the old session first.",
        "The chargehand status output is wrong here.",
        "Keep the dangerously-skip-permissions flag out of the launcher.",
    ),
)
def test_an_inline_body_that_names_a_denied_word_is_refused(body):
    """Why workers post from a file: the rules match the whole command text, the body
    included, and a file's content never reaches them."""
    assert first_matching_rule(LAUNCH_RULES, f"gh pr comment {PR} --body '{body}'")
    assert first_matching_rule(LAUNCH_RULES, f"gh pr comment {PR} --body-file {BODY_DIR}/x.md") is None


def test_the_battery_refuses_an_inline_body_that_names_bypass_permissions(tmp_path):
    (inline,) = [a for a in battery(tmp_path) if a.name == "inline-body"]

    assert inline.expect_blocked and "bypassPermissions" in inline.command
    assert first_matching_rule(LAUNCH_RULES, inline.command) == "Bash(*bypassPermissions*)"


def test_the_body_directory_is_per_user_and_outside_every_checkout():
    # Under the home directory, so a body file never lands in a worktree or an item
    # checkout, and other users do not share it.
    assert BODY_DIR.startswith("~/")
    for role in ("pr-reviewer", "implementer"):
        allow = _shipped_role(role).allow
        assert f"Edit({BODY_DIR}/**)" in allow, role
        assert f"Bash(mkdir -p -m 700 {BODY_DIR})" in allow, role
        assert first_matching_rule(LAUNCH_RULES, f"mkdir -p -m 700 {BODY_DIR}") is None
        # No rule deletes: a wildcard would also match a second path.
        assert not any(rule.startswith("Bash(rm") for rule in allow), role
