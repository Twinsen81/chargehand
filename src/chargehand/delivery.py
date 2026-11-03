"""Delivery: one wake per worker for the events that wait in its inbox.

Once per tick, after the state diff, every worker that is ``idle`` or ``queued`` and has
events in its inbox goes through these steps:

1. Coalesce. The events become one message with a line per event kind and pointer, such
   as ``comment: https://...``, and nothing else. A pointer is untrusted, so only a plain
   http(s) URL or a plain identifier is kept; the worker fetches what a pointer names
   itself. A reply that the operator typed on the board is the one exception: its line
   carries the operator's text, cleaned to one line.
2. Admit. The global cap on working turns, the role's daily wake cap and the usage
   reserve. A worker that is not admitted becomes ``queued`` and keeps its events.
3. Check and prepare, through the caller: a worker whose worktree must not host its
   session again is paused with a fault, and one whose item checkout cannot be brought
   up to date waits like one a cap holds.
4. Wake, through the worker's runtime, so that the same session continues.
5. Record. One ledger transaction marks the events delivered and records the wake on the
   worker and in the sessions table. It comes after the wake: a crash between the two
   leaves the events in the inbox, and a later tick wakes the worker with them again.
   A second wake costs one turn and is otherwise harmless, because the message carries
   kinds and pointers only.

The state write of step 5 is conditional: a hook of the new turn, such as a permission
prompt, can come in while the wake runs, and the state it wrote is newer. Then only the
record of the wake is added.

A worker that is launching, running, blocked or paused is not woken; its events wait. A
blocked worker has an open question or permission prompt, which a stop would discard.

Before the inboxes, each parked worker whose usage limit has reset is resumed in the same
session with :data:`RESUME_MESSAGE`, oldest reset first, through the same admission. The
reason admission holds a worker is kept for the board.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from chargehand import ledger as ledger_mod
from chargehand import runtime as runtime_mod
from chargehand import sanitize
from chargehand.errors import AgentRuntimeError, ConfigError, DeliveryAborted
from chargehand.ledger import InboxEntry, Ledger, Worker
from chargehand.roles import Policy
from chargehand.runtime import Runtime

log = logging.getLogger(__name__)

WAKE_STATES = (ledger_mod.IDLE, ledger_mod.QUEUED)
WORKING_STATES = (ledger_mod.LAUNCHING, ledger_mod.RUNNING)

# The daily wake cap counts the wakes of this window, which moves with the clock.
WAKE_CAP_WINDOW_SECS = 24 * 3600

# No wake while a usage window is above 100 minus this percentage.
DEFAULT_WAKE_RESERVE_PERCENT = 20.0
# No new worker while the weekly window is above 100 minus this percentage.
DEFAULT_NEW_WORKER_RESERVE_PERCENT = 30.0
# An older reading counts as unknown. An unknown reading holds no wake back, because a
# wake continues work that already runs, but it holds new workers back.
USAGE_MAX_AGE_SECS = 30 * 60
# The window that the reserve for new workers reads.
WEEKLY_WINDOW = "seven_day"

# Why a held worker waits, as the board shows it: this prefix and the worker id, in the
# ledger's state table. Written where delivery holds a wake, removed once it goes.
QUEUE_REASON_PREFIX = "queue-reason:"

# The message that resumes a worker that a usage limit parked, once the limit has reset.
# chargehand's own text.
RESUME_MESSAGE = (
    "The usage limit that stopped your last turn has reset. Continue the work where you "
    "left it."
)

MESSAGE_HEADER = "New events on the work you follow. Read each one yourself:"

# The kind a line names when the event's own kind is not a bare lower-case name. Sources
# store bare kinds; the check is a second line of defence, because the message is a
# trusted user turn.
UNKNOWN_KIND = "event"

# How a reply that the operator typed on the board starts its line in the message.
REPLY_PREFIX = "reply from the operator: "


@dataclass(frozen=True)
class UsageWindow:
    """One usage window of a provider: how much of it is used, and when it resets."""

    kind: str
    used_percent: float
    resets_at: float | None = None


@dataclass(frozen=True)
class UsageReading:
    """A provider's usage windows by window kind, and when they were read.

    A reading of one provider says nothing about another: each runtime draws on its
    provider's windows, and the reserve applies per provider.
    """

    provider: str
    windows: Mapping[str, UsageWindow]
    read_at: float

    def is_stale(self, now: float) -> bool:
        return now - self.read_at > USAGE_MAX_AGE_SECS


def usage_admits(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_WAKE_RESERVE_PERCENT,
    now: float | None = None,
) -> bool:
    """Whether the usage reserve lets a wake through. No reading, or an old one, admits."""
    return window_over_reserve(reading, reserve_percent=reserve_percent, now=now) is None


def window_over_reserve(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_WAKE_RESERVE_PERCENT,
    now: float | None = None,
) -> UsageWindow | None:
    """The first window of a current *reading* above 100 minus *reserve_percent*, or None."""
    if reading is None:
        return None
    current = time.time() if now is None else now
    if reading.is_stale(current):
        return None
    limit = 100.0 - reserve_percent
    return next(
        (window for window in reading.windows.values() if window.used_percent > limit), None
    )


def new_worker_admits(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_NEW_WORKER_RESERVE_PERCENT,
    now: float | None = None,
) -> bool:
    """Whether the usage reserve lets a new worker start.

    No reading at all (no usage file) is no reserve. A reading that is too old is unknown,
    and an unknown weekly window holds new workers back: a new worker can run for hours.
    """
    if reading is None:
        return True
    current = time.time() if now is None else now
    if reading.is_stale(current):
        return False
    weekly = reading.windows.get(WEEKLY_WINDOW)
    return weekly is None or weekly.used_percent <= 100.0 - reserve_percent


def safe_pointer(pointer: str) -> str | None:
    """The pointer when it is a plain URL or a plain identifier, otherwise None."""
    url = sanitize.safe_url(pointer)
    if url is not None:
        return url
    return pointer if sanitize.is_safe_identifier(pointer) else None


def safe_kind(kind: str) -> str:
    """The kind when it is a plain lower-case name, otherwise ``UNKNOWN_KIND``."""
    return kind if sanitize.is_event_kind(kind) else UNKNOWN_KIND


def wake_message(entries: Sequence[InboxEntry]) -> tuple[str | None, int]:
    """The message for *entries*, and how many pointers were left out as unusable.

    Each line is the event's kind, a colon and its pointer. The message is None when no
    pointer is usable. Two events of one kind with one pointer, such as two CI results on
    the same pull request, give one line; two kinds on one pointer give two lines. A kind
    that is not a plain name is replaced with ``UNKNOWN_KIND``, so its pointer still
    arrives. A reply from the board gives ``REPLY_PREFIX`` and its line.
    """
    lines: list[str] = []
    left_out = 0
    for entry in entries:
        if (entry.source, entry.kind) == (ledger_mod.REPLY_SOURCE, ledger_mod.REPLY_KIND):
            # The operator's own line from the board: trusted text, which no configured
            # source can write, still kept to one clean line.
            reply = sanitize.one_line(entry.pointer, limit=ledger_mod.REPLY_LIMIT)
            if not reply:
                left_out += 1
            elif f"{REPLY_PREFIX}{reply}" not in lines:
                lines.append(f"{REPLY_PREFIX}{reply}")
            continue
        pointer = safe_pointer(entry.pointer)
        if pointer is None:
            left_out += 1
            continue
        line = f"{safe_kind(entry.kind)}: {pointer}"
        if line not in lines:
            lines.append(line)
    if not lines:
        return None, left_out
    return "\n".join([MESSAGE_HEADER, *lines]), left_out


@dataclass
class DeliveryResult:
    woken: list[Worker] = field(default_factory=list)
    # Parked workers that went on after their usage limit reset; also in *woken*.
    resumed: list[Worker] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    # Workers that a wake fault paused, each with the phrase for its notification.
    faults: list[tuple[Worker, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Delivery:
    """One pass of delivery over the live workers. Every collaborator is injectable.

    *usage* gives the reading of one provider, or None when there is none.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        max_concurrent: int,
        runtime: Callable[[str], Runtime],
        policy: Callable[[Worker], Policy],
        usage: Callable[[str], UsageReading | None] = lambda provider: None,
        reserve_percent: float = DEFAULT_WAKE_RESERVE_PERCENT,
        crash_after_wake: bool = False,
        clock: Callable[[], float] = time.time,
        guard: Callable[[Worker], str | None] = lambda worker: None,
        prepare: Callable[[Worker], str | None] = lambda worker: None,
    ) -> None:
        self.ledger = ledger
        self.max_concurrent = max_concurrent
        self._runtime = runtime
        self._policy = policy
        self._usage = usage
        self.reserve_percent = reserve_percent
        self.crash_after_wake = crash_after_wake
        self._clock = clock
        # Why a worker's session must not be resumed where it is, or None. The text is
        # the caller's own, so it may reach a notification.
        self._guard = guard
        # Brings what the worker reads up to date before the wake; why the wake must
        # wait, or None.
        self._prepare = prepare

    def run(self) -> DeliveryResult:
        result = DeliveryResult()
        # Before the inboxes: a parked worker has waited since its turn failed.
        self._resume_parked(result)
        for worker in self.ledger.workers_in_state(WAKE_STATES):
            self._deliver(worker, result)
        self._forget_reasons()
        return result

    def _resume_parked(self, result: DeliveryResult) -> None:
        """Continue each parked worker whose usage limit has reset, oldest first.

        Each one goes through the same admission as a wake, one at a time, so a long
        list of parked workers does not use the new window at once. A worker parked
        without a reset time waits for `continue`.
        """
        now = self._clock()
        due = sorted(
            (
                worker for worker in self.ledger.workers_in_state([ledger_mod.PARKED])
                if worker.session_id and worker.reset_at is not None and worker.reset_at <= now
            ),
            key=lambda worker: (worker.reset_at or 0.0, worker.id),
        )
        for worker in due:
            if self._admitted(worker, result):
                self._wake(worker, RESUME_MESSAGE, (), result)

    def _deliver(self, worker: Worker, result: DeliveryResult) -> None:
        entries = self.ledger.inbox(worker)
        if not entries:
            if worker.state == ledger_mod.QUEUED:
                self.ledger.update(worker, state=ledger_mod.IDLE)
            return
        message, left_out = wake_message(entries)
        if left_out:
            result.warnings.append(
                f"{worker.handle}: left {left_out} event(s) out of the wake; their pointer "
                f"is not a plain URL or identifier"
            )
        if message is None:
            # Nothing the worker could follow; a wake would only cost a turn.
            self.ledger.mark_delivered(entries, now=self._clock())
            return
        if not worker.session_id:
            return
        if self._admitted(worker, result):
            self._wake(worker, message, entries, result)

    def _admitted(self, worker: Worker, result: DeliveryResult) -> bool:
        """Admission, the worktree guard and the refresh; False when the wake must wait."""
        reason = self._refusal(worker, result)
        if reason is not None:
            self._hold(worker, reason, result)
            return False

        if self._refuse_faulted(worker, result):
            return False

        problem = self._prepare(worker)
        if problem is not None:
            self._hold(worker, f"wake held, {problem}", result)
            return False

        # Again after the refresh: it runs the role's setup step, so a checkout it
        # redirected into the session's own worktree shows here as a worktree off its
        # branch, before the resume.
        return not self._refuse_faulted(worker, result)

    def _hold(self, worker: Worker, reason: str, result: DeliveryResult) -> None:
        """Keep *worker* waiting, with *reason* for the board. A parked worker stays parked."""
        if worker.state == ledger_mod.IDLE:
            self.ledger.update(worker, state=ledger_mod.QUEUED)
        self.ledger.set_state(f"{QUEUE_REASON_PREFIX}{worker.id}", reason)
        result.held.append(f"{worker.handle}: {reason}")

    def _forget_reasons(self) -> None:
        """Drop the reason of every worker that no longer waits."""
        waiting = {
            str(worker.id)
            for worker in self.ledger.workers_in_state([ledger_mod.QUEUED, ledger_mod.PARKED])
        }
        for key in self.ledger.all_state():
            if (
                key.startswith(QUEUE_REASON_PREFIX)
                and key.removeprefix(QUEUE_REASON_PREFIX) not in waiting
            ):
                self.ledger.delete_state(key)

    def _wake(
        self, worker: Worker, message: str, entries: Sequence[InboxEntry], result: DeliveryResult
    ) -> None:
        # Read again: the checks above take time, and a session hook may have moved the
        # worker meanwhile. The record below compares against this read.
        read = self.ledger.get(worker.id)
        if read is None or read.state != worker.state or read.session_id != worker.session_id:
            return
        worker = read
        resume = worker.state == ledger_mod.PARKED
        if resume and worker.notified_state is not None:
            # The resume starts a new turn. A hook of that turn that parks or blocks the
            # worker again during the wake is then notified, as after any wake.
            worker = self.ledger.update(worker, notified_state=None)
        reason_key = f"{QUEUE_REASON_PREFIX}{worker.id}"
        try:
            # The runtime's own look decides, right before its stop: the operator may have
            # started a turn in the session since this tick's listing. A failed turn has
            # ended, so a resume accepts it.
            outcome = self._runtime(worker.runtime).wake(
                worker.session_id, message, session_uuid=worker.session_uuid,
                if_idle=not resume, if_ended=resume,
            )
        except (AgentRuntimeError, ConfigError) as exc:
            # Nothing is known about whether the message arrived. The events stay; if it
            # did arrive, the next wake repeats them, which costs one turn.
            self.ledger.update(worker, last_error=f"wake failed: {exc}"[:500])
            self.ledger.set_state(reason_key, f"wake failed: {exc}"[:300])
            result.errors.append(f"{worker.handle}: wake failed: {exc}")
            return
        if not outcome.ok:
            detail = sanitize.one_line(outcome.detail, limit=300)
            if outcome.busy:
                # The session started a turn, or opened a prompt, after the listing this
                # tick read. The events wait for the turn to end; the state diff of a
                # later tick records the new state.
                self.ledger.set_state(reason_key, f"wake held, {detail}")
                result.held.append(f"{worker.handle}: wake held, {detail}")
                return
            if outcome.copy_id is not None:
                # The message went to a new session that works in the same worktree. The
                # worker keeps its own session and its events; the operator decides. Paused
                # holds: a later state diff would turn a blocked worker back into an idle
                # one, and the next wake would start another copy.
                paused = self.ledger.update(
                    worker,
                    state=ledger_mod.PAUSED,
                    last_error=f"wake fault: {detail}"[:500],
                )
                result.faults.append((paused, "the wake started a copy of the session"))
                result.errors.append(f"{worker.handle}: wake fault: {detail}")
                return
            self.ledger.update(worker, last_error=f"wake failed: {detail}"[:500])
            self.ledger.set_state(reason_key, f"wake failed: {detail}")
            result.errors.append(f"{worker.handle}: wake failed: {detail}")
            return

        if self.crash_after_wake:
            raise DeliveryAborted("delivery aborted after the wake")
        now = self._clock()
        wake_fields: dict[str, object] = {"wakes": worker.wakes + 1, "last_wake_at": now}
        cleared = {"reset_at": None} if resume else {}
        with self.ledger.transaction():
            self.ledger.mark_delivered(entries, now=now)
            self.ledger.record_wake(
                worker, session_id=worker.session_id, session_uuid=worker.session_uuid, now=now
            )
            written = self.ledger.update_if_unchanged(
                worker,
                state=ledger_mod.RUNNING,
                session_state=runtime_mod.WORKING,
                notified_state=runtime_mod.WORKING,
                working_since=now,
                watchdog_notified=0,
                last_error=None,
                **wake_fields,
                **cleared,
            )
            if written is None:
                # A hook of the new turn came in during the wake, such as a permission
                # prompt, and wrote the state that the session shows. That is newer than
                # this write, so only the record of the wake is added. The reset time goes
                # only while no new limit parked the worker: that one keeps its own.
                current = self.ledger.get(worker.id) or worker
                if current.state == ledger_mod.PARKED or current.reset_at != worker.reset_at:
                    cleared = {}
                written = self.ledger.update(current, **wake_fields, **cleared)
            self.ledger.delete_state(reason_key)
        worker = written
        if resume:
            log.info("%s: resumed session %s after the usage limit reset", worker.handle,
                     worker.session_id)
            result.resumed.append(worker)
        else:
            # The message holds only kinds and pointers that passed the checks above.
            log.info("%s: woke session %s with %s", worker.handle, worker.session_id,
                     "; ".join(message.splitlines()[1:]))
        result.woken.append(worker)

    def _refuse_faulted(self, worker: Worker, result: DeliveryResult) -> bool:
        """Pause *worker* when the guard refuses its worktree; True when it did.

        A resume would load what the worktree now holds. The idle session is stopped,
        which loads nothing, so that the state diff leaves the worker paused, with its
        events, until the operator has looked.
        """
        fault = self._guard(worker)
        if fault is None:
            return False
        try:
            self._runtime(worker.runtime).stop(worker.session_id)
        except (AgentRuntimeError, ConfigError):
            pass
        paused = self.ledger.update(
            worker, state=ledger_mod.PAUSED, last_error=f"wake refused: {fault}"[:500]
        )
        result.faults.append((paused, fault))
        result.errors.append(f"{worker.handle}: wake refused: {fault}")
        return True

    def _refusal(self, worker: Worker, result: DeliveryResult) -> str | None:
        """Why admission holds this wake back now, or None when it may go."""
        working = sum(w.state in WORKING_STATES for w in self.ledger.live_workers())
        if working >= self.max_concurrent:
            return f"wake held by the global cap ({working}/{self.max_concurrent} working)"
        try:
            policy = self._policy(worker)
        except ConfigError as exc:
            result.errors.append(f"{worker.handle}: {exc}")
            return "its role does not load"
        now = self._clock()
        wakes = self.ledger.wakes_since(worker, now - WAKE_CAP_WINDOW_SECS)
        if wakes >= policy.max_wakes_per_day:
            return f"wake held by the daily cap ({wakes}/{policy.max_wakes_per_day})"
        provider = self._provider(worker)
        reading = self._usage(provider) if provider else None
        window = window_over_reserve(reading, reserve_percent=self.reserve_percent, now=now)
        if window is not None:
            return (
                f"wake held by the usage reserve ({provider} {window.kind} at "
                f"{window.used_percent:.0f}%, above {100.0 - self.reserve_percent:.0f}%)"
            )
        return None

    def _provider(self, worker: Worker) -> str | None:
        try:
            return self._runtime(worker.runtime).usage_provider
        except ConfigError:
            return None


__all__ = [
    "DEFAULT_NEW_WORKER_RESERVE_PERCENT",
    "DEFAULT_WAKE_RESERVE_PERCENT",
    "Delivery",
    "DeliveryResult",
    "MESSAGE_HEADER",
    "QUEUE_REASON_PREFIX",
    "RESUME_MESSAGE",
    "USAGE_MAX_AGE_SECS",
    "UsageReading",
    "UsageWindow",
    "UNKNOWN_KIND",
    "WAKE_STATES",
    "WEEKLY_WINDOW",
    "new_worker_admits",
    "safe_kind",
    "safe_pointer",
    "usage_admits",
    "wake_message",
    "window_over_reserve",
]
