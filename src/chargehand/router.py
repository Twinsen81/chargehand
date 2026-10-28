"""The router: publish and subscribe from source events to worker inboxes.

Once per tick, after the polls, every event goes through the same steps in this order:

1. Filters. An event already seen, by its source and dedupe id, is dropped. So is an
   event whose actor is the signed-in account when its comment carries the authorship
   marker. That filter covers every chargehand worker, not only the author: workers
   post as the operator, and two workers on one pull request would otherwise wake each
   other in turn.
2. Delivery. One inbox row for every live worker with a subscription to the item that
   names the event's kind, unless the event occurred before the worker started. The
   sources read some minutes back from their previous poll, so that a late write is not
   lost, and that overlap must not wake a new worker for an event that it reads by itself
   at its start. ``START_ALLOWANCE_SECS`` allows for the clock difference between a source
   and this machine.
3. Close. A worker whose primary item the event closes, by its start rule's
   ``close_on``, ends. On an item the worker only follows, the same event is an ordinary
   delivery.
4. Start. The start rules of the enabled roles, checked here in code and never by a
   model. A rule whose ``unless_subscribed`` check finds a suitable worker starts none,
   and the event goes to the inbox of the rule's own workers on the item, so a second
   review request wakes the reviewer that has the pull request rather than starting
   another one.

A start is not a delivery: no worker exists yet to receive the event. When the launch
caps defer a start, the event is kept as a pending start and offered to the start
rules again on the next tick, so the dedupe filter cannot lose it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from chargehand import roles as roles_mod
from chargehand.ledger import Ledger, Worker
from chargehand.roles import StartRule
from chargehand.sources.base import Event, WorkItem

# An event that occurred up to this long before a worker started still reaches it: the
# source's clock and this machine's clock can differ. A minute is enough for that.
START_ALLOWANCE_SECS = 60.0

# Called with an event that passed the filters; returns True when a start rule wanted a
# worker for it and the launch had to wait.
Starter = Callable[[Event], bool]
# Called with an event that passed the filters; ends the workers whose primary item it
# closes and returns how many ended.
Closer = Callable[[Event], int]


def kind_matches(kinds: Iterable[str], kind: str, source: str) -> bool:
    """A subscription or a rule names a kind bare (any source) or qualified by source."""
    names = set(kinds)
    return kind in names or f"{source}.{kind}" in names


def before_start(event: Event, worker: Worker) -> bool:
    """Whether *event* occurred before *worker* started, beyond the clock allowance.

    The cutoff is the worker's start, which is also when its primary subscription is made.
    A pull request that the worker follows is subscribed up to one tick after the push, so a
    comment in that gap is older than the follow; it still reaches the worker, because the
    cutoff is the start of the worker and not the time of the follow.
    """
    return event.occurred_at < worker.created_at - START_ALLOWANCE_SECS


def suitable_worker_exists(ledger: Ledger, rule: StartRule, role: str, item: WorkItem) -> bool:
    """The rule's ``unless_subscribed`` check: does a live worker already have *item*?

    ``role`` asks about workers of the rule's own role, ``any`` about workers of every
    role. A worker has the item when it subscribes to it, or when the item is its own.
    """
    any_role = rule.unless_subscribed == roles_mod.UNLESS_ANY
    for _, worker in ledger.live_subscriptions(item.source, item.key):
        if any_role or worker.role == role:
            return True
    return ledger.live_worker_for(item.key, None if any_role else role) is not None


def deliver_to_holders(ledger: Ledger, role: str, event: Event) -> int:
    """Put a start event into the inbox of the live workers of *role* that have its item.

    A start event that finds its worker is news for that worker, whether or not its
    subscription names the kind: a second review request asks the reviewer to look again.
    The inbox is unique per worker and event, so a subscriber that already got it gets
    no second row. A worker that started more than ``START_ALLOWANCE_SECS`` after
    the event gets none (``before_start``).
    """
    holders = {
        worker.id: worker
        for _, worker in ledger.live_subscriptions(event.item.source, event.item.key)
        if worker.role == role
    }
    own = ledger.live_worker_for(event.item.key, role)
    if own is not None:
        holders[own.id] = own
    return sum(
        int(ledger.enqueue(worker_id, event))
        for worker_id, worker in sorted(holders.items())
        if not before_start(event, worker)
    )


@dataclass
class RouteResult:
    duplicates: int = 0
    own: int = 0
    delivered: int = 0
    deferred: int = 0
    closed: int = 0
    # Subscribers that did not get an event because it occurred before they started.
    skipped_before_start: int = 0

    def merge(self, other: "RouteResult") -> None:
        self.duplicates += other.duplicates
        self.own += other.own
        self.delivered += other.delivered
        self.deferred += other.deferred
        self.closed += other.closed
        self.skipped_before_start += other.skipped_before_start


class Router:
    def __init__(self, ledger: Ledger, *, start: Starter, close: Closer | None = None) -> None:
        self.ledger = ledger
        self._start = start
        self._close = close

    def route(self, events: Iterable[Event], *, signed_in: str) -> RouteResult:
        """Filter, deliver and start, for the events of one source's poll.

        *signed_in* is the account the source reports as its own, from ``whoami``.
        """
        result = RouteResult()
        for event in events:
            if self.ledger.event_seen(event.source, event.dedupe_id):
                result.duplicates += 1
                continue
            if event.marked and event.actor == signed_in:
                self.ledger.record_event(event)
                result.own += 1
                continue
            delivered, skipped = self.deliver(event)
            result.delivered += delivered
            result.skipped_before_start += skipped
            if self._close is not None:
                result.closed += self._close(event)
            deferred = self._start(event)
            self.ledger.record_event(event, start_pending=deferred)
            result.deferred += int(deferred)
        return result

    def deliver(self, event: Event) -> tuple[int, int]:
        """Put *event* into the inbox of each matching subscriber.

        Returns the rows added and the matching subscribers skipped by ``before_start``.
        """
        delivered = skipped = 0
        for subscription, worker in self.ledger.live_subscriptions(event.source, event.item.key):
            if not kind_matches(subscription.kinds, event.kind, event.source):
                continue
            if before_start(event, worker):
                skipped += 1
                continue
            delivered += int(self.ledger.enqueue(subscription.worker_id, event))
        return delivered, skipped

    def retry_pending_starts(self) -> RouteResult:
        """Offer the starts that the caps deferred on an earlier tick to the rules again."""
        result = RouteResult()
        for event in self.ledger.pending_start_events():
            if self._start(event):
                result.deferred += 1
            else:
                self.ledger.settle_start(event)
        return result


__all__ = [
    "Closer",
    "RouteResult",
    "Router",
    "START_ALLOWANCE_SECS",
    "Starter",
    "before_start",
    "deliver_to_holders",
    "kind_matches",
    "suitable_worker_exists",
]
