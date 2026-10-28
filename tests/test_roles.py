"""Roles: what a role file may say, where it is looked up, and the pre-roles fallback."""

from __future__ import annotations

import pytest

from chargehand import roles as roles_mod
from chargehand.claude import launch_settings
from chargehand.config import DEFAULT_DENY_RULES, RepoConfig
from chargehand.errors import ConfigError
from chargehand.roles import Role, StartRule, load_role

FULL = """\
agent = "reviewer"
model = "opus"
effort = "high"
prompt = "/pr-review {item_url}"
permission_mode = "auto"
allow = ["Bash(gh pr *)", "Bash(gh api *)"]
checkout = "item"
status_file = "/tmp/x/{item}/status.json"
setup = "scripts/prepare.sh {worktree}"

[[start]]
on = "github.review_requested"
repositories = ["my-app"]
subscribe = ["comment", "review"]
close_on = ["pr_closed"]

[[start]]
name = "mention"
on = "github.mention"
unless_subscribed = "any"

[policy]
max_workers = 3
max_wakes_per_day = 6
debounce_secs = 30
refresh_after_tokens = 120000
"""


def write_role(repo, name, text):
    target = repo / ".chargehand" / "roles" / f"{name}.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def write_agent(repo, name):
    target = repo / ".claude" / "agents" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nname: {name}\n---\nBe the {name}.\n")
    return target


def test_a_full_role_file_parses(tmp_path):
    write_role(tmp_path, "pr-reviewer", FULL)
    write_agent(tmp_path, "reviewer")

    role = load_role(tmp_path, "pr-reviewer")

    assert role.agent == "reviewer"
    assert role.model == "opus"
    assert role.effort == "high"
    assert role.allow == ("Bash(gh pr *)", "Bash(gh api *)")
    assert role.checkout == "item"
    assert role.runtime == "claude"
    first, second = role.start
    assert (first.name, first.unless_subscribed) == ("github.review_requested", "role")
    assert first.subscribe == ("comment", "review") and first.close_on == ("pr_closed",)
    assert (second.name, second.unless_subscribed) == ("mention", "any")
    assert role.start_rule_for("review_requested", "github", "my-app") is first
    assert role.start_rule_for("review_requested", "github", "elsewhere") is None
    assert role.start_rule_for("mention", "github", "elsewhere") is second
    assert role.start_rule_for("labeled", "linear", "my-app") is None
    assert role.policy.max_workers == 3
    assert role.policy.max_wakes_per_day == 6
    assert role.policy.refresh_after_tokens == 120000
    assert role.policy.refresh_after_wakes is None


def test_an_unqualified_start_kind_matches_every_source():
    rule = StartRule(on="labeled")

    assert rule.name == "labeled"
    assert rule.matches("labeled", "linear")
    assert rule.matches("labeled", "github")
    assert rule.matches("labeled", None)
    assert not StartRule(on="linear.labeled").matches("labeled", "github")


def test_the_repository_file_wins_over_the_machine_file(tmp_path, monkeypatch):
    machine = tmp_path / "config"
    monkeypatch.setenv("CHARGEHAND_CONFIG", str(machine / "config.toml"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (machine / "roles").mkdir(parents=True)
    (machine / "roles" / "implementer.toml").write_text('prompt = "machine {item}"\n')

    assert load_role(repo, "implementer").prompt == "machine {item}"

    write_role(repo, "implementer", 'prompt = "repo {item}"\n')
    assert load_role(repo, "implementer").prompt == "repo {item}"


def test_a_repository_without_a_role_file_gets_implementer_from_its_old_config(tmp_path):
    repo_config = RepoConfig(base="origin/main", prompt="/work {issue}", permission_mode="auto",
                             status_file="/tmp/{issue}/status.json")

    role = load_role(tmp_path, "implementer", repo_config=repo_config, tracker_type="linear")

    assert role.prompt == "/work {issue}"
    assert role.status_file == "/tmp/{issue}/status.json"
    assert [rule.name for rule in role.start] == [roles_mod.QUEUE_RULE]
    assert role.start_rule_for("labeled", "linear") is not None
    assert role.start_rule_for("labeled", "github") is None


def test_the_shipped_implementer_starts_follows_and_closes_like_the_old_config():
    """`chargehand init` writes the template; a pre-roles repository gets legacy_role. A
    queue worker must hear the same events and end the same way under either."""
    import tomllib

    from chargehand import paths

    path = paths.templates_dir() / "roles" / "implementer.toml"
    shipped = Role.parse("implementer", tomllib.loads(path.read_text(encoding="utf-8")))
    legacy = roles_mod.legacy_role(RepoConfig(base="origin/main", prompt="{issue}"))

    (template_rule,) = shipped.start
    (legacy_rule,) = legacy.start
    assert template_rule.on == legacy_rule.on == roles_mod.LABELED
    assert template_rule.subscribe == legacy_rule.subscribe == (roles_mod.COMMENT,)
    assert template_rule.close_on == legacy_rule.close_on == (roles_mod.DONE,)
    assert template_rule.unless_subscribed == legacy_rule.unless_subscribed


def test_the_shipped_implementer_posts_bodies_from_a_file():
    """A body on the command line that names a denied word is refused; a file is not read
    by the rules."""
    import tomllib

    from chargehand import paths

    path = paths.templates_dir() / "roles" / "implementer.toml"
    shipped = Role.parse("implementer", tomllib.loads(path.read_text(encoding="utf-8")))

    assert "mkdir -p -m 700 ~/.cache/gh-body" in shipped.prompt
    assert "Write tool" in shipped.prompt and "delete the file" in shipped.prompt
    assert "--body-file" in shipped.prompt and "-F body=@<file>" in shipped.prompt
    assert shipped.allow == (
        "Bash(mkdir -p -m 700 ~/.cache/gh-body)",
        "Edit(~/.cache/gh-body/**)",
        "Bash(gh pr create *--body-file *)",
        "Bash(gh pr comment *--body-file *)",
    )


def test_only_implementer_falls_back_to_the_old_config(tmp_path):
    repo_config = RepoConfig(base="origin/main", prompt="{issue}")

    with pytest.raises(ConfigError, match="no role named 'pr-reviewer'"):
        load_role(tmp_path, "pr-reviewer", repo_config=repo_config)


def test_a_missing_role_names_where_it_looked(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_role(tmp_path, "ghost", repo_config=RepoConfig(base="origin/main"))

    message = str(caught.value)
    assert ".chargehand/roles/ghost.toml" in message
    assert "chargehand init" in message


def test_an_agent_the_role_names_must_exist(tmp_path):
    write_role(tmp_path, "implementer", 'agent = "nobody"\nprompt = "{item}"\n')

    with pytest.raises(ConfigError, match="agent 'nobody' not found"):
        load_role(tmp_path, "implementer")

    write_agent(tmp_path, "nobody")
    assert load_role(tmp_path, "implementer").agent == "nobody"


def test_the_agent_may_live_in_the_user_directory(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    (claude_dir / "agents").mkdir(parents=True)
    (claude_dir / "agents" / "shared.md").write_text("---\nname: shared\n---\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    write_role(tmp_path, "implementer", 'agent = "shared"\nprompt = "{item}"\n')

    assert load_role(tmp_path, "implementer").agent == "shared"


@pytest.mark.parametrize(
    "text, match",
    [
        ('prompt = "{title}"\n', "not available by design"),
        ('prompt = "{nope}"\n', "unknown template variable"),
        ('prompt = "{item}"\neffort = "turbo"\n', "unknown level"),
        ('prompt = "{item}"\npermission_mode = "yolo"\n', "unknown mode"),
        ('prompt = "{item}"\ncheckout = "elsewhere"\n', "checkout"),
        ('prompt = "{item}"\nallow = ["not a rule!"]\n', "not a permission rule"),
        ('prompt = "{item}"\n[[start]]\non = "Not A Kind"\n', "not an event kind"),
        ('prompt = "{item}"\n[[start]]\nunless_subscribed = "role"\n', "start\\[0\\]\\.on"),
        ('prompt = "{item}"\n[[start]]\non = "labeled"\nunless_subscribed = "never"\n',
         "unless_subscribed"),
        ('prompt = "{item}"\n[[start]]\non = "labeled"\n[[start]]\non = "labeled"\n',
         "used twice"),
        ('prompt = "{item}"\n[[start]]\non = "labeled"\nwake = ["comment"]\n', "unknown key"),
        ('prompt = "{item}"\n[triggers]\nstart = ["labeled"]\n', "unknown key"),
        ('prompt = "{item}"\nruntime = "codex"\n', "unknown runtime"),
        ('prompt = "{item}"\nmystery = 1\n', "unknown key"),
        ('prompt = "{item}"\n[policy]\nmax_workers = -1\n', ">= 0"),
        ("", "prompt"),
    ],
)
def test_a_bad_role_file_is_rejected_when_it_is_read(tmp_path, text, match):
    write_role(tmp_path, "implementer", text)

    with pytest.raises(ConfigError, match=match):
        load_role(tmp_path, "implementer")


def test_launch_settings_carry_the_deny_rules_and_the_roles_allow_rules():
    role = Role(name="r", prompt="{item}", options={"allow": ("Bash(gh pr *)",)})

    settings = launch_settings(role.options, DEFAULT_DENY_RULES)

    assert settings == {
        "permissions": {"deny": list(DEFAULT_DENY_RULES), "allow": ["Bash(gh pr *)"]}
    }
    assert launch_settings(Role(name="r", prompt="{item}").options, ()) is None


def test_role_names_are_kept_boring():
    with pytest.raises(ConfigError):
        roles_mod.validate_role_name("../escape")
    assert roles_mod.validate_role_name("pr-reviewer") == "pr-reviewer"
