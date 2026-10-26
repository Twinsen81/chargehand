"""The tracker interface every adapter implements.

The runner deliberately never reads an issue body. It needs to know who it is, what
is queued, how to move an issue between queue states, and how to re-read one issue
to verify a write. Anything more would put attacker-controlled text on the trusted
side of the prompt boundary.

The tick reaches a tracker through :class:`chargehand.sources.tracker.TrackerSource`,
which turns :meth:`Tracker.poll` into events. An adapter that can ask "what changed
since" overrides ``poll``; the default lists the whole queue on every poll.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
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
    reach a prompt. There is no body field on purpose. ``updated_at`` is the tracker's
    own last-change time in epoch seconds, when the adapter knows it: a labeled event is
    news again once the issue changed, so a re-armed issue starts a new worker.
    """

    id: str
    identifier: str
    url: str | None = None
    title: str = ""
    branch_name: str | None = None
    labels: tuple[str, ...] = ()
    state: str | None = None
    closed: bool = False
    updated_at: float | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Comment:
    """A comment on an issue, without its text.

    The adapter reads the body only to set ``marked``, when it carries the authorship
    marker; the text does not leave the adapter. ``author`` is untrusted and names the
    account the way :meth:`Tracker.whoami` does, so the marker filter can compare them.
    """

    id: str
    issue: Issue
    author: str
    created_at: float
    marked: bool = False


@dataclass
class TrackerPoll:
    """One poll: the queue, the followed issues as they are now, their new comments."""

    queued: list[Issue] = field(default_factory=list)
    followed: list[Issue] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    cursor: str | None = None


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

    def poll(self, cursor: str | None, followed: Sequence[str]) -> TrackerPoll:
        """The queue, and the issues in *followed* (ids), since *cursor*.

        The default lists the whole queue and re-reads each followed issue on every poll,
        reports no comments, and keeps no cursor. The router drops what it has seen before.
        """
        queued = self.list(Status.QUEUED)
        issues = [issue for issue in (self.get(issue_id) for issue_id in followed) if issue]
        return TrackerPoll(queued=queued, followed=issues, cursor=cursor)

    def describe(self) -> str:
        return self.type
