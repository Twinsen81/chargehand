"""The router: publish and subscribe from source events to worker inboxes.

Once per tick, after the polls, every event goes through the same steps in this order:

1. Filters. An event already seen, by its source and dedupe id, is dropped. So is an
   event whose actor is the signed-in account when its comment carries the authorship
   marker. That filter covers every chargehand worker, not only the author: workers
   post as the operator, and two workers on one pull request would otherwise wake each
   other in turn.
2. Delivery. One inbox row for every live worker with a subscription to the item that
   names the event's kind.
3. Start. The start rules of the enabled roles, checked here in code and never by a
   model. A rule whose ``unless_subscribed`` check finds a suitable worker does nothing,
   so a second review request wakes the reviewer that has the pull request rather than
   starting another one.

A start is not a delivery: no worker exists yet to receive the event. When the launch
caps defer a start, the event is kept as a pending start and offered to the start
rules again on the next tick, so the dedupe filter cannot lose it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from chargehand import roles as roles_mod
from chargehand.ledger import Ledger
from chargehand.roles import StartRule
from chargehand.sources.base import Event, WorkItem

# Called with an event that passed the filters; returns True when a start rule wanted a
# worker for it and the launch had to wait.
Starter = Callable[[Event], bool]


def kind_matches(kinds: Iterable[str], kind: str, source: str) -> bool:
    """A subscription or a rule names a kind bare (any source) or qualified by source."""
    names = set(kinds)
    return kind in names or f"{source}.{kind}" in names


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


@dataclass
class RouteResult:
    duplicates: int = 0
    own: int = 0
    delivered: int = 0
    deferred: int = 0

    def merge(self, other: "RouteResult") -> None:
        self.duplicates += other.duplicates
        self.own += other.own
        self.delivered += other.delivered
        self.deferred += other.deferred


class Router:
    def __init__(self, ledger: Ledger, *, start: Starter) -> None:
        self.ledger = ledger
        self._start = start

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
            result.delivered += self.deliver(event)
            deferred = self._start(event)
            self.ledger.record_event(event, start_pending=deferred)
            result.deferred += int(deferred)
        return result

    def deliver(self, event: Event) -> int:
        delivered = 0
        for subscription, _ in self.ledger.live_subscriptions(event.source, event.item.key):
            if kind_matches(subscription.kinds, event.kind, event.source):
                delivered += int(self.ledger.enqueue(subscription.worker_id, event))
        return delivered

    def retry_pending_starts(self) -> RouteResult:
        """Offer the starts that the caps deferred on an earlier tick to the rules again."""
        result = RouteResult()
        for event in self.ledger.pending_start_events():
            if self._start(event):
                result.deferred += 1
            else:
                self.ledger.settle_start(event)
        return result


__all__ = ["RouteResult", "Router", "Starter", "kind_matches", "suitable_worker_exists"]
