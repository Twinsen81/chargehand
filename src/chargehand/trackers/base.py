"""The four-call tracker interface every adapter implements.

The runner deliberately never reads an issue body. It needs to know who it is, what
is queued, how to move an issue between queue states, and how to re-read one issue
to verify a write. Anything more would put attacker-controlled text on the trusted
side of the prompt boundary.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """The queue states chargehand maps onto a tracker's labels."""

    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"


@dataclass(frozen=True)
class Issue:
    """The subset of an issue the runner is allowed to know about.

    ``title`` is untrusted and exists only for ``--verbose`` output; it must never
    reach a prompt. There is no body field on purpose.
    """

    id: str
    identifier: str
    url: str | None = None
    title: str = ""
    branch_name: str | None = None
    labels: tuple[str, ...] = ()
    state: str | None = None
    closed: bool = False
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)


class Tracker(abc.ABC):
    """Adapters are the only modules allowed to know a tracker's API."""

    #: Short adapter name as written in ``tracker = { type = ... }``.
    type: str = ""

    @abc.abstractmethod
    def whoami(self) -> str:
        """Identify the account whose credentials the runner is using."""

    @abc.abstractmethod
    def list(self, status: Status) -> list[Issue]:
        """Issues assigned to :meth:`whoami` currently in *status*."""

    @abc.abstractmethod
    def mark(self, issue: Issue, status: Status) -> Issue:
        """Move *issue* to *status* and verify by re-reading it.

        Raises :class:`~chargehand.errors.AmbiguousWrite` when the outcome cannot be
        established, so the caller leaves the attempt for reconciliation rather than
        assuming either result.
        """

    @abc.abstractmethod
    def get(self, issue_id: str) -> Issue | None:
        """Re-read one issue. ``None`` when it no longer exists."""

    def describe(self) -> str:
        return self.type
