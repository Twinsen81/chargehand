"""Roles: how a worker thinks, and when it runs.

A role is a template for workers, and a role may have any number of them. Its
*brain* is a Claude Code agent definition, a Markdown file with frontmatter that
Claude Code itself reads; chargehand only checks that it exists and passes its name at
launch. Everything else about the role lives in a small TOML file: the runtime, the
first prompt, the permission mode, the model and effort, the allow rules, the start
rules that create a worker on their own, and the policy caps.

``agent``, ``permission_mode`` and ``allow`` belong to the ``claude`` runtime, the
only one so far. Another runtime brings its own keys and checks them itself.

Lookup order for a role named ``pr-reviewer``:

1. ``<repo>/.chargehand/roles/pr-reviewer.toml``
2. ``~/.config/chargehand/roles/pr-reviewer.toml``

A repository whose ``.chargehand.toml`` still carries ``prompt`` and no role file
gets an ``implementer`` role built from it, so a repository configured before roles
existed keeps working without a new file.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from chargehand import claude as claude_mod
from chargehand import paths
from chargehand.config import (
    PERMISSION_MODES,
    TEMPLATE_VARS,
    RepoConfig,
    render_argv,
    validate_permission_rule,
    validate_template,
)
from chargehand.errors import ConfigError

DEFAULT_ROLE = "implementer"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CHECKOUTS = ("base", "item")
RUNTIMES = ("claude",)

# The event kind every tracker adapter produces for "an issue got the queue label".
# A role starts on it as `labeled`, which means any tracker, or as `<tracker>.labeled`.
LABELED = "labeled"

# The start rule a pre-roles `.chargehand.toml` describes, and the rule recorded on
# workers migrated from a version 1 ledger.
QUEUE_RULE = "queue"

# What a start rule checks before it creates a worker for a work item: that no live
# worker of this role has the item (`role`), or that no live worker of any role has it
# (`any`).
UNLESS_ROLE = "role"
UNLESS_ANY = "any"
UNLESS_CHOICES = (UNLESS_ROLE, UNLESS_ANY)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KIND = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?")


def _as_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty string")
    return value


def _as_int(value: object, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer")
    if value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}")
    return value


def _as_strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ConfigError(f"{where}: expected a list of strings")
    return tuple(value)


def _unknown_keys(table: Mapping[str, object], allowed: tuple[str, ...], where: str) -> None:
    extra = sorted(set(table) - set(allowed))
    if extra:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(extra)}")


def _as_kinds(value: object, where: str) -> tuple[str, ...]:
    kinds = _as_strings(value, where)
    for kind in kinds:
        if not _KIND.fullmatch(kind):
            raise ConfigError(
                f"{where}: {kind!r} is not an event kind such as 'labeled' or 'github.comment'"
            )
    return kinds


def _kind_matches(kinds: tuple[str, ...], kind: str, source: str | None) -> bool:
    return kind in kinds or (source is not None and f"{source}.{kind}" in kinds)


@dataclass(frozen=True)
class StartRule:
    """When the router creates a worker of this role on its own.

    A start event is not a delivery: no worker exists yet to receive it, and the same
    event arrives again on the next poll or when a review is requested a second time.
    So a rule only creates a worker when the check named by ``unless_subscribed`` finds
    none on the item. Workers created by hand are not limited by it.
    """

    on: str
    name: str = ""
    repositories: tuple[str, ...] = ()
    unless_subscribed: str = UNLESS_ROLE
    subscribe: tuple[str, ...] = ()
    close_on: tuple[str, ...] = ()

    _KEYS = ("name", "on", "repositories", "unless_subscribed", "subscribe", "close_on")

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.on)

    @classmethod
    def parse(cls, table: object, where: str) -> "StartRule":
        if not isinstance(table, Mapping):
            raise ConfigError(f"{where}: expected a table")
        _unknown_keys(table, cls._KEYS, where)
        on = _as_kinds([_as_str(table.get("on"), f"{where}.on")], f"{where}.on")[0]
        name = table.get("name", on)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigError(f"{where}.name: rule names use letters, digits, dot, dash, underscore")
        unless = _as_str(table.get("unless_subscribed", UNLESS_ROLE), f"{where}.unless_subscribed")
        if unless not in UNLESS_CHOICES:
            raise ConfigError(
                f"{where}.unless_subscribed: expected one of {', '.join(UNLESS_CHOICES)}"
            )
        return cls(
            on=on,
            name=name,
            repositories=_as_strings(table.get("repositories", []), f"{where}.repositories"),
            unless_subscribed=unless,
            subscribe=_as_kinds(table.get("subscribe", []), f"{where}.subscribe"),
            close_on=_as_kinds(table.get("close_on", []), f"{where}.close_on"),
        )

    def matches(self, kind: str, source: str | None = None, repository: str | None = None) -> bool:
        if self.repositories and repository not in self.repositories:
            return False
        return _kind_matches((self.on,), kind, source)


def _parse_start_rules(value: object, where: str) -> tuple[StartRule, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected [[start]] tables")
    rules = tuple(StartRule.parse(table, f"{where}[{index}]") for index, table in enumerate(value))
    names = [rule.name for rule in rules]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(
            f"{where}: rule name(s) used twice: {', '.join(duplicates)}; give each rule its own `name`"
        )
    return rules


@dataclass(frozen=True)
class Policy:
    max_workers: int | None = None
    max_wakes_per_day: int = 12
    debounce_secs: int = 120
    refresh_after_tokens: int | None = None
    refresh_after_wakes: int | None = None

    _KEYS = (
        "max_workers",
        "max_wakes_per_day",
        "debounce_secs",
        "refresh_after_tokens",
        "refresh_after_wakes",
    )

    @classmethod
    def parse(cls, table: object, where: str) -> "Policy":
        if not isinstance(table, Mapping):
            raise ConfigError(f"{where}: expected a table")
        _unknown_keys(table, cls._KEYS, where)
        optional = {
            key: (_as_int(table[key], f"{where}.{key}", minimum=0) if key in table else None)
            for key in ("max_workers", "refresh_after_tokens", "refresh_after_wakes")
        }
        return cls(
            max_wakes_per_day=_as_int(table.get("max_wakes_per_day", 12), f"{where}.max_wakes_per_day"),
            debounce_secs=_as_int(table.get("debounce_secs", 120), f"{where}.debounce_secs"),
            **optional,
        )


@dataclass(frozen=True)
class Role:
    name: str
    prompt: str
    runtime: str = "claude"
    permission_mode: str = "auto"
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    allow: tuple[str, ...] = ()
    checkout: str = "base"
    status_file: str | None = None
    setup: str | None = None
    start: tuple[StartRule, ...] = ()
    policy: Policy = field(default_factory=Policy)
    source: Path | None = None

    _KEYS = (
        "runtime",
        "agent",
        "model",
        "effort",
        "prompt",
        "permission_mode",
        "allow",
        "checkout",
        "status_file",
        "setup",
        "start",
        "policy",
    )

    @classmethod
    def parse(cls, name: str, data: Mapping[str, object], source: Path | None = None) -> "Role":
        where = str(source) if source else f"role {name}"
        _unknown_keys(data, cls._KEYS, where)
        runtime = _as_str(data.get("runtime", "claude"), f"{where}.runtime")
        if runtime not in RUNTIMES:
            raise ConfigError(
                f"{where}.runtime: unknown runtime '{runtime}'; expected one of {', '.join(RUNTIMES)}"
            )
        prompt = _as_str(data.get("prompt") if "prompt" in data else None, f"{where}.prompt")
        _check_prompt(prompt, where)
        mode = _as_str(data.get("permission_mode", "auto"), f"{where}.permission_mode")
        if mode not in PERMISSION_MODES:
            raise ConfigError(
                f"{where}.permission_mode: unknown mode '{mode}'; "
                f"expected one of {', '.join(PERMISSION_MODES)}"
            )
        effort = data.get("effort")
        if effort is not None:
            effort = _as_str(effort, f"{where}.effort")
            if effort not in EFFORT_LEVELS:
                raise ConfigError(
                    f"{where}.effort: unknown level '{effort}'; expected one of "
                    f"{', '.join(EFFORT_LEVELS)}"
                )
        checkout = _as_str(data.get("checkout", "base"), f"{where}.checkout")
        if checkout not in CHECKOUTS:
            raise ConfigError(f"{where}.checkout: expected one of {', '.join(CHECKOUTS)}")
        allow = _as_strings(data.get("allow", []), f"{where}.allow")
        for rule in allow:
            validate_permission_rule(rule, where=f"{where}.allow")
        setup = data.get("setup")
        if setup is not None:
            render_argv(
                _as_str(setup, f"{where}.setup"),
                {variable: "" for variable in TEMPLATE_VARS},
                where=f"{where}.setup",
            )
        status_file = data.get("status_file")
        if status_file is not None:
            validate_template(_as_str(status_file, f"{where}.status_file"), where=f"{where}.status_file")
        return cls(
            name=name,
            prompt=prompt,
            runtime=runtime,
            permission_mode=mode,
            agent=_as_str(data["agent"], f"{where}.agent") if data.get("agent") is not None else None,
            model=_as_str(data["model"], f"{where}.model") if data.get("model") is not None else None,
            effort=effort,
            allow=allow,
            checkout=checkout,
            status_file=status_file,
            setup=setup,
            start=_parse_start_rules(data.get("start", []), f"{where}.start"),
            policy=Policy.parse(data.get("policy", {}), f"{where}.policy"),
            source=source,
        )

    def start_rule_for(
        self, kind: str, source: str | None = None, repository: str | None = None
    ) -> StartRule | None:
        """The first start rule that creates a worker for this event, or None."""
        return next((rule for rule in self.start if rule.matches(kind, source, repository)), None)

    def launch_settings(self, deny_rules: tuple[str, ...]) -> dict[str, object] | None:
        """The per-launch settings payload: the machine's deny rules and this role's allow rules."""
        permissions: dict[str, list[str]] = {}
        if deny_rules:
            permissions["deny"] = list(deny_rules)
        if self.allow:
            permissions["allow"] = list(self.allow)
        return {"permissions": permissions} if permissions else None


def _check_prompt(prompt: str, where: str) -> None:
    for forbidden in ("{title}", "{body}", "{description}"):
        if forbidden in prompt:
            raise ConfigError(
                f"{where}.prompt: {forbidden} is not available by design. Issue text is "
                "untrusted and must reach the agent as a tool result it fetches itself."
            )
    validate_template(prompt, where=f"{where}.prompt")


def validate_role_name(name: str, where: str = "role") -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ConfigError(f"{where}: role names use letters, digits, dot, dash, underscore")
    return name


def role_file_candidates(repo: Path, name: str) -> list[Path]:
    return [
        repo / ".chargehand" / "roles" / f"{name}.toml",
        paths.config_dir() / "roles" / f"{name}.toml",
    ]


def _read_toml(path: Path) -> Mapping[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc


def legacy_role(repo_config: RepoConfig, *, tracker_type: str | None = None) -> Role | None:
    """The `implementer` role a pre-roles `.chargehand.toml` describes, or None."""
    if not repo_config.prompt:
        return None
    on = LABELED if tracker_type is None else f"{tracker_type}.{LABELED}"
    return Role(
        name=DEFAULT_ROLE,
        prompt=repo_config.prompt,
        permission_mode=repo_config.permission_mode,
        status_file=repo_config.status_file,
        start=(StartRule(on=on, name=QUEUE_RULE),),
        source=repo_config.source,
    )


def load_role(
    repo: Path,
    name: str,
    *,
    repo_config: RepoConfig | None = None,
    tracker_type: str | None = None,
    check_agent: bool = True,
) -> Role:
    """Find and parse a role, checking that the agent file it names exists."""
    validate_role_name(name)
    candidates = role_file_candidates(repo, name)
    for candidate in candidates:
        if candidate.is_file():
            role = Role.parse(name, _read_toml(candidate), source=candidate)
            break
    else:
        role = legacy_role(repo_config, tracker_type=tracker_type) if repo_config else None
        if role is None or name != DEFAULT_ROLE:
            looked = ", ".join(str(c) for c in candidates)
            raise ConfigError(
                f"{repo}: no role named '{name}'; looked for {looked}. "
                f"Run `chargehand init` in the repository to write a starter role."
            )
    if check_agent and role.agent and not any(
        path.is_file() for path in claude_mod.agent_file_candidates(repo, role.agent)
    ):
        looked = ", ".join(str(c) for c in claude_mod.agent_file_candidates(repo, role.agent))
        raise ConfigError(f"{role.source or name}: agent '{role.agent}' not found; looked for {looked}")
    return role


__all__ = [
    "DEFAULT_ROLE",
    "EFFORT_LEVELS",
    "LABELED",
    "Policy",
    "QUEUE_RULE",
    "Role",
    "RUNTIMES",
    "StartRule",
    "UNLESS_ANY",
    "UNLESS_ROLE",
    "legacy_role",
    "load_role",
    "role_file_candidates",
    "validate_role_name",
]
