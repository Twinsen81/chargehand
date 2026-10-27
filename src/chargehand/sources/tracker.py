"""A repository's tracker as an event source.

The queue becomes ``labeled`` events, and the issues that live workers follow become
``comment`` and ``done`` events. The source is named after the tracker type, so a start
rule on ``linear.labeled`` names it, and its work items carry the repository's name as
their hint. It is built per repository from ``tracker = {...}``, not from a
``[[source]]`` table.

A ``[[source]]`` may have the same name as a tracker type, for example a ``github``
source next to a GitHub Issues queue, and the two share the dedupe namespace and the
subscriptions. Every dedupe id of this source starts with :data:`DEDUPE_PREFIX`, which
tells its events apart, also after they were stored as waiting starts.

Only a worker that the queue label started calls :meth:`TrackerSource.mark`; the tick
keeps that rule.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from chargehand.config import Repository
from chargehand.errors import ItemGone
from chargehand.roles import COMMENT, DONE, LABELED
from chargehand.sources.base import Event, Source, WorkItem
from chargehand.trackers.base import Comment, Issue, Status, Tracker

DEDUPE_PREFIX = "tracker:"


class TrackerSource(Source):
    @staticmethod
    def produced(event: Event) -> bool:
        """Whether a repository's tracker, rather than a ``[[source]]``, produced *event*."""
        return event.dedupe_id.startswith(DEDUPE_PREFIX)

    def __init__(self, tracker: Tracker, repository: Repository) -> None:
        super().__init__(tracker.type, {})
        self.type = tracker.type
        self.tracker = tracker
        self.repository = repository.name
        self._queue: set[str] | None = None

    def describe(self) -> str:
        return f"{self.repository} ({self.tracker.describe()})"

    def whoami(self) -> str:
        return self.tracker.whoami()

    @property
    def marks_items(self) -> bool:
        return True

    def poll(
        self, cursor: str | None, items: Sequence[WorkItem]
    ) -> tuple[list[Event], str | None]:
        result = self.tracker.poll(cursor, [item.ref for item in items])
        now = time.time()
        self._queue = {issue.id for issue in result.queued}
        events = [self._labeled(issue, now) for issue in result.queued]
        events += [self._done(issue, now) for issue in result.followed if issue.closed]
        events += [self._comment(comment) for comment in result.comments]
        return events, result.cursor

    def item_open(self, item: WorkItem) -> bool:
        issue = self.tracker.get(item.ref)
        return issue is not None and not issue.closed

    def mark(self, item: WorkItem, status: Status) -> None:
        """Move the item's labels to *status*, verified by a re-read.

        Raises :class:`~chargehand.errors.ItemGone` when the issue no longer exists, and
        :class:`~chargehand.errors.AmbiguousWrite` when the write cannot be confirmed.
        """
        issue = self.tracker.get(item.ref)
        if issue is None:
            raise ItemGone(f"{item.key} no longer exists on the tracker")
        self.tracker.mark(issue, status)

    def marked(self, status: Status) -> list[WorkItem]:
        """The items assigned to this account that carry the label of *status*."""
        return [self.work_item(issue) for issue in self.tracker.list(status)]

    def forget_queue(self) -> None:
        """Drop the queue listing kept from the previous poll, at the start of a tick."""
        self._queue = None

    def still_queued(self, item: WorkItem) -> bool:
        """Whether the item is in the queue, as this tick's poll listed it.

        A queue start asks this first, so an issue taken out of the queue while it waited
        does not start. Without a poll in this tick, the queue is listed once.
        """
        if self._queue is None:
            self._queue = {issue.id for issue in self.tracker.list(Status.QUEUED)}
        return item.ref in self._queue

    def find(self, key: str) -> WorkItem | None:
        """The item that *key* names, or None when the tracker does not know it."""
        issue = self.tracker.get(key)
        return self.work_item(issue) if issue is not None else None

    def work_item(self, issue: Issue) -> WorkItem:
        return WorkItem(
            source=self.name,
            key=issue.identifier,
            ref=issue.id,
            url=issue.url,
            hint=self.repository,
        )

    # ----- events -------------------------------------------------------------

    def _pointer(self, issue: Issue) -> str:
        return issue.url or issue.identifier

    def _labeled(self, issue: Issue, now: float) -> Event:
        # The last-change time makes a re-armed issue news again: the label write of the
        # worker that ran before changed the issue.
        return Event(
            source=self.name,
            kind=LABELED,
            item=self.work_item(issue),
            actor="",
            occurred_at=issue.updated_at or now,
            dedupe_id=f"{DEDUPE_PREFIX}labeled:{issue.id}:{issue.updated_at or ''}",
            pointer=self._pointer(issue),
        )

    def _done(self, issue: Issue, now: float) -> Event:
        return Event(
            source=self.name,
            kind=DONE,
            item=self.work_item(issue),
            actor="",
            occurred_at=issue.updated_at or now,
            dedupe_id=f"{DEDUPE_PREFIX}done:{issue.id}:{issue.updated_at or ''}",
            pointer=self._pointer(issue),
        )

    def _comment(self, comment: Comment) -> Event:
        return Event(
            source=self.name,
            kind=COMMENT,
            item=self.work_item(comment.issue),
            actor=comment.author,
            occurred_at=comment.created_at,
            dedupe_id=f"{DEDUPE_PREFIX}comment:{comment.id}",
            pointer=self._pointer(comment.issue),
            marked=comment.marked,
        )


__all__ = ["TrackerSource"]
