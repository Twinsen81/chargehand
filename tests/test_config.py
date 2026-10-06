"""Configuration parsing and the strict template substitution both layers share."""

from __future__ import annotations

import pytest

from chargehand import config as config_mod
from chargehand.config import MachineConfig, RepoConfig, render, render_argv
from chargehand.errors import ConfigError

MINIMAL = {
    "route": [{"repo": "~/code/app", "tracker": {"type": "linear", "team": "ABC"}}],
}


def test_a_minimal_machine_config_gets_sensible_defaults():
    config = MachineConfig.parse(MINIMAL)

    assert config.max_concurrent == 2
    assert config.max_open_attempts == 4
    assert config.routes[0].name == "app"
    assert config.routes[0].tracker.type == "linear"
    assert config.labels.queued == "chargehand"
    assert config.pass_deny_rules_at_launch is True
    assert config.pool_bin == "banksman"


def test_the_pools_command_can_be_named():
    assert MachineConfig.parse({**MINIMAL, "pool_bin": "/opt/bin/banksman"}).pool_bin == (
        "/opt/bin/banksman"
    )
    with pytest.raises(ConfigError, match="pool_bin"):
        MachineConfig.parse({**MINIMAL, "pool_bin": 3})


def test_at_least_one_route_is_required():
    with pytest.raises(ConfigError, match="at least one"):
        MachineConfig.parse({})


def test_unknown_keys_are_rejected_rather_than_silently_ignored():
    with pytest.raises(ConfigError, match="unknown key"):
        MachineConfig.parse({**MINIMAL, "max_concurent": 3})


def test_route_names_must_be_unique():
    data = {"route": [
        {"name": "a", "repo": "/x", "tracker": {"type": "linear"}},
        {"name": "a", "repo": "/y", "tracker": {"type": "linear"}},
    ]}
    with pytest.raises(ConfigError, match="duplicate route"):
        MachineConfig.parse(data)


def test_open_attempts_must_not_starve_the_launch_throttle():
    with pytest.raises(ConfigError, match="max_open_attempts"):
        MachineConfig.parse({**MINIMAL, "max_concurrent": 4, "max_open_attempts": 2})


def test_the_hard_stop_must_come_after_the_notification_limit():
    with pytest.raises(ConfigError, match="hard_stop_hours"):
        MachineConfig.parse({**MINIMAL, "max_run_hours": 10, "hard_stop_hours": 6})


def test_the_three_labels_must_differ():
    with pytest.raises(ConfigError, match="distinct"):
        MachineConfig.parse({**MINIMAL, "labels": {"queued": "x", "running": "x"}})


def test_a_route_can_override_the_labels():
    config = MachineConfig.parse({
        "labels": {"queued": "q", "running": "r", "blocked": "b"},
        "route": [{"repo": "/x", "tracker": {"type": "linear"},
                   "labels": {"blocked": "held"}}],
    })

    assert config.routes[0].labels.as_dict() == {"queued": "q", "running": "r", "blocked": "held"}


def test_a_repo_config_needs_a_base_and_a_prompt():
    with pytest.raises(ConfigError, match="prompt"):
        RepoConfig.parse({"base": "origin/main"})
    with pytest.raises(ConfigError, match="base"):
        RepoConfig.parse({"prompt": "{issue}"})


@pytest.mark.parametrize("variable", ["{title}", "{body}", "{description}"])
def test_issue_text_cannot_be_put_into_the_prompt(variable):
    with pytest.raises(ConfigError, match="untrusted"):
        RepoConfig.parse({"base": "origin/main", "prompt": f"work on {variable}"})


def test_an_unknown_permission_mode_is_rejected():
    with pytest.raises(ConfigError, match="unknown mode"):
        RepoConfig.parse({"base": "origin/main", "prompt": "x", "permission_mode": "yolo"})


def test_a_branch_prefix_cannot_escape_the_refs_namespace():
    with pytest.raises(ConfigError, match="branch_prefix"):
        RepoConfig.parse({"base": "o/m", "prompt": "x", "branch_prefix": "../../"})


def test_render_rejects_an_unknown_variable():
    with pytest.raises(ConfigError, match="unknown template variable"):
        render("do {nonsense}", {"issue": "ABC-1"}, where="test")


def test_render_argv_splits_before_substituting():
    argv = render_argv("setup.sh {worktree}", {"worktree": "/a path/with spaces"}, where="t")

    assert argv == ["setup.sh", "/a path/with spaces"]


def test_render_argv_does_not_let_a_value_become_more_arguments():
    argv = render_argv("run.sh {issue}", {"issue": "A; rm -rf /"}, where="t")

    assert argv == ["run.sh", "A; rm -rf /"]


def test_the_available_template_variables_exclude_issue_text():
    assert "title" not in config_mod.TEMPLATE_VARS
    assert "body" not in config_mod.TEMPLATE_VARS
    assert set(config_mod.TEMPLATE_VARS) == {"issue", "url", "worktree", "branch", "repo"}


def test_loading_a_missing_repo_config_names_the_fix(tmp_path):
    with pytest.raises(ConfigError, match="chargehand init"):
        config_mod.load_repo_config(tmp_path)


def test_a_bad_toml_file_reports_the_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        config_mod.load_machine_config(path)


def test_the_bundled_templates_parse(tmp_path):
    from chargehand import paths

    machine = (paths.templates_dir() / "config.toml").read_text()
    repo = (paths.templates_dir() / "chargehand.toml").read_text()
    (tmp_path / "config.toml").write_text(machine)
    (tmp_path / ".chargehand.toml").write_text(repo)

    assert config_mod.load_machine_config(tmp_path / "config.toml").routes
    assert config_mod.load_repo_config(tmp_path).prompt


@pytest.mark.parametrize("key", ["prompt", "setup", "status_file"])
def test_a_template_typo_is_rejected_when_the_config_is_read(key):
    """Left to launch time it surfaces three attempts later, looking like an outage."""
    data = {"base": "origin/main", "prompt": "{issue}", key: "x {worktre} y"}
    with pytest.raises(ConfigError, match=r"unknown template variable \{worktre\}"):
        RepoConfig.parse(data)


def test_the_error_names_the_variables_that_do_exist():
    with pytest.raises(ConfigError, match="worktree"):
        RepoConfig.parse({"base": "o/m", "prompt": "{issue}", "setup": "s.sh {worktre}"})


def test_an_unparseable_setup_command_is_rejected_at_parse_time():
    with pytest.raises(ConfigError, match="cannot parse command"):
        RepoConfig.parse({"base": "o/m", "prompt": "{issue}", "setup": 'sh -c "unclosed'})


def test_a_valid_template_still_parses():
    config = RepoConfig.parse({
        "base": "origin/main",
        "prompt": "/work {issue} {url}",
        "setup": "scripts/setup.sh {worktree} {branch}",
        "status_file": "/tmp/chargehand/{issue}/status.json",
    })

    assert config.setup.startswith("scripts/setup.sh")
