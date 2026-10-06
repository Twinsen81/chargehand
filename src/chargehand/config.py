"""The two configuration layers, and the strict template substitution they share.

Machine layer (``~/.config/chargehand/config.toml``) holds everything specific to
one machine: poll interval, limits, worktree root, notification command, routes.
Repository layer (``.chargehand.toml`` at a repo root) holds what the repository
owns: base branch, setup script, prompt, permission mode, status file.

Splitting them is what lets a repository be shared without sharing machine details.
"""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from chargehand import paths
from chargehand.errors import ConfigError

# Claude Code's permission modes. `auto` is chargehand's default; `bypassPermissions`
# is a loud opt-in intended for a dedicated machine, user account, or VM.
PERMISSION_MODES = ("auto", "manual", "acceptEdits", "dontAsk", "plan", "bypassPermissions")

# Deliberately no {title} and no {body}: the launch prompt is a trusted user message,
# so attacker-controlled issue text must reach the agent as a tool result it fetches
# itself. Adding a text variable here would undo the main prompt-injection defense.
TEMPLATE_VARS = ("issue", "url", "worktree", "branch", "repo")

DEFAULT_LABELS = {
    "queued": "chargehand",
    "running": "chargehand-running",
    "blocked": "chargehand-blocked",
}

# Runner-launched sessions run as the operator's user, so nothing stops them calling
# the control plane. These are a guardrail, not a sandbox.
#
# Every pattern leads with `*` rather than anchoring on the program name. A Bash rule
# matches the command text Claude Code writes, so an anchored `Bash(chargehand:*)`
# stops `chargehand cancel X` and not `/usr/local/bin/chargehand cancel X` or
# `sh -c 'chargehand cancel X'`. A leading wildcard matches anywhere in the text and
# covers all three. It does not make the rule a boundary: a session that means to get
# past it can still split the name across quotes, build it at runtime, or shell out
# from a script. The rules stop the careless path, not a determined one.
#
# `claude kill` is `claude stop` under its documented alias, so denying one without the
# other leaves the same capability reachable by a different word.
DEFAULT_DENY_RULES = (
    "Bash(*chargehand *)",
    "Bash(*claude stop*)",
    "Bash(*claude kill*)",
    "Bash(*claude rm*)",
    "Bash(*claude respawn*)",
    "Bash(*claude agents*)",
    # The lease pool keeps every operator command under one prefix, so this one rule
    # also covers the ones it adds later. Agents keep the rest of the pool, which their
    # scripts use.
    "Bash(*banksman admin*)",
    # A session that starts a second session without these rules has escaped all of
    # them, so the flags that would do it are denied too.
    "Bash(*dangerously-skip-permissions*)",
    "Bash(*bypassPermissions*)",
)

_DENY_RULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_*]*(\(.*\))?$")


def validate_deny_rule(rule: str) -> None:
    """Reject a rule Claude Code would not accept, at read time.

    A settings payload that fails validation is dropped silently in a non-interactive
    run, and every session chargehand starts is non-interactive. One typo in this list
    would therefore launch sessions with no deny rules at all and say nothing, so the
    shape is checked here where it can still be reported.
    """
    if not rule or rule != rule.strip() or not _DENY_RULE.fullmatch(rule):
        raise ConfigError(
            f"config.deny_rules: {rule!r} is not a permission rule; expected a tool name "
            f"such as 'Bash' or a scoped rule such as 'Bash(*claude stop*)'"
        )


_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class TemplateError(ConfigError):
    """A configured template references a variable that does not exist."""


def render(template: str, values: Mapping[str, str], *, where: str) -> str:
    """Substitute ``{name}`` placeholders, rejecting any name not in *values*."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            known = ", ".join(sorted(values))
            raise TemplateError(
                f"{where}: unknown template variable {{{name}}}; available: {known}"
            )
        return values[name]

    return _PLACEHOLDER.sub(replace, template)


def render_argv(template: str, values: Mapping[str, str], *, where: str) -> list[str]:
    """Split a command template into argv, then substitute per token.

    Splitting first and substituting after means a value containing spaces or shell
    metacharacters stays exactly one argument. Nothing is ever run through a shell.
    """
    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise ConfigError(f"{where}: cannot parse command: {exc}") from exc
    if not tokens:
        raise ConfigError(f"{where}: command is empty")
    return [render(token, values, where=where) for token in tokens]


def validate_template(template: str, *, where: str) -> str:
    """Reject a placeholder that will never resolve, while the operator is looking.

    Left to launch time this surfaces three attempts later, as a failure that reads
    like a tracker problem.
    """
    for name in _PLACEHOLDER.findall(template):
        if name not in TEMPLATE_VARS:
            raise TemplateError(
                f"{where}: unknown template variable {{{name}}}; available: "
                f"{', '.join('{' + v + '}' for v in TEMPLATE_VARS)}"
            )
    return template


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def _require(table: Mapping[str, object], key: str, where: str) -> object:
    if key not in table:
        raise ConfigError(f"{where}: missing required key '{key}'")
    return table[key]


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


def _as_number(value: object, where: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number")
    if value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}")
    return float(value)


def _unknown_keys(table: Mapping[str, object], allowed: Iterable[str], where: str) -> None:
    extra = sorted(set(table) - set(allowed))
    if extra:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(extra)}")


@dataclass(frozen=True)
class Labels:
    queued: str
    running: str
    blocked: str

    @classmethod
    def parse(cls, table: Mapping[str, object] | None, where: str, base: "Labels|None" = None):
        values = dict(DEFAULT_LABELS if base is None else base.as_dict())
        if table:
            _unknown_keys(table, DEFAULT_LABELS, where)
            for key in DEFAULT_LABELS:
                if key in table:
                    values[key] = _as_str(table[key], f"{where}.{key}")
        if len({v.lower() for v in values.values()}) != 3:
            raise ConfigError(f"{where}: the three labels must be distinct")
        return cls(**values)

    def as_dict(self) -> dict[str, str]:
        return {"queued": self.queued, "running": self.running, "blocked": self.blocked}


@dataclass(frozen=True)
class TrackerConfig:
    type: str
    options: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def parse(cls, table: object, where: str) -> "TrackerConfig":
        if not isinstance(table, Mapping):
            raise ConfigError(f"{where}: expected a table, e.g. {{ type = \"linear\", team = \"ABC\" }}")
        kind = _as_str(_require(table, "type", where), f"{where}.type")
        options = {k: v for k, v in table.items() if k != "type"}
        return cls(type=kind, options=options)


@dataclass(frozen=True)
class Route:
    name: str
    repo: Path
    tracker: TrackerConfig
    labels: Labels
    max_concurrent: int | None = None

    @classmethod
    def parse(cls, table: Mapping[str, object], index: int, defaults: Labels) -> "Route":
        where = f"route[{index}]"
        _unknown_keys(table, ("name", "repo", "tracker", "labels", "max_concurrent"), where)
        repo = _expand(_as_str(_require(table, "repo", where), f"{where}.repo"))
        name = _as_str(table.get("name", repo.name), f"{where}.name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ConfigError(f"{where}.name: use letters, digits, dot, dash, underscore")
        max_concurrent = (
            _as_int(table["max_concurrent"], f"{where}.max_concurrent", minimum=0)
            if "max_concurrent" in table
            else None
        )
        return cls(
            name=name,
            repo=repo,
            tracker=TrackerConfig.parse(_require(table, "tracker", where), f"{where}.tracker"),
            labels=Labels.parse(table.get("labels"), f"{where}.labels", defaults),
            max_concurrent=max_concurrent,
        )


@dataclass(frozen=True)
class NotifyConfig:
    command: str | None = None
    timeout_secs: float = 30.0

    @classmethod
    def parse(cls, table: Mapping[str, object] | None) -> "NotifyConfig":
        if not table:
            return cls()
        where = "notify"
        _unknown_keys(table, ("command", "timeout_secs"), where)
        command = table.get("command")
        return cls(
            command=_as_str(command, f"{where}.command") if command is not None else None,
            timeout_secs=_as_number(table.get("timeout_secs", 30.0), f"{where}.timeout_secs", minimum=1),
        )


@dataclass(frozen=True)
class MachineConfig:
    routes: tuple[Route, ...]
    worktree_root: Path
    poll_interval_secs: int = 180
    max_concurrent: int = 2
    max_open_attempts: int = 4
    max_run_hours: float = 6.0
    hard_stop_hours: float = 10.0
    min_free_disk_gb: float = 30.0
    max_launch_attempts: int = 3
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    labels: Labels = field(default_factory=lambda: Labels(**DEFAULT_LABELS))
    deny_rules: tuple[str, ...] = DEFAULT_DENY_RULES
    pass_deny_rules_at_launch: bool = True
    trust_worktrees: bool = True
    claude_bin: str = "claude"
    git_bin: str = "git"
    pool_bin: str = "banksman"
    source: Path | None = None

    _TOP_LEVEL = (
        "poll_interval_secs",
        "max_concurrent",
        "max_open_attempts",
        "max_run_hours",
        "hard_stop_hours",
        "min_free_disk_gb",
        "max_launch_attempts",
        "worktree_root",
        "deny_rules",
        "pass_deny_rules_at_launch",
        "trust_worktrees",
        "claude_bin",
        "git_bin",
        "pool_bin",
        "notify",
        "labels",
        "route",
    )

    def route_by_name(self, name: str) -> Route | None:
        for route in self.routes:
            if route.name == name:
                return route
        return None

    def limit_for(self, route: Route) -> int:
        return self.max_concurrent if route.max_concurrent is None else route.max_concurrent

    @classmethod
    def parse(cls, data: Mapping[str, object], source: Path | None = None) -> "MachineConfig":
        _unknown_keys(data, cls._TOP_LEVEL, "config")
        labels = Labels.parse(data.get("labels"), "labels")

        raw_routes = data.get("route", [])
        if not isinstance(raw_routes, list) or not raw_routes:
            raise ConfigError("config: at least one [[route]] is required")
        routes = []
        for index, entry in enumerate(raw_routes):
            if not isinstance(entry, Mapping):
                raise ConfigError(f"route[{index}]: expected a table")
            routes.append(Route.parse(entry, index, labels))
        names = [r.name for r in routes]
        duplicate = next((n for n in names if names.count(n) > 1), None)
        if duplicate:
            raise ConfigError(f"config: duplicate route name '{duplicate}'")

        worktree_root = _expand(
            _as_str(data.get("worktree_root", "~/chargehand-worktrees"), "config.worktree_root")
        )

        deny = data.get("deny_rules", list(DEFAULT_DENY_RULES))
        if not isinstance(deny, list) or any(not isinstance(rule, str) for rule in deny):
            raise ConfigError("config.deny_rules: expected a list of strings")
        for rule in deny:
            validate_deny_rule(rule)

        max_concurrent = _as_int(data.get("max_concurrent", 2), "config.max_concurrent", minimum=0)
        max_open = _as_int(data.get("max_open_attempts", 4), "config.max_open_attempts", minimum=0)
        if max_open < max_concurrent:
            raise ConfigError(
                "config: max_open_attempts must be >= max_concurrent, otherwise blocked runs "
                "would starve the launch throttle"
            )
        max_run = _as_number(data.get("max_run_hours", 6.0), "config.max_run_hours", minimum=0)
        hard_stop = _as_number(data.get("hard_stop_hours", 10.0), "config.hard_stop_hours", minimum=0)
        if hard_stop and max_run and hard_stop <= max_run:
            raise ConfigError("config: hard_stop_hours must be greater than max_run_hours")

        return cls(
            routes=tuple(routes),
            worktree_root=worktree_root,
            poll_interval_secs=_as_int(
                data.get("poll_interval_secs", 180), "config.poll_interval_secs", minimum=10
            ),
            max_concurrent=max_concurrent,
            max_open_attempts=max_open,
            max_run_hours=max_run,
            hard_stop_hours=hard_stop,
            min_free_disk_gb=_as_number(
                data.get("min_free_disk_gb", 30.0), "config.min_free_disk_gb", minimum=0
            ),
            max_launch_attempts=_as_int(
                data.get("max_launch_attempts", 3), "config.max_launch_attempts", minimum=1
            ),
            notify=NotifyConfig.parse(data.get("notify")),
            labels=labels,
            deny_rules=tuple(deny),
            pass_deny_rules_at_launch=bool(data.get("pass_deny_rules_at_launch", True)),
            trust_worktrees=bool(data.get("trust_worktrees", True)),
            claude_bin=_as_str(data.get("claude_bin", "claude"), "config.claude_bin"),
            git_bin=_as_str(data.get("git_bin", "git"), "config.git_bin"),
            pool_bin=_as_str(data.get("pool_bin", "banksman"), "config.pool_bin"),
            source=source,
        )


@dataclass(frozen=True)
class RepoConfig:
    base: str
    prompt: str
    setup: str | None = None
    permission_mode: str = "auto"
    status_file: str | None = None
    branch_prefix: str = "chargehand/"
    setup_timeout_secs: float = 1800.0
    source: Path | None = None

    _KEYS = (
        "base",
        "prompt",
        "setup",
        "permission_mode",
        "status_file",
        "branch_prefix",
        "setup_timeout_secs",
    )

    @classmethod
    def parse(cls, data: Mapping[str, object], source: Path | None = None) -> "RepoConfig":
        where = str(source) if source else paths.REPO_CONFIG_NAME
        _unknown_keys(data, cls._KEYS, where)
        mode = _as_str(data.get("permission_mode", "auto"), f"{where}.permission_mode")
        if mode not in PERMISSION_MODES:
            raise ConfigError(
                f"{where}.permission_mode: unknown mode '{mode}'; "
                f"expected one of {', '.join(PERMISSION_MODES)}"
            )
        prompt = _as_str(_require(data, "prompt", where), f"{where}.prompt")
        for forbidden in ("{title}", "{body}", "{description}"):
            if forbidden in prompt:
                raise ConfigError(
                    f"{where}.prompt: {forbidden} is not available by design. Issue text is "
                    "untrusted and must reach the agent as a tool result it fetches itself."
                )
        validate_template(prompt, where=f"{where}.prompt")
        branch_prefix = _as_str(data.get("branch_prefix", "chargehand/"), f"{where}.branch_prefix")
        if branch_prefix.startswith("/") or ".." in branch_prefix:
            raise ConfigError(f"{where}.branch_prefix: must not start with '/' or contain '..'")
        setup = data.get("setup")
        if setup is not None:
            # Split as well as substituted: an unparseable command is the same class of
            # mistake and should surface in the same place.
            render_argv(
                _as_str(setup, f"{where}.setup"),
                {name: "" for name in TEMPLATE_VARS},
                where=f"{where}.setup",
            )
        status_file = data.get("status_file")
        if status_file is not None:
            validate_template(
                _as_str(status_file, f"{where}.status_file"), where=f"{where}.status_file"
            )
        return cls(
            base=_as_str(_require(data, "base", where), f"{where}.base"),
            prompt=prompt,
            setup=_as_str(setup, f"{where}.setup") if setup is not None else None,
            permission_mode=mode,
            status_file=(
                _as_str(data["status_file"], f"{where}.status_file")
                if data.get("status_file") is not None
                else None
            ),
            branch_prefix=branch_prefix,
            setup_timeout_secs=_as_number(
                data.get("setup_timeout_secs", 1800.0), f"{where}.setup_timeout_secs", minimum=1
            ),
            source=source,
        )


def _read_toml(path: Path) -> Mapping[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing configuration file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc


def load_machine_config(path: Path | None = None) -> MachineConfig:
    target = path or paths.config_file()
    return MachineConfig.parse(_read_toml(target), source=target)


def load_repo_config(repo: Path) -> RepoConfig:
    target = repo / paths.REPO_CONFIG_NAME
    if not target.exists():
        raise ConfigError(
            f"{repo}: no {paths.REPO_CONFIG_NAME}. Run `chargehand init` in the repository."
        )
    return RepoConfig.parse(_read_toml(target), source=target)
