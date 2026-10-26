"""The runtime interface: what chargehand needs from a coding agent.

A runtime starts an agent session in a worktree, observes it, wakes it with a message,
stops it, and says how an operator opens it. Each runtime is one module, and that module
is the only one that knows the agent's command line. ``chargehand.claude`` is the
``claude`` runtime, the default and the only one that ships.

Runtimes differ in what they can do, so each declares :class:`Capabilities`. The tick,
the CLI and later the board check a capability, and never compare a runtime's name.

A role file stays flat: the common keys sit at the top level next to the keys of its
runtime, and the runtime checks its own keys. Session ids are opaque text: only the
runtime that issued one reads it.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from chargehand.errors import AgentRuntimeError, ConfigError

if TYPE_CHECKING:
    from chargehand.config import MachineConfig
    from chargehand.roles import Role

DEFAULT_RUNTIME = "claude"

# The session states every runtime maps its own vocabulary onto.
WORKING = "working"
BLOCKED = "blocked"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
UNKNOWN = "unknown"

SESSION_STATES = (WORKING, BLOCKED, DONE, FAILED, STOPPED, UNKNOWN)

# The states in which a stop counts as confirmed: no turn is running.
AT_REST = (DONE, STOPPED, FAILED)


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
    kind: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def cwd_path(self) -> Path | None:
        return Path(self.cwd) if self.cwd else None

    def age_secs(self, now: float | None = None) -> float | None:
        if self.started_at is None:
            return None
        return (time.time() if now is None else now) - self.started_at


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Capabilities:
    """What a runtime's agent supports. A missing capability is a feature the board and delivery skip.

    A flag describes the agent, not a method of this interface: a feature that uses it
    brings its own call when it is built. In particular ``wake()`` is always the
    stop-and-resume path; a wake into a live session, for a runtime with
    ``in_place_wake``, goes through the in-session channel, which is a separate path.
    Usage readings and board permissions have no call here yet for the same reason.

    ``live_attach``: an operator can open the running session without stopping it.
    ``in_place_wake``: a message reaches a live session without a restart.
    ``board_permissions``: the session's permission prompts can be answered from the board.
    ``usage_readings``: the runtime reports its provider's usage windows.
    ``fork``: a session can be copied into a new one that keeps the conversation.
    ``external_supervisor``: a supervisor outside chargehand hosts the session processes;
    without it chargehand hosts them, and a process ends with the runner.
    """

    live_attach: bool = False
    in_place_wake: bool = False
    board_permissions: bool = False
    usage_readings: bool = False
    fork: bool = False
    external_supervisor: bool = False

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


# Statuses of a health check, as `doctor` prints them.
CHECK_OK = "ok"
CHECK_INFO = "info"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"


@dataclass(frozen=True)
class HealthCheck:
    """One line of `doctor` output about a runtime. *detail* is written by the runtime module."""

    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class LaunchSpec:
    """One new session. *options* are the role's runtime-owned keys, already checked."""

    worktree: Path
    name: str
    prompt: str
    options: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class LaunchResult:
    """What a launch printed, for an error message."""

    output: str = ""


@dataclass(frozen=True)
class WakeResult:
    """The outcome of a wake. *detail* is written by the runtime module, never by the agent.

    *copy_id* is set when the runtime started a new session instead of continuing the
    old one. That is a fault: the worker's session did not get the message.
    """

    ok: bool
    detail: str = ""
    copy_id: str | None = None


class Runtime(ABC):
    """One coding agent behind the interface the tick, the CLI and reconciliation use."""

    name: ClassVar[str]
    capabilities: ClassVar[Capabilities]
    # The role-file keys this runtime owns. Any other key that is not a common one is
    # an error in a role on this runtime.
    role_keys: ClassVar[tuple[str, ...]] = ()

    # ----- configuration ----------------------------------------------------

    @classmethod
    @abstractmethod
    def from_config(cls, config: "MachineConfig") -> "Runtime":
        """The runtime as the machine configuration sets it up."""

    @classmethod
    def parse_role_options(cls, table: Mapping[str, object], where: str) -> dict[str, object]:
        """Check this runtime's keys of a role file and return them with their defaults."""
        return dict(table)

    @classmethod
    def check_role(cls, role: "Role", repo: Path) -> None:
        """Check what a role needs outside its file, such as an agent definition. Raise ConfigError."""

    # ----- sessions ---------------------------------------------------------

    @abstractmethod
    def list_sessions(self) -> list[Session]:
        """Every session this runtime knows, with its state mapped onto SESSION_STATES."""

    @abstractmethod
    def launch(self, spec: LaunchSpec, *, warn: Callable[[str], None] | None = None) -> LaunchResult:
        """Start a session in *spec.worktree*, named *spec.name*.

        The tick finds the new session in the listing by name or working directory, so a
        launch does not return its id. *warn* takes a problem that does not stop the
        launch, also when the launch fails afterwards.
        """

    @abstractmethod
    def wake(self, session_id: str, message: str, *, session_uuid: str | None = None) -> WakeResult:
        """Give the session *message* as its next turn, so that the same session continues.

        A result with ``ok`` False is a fault the runtime detected, such as a session that
        did not stop or a copy that started. A failure to run the agent at all (missing
        binary, timeout, unreadable listing) raises AgentRuntimeError; then nothing is
        known about whether the message arrived, and the caller treats it like a failed
        call to the agent, not like a refused wake.
        """

    @abstractmethod
    def stop(self, session_id: str) -> CommandResult:
        """Stop the session's process and keep the session. The caller confirms in the listing."""

    @abstractmethod
    def respawn(self, session_id: str) -> CommandResult:
        """Start a stopped session's process again, without a new message."""

    @abstractmethod
    def remove(self, session_id: str) -> CommandResult:
        """Delete the session. The caller confirms in the listing."""

    @abstractmethod
    def logs(self, session_id: str, *, lines: int = 200) -> str:
        """The last *lines* of the session's output. Untrusted text."""

    @abstractmethod
    def attach_command(self, session_id: str) -> str:
        """The command an operator runs to open the session in a terminal."""

    @abstractmethod
    def fork(
        self,
        session_id: str,
        prompt: str,
        *,
        session_uuid: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CommandResult:
        """Start a new session that continues a copy of this one's conversation."""

    def health_checks(self) -> list[HealthCheck]:
        """What `doctor` reports about this runtime. By default: whether the listing works."""
        label = f"{self.name} sessions"
        try:
            sessions = self.list_sessions()
        except AgentRuntimeError as exc:
            return [HealthCheck(label, CHECK_FAIL, str(exc))]
        return [HealthCheck(label, CHECK_OK, f"{len(sessions)} session(s) listed")]

    def find_session(
        self,
        sessions: Sequence[Session],
        *,
        name: str | None = None,
        cwd: Path | None = None,
        exclude: Collection[str] = (),
    ) -> Session | None:
        """Adoption lookup, shared by the last launch step and reconciliation."""
        return find_session(sessions, name=name, cwd=cwd, exclude=exclude)


def find_session(
    sessions: Sequence[Session],
    *,
    name: str | None = None,
    cwd: Path | None = None,
    exclude: Collection[str] = (),
) -> Session | None:
    """The session in *sessions* that works in *cwd*, else the one named *name*.

    *exclude* keeps a session that another attempt already owns out of the match. A
    stopped session keeps its name and working directory, so without it a retry of the
    same item would adopt the run it is replacing.
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


def _resolve(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


_ROLE_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_ITEM = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_HANDLE = re.compile(rf"({_ROLE_NAME})/({_ITEM})")
_LEGACY_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")

LEGACY_ROLE = "implementer"


def worker_from_session(session: Session, worktree_root: Path) -> tuple[str, str] | None:
    """Derive ``(role, item)`` from an unadopted session's name or path.

    Used by the session side of reconciliation, so a session started by a tick that
    died before recording its ids is adopted instead of left unsupervised. A session
    named after its worker reads ``role/item``; a session from before roles existed is
    named after the issue alone and sits in ``<root>/<issue>``, so that shape maps to
    the ``implementer`` role.
    """
    if session.name:
        name = session.name.strip()
        match = _HANDLE.fullmatch(name)
        if match:
            return match.group(1), match.group(2)
        if _LEGACY_IDENTIFIER.fullmatch(name):
            return LEGACY_ROLE, name
    if session.cwd:
        try:
            relative = Path(session.cwd).resolve().relative_to(worktree_root.resolve())
        except (ValueError, OSError):
            relative = None
        if relative is not None:
            parts = relative.parts
            if len(parts) >= 2 and re.fullmatch(_ROLE_NAME, parts[0]) and re.fullmatch(_ITEM, parts[1]):
                return parts[0], parts[1]
            if len(parts) == 1 and _LEGACY_IDENTIFIER.fullmatch(parts[0]):
                return LEGACY_ROLE, parts[0]
    return None


# ----- the registry ---------------------------------------------------------

_REGISTRY: dict[str, type[Runtime]] = {}


def register(runtime: type[Runtime]) -> None:
    """Make *runtime* available by its name. The test suite registers its fake this way."""
    _REGISTRY[runtime.name] = runtime


def _load_builtins() -> None:
    # Imported here and not at the top: the claude module imports this one.
    from chargehand import claude  # noqa: F401


def names() -> tuple[str, ...]:
    _load_builtins()
    return tuple(sorted(_REGISTRY))


def get(name: str) -> type[Runtime]:
    _load_builtins()
    runtime = _REGISTRY.get(name)
    if runtime is None:
        raise ConfigError(f"unknown runtime '{name}'; expected one of {', '.join(names())}")
    return runtime


def create(name: str, config: "MachineConfig") -> Runtime:
    return get(name).from_config(config)


__all__ = [
    "AT_REST",
    "BLOCKED",
    "Capabilities",
    "CommandResult",
    "DEFAULT_RUNTIME",
    "DONE",
    "CHECK_FAIL",
    "CHECK_INFO",
    "CHECK_OK",
    "CHECK_WARN",
    "FAILED",
    "HealthCheck",
    "LaunchResult",
    "LaunchSpec",
    "Runtime",
    "SESSION_STATES",
    "STOPPED",
    "Session",
    "UNKNOWN",
    "WORKING",
    "WakeResult",
    "create",
    "find_session",
    "get",
    "worker_from_session",
    "names",
    "register",
]
