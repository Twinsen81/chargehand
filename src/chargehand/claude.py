"""The ``claude`` runtime, and the only module that knows the Claude Code command line.

:class:`ClaudeCLI` is a thin wrapper around the binary. :class:`ClaudeRuntime` puts it
behind the runtime interface of :mod:`chargehand.runtime`, together with what only a
Claude Code launch needs: the workspace trust entry, the per-launch permission settings,
and the role keys ``agent``, ``permission_mode`` and ``allow``.

Two notes on robustness. The listing is parsed defensively: envelope shape, field
names, and state vocabulary are all accepted in several spellings and an unrecognized
state is preserved as ``unknown`` rather than guessed at, because acting on a wrong
guess is worse than reporting "go look". And a launch does not scrape ids out of
stdout; it starts the session and then finds it in the listing by name or working
directory. That makes the last launch step idempotent and identical to the adoption
path reconciliation already needs.

Observed against Claude Code 2.1.278. ``claude agents --json --all`` prints a bare
JSON array. A background entry carries ``id`` (the short id that ``--bg`` prints and
that ``attach``, ``logs``, ``stop`` and ``rm`` take), ``sessionId`` (a UUID), ``name``,
``cwd``, ``pid``, ``kind``, a millisecond ``startedAt``, and *two* liveness fields:
``state`` and ``status``.

Those two disagree, and that difference matters more than anything else here. A session
that finished its work reports ``state: done`` with ``status: idle``. A session that
asked a question and ended its turn reports ``state: blocked`` with ``status: idle``.
Reading ``status`` would call the second one finished, and the runner would clear its
label and collect its worktree while it was still waiting for an answer. So ``state`` is
read first, and ``status`` is only the fallback for entries that lack it, which is how
interactive sessions appear.

The aliases below are wider than what was observed, on purpose, because the output is
not a documented contract. None of them is a guess about the shape in use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chargehand import runtime as runtime_mod
from chargehand import sanitize
from chargehand.config import PERMISSION_MODES, validate_permission_rule
from chargehand.errors import ClaudeError, ConfigError, WorkspaceNotTrusted
from chargehand.runtime import (
    BLOCKED,
    CHECK_FAIL,
    CHECK_OK,
    CHECK_WARN,
    DONE,
    FAILED,
    SESSION_STATES,
    STOPPED,
    UNKNOWN,
    WORKING,
    Capabilities,
    CommandResult,
    HealthCheck,
    LaunchResult,
    LaunchSpec,
    Runtime,
    Session,
    WakeResult,
    worker_from_session,
)

if TYPE_CHECKING:
    from chargehand.config import MachineConfig
    from chargehand.roles import Role

log = logging.getLogger(__name__)

# How many times one launch writes the worktree's trust entry and starts the session,
# when Claude Code refuses the worktree as untrusted each time.
TRUST_LAUNCH_ATTEMPTS = 3

DEFAULT_PERMISSION_MODE = "auto"

# How long a wake waits, after a stop, for the listing and the old process to confirm it.
STOP_WAIT_SECS = 10.0
STOP_POLL_SECS = 0.25


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError, OSError):
        return False
    return True


def _listed_pid(session: Session) -> int | None:
    value = session.raw.get("pid")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

# Claude Code's vocabulary is not contractual, so recognize the plausible spellings
# and fall back to UNKNOWN rather than mapping something unexpected onto DONE.
# Confirmed live: working / done / blocked from `state`, busy / idle / waiting from `status`.
_STATE_ALIASES = {
    "working": WORKING,
    "running": WORKING,
    "active": WORKING,
    "busy": WORKING,
    "in_progress": WORKING,
    "in-progress": WORKING,
    "thinking": WORKING,
    "blocked": BLOCKED,
    "needs_input": BLOCKED,
    "needs-input": BLOCKED,
    "needsinput": BLOCKED,
    "waiting": BLOCKED,
    "waiting_for_input": BLOCKED,
    "awaiting_input": BLOCKED,
    "permission": BLOCKED,
    "permission_prompt": BLOCKED,
    "done": DONE,
    "complete": DONE,
    "completed": DONE,
    "finished": DONE,
    "idle": DONE,
    "failed": FAILED,
    "error": FAILED,
    "errored": FAILED,
    "crashed": FAILED,
    "stopped": STOPPED,
    "killed": STOPPED,
    "terminated": STOPPED,
    "cancelled": STOPPED,
    "canceled": STOPPED,
}

_ID_KEYS = ("id", "shortId", "short_id", "agentId", "agent_id", "sessionShortId")
_KIND_KEYS = ("kind", "type")
_UUID_KEYS = ("uuid", "sessionId", "session_id", "sessionUuid", "session_uuid")
_NAME_KEYS = ("name", "title", "label")
_CWD_KEYS = ("cwd", "workingDirectory", "working_directory", "working_dir", "directory", "path", "worktree")
_STATE_KEYS = ("state", "status")
_WAITING_KEYS = ("waitingFor", "waiting_for", "blockedReason", "blocked_reason", "reason")
_STARTED_KEYS = ("startedAt", "started_at", "createdAt", "created_at", "launchedAt")
_PR_KEYS = ("prUrl", "pr_url", "pullRequestUrl", "pull_request_url", "pr")


def _first(node: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in node and node[key] not in (None, ""):
            return node[key]
    return None


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Heuristic: anything past the year 2286 in seconds is really milliseconds.
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def parse_session(node: Mapping[str, Any]) -> Session | None:
    identifier = _first(node, _ID_KEYS)
    uuid = _first(node, _UUID_KEYS)
    if identifier is None and uuid is None:
        return None
    raw_state = str(_first(node, _STATE_KEYS) or "")
    waiting = _first(node, _WAITING_KEYS)
    return Session(
        id=str(identifier if identifier is not None else uuid),
        uuid=str(uuid) if uuid is not None else None,
        name=(lambda v: str(v) if v is not None else None)(_first(node, _NAME_KEYS)),
        cwd=(lambda v: str(v) if v is not None else None)(_first(node, _CWD_KEYS)),
        state=_STATE_ALIASES.get(raw_state.strip().lower().replace(" ", "_"), UNKNOWN),
        raw_state=raw_state,
        waiting_for=str(waiting) if waiting is not None else None,
        started_at=_parse_time(_first(node, _STARTED_KEYS)),
        # The session's agent influences this value, and `status --json` prints it by
        # default, so it has to be a URL and nothing else.
        pr_url=sanitize.safe_url(_first(node, _PR_KEYS)),
        kind=(lambda v: str(v) if v is not None else None)(_first(node, _KIND_KEYS)),
        raw=dict(node),
    )


def parse_sessions(payload: str) -> list[Session]:
    """Accept a bare array, a common envelope key, or JSON Lines."""
    text = payload.strip()
    if not text:
        return []
    nodes: list[Any] = []
    try:
        data = json.loads(text)
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                nodes.append(json.loads(line))
            except ValueError:
                raise ClaudeError(
                    "could not parse the output of `claude agents --json --all`; "
                    "run `chargehand doctor` to see what it returned"
                ) from None
    else:
        if isinstance(data, list):
            nodes = data
        elif isinstance(data, dict):
            for key in ("agents", "sessions", "data", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    nodes = value
                    break
            else:
                nodes = [data] if _first(data, _ID_KEYS + _UUID_KEYS) else []
        else:
            raise ClaudeError("`claude agents --json --all` returned an unexpected JSON type")

    sessions = []
    for node in nodes:
        if isinstance(node, Mapping):
            session = parse_session(node)
            if session is not None:
                sessions.append(session)
    return sessions


# The start of the refusal Claude Code prints when a background session would start in a
# directory without a persisted trust entry. Observed in Claude Code 2.1.306:
# "Workspace not trusted. Run `claude` in <path> once and accept the trust prompt, then
# retry."
_UNTRUSTED_WORKSPACE = "Workspace not trusted"


class ClaudeCLI:
    """A thin, testable wrapper. The fake `claude` in the test suite replaces the binary."""

    def __init__(self, binary: str = "claude", *, timeout_secs: float = 120.0) -> None:
        self.binary = binary
        self.timeout_secs = timeout_secs

    # ----- plumbing ---------------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        argv = (self.binary, *args)
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                # Nothing here reads stdin, and `-p` waits for input on an open pipe.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout_secs,
                check=False,
                env={**os.environ, **(env or {})},
            )
        except FileNotFoundError as exc:
            raise ClaudeError(
                f"'{self.binary}' not found on PATH. launchd starts jobs with a minimal PATH; "
                f"set claude_bin to an absolute path or extend the job's PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeError(f"`{' '.join(argv)}` timed out after {exc.timeout:.0f}s") from exc
        except OSError as exc:
            raise ClaudeError(f"could not run `{' '.join(argv)}`: {exc}") from exc
        result = CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)
        if check:
            _check(result)
        return result

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None or Path(self.binary).exists()

    def version(self) -> str:
        return self._run(["--version"], timeout=30).stdout.strip()

    def help_text(self) -> str:
        result = self._run(["--help"], timeout=30, check=False)
        return result.stdout + result.stderr

    def supports_flag(self, flag: str) -> bool:
        """Used by `doctor`: some launch-time options are version dependent."""
        try:
            return flag in self.help_text()
        except ClaudeError:
            return False

    # ----- the interface ----------------------------------------------------

    def list_sessions(self) -> list[Session]:
        result = self._run(["agents", "--json", "--all"], timeout=60)
        return parse_sessions(result.stdout)

    def launch(
        self,
        *,
        worktree: Path,
        name: str,
        prompt: str,
        permission_mode: str,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        plugin_dirs: Sequence[Path | str] = (),
        settings: Mapping[str, Any] | None = None,
        timeout_secs: float = 180.0,
    ) -> CommandResult:
        """Start a background session inside *worktree*.

        Starting inside a linked worktree is deliberate: Claude Code skips its own
        background-session worktree isolation, so no nested worktree appears.

        Claude Code stores these flags with the job and applies them again when the
        session is woken with ``--resume --bg`` or ``respawn``, so what is passed here
        holds for the worker's whole life.

        Raises :class:`WorkspaceNotTrusted` when Claude Code refuses *worktree* as an
        untrusted workspace, so the caller can record the trust again and retry.
        """
        args = ["--bg", "-n", name, "--permission-mode", permission_mode]
        if agent:
            args += ["--agent", agent]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        for plugin_dir in plugin_dirs:
            args += ["--plugin-dir", str(plugin_dir)]
        if settings:
            args += ["--settings", json.dumps(settings, sort_keys=True)]
        args.append(prompt)
        result = self._run(args, cwd=worktree, timeout=timeout_secs, check=False)
        if result.returncode != 0 and _UNTRUSTED_WORKSPACE in result.stdout + result.stderr:
            raise WorkspaceNotTrusted(
                f"Claude Code refused {worktree} as an untrusted workspace: "
                f"{sanitize.one_line(result.stderr or result.stdout, limit=200)}"
            )
        return _check(result)

    def resume(self, session_uuid: str, prompt: str, *, timeout_secs: float = 180.0) -> CommandResult:
        """Wake a stopped background session with *prompt* as its next turn.

        Only the full session id continues the session in place, and only while its
        process is stopped; the short job id, or a process that is still alive, makes
        Claude Code start a copy under a new id and say so with a ``note:`` line, which
        :func:`copy_started_by` reads. No other flag is passed: the job's saved options
        are applied again, and any extra flag would also force a copy. stdin is closed,
        because a resume reads piped text into the prompt.
        """
        return self._run(["--resume", session_uuid, "--bg", prompt], timeout=timeout_secs, check=False)

    def fork(
        self,
        session_uuid: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        timeout_secs: float = 180.0,
    ) -> CommandResult:
        """Start a new background session that continues a copy of *session_uuid*'s conversation."""
        args = ["--resume", session_uuid, "--fork-session", "--bg"]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        args.append(prompt)
        return self._run(args, timeout=timeout_secs, check=False)

    def one_shot(
        self,
        prompt: str,
        *,
        cwd: Path,
        permission_mode: str,
        settings: Mapping[str, Any] | Path | None = None,
        env: Mapping[str, str] | None = None,
        extra_args: Sequence[str] = (),
        timeout_secs: float = 180.0,
    ) -> CommandResult:
        """Run one non-interactive turn and return when it ends.

        Not used by the tick, which only ever starts background sessions. This is the
        shape the probes need: a single turn whose exit is observable, so a refusal can
        be attributed to the settings the turn was given. A path is passed as it is, so
        a probe can load a shipped settings file byte for byte.
        """
        args = ["-p", "--permission-mode", permission_mode]
        if isinstance(settings, Path):
            args += ["--settings", str(settings)]
        elif settings is not None:
            args += ["--settings", json.dumps(settings, sort_keys=True)]
        args += list(extra_args)
        args.append(prompt)
        return self._run(args, cwd=cwd, timeout=timeout_secs, check=False, env=env)

    def find_session(
        self,
        sessions: Sequence[Session],
        *,
        name: str | None = None,
        cwd: Path | None = None,
        exclude: Collection[str] = (),
    ) -> Session | None:
        return runtime_mod.find_session(sessions, name=name, cwd=cwd, exclude=exclude)

    def stop(self, session_id: str) -> CommandResult:
        return self._run(["stop", session_id], timeout=60, check=False)

    def respawn(self, session_id: str) -> CommandResult:
        return self._run(["respawn", session_id], timeout=120, check=False)

    def remove(self, session_id: str) -> CommandResult:
        return self._run(["rm", session_id], timeout=60, check=False)

    def config_file(self) -> Path:
        """Where Claude Code keeps the per-project state the trust flag lives in."""
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        root = Path(override).expanduser() if override else Path(os.path.expanduser("~"))
        return root / ".claude.json"

    def is_trusted(self, worktree: Path) -> bool:
        projects = (self._read_config() or {}).get("projects")
        if not isinstance(projects, dict):
            return False
        return any(
            isinstance(projects.get(name), Mapping)
            and projects[name].get("hasTrustDialogAccepted") is True
            for name in _trust_keys(worktree)
        )

    def trust_worktree(self, worktree: Path) -> bool:
        """Mark *worktree* as trusted, so a background session may start in it.

        Claude Code refuses to start a background session in a directory nobody has
        accepted a trust dialog for. A runner can never answer that dialog, and every
        run it makes is a worktree that has just been created, so without this no launch
        would ever succeed. The refusal names the alternative itself: set
        ``projects[<path>].hasTrustDialogAccepted`` in its configuration file.

        Granting it here adds no authority. The operator already decided the repository
        is trusted when they wrote the route, and the worktree is a checkout of that
        repository which the runner made a moment ago.

        Returns True when the flag is set afterwards. A configuration file that cannot
        be parsed is left alone: overwriting Claude Code's own state on a guess would
        cost far more than the launch that is about to fail with a clear message.
        """
        path = self.config_file()
        data = self._read_config()
        if data is None:
            return False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return False
        changed = False
        for name in _trust_keys(worktree):
            entry = projects.get(name)
            if not isinstance(entry, dict):
                entry = {}
                projects[name] = entry
            if entry.get("hasTrustDialogAccepted") is not True:
                entry["hasTrustDialogAccepted"] = True
                changed = True
        if not changed:
            return True
        return _write_json_atomically(path, data)

    def _read_config(self) -> dict[str, Any] | None:
        path = self.config_file()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def logs(self, session_id: str, *, lines: int = 200) -> str:
        result = self._run(["logs", session_id], timeout=60, check=False)
        output = result.stdout or result.stderr
        tail = output.splitlines()[-lines:]
        return "\n".join(tail)


def _trust_keys(worktree: Path) -> list[str]:
    """Every spelling of *worktree* the trust lookup might use.

    A path can reach Claude Code as written, as its realpath, or with its unicode
    normalized differently, and the lookup is a dictionary key rather than a path
    comparison. Setting all of them costs nothing and avoids a refusal that would look
    like the flag had not been written at all.
    """
    spellings = [str(worktree)]
    try:
        spellings.append(str(worktree.resolve()))
    except OSError:
        pass
    for spelling in list(spellings):
        spellings.append(unicodedata.normalize("NFC", spelling))
    seen: list[str] = []
    for spelling in spellings:
        if spelling not in seen:
            seen.append(spelling)
    return seen


def _write_json_atomically(path: Path, data: Mapping[str, Any]) -> bool:
    """Replace *path* in one step, so an interrupted write cannot truncate it.

    The file belongs to Claude Code and holds state the runner did not author, so a
    half-written one would be worse than the launch failure this is trying to prevent.
    """
    temporary = path.with_name(f"{path.name}.chargehand.tmp")
    try:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def _check(result: CommandResult) -> CommandResult:
    if result.returncode != 0:
        raise ClaudeError(
            f"`{' '.join(result.argv)}` exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    return result


_COPY_NOTE = re.compile(r"started a copy (?:of that conversation )?as ([0-9a-f]{8})")


def copy_started_by(result: CommandResult) -> str | None:
    """The job id of the copy a resume started instead of continuing in place, or None."""
    match = _COPY_NOTE.search(result.stdout + result.stderr)
    return match.group(1) if match else None


def user_claude_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path(os.path.expanduser("~")) / ".claude"


def agent_file_candidates(repo: Path, name: str) -> list[Path]:
    """Where Claude Code looks for the agent definition *name*: the project, then the user."""
    return [repo / ".claude" / "agents" / f"{name}.md", user_claude_dir() / "agents" / f"{name}.md"]


def launch_settings(
    options: Mapping[str, Any], deny_rules: Sequence[str], read_paths: Sequence[Path] = ()
) -> dict[str, object] | None:
    """The per-launch settings payload: the machine's deny rules and the role's allow rules.

    Each of *read_paths* gets a `Read` allow rule, which also covers Grep and Glob. It is
    not passed as `--add-dir`: Claude Code loads skills from an added directory, and these
    paths hold content the operator did not write.
    """
    permissions: dict[str, list[str]] = {}
    if deny_rules:
        permissions["deny"] = list(deny_rules)
    # `//` starts an absolute path in a Claude Code permission rule.
    allow = [*(options.get("allow") or ()), *(f"Read(/{Path(p)}/**)" for p in read_paths)]
    if allow:
        permissions["allow"] = allow
    return {"permissions": permissions} if permissions else None


class ClaudeRuntime(Runtime):
    """Claude Code background sessions, hosted by Claude Code's own supervisor."""

    name = "claude"
    # What Claude Code supports. In-place wake, board permissions and usage readings go
    # through the in-session channel and hooks, not through wake() and the CLI here.
    capabilities = Capabilities(
        live_attach=True,
        in_place_wake=True,
        board_permissions=True,
        usage_readings=True,
        fork=True,
        external_supervisor=True,
    )
    role_keys = ("agent", "permission_mode", "allow")

    def __init__(
        self,
        cli: ClaudeCLI | None = None,
        *,
        deny_rules: Sequence[str] = (),
        trust_worktrees: bool = True,
        stop_wait_secs: float = STOP_WAIT_SECS,
        stop_poll_secs: float = STOP_POLL_SECS,
        pid_alive: Callable[[int], bool] = _pid_alive,
    ) -> None:
        self.cli = cli or ClaudeCLI()
        self.deny_rules = tuple(deny_rules)
        self.trust_worktrees = trust_worktrees
        self.stop_wait_secs = stop_wait_secs
        self.stop_poll_secs = stop_poll_secs
        self.pid_alive = pid_alive

    @classmethod
    def from_config(cls, config: "MachineConfig") -> "ClaudeRuntime":
        return cls(
            ClaudeCLI(config.claude_bin),
            deny_rules=config.deny_rules if config.pass_deny_rules_at_launch else (),
            trust_worktrees=config.trust_worktrees,
        )

    @classmethod
    def parse_role_options(cls, table: Mapping[str, object], where: str) -> dict[str, object]:
        mode = table.get("permission_mode", DEFAULT_PERMISSION_MODE)
        if not isinstance(mode, str) or mode not in PERMISSION_MODES:
            raise ConfigError(
                f"{where}.permission_mode: unknown mode '{mode}'; "
                f"expected one of {', '.join(PERMISSION_MODES)}"
            )
        agent = table.get("agent")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            raise ConfigError(f"{where}.agent: expected a non-empty string")
        allow = table.get("allow", [])
        if not isinstance(allow, list) or any(not isinstance(r, str) or not r.strip() for r in allow):
            raise ConfigError(f"{where}.allow: expected a list of strings")
        for rule in allow:
            validate_permission_rule(rule, where=f"{where}.allow")
        return {"permission_mode": mode, "agent": agent, "allow": tuple(allow)}

    @classmethod
    def check_role(cls, role: "Role", repo: Path) -> None:
        if role.agent and not any(path.is_file() for path in agent_file_candidates(repo, role.agent)):
            looked = ", ".join(str(c) for c in agent_file_candidates(repo, role.agent))
            raise ConfigError(
                f"{role.source or role.name}: agent '{role.agent}' not found; looked for {looked}"
            )

    def health_checks(self) -> list[HealthCheck]:
        """The binary and its version, the session listing, and whether `--settings` exists."""
        if not self.cli.is_available():
            return [HealthCheck("claude", CHECK_FAIL, f"'{self.cli.binary}' not found on PATH")]
        try:
            checks = [HealthCheck("claude", CHECK_OK, self.cli.version())]
        except ClaudeError as exc:
            return [HealthCheck("claude", CHECK_FAIL, str(exc))]
        try:
            sessions = self.cli.list_sessions()
            checks.append(HealthCheck("claude agents", CHECK_OK, f"{len(sessions)} session(s) listed"))
        except ClaudeError as exc:
            checks.append(HealthCheck("claude agents", CHECK_FAIL, str(exc)))
        if self.deny_rules:
            supported = self.cli.supports_flag("--settings")
            checks.append(
                HealthCheck(
                    "launch deny rules",
                    CHECK_OK if supported else CHECK_WARN,
                    "passed per launch with --settings"
                    if supported
                    else "this Claude Code build does not advertise --settings; put the "
                    "deny rules in user settings instead, or set "
                    "pass_deny_rules_at_launch = false",
                )
            )
        return checks

    # ----- sessions ---------------------------------------------------------

    def list_sessions(self) -> list[Session]:
        return self.cli.list_sessions()

    def launch(self, spec: LaunchSpec, *, warn: Callable[[str], None] | None = None) -> LaunchResult:
        """Record the worktree as trusted, then start its session.

        Claude Code will not start a background session in a directory nobody has
        accepted a trust dialog for, and every worktree here was made seconds ago. There
        is no dialog a runner can answer, so the flag is written instead.

        A Claude Code process that is already running can write its configuration file
        back without the entry written a moment before, and the launch is then refused.
        When that was seen, writing the entry again and launching at once succeeded, so
        that is done here a few times, rather than leaving it to the next tick. A retry
        helps only when the entry is gone: a refusal while the entry is still in the
        file has another cause, and is reported at once.
        """
        attempt = 1
        while True:
            trusted = self._trust(spec, warn)
            try:
                result = self.cli.launch(
                    worktree=spec.worktree,
                    name=spec.name,
                    prompt=spec.prompt,
                    permission_mode=str(spec.options.get("permission_mode") or DEFAULT_PERMISSION_MODE),
                    agent=spec.options.get("agent") or None,
                    model=spec.model,
                    effort=spec.effort,
                    settings=launch_settings(spec.options, self.deny_rules, spec.read_paths),
                )
            except WorkspaceNotTrusted as exc:
                if not trusted:
                    raise
                config_file = self.cli.config_file()
                if self.cli.is_trusted(spec.worktree):
                    raise WorkspaceNotTrusted(
                        f"{exc} (the trust entry is still in {config_file}, so writing it "
                        f"again would change nothing; Claude Code may be checking another "
                        f"path or an older copy of the file)"
                    ) from exc
                if attempt >= TRUST_LAUNCH_ATTEMPTS:
                    raise WorkspaceNotTrusted(
                        f"{exc} (refused {attempt} times in a row, and each time the trust "
                        f"entry was gone from {config_file} afterwards; another Claude Code "
                        f"process may be writing the file back without it)"
                    ) from exc
                log.warning(
                    "%s: launch refused as untrusted and the trust entry was gone from %s; "
                    "writing it again (attempt %s of %s)",
                    spec.name, config_file, attempt + 1, TRUST_LAUNCH_ATTEMPTS,
                )
                attempt += 1
                continue
            return LaunchResult(output=result.stdout or result.stderr)

    def _trust(self, spec: LaunchSpec, warn: Callable[[str], None] | None) -> bool:
        """True when the worktree's trust entry is in place, False when it was not written."""
        if not self.trust_worktrees:
            return False
        if self.cli.trust_worktree(spec.worktree):
            return True
        if warn is not None:
            warn(
                f"{spec.name}: could not record {spec.worktree} as a trusted workspace; "
                f"the launch may be refused"
            )
        return False

    def is_stopped(self, session: Session) -> bool:
        """No process is left: the entry has neither ``pid`` nor ``status``, in a known state.

        ``state`` is the state of the last turn, not of the process. Observed in Claude Code
        2.1.310: an idle session lists as ``done`` with ``pid`` and ``status: idle`` while
        its process lives, and after ``claude stop`` it still lists as ``done``, with ``pid``
        and ``status`` gone. A session stopped in the middle of a turn lists as ``working``
        without ``pid`` and ``status`` for about a second, then as ``stopped``. A turn that
        ended in an error can list as ``failed`` with its process still alive. So the live
        fields decide.

        A state that chargehand does not recognise fails closed: a confirmed stop allows a
        resume and the removal of a worktree, so a listing it cannot read never counts.
        """
        if session.state == UNKNOWN:
            return False
        return _listed_pid(session) is None and session.raw.get("status") in (None, "")

    def wake(
        self,
        session_id: str,
        message: str,
        *,
        session_uuid: str | None = None,
        if_idle: bool = False,
    ) -> WakeResult:
        """Stop the live idle process, confirm it is gone, then resume by the full session id.

        With *if_idle*, only a stopped session, or a live one whose last turn ended
        ``done``, is woken.

        A resume against a running process, or by the short job id, starts a copy under a
        new id, and the copy works in the same worktree. Both are ruled out first, and a
        copy that starts anyway is a fault.

        A stop that exits non-zero refuses the wake. Otherwise the wake waits, up to
        ``stop_wait_secs``, until the listing shows the job stopped (see
        :meth:`is_stopped`) and the process the listing named before the stop has exited:
        the listing drops ``pid`` about a second before that process ends.
        """
        session = self._find(session_id)
        if session is None:
            return WakeResult(False, f"session {session_id} is not in the listing")
        old_pid = _listed_pid(session)
        if if_idle and not self.is_stopped(session) and session.state != DONE:
            # A turn runs, or a question or a permission prompt waits; a stop would
            # interrupt the turn or discard the prompt. The caller's listing may be older:
            # a leftover background shell starts a turn by itself, and the operator can
            # answer in agent view.
            return WakeResult(
                False, f"session {session_id} is {session.state}, not idle", busy=True
            )
        if not self.is_stopped(session):
            stopped = self.cli.stop(session_id)
            if stopped.returncode != 0:
                return WakeResult(
                    False,
                    f"the stop of session {session_id} exited {stopped.returncode}: "
                    f"{sanitize.one_line(stopped.stderr or stopped.stdout, limit=200)}",
                )
        deadline = time.monotonic() + self.stop_wait_secs
        while True:
            session = self._find(session_id)
            if session is None:
                return WakeResult(False, f"session {session_id} is gone after the stop")
            listed_stopped = self.is_stopped(session)
            process_alive = old_pid is not None and self.pid_alive(old_pid)
            if listed_stopped and not process_alive:
                break
            if time.monotonic() >= deadline:
                if not listed_stopped:
                    return WakeResult(
                        False,
                        f"session {session_id} did not stop; it lists as {session.state} "
                        f"with a live process",
                    )
                return WakeResult(
                    False, f"session {session_id} stopped, but its process {old_pid} is still running"
                )
            time.sleep(self.stop_poll_secs)
        uuid = session_uuid or session.uuid
        if not uuid:
            return WakeResult(False, f"session {session_id} has no full session id to resume by")
        result = self.cli.resume(uuid, message)
        copy = copy_started_by(result)
        if copy is not None:
            return WakeResult(False, f"the resume started a copy as {copy}", copy_id=copy)
        if result.returncode != 0:
            return WakeResult(
                False,
                f"the resume exited {result.returncode}: "
                f"{sanitize.one_line(result.stderr or result.stdout, limit=200)}",
            )
        return WakeResult(True, "resumed")

    def _find(self, session_id: str) -> Session | None:
        return next((s for s in self.cli.list_sessions() if s.id == session_id), None)

    def stop(self, session_id: str) -> CommandResult:
        return self.cli.stop(session_id)

    def respawn(self, session_id: str) -> CommandResult:
        return self.cli.respawn(session_id)

    def remove(self, session_id: str) -> CommandResult:
        return self.cli.remove(session_id)

    def logs(self, session_id: str, *, lines: int = 200) -> str:
        return self.cli.logs(session_id, lines=lines)

    def attach_command(self, session_id: str) -> str:
        return f"claude attach {session_id}"

    def fork(
        self,
        session_id: str,
        prompt: str,
        *,
        session_uuid: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CommandResult:
        return self.cli.fork(session_uuid or session_id, prompt, model=model, effort=effort)


runtime_mod.register(ClaudeRuntime)


__all__ = [
    "BLOCKED",
    "ClaudeCLI",
    "ClaudeRuntime",
    "CommandResult",
    "DONE",
    "FAILED",
    "SESSION_STATES",
    "STOPPED",
    "Session",
    "UNKNOWN",
    "WORKING",
    "agent_file_candidates",
    "copy_started_by",
    "launch_settings",
    "parse_session",
    "parse_sessions",
    "worker_from_session",
]
