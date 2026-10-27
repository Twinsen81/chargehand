"""Delivery: one wake per worker for the events that wait in its inbox.

Once per tick, after the state diff, every worker that is ``idle`` or ``queued`` and has
events in its inbox goes through these steps:

1. Coalesce. The events become one message that lists their pointers and nothing else.
   A pointer is untrusted, so only a plain http(s) URL or a plain identifier is kept;
   the worker fetches what a pointer names itself.
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
   pointers only.

A worker that is launching, running, blocked or paused is not woken; its events wait. A
blocked worker has an open question or permission prompt, which a stop would discard.
"""

from __future__ import annotations

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

WAKE_STATES = (ledger_mod.IDLE, ledger_mod.QUEUED)
WORKING_STATES = (ledger_mod.LAUNCHING, ledger_mod.RUNNING)

# The daily wake cap counts the wakes of this window, which moves with the clock.
WAKE_CAP_WINDOW_SECS = 24 * 3600

# No wake while a usage window is above 100 minus this percentage.
DEFAULT_WAKE_RESERVE_PERCENT = 20.0
# An older reading counts as unknown, and an unknown reading holds no wake back.
USAGE_MAX_AGE_SECS = 30 * 60

MESSAGE_HEADER = "New events on the work you follow. Read each one yourself:"


@dataclass(frozen=True)
class UsageReading:
    """The provider's usage windows, as percent used by window name, and when they were read."""

    used_percent: Mapping[str, float]
    read_at: float


def usage_admits(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_WAKE_RESERVE_PERCENT,
    now: float | None = None,
) -> bool:
    """Whether the usage reserve lets a wake through. No reading, or an old one, admits."""
    if reading is None:
        return True
    current = time.time() if now is None else now
    if current - reading.read_at > USAGE_MAX_AGE_SECS:
        return True
    limit = 100.0 - reserve_percent
    return all(percent <= limit for percent in reading.used_percent.values())


def safe_pointer(pointer: str) -> str | None:
    """The pointer when it is a plain URL or a plain identifier, otherwise None."""
    url = sanitize.safe_url(pointer)
    if url is not None:
        return url
    return pointer if sanitize.is_safe_identifier(pointer) else None


def wake_message(entries: Sequence[InboxEntry]) -> tuple[str | None, int]:
    """The message for *entries*, and how many pointers were left out as unusable.

    The message is None when no pointer is usable. Two events with one pointer, such as
    two CI results on the same pull request, give one line.
    """
    pointers: list[str] = []
    left_out = 0
    for entry in entries:
        pointer = safe_pointer(entry.pointer)
        if pointer is None:
            left_out += 1
        elif pointer not in pointers:
            pointers.append(pointer)
    if not pointers:
        return None, left_out
    return "\n".join([MESSAGE_HEADER, *(f"- {pointer}" for pointer in pointers)]), left_out


@dataclass
class DeliveryResult:
    woken: list[Worker] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    # Workers that a wake fault paused, each with the phrase for its notification.
    faults: list[tuple[Worker, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Delivery:
    """One pass of delivery over the live workers. Every collaborator is injectable."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        max_concurrent: int,
        runtime: Callable[[str], Runtime],
        policy: Callable[[Worker], Policy],
        usage: Callable[[], UsageReading | None] = lambda: None,
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
        for worker in self.ledger.workers_in_state(WAKE_STATES):
            self._deliver(worker, result)
        return result

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

        reason = self._refusal(worker, result)
        if reason is not None:
            if worker.state != ledger_mod.QUEUED:
                self.ledger.update(worker, state=ledger_mod.QUEUED)
            result.held.append(f"{worker.handle}: {reason}")
            return

        if self._refuse_faulted(worker, result):
            return

        problem = self._prepare(worker)
        if problem is not None:
            if worker.state != ledger_mod.QUEUED:
                self.ledger.update(worker, state=ledger_mod.QUEUED)
            result.held.append(f"{worker.handle}: wake held, {problem}")
            return

        # Again after the refresh: it runs the role's setup step, so a checkout it
        # redirected into the session's own worktree shows here as a worktree off its
        # branch, before the resume.
        if self._refuse_faulted(worker, result):
            return

        try:
            outcome = self._runtime(worker.runtime).wake(
                worker.session_id, message, session_uuid=worker.session_uuid, if_idle=True
            )
        except (AgentRuntimeError, ConfigError) as exc:
            # Nothing is known about whether the message arrived. The events stay; if it
            # did arrive, the next wake repeats them, which costs one turn.
            self.ledger.update(worker, last_error=f"wake failed: {exc}"[:500])
            result.errors.append(f"{worker.handle}: wake failed: {exc}")
            return
        if not outcome.ok:
            detail = sanitize.one_line(outcome.detail, limit=300)
            if outcome.busy:
                # The session started a turn, or opened a prompt, after the listing this
                # tick read. The events wait for the turn to end; the state diff of a
                # later tick records the new state.
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
            result.errors.append(f"{worker.handle}: wake failed: {detail}")
            return

        if self.crash_after_wake:
            raise DeliveryAborted("delivery aborted after the wake")
        now = self._clock()
        with self.ledger.transaction():
            self.ledger.mark_delivered(entries, now=now)
            self.ledger.record_wake(
                worker, session_id=worker.session_id, session_uuid=worker.session_uuid, now=now
            )
            worker = self.ledger.update(
                worker,
                state=ledger_mod.RUNNING,
                session_state=runtime_mod.WORKING,
                notified_state=runtime_mod.WORKING,
                wakes=worker.wakes + 1,
                last_wake_at=now,
                working_since=now,
                watchdog_notified=0,
                last_error=None,
            )
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
        if not usage_admits(self._usage(), reserve_percent=self.reserve_percent, now=now):
            return "wake held by the usage reserve"
        return None


__all__ = [
    "DEFAULT_WAKE_RESERVE_PERCENT",
    "Delivery",
    "DeliveryResult",
    "MESSAGE_HEADER",
    "UsageReading",
    "WAKE_STATES",
    "safe_pointer",
    "usage_admits",
    "wake_message",
]
