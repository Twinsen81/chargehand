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

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from chargehand import roles as roles_mod
from chargehand import sanitize
from chargehand.ledger import Ledger, Worker
from chargehand.roles import StartRule
from chargehand.sources.base import Event, WorkItem

log = logging.getLogger(__name__)

# The longest dedupe id an event's log line carries. The id finds the event's row in the
# ledger's `events` table; a source builds it, and it names no actor.
DEDUPE_ID_LIMIT = 80

# An event that occurred up to this long before a worker started still reaches it: the
# source's clock and this machine's clock can differ. A minute is enough for that.
START_ALLOWANCE_SECS = 60.0



@dataclass(frozen=True)
class StartResult:
    """What the start rules did with an event. True when a launch has to wait.

    *outcome* is chargehand's own phrase for the log, such as the worker it launched or
    why the start waits; None when no rule wanted a worker, and empty when the start step
    has nothing to say about the event.
    """

    deferred: bool = False
    outcome: str | None = None

    def __bool__(self) -> bool:
        return self.deferred


# Called with an event that passed the filters; true when a start rule wanted a worker for
# it and the launch had to wait. A StartResult also says what happened, for the log.
Starter = Callable[[Event], "bool | StartResult"]
# Called with an event that passed the filters; ends the workers whose primary item it
# closes and returns how many ended.
Closer = Callable[[Event], int]


def kind_matches(kinds: Iterable[str], kind: str, source: str) -> bool:
    """A subscription or a rule names a kind bare (any source) or qualified by source."""
    names = set(kinds)
    return kind in names or f"{source}.{kind}" in names


def worker_start(ledger: Ledger, worker: Worker) -> float:
    """When *worker* started, for delivery: its row, or its primary subscription if older.

    A worker's primary subscription is made with its row. An adopted session gets a new
    row, but its primary subscription keeps the time of the row it replaces, because the
    session has run since then. The row itself keeps the time of the adoption, so a status
    file of the earlier run stays stale.
    """
    primary = ledger.primary_subscription(worker)
    if primary is None:
        return worker.created_at
    return min(worker.created_at, primary.created_at)


def before_start(event: Event, started: float) -> bool:
    """Whether *event* occurred before a worker *started*, beyond the clock allowance.

    The cutoff is the worker's start (``worker_start``), not the time of the subscription
    that matches. A pull request that the worker follows is subscribed up to one tick after
    the push, so a comment in that gap is older than the follow; it still reaches the worker.
    """
    return event.occurred_at < started - START_ALLOWANCE_SECS


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


def deliver_to_holders(ledger: Ledger, role: str, event: Event) -> tuple[int, int]:
    """Put a start event into the inbox of the live workers of *role* that have its item.

    A start event that finds its worker is news for that worker, whether or not its
    subscription names the kind: a second review request asks the reviewer to look again.
    The inbox is unique per worker and event, so a subscriber that already got it gets
    no second row. A worker that started more than ``START_ALLOWANCE_SECS`` after
    the event gets none (``before_start``). Returns the rows added and the holders skipped.
    """
    holders = {
        worker.id: worker
        for _, worker in ledger.live_subscriptions(event.item.source, event.item.key)
        if worker.role == role
    }
    own = ledger.live_worker_for(event.item.key, role)
    if own is not None:
        holders[own.id] = own
    delivered = skipped = 0
    for worker_id, worker in sorted(holders.items()):
        if before_start(event, worker_start(ledger, worker)):
            skipped += 1
        else:
            delivered += int(ledger.enqueue(worker_id, event))
    return delivered, skipped


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
                log.info("%s: dropped, a marked comment by the signed-in account",
                         event_name(event))
                continue
            delivered, skipped = self._deliver(event)
            result.delivered += len(delivered)
            result.skipped_before_start += len(skipped)
            closed = self._close(event) if self._close is not None else 0
            result.closed += closed
            started = self._start(event)
            deferred = bool(started)
            self.ledger.record_event(event, start_pending=deferred)
            result.deferred += int(deferred)
            log.info("%s: %s", event_name(event),
                     decision(delivered, skipped, closed, started, event))
        return result

    def deliver(self, event: Event) -> tuple[int, int]:
        """Put *event* into the inbox of each matching subscriber.

        Returns the rows added and the matching subscribers skipped by ``before_start``.
        """
        delivered, skipped = self._deliver(event)
        return len(delivered), len(skipped)

    def _deliver(self, event: Event) -> tuple[list[str], list[str]]:
        """The handles of the workers that got *event*, and of those it predates."""
        delivered: list[str] = []
        skipped: list[str] = []
        for subscription, worker in self.ledger.live_subscriptions(event.source, event.item.key):
            if not kind_matches(subscription.kinds, event.kind, event.source):
                continue
            if before_start(event, worker_start(self.ledger, worker)):
                skipped.append(worker.handle)
                continue
            if self.ledger.enqueue(subscription.worker_id, event):
                delivered.append(worker.handle)
        return delivered, skipped

    def retry_pending_starts(self) -> RouteResult:
        """Offer the starts that the caps deferred on an earlier tick to the rules again.

        A start that still waits says why on every tick in the report, and an event that no
        rule wanted is not news, so their log lines are DEBUG; a launch or a dropped start
        is INFO, once.
        """
        result = RouteResult()
        for event in self.ledger.pending_start_events():
            started = self._start(event)
            outcome = getattr(started, "outcome", None)
            if started:
                result.deferred += 1
                log.debug("%s: waiting start: %s", event_name(event), outcome or "still deferred")
            else:
                self.ledger.settle_start(event)
                log.log(logging.INFO if outcome else logging.DEBUG, "%s: waiting start: %s",
                        event_name(event), outcome or "no rule starts on it")
        return result


def event_name(event: Event) -> str:
    """How an event is named in the log: its source, kind, item and dedupe id.

    Never its actor: on Linear the actor is an email address.
    """
    kind = event.kind if sanitize.is_event_kind(event.kind) else sanitize.one_line(event.kind, limit=40)
    dedupe = sanitize.one_line(event.dedupe_id, limit=DEDUPE_ID_LIMIT)
    return (
        f"event {event.source} {kind} {sanitize.scrub_identifier(event.item.key)} [{dedupe}]"
    )


def decision(
    delivered: list[str],
    skipped: list[str],
    closed: int,
    started: "bool | StartResult",
    event: Event,
) -> str:
    """What routing did with one event, as one phrase for its log line."""
    parts = []
    if delivered:
        parts.append(f"delivered to {', '.join(delivered)}")
    if skipped:
        parts.append(f"not delivered to {', '.join(skipped)}, it predates their start")
    if closed:
        parts.append(f"closed {closed} worker(s)")
    outcome = getattr(started, "outcome", None)
    if outcome:
        parts.append(outcome)
    elif started and outcome is None:
        parts.append("start deferred")
    if not parts:
        parts.append("no subscriber and no start")
    pointer = sanitize.safe_url(event.pointer)
    if pointer:
        parts.append(pointer)
    return "; ".join(parts)


__all__ = [
    "Closer",
    "RouteResult",
    "Router",
    "START_ALLOWANCE_SECS",
    "StartResult",
    "Starter",
    "before_start",
    "decision",
    "deliver_to_holders",
    "event_name",
    "kind_matches",
    "suitable_worker_exists",
    "worker_start",
]
