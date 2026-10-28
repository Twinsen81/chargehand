"""An in-memory tracker adapter, with the fault injection the acceptance checks need.

Registered under the type name ``fake``. A route selects one board with
``tracker = { type = "fake", board = "<name>" }``.
"""

from __future__ import annotations

import dataclasses
import itertools
import time
from collections.abc import Mapping, Sequence
from typing import Any

from chargehand.errors import AmbiguousWrite, TrackerError
from chargehand.sources import is_marked
from chargehand.trackers import register
from chargehand.trackers.base import Comment, Issue, Status, Tracker, TrackerPoll

_BOARDS: dict[str, "Board"] = {}
_COUNTER = itertools.count(1)


class _Issues(dict):
    """Every write is a change, as on a real tracker: it moves the issue's ``updated_at``."""

    def __init__(self, board: "Board") -> None:
        super().__init__()
        self._board = board

    def __setitem__(self, key: str, issue: Issue) -> None:
        super().__setitem__(key, dataclasses.replace(issue, updated_at=self._board.next_time()))


class Board:
    """The tracker's side of the world, shared by every adapter instance for a board."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.issues: dict[str, Issue] = _Issues(self)
        self.comments: list[Comment] = []
        self.viewer = "tester@example.invalid"
        self.calls: list[tuple[str, str]] = []
        # Near this machine's clock: the router drops an event from before its
        # subscriber started, and a worker's start is stamped with time.time().
        self.clock = float(int(time.time()))
        # Fault injection, each consumed once.
        self.fail_next_write: str | None = None
        self.silently_drop_next_write = False
        self.land_then_report_failure = False
        self.fail_next_list: str | None = None

    def next_time(self) -> float:
        self.clock += 1.0
        return self.clock

    def add(
        self,
        identifier: str,
        *,
        labels: tuple[str, ...] = ("chargehand",),
        title: str = "Do the thing",
        closed: bool = False,
    ) -> Issue:
        issue = Issue(
            id=f"id-{next(_COUNTER)}",
            identifier=identifier,
            url=f"https://tracker.invalid/{identifier}",
            title=title,
            branch_name=f"feature/{identifier.lower()}",
            labels=labels,
            state="Todo",
            closed=closed,
        )
        self.issues[issue.id] = issue
        return self.issues[issue.id]

    def comment(
        self, identifier: str, *, author: str = "someone@example.invalid", body: str = "a reply"
    ) -> Comment:
        """Post a comment. As in an adapter, only whether the body carries the marker is kept."""
        issue = self.by_identifier(identifier)
        if issue is None:
            raise KeyError(identifier)
        comment = Comment(
            id=f"comment-{next(_COUNTER)}",
            issue=issue,
            author=author,
            created_at=self.next_time(),
            marked=is_marked(body),
        )
        self.comments.append(comment)
        return comment

    def by_identifier(self, identifier: str) -> Issue | None:
        for issue in self.issues.values():
            if issue.identifier == identifier:
                return issue
        return None

    def labels_of(self, identifier: str) -> tuple[str, ...]:
        issue = self.by_identifier(identifier)
        return issue.labels if issue else ()

    def close(self, identifier: str) -> None:
        issue = self.by_identifier(identifier)
        if issue:
            self.issues[issue.id] = Issue(**{**issue.__dict__, "closed": True, "state": "Done"})


def board(name: str = "default") -> Board:
    return _BOARDS.setdefault(name, Board(name))


def reset() -> None:
    _BOARDS.clear()


class FakeTracker(Tracker):
    type = "fake"

    def __init__(self, options: Mapping[str, Any], labels: Mapping[str, str]) -> None:
        self.board = board(str(options.get("board", "default")))
        self.labels = dict(labels)

    def whoami(self) -> str:
        return self.board.viewer

    def poll(self, cursor: str | None, followed: Sequence[str]) -> TrackerPoll:
        """The default poll, plus every comment on the followed issues; the router drops
        the ones it has seen."""
        result = super().poll(cursor, followed)
        current = {issue.id: issue for issue in result.followed}
        result.comments = [
            dataclasses.replace(comment, issue=current[comment.issue.id])
            for comment in self.board.comments
            if comment.issue.id in current
        ]
        return result

    def list(self, status: Status) -> list[Issue]:
        self.board.calls.append(("list", status.value))
        if self.board.fail_next_list:
            message, self.board.fail_next_list = self.board.fail_next_list, None
            raise TrackerError(message)
        label = self.labels.get(status.value)
        if label is None:
            raise TrackerError("cannot list by 'done'")
        return [
            issue
            for issue in self.board.issues.values()
            if any(name.lower() == label.lower() for name in issue.labels) and not issue.closed
        ]

    def get(self, issue_id: str) -> Issue | None:
        self.board.calls.append(("get", issue_id))
        found = self.board.issues.get(issue_id)
        if found is None:
            found = self.board.by_identifier(issue_id)
        return found

    def mark(self, issue: Issue, status: Status) -> Issue:
        self.board.calls.append(("mark", f"{issue.identifier}:{status.value}"))
        if self.board.fail_next_write:
            message, self.board.fail_next_write = self.board.fail_next_write, None
            raise AmbiguousWrite(message)

        target = self.labels.get(status.value) if status is not Status.DONE else None
        managed = {name.lower() for name in self.labels.values()}
        kept = tuple(name for name in issue.labels if name.lower() not in managed)
        new_labels = kept + ((target,) if target else ())

        if self.board.silently_drop_next_write:
            # The write reports success but does not land: the re-read must catch it.
            self.board.silently_drop_next_write = False
            raise AmbiguousWrite(
                f"fake: label write on {issue.identifier} reported success but did not land"
            )

        self.board.issues[issue.id] = Issue(**{**issue.__dict__, "labels": new_labels})
        updated = self.board.issues[issue.id]

        if self.board.land_then_report_failure:
            # The write landed; the caller is told it failed. Reconciliation must settle it.
            self.board.land_then_report_failure = False
            raise AmbiguousWrite(f"fake: 502 after the write on {issue.identifier} landed")
        return updated


register(FakeTracker)
