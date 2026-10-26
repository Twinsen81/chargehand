"""The fake tracker as an event source.

Registered as source type ``fake``. It reads the same in-memory boards as the fake
tracker, so an item on a board is both an issue the queue can start and a work item
that events are about. A test emits events onto a board's feed; each poll returns the
events after the cursor.

    [[source]]
    name = "fake"
    type = "fake"
    board = "default"
    repository = "test"   # the configured repository its start events belong to
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from chargehand.errors import TrackerError
from chargehand.sources import register
from chargehand.sources.base import Event, Source, WorkItem
from support import fake_tracker

OTHER_ACTOR = "someone-else@example.invalid"

_COUNTER = itertools.count(1)


@dataclass
class Feed:
    """What the source will report, and what it was asked."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    polls: list[tuple[str | None, tuple[WorkItem, ...]]] = field(default_factory=list)
    fail_next_poll: str | None = None

    def emit(
        self,
        kind: str,
        item: str,
        *,
        actor: str = OTHER_ACTOR,
        dedupe_id: str | None = None,
        pointer: str | None = None,
        marked: bool = False,
        occurred_at: float = 1.0,
    ) -> str:
        """Queue one event for the next poll and return its dedupe id."""
        dedupe = dedupe_id or f"evt-{next(_COUNTER)}"
        self.entries.append(
            {
                "kind": kind,
                "item": item,
                "actor": actor,
                "dedupe_id": dedupe,
                "pointer": pointer or f"https://tracker.invalid/{item}#{dedupe}",
                "marked": marked,
                "occurred_at": occurred_at,
            }
        )
        return dedupe


_FEEDS: dict[str, Feed] = {}


def feed(board: str = "default") -> Feed:
    return _FEEDS.setdefault(board, Feed())


def reset() -> None:
    _FEEDS.clear()


class FakeSource(Source):
    type = "fake"

    def __init__(self, name: str, options: Mapping[str, object]) -> None:
        super().__init__(name, options)
        self.board = fake_tracker.board(str(options.get("board", "default")))
        self.feed = feed(self.board.name)
        repository = options.get("repository")
        self.repository = str(repository) if repository is not None else None

    def whoami(self) -> str:
        return self.board.viewer

    def poll(
        self, cursor: str | None, items: Sequence[WorkItem]
    ) -> tuple[list[Event], str | None]:
        self.feed.polls.append((cursor, tuple(items)))
        if self.feed.fail_next_poll:
            message, self.feed.fail_next_poll = self.feed.fail_next_poll, None
            raise TrackerError(message)
        start = int(cursor or 0)
        events = [self._event(entry) for entry in self.feed.entries[start:]]
        return events, str(len(self.feed.entries))

    def item_open(self, item: WorkItem) -> bool:
        issue = self.board.by_identifier(item.key)
        return issue is not None and not issue.closed

    def work_item(self, key: str) -> WorkItem:
        issue = self.board.by_identifier(key)
        return WorkItem(
            source=self.name,
            key=key,
            ref=issue.id if issue else key,
            url=issue.url if issue else None,
            hint=self.repository,
        )

    def _event(self, entry: dict[str, Any]) -> Event:
        return Event(
            source=self.name,
            kind=entry["kind"],
            item=self.work_item(entry["item"]),
            actor=entry["actor"],
            occurred_at=entry["occurred_at"],
            dedupe_id=entry["dedupe_id"],
            pointer=entry["pointer"],
            marked=entry["marked"],
        )


register(FakeSource)
