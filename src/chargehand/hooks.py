"""The hook receiver: what worker sessions post to the daemon, applied to the ledger at once.

Each session is launched with settings hooks that post to ``POST /hooks/<event>`` with
the session's own token (see :mod:`chargehand.worker_tokens`): its start, the end of each
turn, a turn that failed, its notifications and its permission prompts. The token says
which worker posts. A session launched before sessions had their own token posts with the
board's token, and its post maps to a worker by its session id and working directory, as
before. The runtime that owns the event reads the payload; this module finds the worker
and hands the post to the tick's own transition code, so a hook and the session diff move a worker the same way and send the same
notification. The diff of each tick stays as the fallback for a hook that never came.

Almost every answer is an empty JSON object. A turn end gets no decision that would keep
the turn running, and a session start gets no context for the model. A post that maps to
no live worker changes nothing.

A permission prompt of a runtime with board permissions is held open instead: the board
shows a card, and the hook answers with the decision once the operator clicks Allow, Deny
or Always allow. Always allow records a rule for that worker alone, and the next post of
the same tool with the same input is allowed at once, without a card. The ledger row is
the record, and the first answer wins. The hook answers with no decision, so that the
session shows its own dialog, when the worker leaves ``blocked`` (the operator answered
in the session), when the session's hook timeout comes near, and when the daemon stops.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from chargehand import ledger as ledger_mod
from chargehand import runtime as runtime_mod
from chargehand import sanitize
from chargehand.ledger import Ledger, PermissionPrompt, Worker
from chargehand.runtime import Hook, Runtime
from chargehand.serve import AUTH_WORKER_OR_BOARD, HTTPError, Request, Response, Routes
from chargehand.tick import TickReport

log = logging.getLogger(__name__)

PREFIX = "/hooks"

# How much of a session id a hook's log line carries.
SESSION_PREFIX = 8

# The answer to every hook that the daemon does not decide. Anything else could reach the
# session's turn or its model.
NO_DECISION = Response.json({})

# How often a held permission hook reads its prompt again. An answer on the board wakes it
# at once; a cancel by the tick or another hook is seen within this time.
HOLD_POLL_SECS = 0.25
# How often a held hook checks that its session still shows the dialog. An answer in
# `claude attach` sends no hook, and the tick may be a minute away.
SESSION_CHECK_SECS = 2.0
# The first of those checks comes sooner, to see the session show the dialog.
FIRST_SESSION_CHECK_SECS = 0.5
# How long the process waits at exit for held hooks to send their answers.
EXIT_DRAIN_SECS = 2.0

# What the board sends, and the decision and rule each one means.
ALWAYS = "always"
ANSWERS: dict[str, tuple[str, bool]] = {
    ledger_mod.ALLOW: (ledger_mod.ALLOW, False),
    ledger_mod.DENY: (ledger_mod.DENY, False),
    ALWAYS: (ledger_mod.ALLOW, True),
}
ALREADY_RESOLVED = "already resolved"
NOT_HELD = "the session no longer waits for the board; attach to it and answer there"
ANSWERED_IN_SESSION = f"{ALREADY_RESOLVED}: the session no longer shows this prompt"
NOT_CONFIRMED = (
    "cannot confirm that the session still shows this prompt; try again, or attach to it "
    "and answer there"
)


class _Holds:
    """The permission hooks this process holds open, by ledger and prompt id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, int], threading.Event] = {}

    def add(self, key: tuple[str, int]) -> threading.Event:
        with self._lock:
            event = self._events[key] = threading.Event()
        return event

    def remove(self, key: tuple[str, int]) -> None:
        with self._lock:
            self._events.pop(key, None)

    def wake(self, key: tuple[str, int]) -> None:
        with self._lock:
            event = self._events.get(key)
        if event is not None:
            event.set()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._events

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


Look = tuple[float, "dict[str, bool] | None"]


class _Listings:
    """The last look at which sessions show a permission dialog, per ledger and runtime.

    Held hooks ask for a look every SESSION_CHECK_SECS and read the newest one, so a
    terminal answer clears its card within a few seconds; with many held hooks it is still
    one listing per interval. Their listing runs on a thread of its own, so a slow
    `claude agents` never keeps a held hook from its stop or its timeout. A click lists at
    once and waits for it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._looks: dict[tuple[str, str], Look] = {}
        self._busy: set[tuple[str, str]] = set()

    def latest(self, key: tuple[str, str]) -> Look | None:
        with self._lock:
            return self._looks.get(key)

    def refresh_soon(self, key: tuple[str, str], runtime: Runtime, *, max_age: float) -> None:
        with self._lock:
            look = self._looks.get(key)
            if key in self._busy or (look is not None and time.monotonic() - look[0] <= max_age):
                return
            self._busy.add(key)
        threading.Thread(
            target=self.refresh, args=(key, runtime), name="chargehand-permission-listing",
            daemon=True,
        ).start()

    def refresh(self, key: tuple[str, str], runtime: Runtime) -> dict[str, bool] | None:
        try:
            waits = runtime.permission_waits()
        except Exception:
            log.exception("cannot read whether the sessions wait for a permission answer")
            waits = None
        with self._lock:
            self._looks[key] = (time.monotonic(), waits)
            self._busy.discard(key)
        return waits


_holds = _Holds()
_listings = _Listings()
_drain_registered = False


def _key(daemon: object, prompt_id: int) -> tuple[str, int]:
    return (os.path.realpath(getattr(daemon, "ledger_path")), prompt_id)


def is_held(daemon: object, prompt_id: int) -> bool:
    """Whether a hook of this daemon waits for the board's answer to the prompt."""
    return _key(daemon, prompt_id) in _holds


def _drain() -> None:
    """At exit, give the held hooks the moment they need to answer with no decision.

    The daemon's threads end with the process; a hook cut off there would read as an
    error in the session instead of an answer.
    """
    if not len(_holds):
        return
    deadline = time.monotonic() + EXIT_DRAIN_SECS
    while len(_holds) and time.monotonic() < deadline:
        time.sleep(0.05)
    # The answer is written after the handler returns.
    time.sleep(0.2)


def register_routes(routes: Routes) -> None:
    global _drain_registered
    routes.add("POST", PREFIX, receive, prefix=True, auth=AUTH_WORKER_OR_BOARD)
    routes.extend_state(add_to_state)
    if not _drain_registered:
        atexit.register(_drain)
        _drain_registered = True


def hook_runtimes(event: str) -> list[type[Runtime]]:
    """The runtimes whose sessions post *event*."""
    found = []
    for name in runtime_mod.names():
        runtime = runtime_mod.get(name)
        if runtime.capabilities.session_hooks and event in runtime.hook_events:
            found.append(runtime)
    return found


def receive(request: Request) -> Response:
    event = request.path[len(PREFIX):].strip("/")
    runtimes = hook_runtimes(event) if event and "/" not in event else []
    if not runtimes:
        raise HTTPError(404, "not found")
    payload = request.json()
    if not isinstance(payload, Mapping):
        raise HTTPError(400, "the body is not a JSON object")
    for runtime in runtimes:
        hook = runtime.parse_hook(event, payload)
        if hook is not None:
            break
    else:
        log.info("hook %s: the payload is not one a runtime reads", event)
        return NO_DECISION
    ledger = request.ledger()
    session = session_prefix(hook)
    if request.caller is not None:
        worker = ledger.get(request.caller.id)
        if worker is not None and (worker.is_terminal or worker.runtime != runtime.name):
            worker = None
    else:
        worker = find_worker(ledger, hook, runtime.name)
        if worker is not None:
            _note_board_token(worker, hook)
    if worker is None:
        log.info("hook %s from session %s: no live worker has this session (cwd %s)",
                 event, session, sanitize.one_line(hook.cwd or "-", limit=300))
        return NO_DECISION
    log.info("%s: hook %s from session %s", worker.handle, event, session)
    held = hook.kind == runtime_mod.HOOK_PERMISSION and runtime.capabilities.board_permissions
    if held and _allowed_by_rule(ledger, worker, hook):
        log.info("%s: an always-allow rule of this worker allowed the permission prompt",
                 worker.handle)
        return Response.json(runtime.permission_decision(ledger_mod.ALLOW))
    report = TickReport()
    try:
        request.daemon.runner(ledger).apply_hook(worker, hook, report)
    except Exception:
        # The session must not see an error it could act on; the next diff catches up.
        log.exception("hook %s for %s failed", event, worker.handle)
        return NO_DECISION
    # Into the daemon's log in the tick's own format, so a turn end shows there as the
    # change it made. A report with nothing in it prints nothing.
    request.daemon.report(report)
    if held:
        return _hold(request, ledger, runtime, worker, hook)
    return NO_DECISION


# ----- permission prompts on the board -----------------------------------------------


def _allowed_by_rule(ledger: Ledger, worker: Worker, hook: Hook) -> bool:
    """Record the prompt as allowed when the worker has a rule for this tool and input."""
    digest = hook.tool_input_digest
    tool = hook.tool_name or "unknown"
    if not digest or not ledger.has_permission_rule(worker, tool, digest):
        return False
    # A session shows one dialog at a time; an older open prompt was answered in it. A
    # question that the ask tool recorded stays open.
    ledger.resolve_permission_prompts(worker, kind=ledger_mod.PROMPT_PERMISSION)
    ledger.record_permission_prompt(
        worker, tool_name=tool, tool_input=hook.tool_input or "", input_digest=digest,
        resolved_by=ledger_mod.RESOLVED_BY_RULE,
    )
    return True


def _recorded_prompt(ledger: Ledger, worker: Worker, hook: Hook) -> PermissionPrompt | None:
    """The open prompt that applying *hook* recorded: the newest with its tool and input."""
    tool, given = hook.tool_name or "unknown", hook.tool_input or ""
    for prompt in reversed(ledger.open_permission_prompts(worker)):
        if prompt.tool_name == tool and prompt.tool_input == given:
            return prompt
    return None


def _hold(
    request: Request, ledger: Ledger, runtime: type[Runtime], worker: Worker, hook: Hook
) -> Response:
    """Hold the hook until the prompt is answered or cancelled, the timeout nears, or a stop."""
    prompt = _recorded_prompt(ledger, worker, hook)
    if prompt is None:
        return NO_DECISION
    if hook.tool_input_digest:
        ledger.set_prompt_digest(prompt.id, hook.tool_input_digest)
    try:
        hold_secs = runtime.permission_hold_secs(Path(worker.worktree))
    except Exception:
        log.exception("%s: cannot read how long the permission hook waits", worker.handle)
        hold_secs = None
    if not hold_secs or hold_secs <= 0:
        ledger.release_permission_prompt(prompt.id)
        log.info("%s: permission prompt %d: the session's hook timeout is too short to wait "
                 "for the board; it answers in the session", worker.handle, prompt.id)
        return NO_DECISION
    daemon = request.daemon
    key = _key(daemon, prompt.id)
    wake = _holds.add(key)
    deadline = time.monotonic() + hold_secs
    log.info("%s: permission prompt %d waits for the board, up to %ds",
             worker.handle, prompt.id, int(hold_secs))
    check = _SessionCheck(request, ledger, worker)
    try:
        while True:
            if check.answered_in_session():
                if ledger.cancel_permission_prompt(prompt.id):
                    log.info("%s: permission prompt %d: the session no longer shows it",
                             worker.handle, prompt.id)
            current = ledger.get_permission_prompt(prompt.id)
            if current is None or current.state == ledger_mod.PROMPT_CANCELLED:
                log.info("%s: permission prompt %d was cancelled: the session no longer "
                         "shows it, or the worker stopped; the hook returns no decision",
                         worker.handle, prompt.id)
                return NO_DECISION
            if current.state == ledger_mod.PROMPT_RESOLVED and current.decision:
                return Response.json(runtime.permission_decision(current.decision))
            if current.released_at is not None:
                return NO_DECISION
            stopping = daemon.stop_requested()
            if stopping or time.monotonic() >= deadline:
                if ledger.release_permission_prompt(prompt.id):
                    log.info("%s: permission prompt %d: %s; the session shows its own dialog",
                             worker.handle, prompt.id,
                             "the daemon stops" if stopping else "no answer before the timeout")
                    return NO_DECISION
                continue
            wake.wait(HOLD_POLL_SECS)
    finally:
        _holds.remove(key)


class _SessionCheck:
    """Whether a held prompt's session stopped showing the dialog, looked at now and then.

    Only a session that was seen showing a permission dialog and no longer shows one
    counts as answered there, so a listing that lags behind the hook cancels nothing. The
    first look comes soon after the hold starts, so an answer in the session within the
    first seconds is seen too. Only looks taken after the hold started count.
    """

    def __init__(self, request: Request, ledger: Ledger, worker: Worker) -> None:
        self._ledger = ledger
        self._worker_id = worker.id
        self._seen = False
        self._used = time.monotonic()
        self._next = self._used + FIRST_SESSION_CHECK_SECS
        try:
            self._runtime: Runtime | None = request.daemon.runner(ledger).runtime(worker.runtime)
        except Exception:
            log.exception("%s: cannot check the session of a held permission prompt",
                          worker.handle)
            self._runtime = None
        self._key = (os.path.realpath(request.daemon.ledger_path), worker.runtime)

    def answered_in_session(self) -> bool:
        if self._runtime is None:
            return False
        if time.monotonic() >= self._next:
            self._next = time.monotonic() + SESSION_CHECK_SECS
            # Held hooks share one listing per interval.
            _listings.refresh_soon(self._key, self._runtime, max_age=SESSION_CHECK_SECS * 0.9)
        look = _listings.latest(self._key)
        if look is None or look[0] <= self._used or look[1] is None:
            return False
        self._used = look[0]
        worker = self._ledger.get(self._worker_id)
        session = worker and (worker.session_id or worker.session_uuid)
        if not session:
            return False
        if look[1].get(session, False):
            self._seen = True
            return False
        return self._seen


def answer_prompt(
    request: Request, ledger: Ledger, worker: Worker, prompt_id: object, answer: object
) -> dict[str, object]:
    """The board's answer to an open prompt of *worker*. Raises HTTPError when refused.

    Only a prompt whose hook this daemon still holds can be answered: after a timeout or a
    restart the session's own dialog is the only place left to answer. The runtime confirms
    that the session still shows the dialog, so a click after an answer in the session is
    refused and cancels the card, instead of recording the opposite decision.
    """
    if not isinstance(prompt_id, int) or isinstance(prompt_id, bool):
        raise HTTPError(400, "prompt must be the id of the prompt")
    if answer not in ANSWERS:
        raise HTTPError(400, f"answer must be one of {', '.join(ANSWERS)}")
    prompt = ledger.get_permission_prompt(prompt_id)
    if prompt is None or prompt.worker_id != worker.id:
        raise HTTPError(404, f"{worker.handle} has no prompt {prompt_id}")
    if prompt.state != ledger_mod.PROMPT_OPEN:
        raise HTTPError(409, ALREADY_RESOLVED)
    if prompt.kind != ledger_mod.PROMPT_PERMISSION or not is_held(request.daemon, prompt.id):
        raise HTTPError(409, NOT_HELD)
    decision, rule = ANSWERS[str(answer)]
    if rule and not prompt.input_digest:
        raise HTTPError(409, "this prompt cannot become a rule; allow it once instead")
    waiting = _session_waits(request, ledger, worker)
    if waiting is False:
        # Answered in the session: the hook still waits, but the dialog is gone. The next
        # hook or tick would cancel the record; this click does it now.
        if ledger.cancel_permission_prompt(prompt.id):
            _holds.wake(_key(request.daemon, prompt.id))
            log.info("%s: permission prompt %d: the session no longer shows it; the board's "
                     "answer is refused", worker.handle, prompt.id)
        raise HTTPError(409, ANSWERED_IN_SESSION)
    if waiting is None:
        raise HTTPError(409, NOT_CONFIRMED)
    if not ledger.answer_permission_prompt(
        prompt.id, decision, by=ledger_mod.RESOLVED_BY_BOARD, rule=rule
    ):
        # Another answer won, or the hook let go of the prompt (its timeout or a stop).
        now = ledger.get_permission_prompt(prompt.id)
        still_open = now is not None and now.state == ledger_mod.PROMPT_OPEN
        raise HTTPError(409, NOT_HELD if still_open else ALREADY_RESOLVED)
    _holds.wake(_key(request.daemon, prompt.id))
    log.info("%s: permission prompt %d answered %s on the board", worker.handle, prompt.id, answer)
    message = {
        ledger_mod.ALLOW: "allowed",
        ledger_mod.DENY: "denied",
        ALWAYS: "allowed, and allowed again for this worker without asking",
    }[str(answer)]
    return {"prompt": prompt.id, "answer": answer, "ok": True, "message": message}


def _session_waits(request: Request, ledger: Ledger, worker: Worker) -> bool | None:
    """Whether the worker's session still shows a permission dialog, from a fresh look.

    The held hook does not tell: Claude Code keeps its connection open after the operator
    answers in `claude attach`. None when the runtime cannot tell, which refuses the answer.
    """
    session = worker.session_id or worker.session_uuid
    if not session:
        return None
    try:
        runtime = request.daemon.runner(ledger).runtime(worker.runtime)
        key = (os.path.realpath(request.daemon.ledger_path), worker.runtime)
        waits = _listings.refresh(key, runtime)
        return None if waits is None else waits.get(session, False)
    except Exception:
        log.exception("%s: cannot read whether the session waits for a permission answer",
                      worker.handle)
        return None


# The sessions whose hooks were seen with the board's token, so that the log says it once
# per session. Bounded: a long-running daemon forgets the oldest.
_BOARD_TOKEN_SESSIONS: dict[str, None] = {}
_BOARD_TOKEN_SESSIONS_MAX = 1000
_board_token_lock = threading.Lock()


def _note_board_token(worker: Worker, hook: Hook) -> None:
    key = hook.session_uuid or worker.worktree
    with _board_token_lock:
        if key in _BOARD_TOKEN_SESSIONS:
            return
        _BOARD_TOKEN_SESSIONS[key] = None
        while len(_BOARD_TOKEN_SESSIONS) > _BOARD_TOKEN_SESSIONS_MAX:
            del _BOARD_TOKEN_SESSIONS[next(iter(_BOARD_TOKEN_SESSIONS))]
    log.info("%s: session %s posts its hooks with the board's token; it was launched before "
             "sessions had their own token, and its next launch gets one", worker.handle,
             session_prefix(hook))


def session_prefix(hook: Hook) -> str:
    """The first characters of the session id, enough to tell sessions apart in the log."""
    return sanitize.one_line(hook.session_uuid or "", limit=40)[:SESSION_PREFIX] or "-"


def find_worker(ledger: Ledger, hook: Hook, runtime: str) -> Worker | None:
    """The live worker of the posting session: by session id, else by working directory.

    The working directory decides only for a worker that has no session id yet, as in
    the middle of its launch: a session that another process started in a worker's
    worktree is not that worker's session.
    """
    if hook.session_uuid:
        worker = ledger.live_worker_by_session_uuid(hook.session_uuid)
        if worker is not None:
            return worker if worker.runtime == runtime else None
    if not hook.cwd:
        return None
    # /tmp is /private/tmp on macOS, and either spelling can reach either side.
    target = os.path.realpath(hook.cwd)
    for worker in ledger.live_workers():
        if worker.runtime != runtime or os.path.realpath(worker.worktree) != target:
            continue
        if worker.session_uuid and hook.session_uuid:
            return None
        return worker
    return None


# ----- the state the board reads ------------------------------------------------


def _prompt(request: Request, prompt: PermissionPrompt) -> dict[str, object]:
    return {
        "id": prompt.id,
        "tool_name": sanitize.one_line(prompt.tool_name, limit=100),
        "tool_input": sanitize.clean(prompt.tool_input),
        "created_at": prompt.created_at,
        "answerable": is_held(request.daemon, prompt.id),
    }


def add_to_state(request: Request, payload: dict[str, object]) -> None:
    """Add what the hooks reported to each live worker of `/api/state`.

    The `status --json` keys stay as they are, its last hook and reset time included;
    each worker gains its last answer and its open permission prompts, which are
    session text and stay out of `status`.
    """
    ledger = request.ledger()
    # The status payload has no row id; a live worker's worktree and creation time name it.
    by_key = {(worker.worktree, worker.created_at): worker for worker in ledger.live_workers()}
    prompts = ledger.open_permission_prompts()
    for entry in payload.get("workers") or []:
        if not isinstance(entry, dict):
            continue
        worker = by_key.get((entry.get("worktree"), entry.get("created_at")))
        if worker is None:
            continue
        entry["last_answer"] = sanitize.clean(worker.last_answer) if worker.last_answer else None
        entry["permission_prompts"] = [
            _prompt(request, prompt) for prompt in prompts if prompt.worker_id == worker.id
        ]


__all__ = [
    "ANSWERS",
    "NO_DECISION",
    "add_to_state",
    "answer_prompt",
    "find_worker",
    "hook_runtimes",
    "is_held",
    "receive",
    "register_routes",
]
