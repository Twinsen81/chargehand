"""Exception types shared across the package."""


class ChargehandError(Exception):
    """Base class for every error chargehand raises deliberately."""


class ConfigError(ChargehandError):
    """The machine or repository configuration is missing, unreadable, or invalid."""


class TrackerError(ChargehandError):
    """A tracker call failed, or returned something the adapter cannot use."""


class TrackerAuthError(TrackerError):
    """The tracker's credentials are missing or unreadable.

    A subclass of TrackerError on purpose: from the tick's point of view a locked
    keychain and an unreachable API are the same transient condition. Keeping it out
    of ConfigError is what lets a genuinely broken configuration fail loudly instead.
    """


class AmbiguousWrite(TrackerError):
    """A tracker write may or may not have landed.

    Raised instead of a plain failure so the caller leaves the attempt for
    reconciliation rather than assuming either outcome.
    """


class ItemGone(ChargehandError):
    """The work item no longer exists on its source.

    Not a TrackerError: the tick treats those as a passing outage and tries again, and an
    item that is gone does not come back.
    """


class AgentRuntimeError(ChargehandError):
    """A runtime's agent is missing, failed, or returned output its adapter cannot use."""


class ClaudeError(AgentRuntimeError):
    """The Claude Code CLI is missing, failed, or returned unparseable output."""


class WorkspaceNotTrusted(ClaudeError):
    """Claude Code refused to start a session because its directory is not trusted.

    A subclass of ClaudeError so that a caller that does not handle it gets the usual
    launch failure.
    """


class GitError(ChargehandError):
    """A git command failed."""


class TickBusy(ChargehandError):
    """Another tick holds the lock."""


class LaunchAborted(ChargehandError):
    """Fault injection stopped a launch after a given step."""

    def __init__(self, step: int) -> None:
        super().__init__(f"launch aborted after step {step}")
        self.step = step
