"""Delivery: one wake per worker for the events that wait in its inbox.

Once per tick, after the state diff, every worker that is ``idle`` or ``queued`` and has
events in its inbox goes through these steps:

1. Coalesce. The events become one message with a line per event kind and pointer, such
   as ``comment: https://...``, and nothing else. A pointer is untrusted, so only a plain
   http(s) URL or a plain identifier is kept; the worker fetches what a pointer names
   itself. A reply that the operator typed on the board is the one exception: its line
   carries the operator's text, cleaned to one line.
2. Debounce and admit. The wake waits until no new event has arrived for the role's
   ``debounce_secs``, so a burst becomes one wake, and at most
   :data:`DEBOUNCE_MAX_HOLD_FACTOR` times that after the oldest waiting event; a reply
   from the board, a due timer and a summary clone's request for a current handoff note
   do not wait. Then the global cap on working turns,
   across all repositories, the role's daily wake cap and the usage reserve. A worker that waits becomes
   ``queued`` and keeps its events. A worker that reaches its daily cap is notified
   once, until its next wake.
3. Check and prepare, through the caller: a worker whose worktree must not host its
   session again is paused with a fault, and one whose item checkout cannot be brought
   up to date waits like one a cap holds.
4. Wake, so that the same session continues. A session whose chargehand mod said hello
   takes the message from its outbox (see below); any other session is stopped and
   resumed with it, through the worker's runtime. Past the role's refresh limit the
   wake starts a fresh session instead (see below).
5. Record. One ledger transaction marks the events delivered and records the wake on the
   worker and in the sessions table. It comes after the wake: a crash between the two
   leaves the events in the inbox, and a later tick wakes the worker with them again.
   A second wake costs one turn and is otherwise harmless, because the message carries
   kinds and pointers only.

The wake through the outbox. For a runtime with ``in_place_wake`` and a session whose
mod said hello (:func:`record_mod_hello`), the message goes into the worker's outbox, one
open message per worker: events that come in before the mod takes it join it. A live
process keeps running; a stopped one is started again with the runtime's respawn first.
The mod polls the outbox, submits the text as the session's next turn once it is idle,
and confirms (:func:`confirm_outbox`); the confirm records the wake as step 5 does,
once. Until then the events stay in the inbox. A message that the mod does not take
within :data:`OUTBOX_TAKE_SECS` is dropped, the session no longer counts as having the
mod, and the wake goes the stop-and-resume way.

The state write of step 5 is conditional: a hook of the new turn, such as a permission
prompt, can come in while the wake runs, and the state it wrote is newer. Then only the
record of the wake is added.

A worker that is launching, running, blocked or paused is not woken; its events wait. A
blocked worker has an open question or permission prompt, which a stop would discard.
The one exception is a reply from the board to a blocked worker whose turn ended with a
question: the reply is the answer, so it wakes the worker, through the same admission,
with every event that waits. No dialog is open then, so the stop discards nothing.

Before the inboxes, each parked worker whose usage limit has reset is resumed in the same
session with :data:`RESUME_MESSAGE`, oldest reset first, through the same admission. The
reason admission holds a worker is kept for the board.

The refresh. A long session costs more with every wake, because each wake reads the
whole conversation again. Once an idle or queued worker's session has taken
``refresh_after_wakes`` wakes, or holds about ``refresh_after_tokens`` tokens (the
context that the mod reported, else the size of the transcript), its next wake goes to
a fresh session for the same worker, through the caller: the old session is stopped and
kept, and a new one starts in the same worktree. Its first prompt is chargehand's own
text, which names the item and the tool that gives the worker's handoff note, then the
role's prompt, then the waiting events as usual. The note itself never reaches the
prompt: the fresh session reads it as a tool result. The resume of a parked worker, the
answer to a question that the session asked, and a summary clone's request for a current
note stay in the session they belong to; the refresh then comes at the next wake.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import stat
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

# The debounce holds a wake at most this many times the role's debounce_secs after the
# oldest waiting event, so a steady stream of events, such as CI results during a long
# run, cannot hold the worker for ever.
DEBOUNCE_MAX_HOLD_FACTOR = 5

# No wake while the weekly window is above 100 minus this percentage.
DEFAULT_WAKE_RESERVE_PERCENT = 20.0
# No new worker while the weekly window is above 100 minus this percentage.
DEFAULT_NEW_WORKER_RESERVE_PERCENT = 30.0
# The window that both reserves read. A reading can carry other kinds; no reserve reads them.
WEEKLY_WINDOW = "seven_day"

# Why a held worker waits, as the board shows it: this prefix and the worker id, in the
# ledger's state table. Written where delivery holds a wake, removed once it goes.
QUEUE_REASON_PREFIX = "queue-reason:"
# A worker whose daily cap was notified: this prefix and the worker id, in the same table.
# Removed at the worker's next wake, so a worker that stays at its cap is notified once.
CAP_NOTIFIED_PREFIX = "cap-notified:"

# How often a session's chargehand mod polls its outbox; its hello answer says so.
OUTBOX_POLL_SECS = 5
# A pending outbox message that the mod has not taken in this time: the mod does not run in
# the session, and the wake falls back to a stop and a resume.
OUTBOX_TAKE_SECS = 60
# A taken message without a confirm for this long is handed out again: the mod that took it
# may have been reloaded before its submit. A submit that waits for the session to be idle
# confirms later than this only when the session's turn ran that long.
OUTBOX_RETAKE_SECS = 120

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

# A transcript holds about this many bytes per token. The estimate is approximate.
BYTES_PER_TOKEN = 4
# The context window that a reported percentage is read against when the report gave no
# token count. The mod sends the count, which decides; a percentage alone is only a
# fallback, and the ledger keeps no window size. For a model with a larger window this
# reads the session as smaller than it is, so the refresh comes later, never earlier.
DEFAULT_CONTEXT_WINDOW = 200_000


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


def weekly_window(reading: UsageReading | None, now: float | None = None) -> UsageWindow | None:
    """The weekly window of *reading*, or None when there is none or it has reset since.

    The newest reading counts however old it is. Only a reset time that has passed ends
    it: the percentage then belongs to the week before.
    """
    if reading is None:
        return None
    weekly = reading.windows.get(WEEKLY_WINDOW)
    if weekly is None or weekly.resets_at is None:
        return weekly
    return weekly if weekly.resets_at > (time.time() if now is None else now) else None


def usage_admits(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_WAKE_RESERVE_PERCENT,
    now: float | None = None,
) -> bool:
    """Whether the usage reserve lets a wake through. No reading admits."""
    return window_over_reserve(reading, reserve_percent=reserve_percent, now=now) is None


def window_over_reserve(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_WAKE_RESERVE_PERCENT,
    now: float | None = None,
) -> UsageWindow | None:
    """The weekly window of *reading* when above 100 minus *reserve_percent*, or None."""
    weekly = weekly_window(reading, now)
    return weekly if weekly is not None and weekly.used_percent > 100.0 - reserve_percent else None


def new_worker_admits(
    reading: UsageReading | None,
    *,
    reserve_percent: float = DEFAULT_NEW_WORKER_RESERVE_PERCENT,
    now: float | None = None,
) -> bool:
    """Whether the usage reserve lets a new worker start. No reading admits."""
    return window_over_reserve(reading, reserve_percent=reserve_percent, now=now) is None


def safe_pointer(pointer: str) -> str | None:
    """The pointer when it is a plain URL or a plain identifier, otherwise None."""
    url = sanitize.safe_url(pointer)
    if url is not None:
        return url
    return pointer if sanitize.is_safe_identifier(pointer) else None


def safe_kind(kind: str) -> str:
    """The kind when it is a plain lower-case name, otherwise ``UNKNOWN_KIND``."""
    return kind if sanitize.is_event_kind(kind) else UNKNOWN_KIND


# The inbox entries, besides a reply from the board, whose wake the debounce does not
# hold: a due timer, and a summary clone's request for a current handoff note.
UNDEBOUNCED = frozenset((
    (ledger_mod.TIMER_SOURCE, ledger_mod.TIMER_KIND),
    (ledger_mod.HANDOFF_SOURCE, ledger_mod.HANDOFF_KIND),
))


def is_reply(entry: InboxEntry) -> bool:
    """Whether *entry* is a line that the operator typed on the board."""
    return (entry.source, entry.kind) == (ledger_mod.REPLY_SOURCE, ledger_mod.REPLY_KIND)


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
        if is_reply(entry):
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


def tokens_estimate(transcript_path: str | None) -> int | None:
    """About how many tokens the session's transcript holds, from its size on disk."""
    if not transcript_path or not os.path.isabs(transcript_path):
        return None
    try:
        info = os.stat(transcript_path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_size // BYTES_PER_TOKEN


def session_tokens(worker: Worker) -> int | None:
    """About how many tokens *worker*'s session reads at a wake, or None when nothing says.

    The context that the session's mod reported after its last turn, as a count or as a
    percentage of the window; without a report, the size of the transcript.
    """
    if worker.context_tokens is not None:
        return worker.context_tokens
    if worker.context_percent is not None:
        return int(worker.context_percent / 100.0 * DEFAULT_CONTEXT_WINDOW)
    return tokens_estimate(worker.transcript_path)


def _record_wake(
    ledger: Ledger,
    worker: Worker,
    entries: Sequence[InboxEntry],
    now: float,
    *,
    start_turn: bool,
    wake_row: bool = True,
) -> Worker:
    """Mark *entries* delivered and record one wake of *worker*. The caller holds a transaction.

    With *start_turn* the worker becomes ``running``, unless a hook of the new turn wrote
    the state the session shows since *worker* was read; then, and without it, only the
    record of the wake is added. Without *wake_row* the caller has written the session
    row that records the wake, as a refresh does.
    """
    wake_fields: dict[str, object] = {"wakes": worker.wakes + 1, "last_wake_at": now}
    ledger.mark_delivered(entries, now=now)
    if wake_row:
        ledger.record_wake(
            worker, session_id=worker.session_id, session_uuid=worker.session_uuid, now=now
        )
    written = None
    if start_turn:
        # The state write drops the reset time of a resumed worker.
        written = ledger.update_if_unchanged(
            worker,
            state=ledger_mod.RUNNING,
            session_state=runtime_mod.WORKING,
            notified_state=runtime_mod.WORKING,
            working_since=now,
            watchdog_notified=0,
            last_error=None,
            **wake_fields,
        )
    if written is None:
        # A hook of the new turn came in during the wake, such as a permission prompt,
        # and wrote the state that the session shows. That is newer than this write, so
        # only the record of the wake is added. A new limit that parked the worker again
        # keeps its own reset time.
        written = ledger.update(ledger.get(worker.id) or worker, **wake_fields)
    ledger.delete_state(f"{QUEUE_REASON_PREFIX}{worker.id}")
    ledger.delete_state(f"{CAP_NOTIFIED_PREFIX}{worker.id}")
    return written


def switch_session(
    ledger: Ledger,
    worker: Worker,
    session: runtime_mod.Session,
    now: float,
    *,
    replaced: str | None,
) -> Worker:
    """Make *session* the current session of *worker*, as read before its launch, with a
    refresh row; *replaced* is the session it replaces. The caller holds a transaction.

    What described the old session goes, and its transcript path stays on its own row, for
    an archive. A hook of the fresh session may have written its own values already, during
    the launch, and those stay.
    """
    current = ledger.get(worker.id) or worker
    changes: dict[str, object] = {"session_id": session.id, "session_uuid": session.uuid}
    if current.last_hook_at == worker.last_hook_at:
        changes["session_state"] = session.state
    for name in ("transcript_path", "context_tokens", "context_percent", "waiting_for"):
        if getattr(current, name) == getattr(worker, name):
            changes[name] = None
    if replaced and worker.transcript_path:
        ledger.keep_session_transcript(worker, replaced, worker.transcript_path)
    written = ledger.update(current, **changes)
    ledger.record_session(
        written, kind=ledger_mod.SESSION_REFRESH, session_id=session.id,
        session_uuid=session.uuid, now=now,
    )
    return written


def has_mod(worker: Worker) -> bool:
    """Whether *worker*'s current session takes its wakes from the outbox."""
    return bool(worker.session_uuid) and worker.mod_session == worker.session_uuid


def record_mod_hello(ledger: Ledger, worker: Worker, session_uuid: str, *, now: float | None = None) -> Worker:
    """Record that the chargehand mod of *session_uuid* runs in *worker*'s session.

    The hello can come before the tick records the session's id, in the middle of the
    launch; the comparison with the worker's session waits until it does.
    """
    return ledger.update(
        worker, mod_session=session_uuid, mod_seen_at=time.time() if now is None else now
    )


def drop_open_outbox(ledger: Ledger, worker: Worker, reason: str, *, now: float | None = None) -> bool:
    """Drop *worker*'s open outbox message, so that it never goes out; False when there is none.

    Its events stay in the inbox for the next wake.
    """
    message = ledger.outbox_message(worker)
    return message is not None and ledger.drop_outbox(
        message.id, reason, time.time() if now is None else now
    )


def take_outbox(
    ledger: Ledger, worker: Worker, session_uuid: str, *, now: float | None = None
) -> ledger_mod.OutboxMessage | None:
    """The message the mod of *session_uuid* submits next, or None. See :meth:`Ledger.take_outbox`."""
    if not session_uuid or (worker.session_uuid and worker.session_uuid != session_uuid):
        return None
    return ledger.take_outbox(
        worker, session_uuid=session_uuid, retake_after=OUTBOX_RETAKE_SECS,
        now=time.time() if now is None else now,
    )


# What a confirm did, for the mod's answer and the log.
CONFIRM_RECORDED = "recorded"
CONFIRM_ALREADY = "already recorded"
CONFIRM_DROPPED = "dropped"
CONFIRM_UNKNOWN = "unknown message"


def confirm_outbox(
    ledger: Ledger,
    worker: Worker,
    message_id: int,
    *,
    submitted: bool,
    now: float | None = None,
) -> tuple[str, Worker | None]:
    """Record the wake that the mod's submit of a taken message started.

    Once per message: a second confirm changes nothing. The events the message carried
    that are still in the inbox are delivered, and an idle, queued, parked or blocked
    worker becomes ``running``. A message the session did not submit (a hook refused it)
    is dropped, its events wait, and the session no longer counts as having the mod, so
    the next wake stops and resumes it.
    """
    stamp = time.time() if now is None else now
    message = ledger.get_outbox(message_id)
    if message is None or message.worker_id != worker.id:
        return CONFIRM_UNKNOWN, None
    if not submitted:
        if ledger.drop_outbox(message_id, "the session did not submit it", stamp):
            worker = ledger.update(worker, mod_session=None)
            log.warning("%s: the session did not submit the wake message; the next wake stops "
                        "and resumes it", worker.handle)
        return CONFIRM_DROPPED, worker
    start_turn = worker.state in (*WAKE_STATES, ledger_mod.PARKED, ledger_mod.BLOCKED)
    with ledger.transaction():
        if ledger.submit_outbox(message_id, stamp) is None:
            # Dropped meanwhile, such as by a stop: its events wait, and no wake is recorded.
            dropped = ledger.get_outbox(message_id)
            if dropped is not None and dropped.state == ledger_mod.OUTBOX_DROPPED:
                return CONFIRM_DROPPED, worker
            return CONFIRM_ALREADY, worker
        entries = ledger.inbox_entries(worker, message.entries)
        written = _record_wake(ledger, worker, entries, stamp, start_turn=start_turn)
    log.info("%s: session %s started the wake's turn from its outbox (%d event(s))",
             written.handle, written.session_id, len(entries))
    return CONFIRM_RECORDED, written


@dataclass(frozen=True)
class Refresh:
    """What the caller's refresh of a worker's session did.

    *session* is the fresh session. *prompted* says whether it started with the first
    prompt and the waiting events; a session that an earlier, interrupted refresh started
    did not get them, so they wait for its next wake. *busy* says why the old session
    cannot be stopped now; then nothing changed. *keep* says why the worker goes on in
    its session instead, such as a fresh session that would not load the mod and its tool
    for the handoff note; then the wake continues the session as any wake does. *read* is
    the worker as read once the old session stopped: a post that the old session sent
    while it stopped, such as its session end, is older than the fresh session.
    """

    session: runtime_mod.Session | None = None
    prompted: bool = False
    busy: str | None = None
    keep: str | None = None
    read: Worker | None = None


@dataclass
class DeliveryResult:
    woken: list[Worker] = field(default_factory=list)
    # Parked workers that went on after their usage limit reset; also in *woken*.
    resumed: list[Worker] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    # Workers that a wake fault paused, each with the phrase for its notification.
    faults: list[tuple[Worker, str]] = field(default_factory=list)
    # Workers that reached their daily cap since their last wake, each with the phrase
    # for its notification.
    capped: list[tuple[Worker, str]] = field(default_factory=list)
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
        resume_hold: Callable[[Worker], str | None] = lambda worker: None,
        awaits_answer: Callable[[Worker], bool] = lambda worker: False,
        ended_without_dialog: Callable[[Worker], bool] = lambda worker: False,
        refresh: Callable[[Worker, str], Refresh] | None = None,
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
        # Why the tick's listing does not show a parked worker's session with no turn and
        # no prompt, or None when it does. A cheap first look, so that a session the
        # operator went on in costs no refresh of its item checkout; the runtime's wake
        # looks again before its stop. A listing that cannot be read raises
        # AgentRuntimeError.
        self._resume_hold = resume_hold
        # Whether a blocked worker waits for the answer to a question: no permission
        # prompt is open, and the tick's listing shows no turn and no dialog. A first
        # look, as above.
        self._awaits_answer = awaits_answer
        # Whether the tick's listing shows the worker's live session `blocked` while its
        # turn ended and no dialog waits, as after a question in plain text.
        self._ended_without_dialog = ended_without_dialog
        # Stops the worker's session and starts a fresh one in its worktree, whose first
        # prompt carries the message; raises AgentRuntimeError or ConfigError when it
        # fails. Without it, every wake continues the same session.
        self._refresh = refresh
        # One fresh listing per runtime and run, for the wakes through the outbox.
        self._listings: dict[str, list[runtime_mod.Session]] = {}

    def run(self) -> DeliveryResult:
        result = DeliveryResult()
        # A worker that was stopped, paused or cancelled with a wake in its outbox: the
        # wake does not go out, and its events wait for a wake after a continue.
        now = self._clock()
        for message in self.ledger.stale_outbox():
            self.ledger.drop_outbox(message.id, "the worker was stopped or ended", now)
        # Before the inboxes: a parked worker has waited since its turn failed.
        self._resume_parked(result)
        for worker in self.ledger.workers_in_state(WAKE_STATES):
            self._deliver(worker, result)
        # A blocked worker whose answer waits keeps the reason it is held, as a queued one.
        answering: set[int] = set()
        for worker in self.ledger.workers_in_state([ledger_mod.BLOCKED]):
            # Only the operator answers a question, so only a reply wakes the worker.
            if any(is_reply(entry) for entry in self.ledger.inbox(worker, now=now)) and (
                self._awaits_answer(worker)
            ):
                answering.add(worker.id)
                self._deliver(worker, result)
        self._forget_reasons(answering)
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
            try:
                busy = self._resume_hold(worker)
            except AgentRuntimeError as exc:
                busy = f"the session listing failed: {sanitize.one_line(str(exc), limit=200)}"
                result.errors.append(f"{worker.handle}: {busy}")
            if busy is not None:
                self._hold(worker, f"resume held, {busy}", result)
                continue
            if self._admitted(worker, result):
                self._wake(worker, RESUME_MESSAGE, (), result)

    def _deliver(self, worker: Worker, result: DeliveryResult) -> None:
        entries = self.ledger.inbox(worker, now=self._clock())
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
        until = self._debounced_until(worker, entries)
        if until is not None:
            at = time.strftime("%H:%M:%S", time.localtime(until))
            self._hold(worker, f"wake held by the debounce until {at}", result)
            return
        if self._admitted(worker, result):
            self._wake(worker, message, entries, result)

    def _debounced_until(self, worker: Worker, entries: Sequence[InboxEntry]) -> float | None:
        """When the role's debounce lets *entries* go, or None when they may go now.

        The wake waits until no new event has arrived for ``debounce_secs``, so a burst
        becomes one wake, but not longer than ``DEBOUNCE_MAX_HOLD_FACTOR`` times that
        after the oldest waiting event. A reply from the board, a due timer and a
        summary clone's request for a current handoff note do not wait: the operator waits
        for the answer, the worker chose the time itself, and the clone's launch waits for
        the note. The wake then takes the events of the burst along.
        """
        if any((entry.source, entry.kind) in UNDEBOUNCED or is_reply(entry) for entry in entries):
            return None
        try:
            debounce = self._policy(worker).debounce_secs
        except ConfigError:
            # Admission reports the role that does not load.
            return None
        if debounce <= 0:
            return None
        arrived = [entry.queued_at for entry in entries]
        until = min(max(arrived) + debounce, min(arrived) + DEBOUNCE_MAX_HOLD_FACTOR * debounce)
        return until if self._clock() < until else None

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

    def _forget_reasons(self, answering: set[int]) -> None:
        """Drop the reason of every worker that no longer waits.

        *answering* are the blocked workers whose reply this run tried to deliver.
        """
        waiting = {
            str(worker.id)
            for worker in self.ledger.workers_in_state([ledger_mod.QUEUED, ledger_mod.PARKED])
        } | {str(worker_id) for worker_id in answering}
        live = {str(worker.id) for worker in self.ledger.live_workers()}
        for key in self.ledger.all_state():
            if (
                key.startswith(QUEUE_REASON_PREFIX)
                and key.removeprefix(QUEUE_REASON_PREFIX) not in waiting
            ) or (
                key.startswith(CAP_NOTIFIED_PREFIX)
                and key.removeprefix(CAP_NOTIFIED_PREFIX) not in live
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
        if (
            self._refresh is not None and worker.state in WAKE_STATES and entries
            and not self._continues_session(worker, entries)
        ):
            why = self._refresh_reason(worker)
            if why is not None and self._wake_fresh(worker, message, entries, why, result):
                return
        # A wake through the outbox starts no turn here, so the state stays as it is.
        if self._wake_in_place(worker, message, entries, result):
            return
        resume = worker.state == ledger_mod.PARKED
        # A reply answers a question that the session asked after this tick's listing too.
        answer = not resume and any(is_reply(entry) for entry in entries)
        notified = worker.notified_state
        renotify = worker.state in (ledger_mod.PARKED, ledger_mod.BLOCKED) and notified is not None
        if renotify:
            # The resume, or the answer to a question, starts a new turn. A hook of that turn
            # that parks or blocks the worker again during the wake is then notified, as
            # after any wake, also when no SessionStart hook came first.
            worker = self.ledger.update(worker, notified_state=None)

        def keep_notified() -> None:
            # No new turn started, so a blocked worker still shows the question that was
            # notified; without this, the next state diff would notify it again. A hook that
            # came in during the wake wrote its own record.
            if renotify and worker.state == ledger_mod.BLOCKED:
                self.ledger.update_if_unchanged(worker, notified_state=notified)

        reason_key = f"{QUEUE_REASON_PREFIX}{worker.id}"
        try:
            # The runtime's own look decides, right before its stop: the operator may have
            # started a turn in the session since this tick's listing. A failed turn has
            # ended, so a resume accepts it.
            outcome = self._runtime(worker.runtime).wake(
                worker.session_id, message, session_uuid=worker.session_uuid,
                if_idle=not resume, if_ended=resume, if_asked=answer,
            )
        except (AgentRuntimeError, ConfigError) as exc:
            # Nothing is known about whether the message arrived. The events stay; if it
            # did arrive, the next wake repeats them, which costs one turn.
            self.ledger.update(worker, last_error=f"wake failed: {exc}"[:500])
            self.ledger.set_state(reason_key, f"wake failed: {exc}"[:300])
            result.errors.append(f"{worker.handle}: wake failed: {exc}")
            keep_notified()
            return
        if not outcome.ok:
            detail = sanitize.one_line(outcome.detail, limit=300)
            if outcome.busy:
                # The session started a turn, or opened a prompt, after the listing this
                # tick read. The events wait for the turn to end; the state diff of a
                # later tick records the new state.
                self.ledger.set_state(reason_key, f"wake held, {detail}")
                result.held.append(f"{worker.handle}: wake held, {detail}")
                keep_notified()
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
            keep_notified()
            return

        if self.crash_after_wake:
            raise DeliveryAborted("delivery aborted after the wake")
        now = self._clock()
        with self.ledger.transaction():
            worker = _record_wake(self.ledger, worker, entries, now, start_turn=True)
        if resume:
            log.info("%s: resumed session %s after the usage limit reset", worker.handle,
                     worker.session_id)
            result.resumed.append(worker)
        else:
            # The message holds only kinds and pointers that passed the checks above.
            log.info("%s: woke session %s with %s", worker.handle, worker.session_id,
                     "; ".join(message.splitlines()[1:]))
        result.woken.append(worker)

    def _continues_session(self, worker: Worker, entries: Sequence[InboxEntry]) -> bool:
        """Whether this wake must go to *worker*'s own session, past any refresh limit.

        Only the session that holds the conversation can use it: the answer to a question
        that the session asked since its last wake, and a summary clone's request for a
        note about that conversation. The refresh comes at the worker's next wake. A
        reply to a session that lists ``blocked`` with no dialog may answer a question in
        plain text, so it stays too. Another reply refreshes as any event does: a worker
        that follows no item gets nothing else.
        """
        if any(
            (entry.source, entry.kind) == (ledger_mod.HANDOFF_SOURCE, ledger_mod.HANDOFF_KIND)
            for entry in entries
        ):
            return True
        if not any(is_reply(entry) for entry in entries):
            return False
        if self._ended_without_dialog(worker):
            return True
        question = self.ledger.latest_question(worker)
        return (
            question is not None
            and question.state == ledger_mod.PROMPT_RESOLVED
            and question.resolved_by in (ledger_mod.RESOLVED_BY_BOARD, ledger_mod.RESOLVED_BY_REPLY)
            and (question.resolved_at or 0.0) >= (worker.last_wake_at or worker.created_at)
        )

    def _refresh_reason(self, worker: Worker) -> str | None:
        """Why *worker*'s next wake goes to a fresh session, or None when it continues this one."""
        try:
            policy = self._policy(worker)
        except ConfigError:
            return None
        # A limit of 0 is no limit, as for the role's other keys that 0 turns off.
        limit = policy.refresh_after_wakes
        if limit:
            wakes = self.ledger.wakes_in_session(worker)
            if wakes >= limit:
                return f"{wakes} wake(s) in its session, the role's limit is {limit}"
        limit = policy.refresh_after_tokens
        if limit:
            tokens = session_tokens(worker)
            if tokens is not None and tokens >= limit:
                return f"about {tokens} tokens in its session, the role's limit is {limit}"
        return None

    def _wake_fresh(
        self,
        worker: Worker,
        message: str,
        entries: Sequence[InboxEntry],
        why: str,
        result: DeliveryResult,
    ) -> bool:
        """Wake *worker* in a fresh session that the caller starts; the old one is kept.

        False when the caller keeps the session, so that the wake continues it.
        """
        assert self._refresh is not None
        reason_key = f"{QUEUE_REASON_PREFIX}{worker.id}"
        try:
            refresh = self._refresh(worker, message)
        except (AgentRuntimeError, ConfigError) as exc:
            problem = f"refresh failed: {sanitize.one_line(str(exc), limit=300)}"
            self.ledger.update(self.ledger.get(worker.id) or worker, last_error=problem[:500])
            self.ledger.set_state(reason_key, problem[:300])
            result.errors.append(f"{worker.handle}: {problem}")
            return True
        if refresh.keep is not None:
            result.warnings.append(
                f"{worker.handle}: no refresh, {sanitize.one_line(refresh.keep, limit=300)}; "
                f"the wake continues its session"
            )
            return False
        if refresh.busy is not None or refresh.session is None:
            detail = sanitize.one_line(refresh.busy or "no fresh session", limit=300)
            self.ledger.set_state(reason_key, f"wake held, {detail}")
            result.held.append(f"{worker.handle}: wake held, {detail}")
            return True
        # A wake in the old session's outbox would have gone to the process that stopped.
        drop_open_outbox(self.ledger, worker, "the worker goes on in a fresh session",
                         now=self._clock())
        if self.crash_after_wake:
            raise DeliveryAborted("delivery aborted after the wake")
        now = self._clock()
        base = refresh.read or worker
        with self.ledger.transaction():
            written = switch_session(
                self.ledger, base, refresh.session, now, replaced=worker.session_id
            )
            if refresh.prompted:
                # Against the worker as read before the launch: a hook of the fresh
                # session during its launch, such as a permission prompt, is newer, and
                # keeps the state it wrote.
                before = dataclasses.replace(
                    written, state=base.state, last_hook_at=base.last_hook_at
                )
                written = _record_wake(
                    self.ledger, before, entries, now, start_turn=True, wake_row=False
                )
        log.info("%s: session %s replaces session %s, which is stopped and kept (%s)",
                 written.handle, written.session_id, worker.session_id or "-", why)
        if refresh.prompted:
            result.woken.append(written)
        return True

    def _wake_in_place(
        self, worker: Worker, message: str, entries: Sequence[InboxEntry], result: DeliveryResult
    ) -> bool:
        """Put *message* into the outbox of a session with the mod; False to stop and resume instead.

        A stopped process is respawned first. The wake is recorded when the mod confirms
        its submit, not here.
        """
        try:
            runtime = self._runtime(worker.runtime)
        except ConfigError:
            return False
        if not runtime.capabilities.in_place_wake or not has_mod(worker):
            return False
        assert worker.session_id is not None and worker.session_uuid is not None
        now = self._clock()
        reason_key = f"{QUEUE_REASON_PREFIX}{worker.id}"
        current = self.ledger.outbox_message(worker)
        if current is not None and current.session_uuid != worker.session_uuid:
            self.ledger.drop_outbox(current.id, "the session changed", now)
            current = None
        if (
            current is not None
            and current.state == ledger_mod.OUTBOX_PENDING
            and now - current.created_at >= OUTBOX_TAKE_SECS
        ):
            self.ledger.drop_outbox(current.id, "the session's mod did not take it", now)
            self.ledger.update(worker, mod_session=None)
            result.warnings.append(
                f"{worker.handle}: the session did not take its wake from the outbox within "
                f"{OUTBOX_TAKE_SECS}s; it is stopped and resumed instead"
            )
            return False
        try:
            session = self._listed(runtime, worker)
            stopped = session is not None and runtime.is_stopped(session)
            if session is None:
                problem = f"session {worker.session_id} is not in the listing"
            elif not stopped and current is not None and current.state == ledger_mod.OUTBOX_TAKEN:
                # The mod submitted the message, or is about to; the session starts its
                # turn once it is idle.
                result.held.append(f"{worker.handle}: wake held, the session took its wake "
                                   f"message and has not started the turn yet")
                return True
            elif stopped:
                if current is not None:
                    # The process that took it is gone, and its submit with it.
                    self.ledger.drop_outbox(current.id, "the session's process stopped", now)
                respawned = runtime.respawn(worker.session_id)
                problem = (
                    f"the respawn of session {worker.session_id} exited {respawned.returncode}: "
                    f"{sanitize.one_line(respawned.stderr or respawned.stdout, limit=200)}"
                    if respawned.returncode != 0 else None
                )
                self._listings.pop(worker.runtime, None)
            else:
                problem = None
        except (AgentRuntimeError, ConfigError) as exc:
            problem = f"{exc}"
        if problem is not None:
            self.ledger.update(worker, last_error=f"wake failed: {problem}"[:500])
            self.ledger.set_state(reason_key, f"wake failed: {problem}"[:300])
            result.errors.append(f"{worker.handle}: wake failed: {problem}")
            return True
        put = self.ledger.put_outbox(
            worker, session_uuid=worker.session_uuid, text=message,
            entries=[entry.id for entry in entries], now=now,
        )
        if put is None:
            result.held.append(f"{worker.handle}: wake held, the session took its wake message "
                               f"and has not started the turn yet")
            return True
        self.ledger.delete_state(reason_key)
        # The message holds only kinds and pointers that passed the checks of wake_message.
        log.info("%s: wake for session %s is in its outbox%s: %s", worker.handle,
                 worker.session_id, " after a respawn" if stopped else "",
                 "; ".join(message.splitlines()[1:]) or message)
        return True

    def _listed(self, runtime: Runtime, worker: Worker) -> runtime_mod.Session | None:
        """*worker*'s session in a fresh listing, read once per run. AgentRuntimeError when it fails."""
        if worker.runtime not in self._listings:
            self._listings[worker.runtime] = runtime.list_sessions()
        return next((s for s in self._listings[worker.runtime] if s.id == worker.session_id), None)

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
        # The stopped process took no wake; a continue gets a new one.
        drop_open_outbox(self.ledger, paused, "the wake was refused", now=self._clock())
        result.faults.append((paused, fault))
        result.errors.append(f"{worker.handle}: wake refused: {fault}")
        return True

    def _refusal(self, worker: Worker, result: DeliveryResult) -> str | None:
        """Why admission holds this wake back now, or None when it may go."""
        # A wake in an outbox is a turn about to start.
        sending = self.ledger.outbox_workers() - {worker.id}
        working = sum(
            w.state in WORKING_STATES or w.id in sending for w in self.ledger.live_workers()
        )
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
            notified = f"{CAP_NOTIFIED_PREFIX}{worker.id}"
            if self.ledger.get_state(notified) is None:
                self.ledger.set_state(notified, str(now))
                result.capped.append(
                    (worker, f"{wakes} wakes in 24 hours, the role's daily cap is "
                             f"{policy.max_wakes_per_day}")
                )
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
    "CAP_NOTIFIED_PREFIX",
    "CONFIRM_ALREADY",
    "CONFIRM_DROPPED",
    "CONFIRM_RECORDED",
    "CONFIRM_UNKNOWN",
    "DEBOUNCE_MAX_HOLD_FACTOR",
    "DEFAULT_NEW_WORKER_RESERVE_PERCENT",
    "DEFAULT_WAKE_RESERVE_PERCENT",
    "Delivery",
    "DeliveryResult",
    "MESSAGE_HEADER",
    "OUTBOX_POLL_SECS",
    "OUTBOX_RETAKE_SECS",
    "OUTBOX_TAKE_SECS",
    "QUEUE_REASON_PREFIX",
    "RESUME_MESSAGE",
    "Refresh",
    "UsageReading",
    "UsageWindow",
    "UNKNOWN_KIND",
    "WAKE_STATES",
    "WEEKLY_WINDOW",
    "confirm_outbox",
    "drop_open_outbox",
    "has_mod",
    "is_reply",
    "new_worker_admits",
    "safe_kind",
    "record_mod_hello",
    "safe_pointer",
    "session_tokens",
    "switch_session",
    "take_outbox",
    "tokens_estimate",
    "usage_admits",
    "wake_message",
    "weekly_window",
    "window_over_reserve",
]
