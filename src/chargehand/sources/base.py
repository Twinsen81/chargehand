"""The interface every event source implements, and the records it produces.

A source is a poller. It turns what happened on a pull request or an issue into
events, and the router decides who receives them. An event names what happened and
where to look, never what was said: a comment body is read by its source at most to
find the authorship marker, and it does not leave the source.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from chargehand.config import Repository
from chargehand.trackers.base import Status

# Every comment a chargehand worker posts carries this text. Workers post as the operator,
# so the actor alone cannot tell a worker's comment from the operator's own; the marker
# can. The router drops a marked comment by the signed-in account for every worker, so
# two workers on one pull request do not wake each other in turn.
AUTHORSHIP_MARKER = "<!-- chargehand -->"


@dataclass(frozen=True)
class WorkItem:
    """An external thing that events are about: a pull request or an issue.

    ``key`` names the item inside chargehand: it becomes the worker's ``item``, its
    worktree and branch name and its session name, so it must pass
    :func:`chargehand.sanitize.is_safe_identifier` before a worker starts on it.
    ``ref`` is the source's own reference, stored as the worker's ``item_id``.
    ``hint`` says where the item lives in the source's own terms (a repository name,
    ``owner/repo``); :meth:`Source.repository_for` maps it to a configured repository.
    ``url`` is untrusted: it reaches the launch prompt as ``{item_url}``, so the launch
    keeps it only when it is a plain http(s) URL.
    """

    source: str
    key: str
    ref: str
    url: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class Event:
    """A fact from a source. There is no body field, on purpose.

    ``actor`` and ``pointer`` are untrusted text: the actor is used for filtering only,
    and the pointer is a URL or identifier the worker fetches itself. ``marked`` is set
    by the source when the comment the event is about carries :data:`AUTHORSHIP_MARKER`.
    """

    source: str
    kind: str
    item: WorkItem
    actor: str
    occurred_at: float
    dedupe_id: str
    pointer: str
    marked: bool = False


class Source(abc.ABC):
    """One module per source; only that module knows the system it polls.

    An expected failure (the system cannot be reached, a credential is missing) raises a
    :class:`~chargehand.errors.ChargehandError` subtype such as ``TrackerError``. The tick
    still contains any exception to the one source, or the one worker, that raised it.
    """

    #: The type name as written in ``[[source]] type = ...``.
    type: str = ""

    def __init__(self, name: str, options: Mapping[str, object]) -> None:
        self.name = name
        self.options = dict(options)

    @abc.abstractmethod
    def whoami(self) -> str:
        """The account the events are filtered for: the one the workers post as."""

    @abc.abstractmethod
    def poll(
        self, cursor: str | None, items: Sequence[WorkItem]
    ) -> tuple[list[Event], str | None]:
        """Events since *cursor*, and the cursor to pass next time.

        *items* are this source's items that live workers subscribe to, for a source
        that has to ask about each item it follows rather than about the account.
        """

    @abc.abstractmethod
    def item_open(self, item: WorkItem) -> bool:
        """False once the item is closed, merged or gone; garbage collection asks."""

    def mark(self, item: WorkItem, status: Status) -> None:
        """Reflect a worker's state on the item, for a source that keeps labels."""
        return None

    def repository_for(
        self, item: WorkItem, repositories: Sequence[Repository]
    ) -> Repository | None:
        """The configured repository a start event on *item* belongs to, or None.

        The default matches the item's hint against the repository names, which suits a
        source configured for one repository. A source that names items in its own terms,
        such as ``owner/repo``, overrides this.
        """
        return next((r for r in repositories if r.name == item.hint), None)

    def pull_request_for(self, repository: Repository, branch: str) -> WorkItem | None:
        """The open pull request of *branch* in *repository*, for a source that has them.

        The tick subscribes a worker to the pull request of its own branch, so that a
        comment or a CI result on it wakes the worker. Other sources have none.
        """
        return None

    def describe(self) -> str:
        return f"{self.name} ({self.type})"
