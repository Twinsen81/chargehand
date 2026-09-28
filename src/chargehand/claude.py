"""The only module that knows the Claude Code command line.

Everything else works on :class:`Session` values. Swapping in a second agent backend
means reimplementing this interface — launch, list, stop, respawn, remove, logs — and
nothing else.

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
import os
import re
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chargehand import sanitize
from chargehand.errors import ClaudeError

WORKING = "working"
BLOCKED = "blocked"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
UNKNOWN = "unknown"

SESSION_STATES = (WORKING, BLOCKED, DONE, FAILED, STOPPED, UNKNOWN)

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
_PID_KEYS = ("pid", "processId", "process_id")
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


@dataclass(frozen=True)
class Session:
    id: str
    uuid: str | None = None
    name: str | None = None
    cwd: str | None = None
    state: str = UNKNOWN
    raw_state: str = ""
    waiting_for: str | None = None
    started_at: float | None = None
    pr_url: str | None = None
    # Carried because the lease pool needs an owner process to test for liveness.
    pid: int | None = None
    kind: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def cwd_path(self) -> Path | None:
        return Path(self.cwd) if self.cwd else None

    def age_secs(self, now: float | None = None) -> float | None:
        if self.started_at is None:
            return None
        return (time.time() if now is None else now) - self.started_at


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
        pid=(lambda v: int(v) if isinstance(v, int) and not isinstance(v, bool) else None)(
            _first(node, _PID_KEYS)
        ),
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


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


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
        if check and completed.returncode != 0:
            raise ClaudeError(
                f"`{' '.join(argv)}` exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[:500]}"
            )
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
        settings: Mapping[str, Any] | None = None,
        timeout_secs: float = 180.0,
    ) -> CommandResult:
        """Start a background session inside *worktree*.

        Starting inside a linked worktree is deliberate: Claude Code skips its own
        background-session worktree isolation, so no nested worktree appears.
        """
        args = ["--bg", "-n", name, "--permission-mode", permission_mode]
        if settings:
            args += ["--settings", json.dumps(settings, sort_keys=True)]
        args.append(prompt)
        return self._run(args, cwd=worktree, timeout=timeout_secs)

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
        """Adoption lookup, shared by launch step 4 and reconciliation.

        *exclude* keeps a session that another attempt already owns out of the match. A
        stopped session keeps its name and working directory, so without it a retry of
        the same issue would adopt the run it is replacing.
        """
        candidates = [s for s in sessions if s.id not in exclude]
        resolved = _resolve(cwd) if cwd else None
        if resolved is not None:
            for session in candidates:
                if session.cwd and _resolve(Path(session.cwd)) == resolved:
                    return session
        if name:
            for session in candidates:
                if session.name == name:
                    return session
        return None

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


def _resolve(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


_IDENTIFIER_IN_PATH = re.compile(r"([A-Za-z][A-Za-z0-9]*-\d+)")


def identifier_from_session(session: Session, worktree_root: Path) -> str | None:
    """Derive an issue identifier from an unadopted session's name or path.

    Used by the session side of reconciliation, so a session started by a tick that
    died before recording its ids is adopted instead of left unsupervised.
    """
    if session.name:
        match = _IDENTIFIER_IN_PATH.fullmatch(session.name.strip())
        if match:
            return match.group(1)
    if session.cwd:
        try:
            relative = Path(session.cwd).resolve().relative_to(worktree_root.resolve())
        except (ValueError, OSError):
            relative = None
        if relative is not None and relative.parts:
            match = _IDENTIFIER_IN_PATH.fullmatch(relative.parts[0])
            if match:
                return match.group(1)
    return None
