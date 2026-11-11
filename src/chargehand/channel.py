"""The agent channel: what a worker session's chargehand mod posts, and the tools it serves.

The tool contract is defined here once, independent of the transport: each tool has a
name, a description for the model, a JSON schema for its input and a handler in the
daemon. The mod reads the list at the session start (``POST /channel/hello``), registers
each tool, and forwards every call to ``POST /channel/tool/<name>``, so a new tool needs no
change in the mod. A later transport, such as a stdio MCP server for another runtime,
serves the same list.

The other routes carry the rest of the mod's work. ``/channel/outbox`` hands the worker's
wake message to the mod, and ``/channel/outbox/confirm`` records the wake once its submit
started the turn (see :mod:`chargehand.delivery`). ``/channel/lifecycle/<event>`` reports
the session start, each turn end and the session end; a turn end goes through the same
transition as the session hook's, and the other two change no state. ``/channel/usage``
carries the session's rate-limit windows and its context window after each turn.

Every route is a POST with the token of the worker's own session (see
:mod:`chargehand.worker_tokens`), and the token, not the session id or the working
directory that the post names, says which worker calls. The board's token is refused here,
and so is the token of a worker that ended, except by the outbox, which tells the mod to
stop polling. Everything a post carries is untrusted session output: it is cleaned and
capped, and no tool argument reaches a log line, a notification or a prompt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from chargehand import delivery as delivery_mod
from chargehand import hooks
from chargehand import ledger as ledger_mod
from chargehand import runtime as runtime_mod
from chargehand import sanitize, usage
from chargehand.ledger import Ledger, Worker
from chargehand.runtime import Runtime
from chargehand.serve import AUTH_WORKER, HTTPError, Request, Response, Routes
from chargehand.tick import HANDOFF_TOOL, TickReport

log = logging.getLogger(__name__)

PREFIX = "/channel"

# A worker in one of these states gets no wake from its outbox: the operator paused it, or
# it ended.
NO_WAKE_STATES = (ledger_mod.PAUSED, *ledger_mod.TERMINAL)

# The most event kinds one subscribe names.
MAX_KINDS = 20

# What a report keeps: one line of summary, and a few plain http(s) links.
SUMMARY_LIMIT = 300
MAX_LINKS = 10
# A link that a report gives which names a pull request: it becomes the card's link, and
# the worker follows the pull request.
_PULL_REQUEST_URL = re.compile(
    r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+/?"
)

# What a question keeps: one line, and up to MAX_OPTIONS options of one short line each.
QUESTION_LIMIT = 1000
OPTION_LIMIT = 100
MAX_OPTIONS = 10

# The largest handoff note, in characters.
HANDOFF_LIMIT = 16 * 1024

# How far ahead request_wake may set its timer.
MAX_WAKE_AFTER_SECS = 7 * 24 * 3600


@dataclass(frozen=True)
class ToolCall:
    """One call of a tool by a worker's session. *arguments* are untrusted."""

    request: Request
    ledger: Ledger
    worker: Worker
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """What the model reads. chargehand's own text, or text that passed its checks."""

    text: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[ToolCall], ToolResult]

    def spec(self) -> dict[str, Any]:
        """The tool as the mod registers it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


def _report(call: ToolCall) -> ToolResult:
    """Keep the worker's state, summary and links; a pull request link becomes its card's link."""
    state = call.arguments.get("state")
    summary = call.arguments.get("summary")
    links = call.arguments.get("links", [])
    if state not in ledger_mod.REPORT_STATES:
        return ToolResult(
            f"state must be one of {', '.join(ledger_mod.REPORT_STATES)}.", is_error=True
        )
    if not isinstance(summary, str):
        return ToolResult("summary must be one short sentence.", is_error=True)
    if not isinstance(links, list) or len(links) > MAX_LINKS or not all(
        isinstance(link, str) for link in links
    ):
        return ToolResult(f"links must be a list of at most {MAX_LINKS} URLs.", is_error=True)
    kept = list(dict.fromkeys(url for link in links if (url := sanitize.safe_url(link))))
    worker = call.ledger.update(
        call.worker,
        report_state=state,
        report_summary=sanitize.one_line(summary, limit=SUMMARY_LIMIT) or None,
        report_links=json.dumps(kept) if kept else None,
        reported_at=time.time(),
    )
    log.info("%s: reported %s", worker.handle, state)
    said = [f"Reported {state}."]
    if len(kept) < len(links):
        said.append(
            f"{len(links) - len(kept)} link(s) were left out: only plain http(s) URLs count."
        )
    pull = next((url for url in kept if _PULL_REQUEST_URL.fullmatch(url)), None)
    if pull is not None:
        runner = call.request.daemon.runner(call.ledger)
        followed = runner.follow_reported_pull_request(worker, pull)
        if followed is not None:
            log.info("%s: follows its reported pull request %s", worker.handle, followed)
            said.append(
                "You follow this pull request now: its comments, reviews and CI results wake you."
            )
    if state in (ledger_mod.REPORT_DONE, ledger_mod.REPORT_FAILED):
        said.append(
            f"chargehand ends this worker as {state} when your turn ends. End your turn now."
        )
    return ToolResult(" ".join(said))


def _ask(call: ToolCall) -> ToolResult:
    """Put the question and its options on the board; the answer comes as the next wake."""
    question = call.arguments.get("question")
    options = call.arguments.get("options", [])
    text = sanitize.one_line(question, limit=QUESTION_LIMIT) if isinstance(question, str) else ""
    if not text:
        return ToolResult("question must be a non-empty question.", is_error=True)
    if not isinstance(options, list) or len(options) > MAX_OPTIONS or not all(
        isinstance(option, str) for option in options
    ):
        return ToolResult(
            f"options must be a list of at most {MAX_OPTIONS} short answers.", is_error=True
        )
    kept = list(dict.fromkeys(
        line for option in options if (line := sanitize.one_line(option, limit=OPTION_LIMIT))
    ))
    call.ledger.record_question(call.worker, text, kept)
    log.info("%s: asked a question with %d option(s)", call.worker.handle, len(kept))
    return ToolResult(
        "Your question is on the operator's board. End your turn now and do not wait for "
        "the answer: it arrives as your next message, as a reply from the operator."
    )


def _handoff(call: ToolCall) -> ToolResult:
    """Keep the note on the worker, for a later session of this work."""
    note = call.arguments.get("note")
    text = sanitize.clean(note).strip() if isinstance(note, str) else ""
    if not text:
        return ToolResult("note must be a non-empty text.", is_error=True)
    cut = len(text) > HANDOFF_LIMIT
    text = text[:HANDOFF_LIMIT]
    call.ledger.update(call.worker, handoff_note=text, handoff_at=time.time())
    # The note is session text: the log gives its length only.
    log.info("%s: stored its handoff note (%d characters)", call.worker.handle, len(text))
    if cut:
        return ToolResult(
            f"Your handoff note is stored, cut to its first {HANDOFF_LIMIT} characters."
        )
    return ToolResult("Your handoff note is stored. A later call replaces it.")


def _read_handoff(call: ToolCall) -> ToolResult:
    """The worker's stored handoff note: its own text, which reaches the model only as this result."""
    note = call.worker.handoff_note
    if not note:
        return ToolResult("No handoff note is stored for this work yet.")
    log.info("%s: read its handoff note (%d characters)", call.worker.handle, len(note))
    return ToolResult(note)


def _request_wake(call: ToolCall) -> ToolResult:
    """Put a timer into the worker's inbox; delivery wakes it once it is due, as for any event."""
    after = call.arguments.get("after_secs")
    if (
        not isinstance(after, int) or isinstance(after, bool)
        or not 0 < after <= MAX_WAKE_AFTER_SECS
    ):
        return ToolResult(
            f"after_secs must be a whole number of seconds from 1 to {MAX_WAKE_AFTER_SECS}.",
            is_error=True,
        )
    worker = call.worker
    now = time.time()
    key = worker.item or worker.handle
    call.ledger.put_timer(
        worker, due_at=now + after, item=key, item_ref=worker.item_id or key, now=now
    )
    log.info("%s: asked to be woken in %d s", worker.handle, after)
    return ToolResult(
        f"chargehand wakes you in {after} seconds or later, with the line "
        f"'{ledger_mod.TIMER_KIND}: {ledger_mod.TIMER_POINTER}'. The wake counts against your "
        f"daily wake cap, and the caps can delay it. A later request_wake replaces this one. "
        f"Keep in mind what you want to do then, and end your turn when you are ready."
    )


def _subscribe(call: ToolCall) -> ToolResult:
    """Subscribe the calling worker to an item that a configured source names."""
    text = call.arguments.get("item")
    kinds = call.arguments.get("kinds")
    if not isinstance(text, str) or delivery_mod.safe_pointer(text.strip()) is None:
        return ToolResult("item must be a plain http(s) URL or a plain identifier.", is_error=True)
    if (
        not isinstance(kinds, list)
        or not 0 < len(kinds) <= MAX_KINDS
        or not all(isinstance(kind, str) and sanitize.is_event_kind(kind) for kind in kinds)
    ):
        return ToolResult(
            f"kinds must be a list of 1 to {MAX_KINDS} event kinds, each a lower-case name "
            f"such as comment or ci_status.",
            is_error=True,
        )
    runner = call.request.daemon.runner(call.ledger)
    item = None
    # As the tick follows a pull request: ask each configured source in turn.
    for config in runner.config.sources:
        source = runner.source(config.name)
        if source is not None and (item := source.item_for(text.strip())) is not None:
            break
    if item is None:
        return ToolResult("no configured source follows this item.", is_error=True)
    existing = next(
        (s for s in call.ledger.subscriptions_of(call.worker)
         if (s.source, s.item) == (item.source, item.key)),
        None,
    )
    # A second subscribe to the same item adds its kinds to the ones already followed.
    wanted = list(dict.fromkeys([*(existing.kinds if existing else ()), *kinds]))
    call.ledger.subscribe(call.worker, item, wanted)
    log.info("%s: follows %s on %s (%s)", call.worker.handle, item.key, item.source,
             ", ".join(wanted))
    events = " or ".join(f"a {kind}" for kind in wanted)
    return ToolResult(f"You follow {item.key} now: {events} on it wakes you.")


def _schema(properties: Mapping[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_TEXT = {"type": "string"}
_TEXTS = {"type": "array", "items": {"type": "string"}}

# The tools every worker session gets, in the order the mod registers them.
REGISTRY: tuple[Tool, ...] = (
    Tool(
        "report",
        "Report your progress to the operator, and call it when you finish. state is one of "
        "working, done, failed or needs-input; summary is one short sentence; links is an "
        "optional list of URLs, such as your pull request, which you then follow. done and "
        "failed end your work when your turn ends.",
        _schema({
            "state": {"type": "string", "enum": ["working", "done", "failed", "needs-input"]},
            "summary": _TEXT,
            "links": _TEXTS,
        }, ("state", "summary")),
        _report,
    ),
    Tool(
        "ask",
        "Ask the operator a question, with optional answer options. Use it instead of "
        "AskUserQuestion. It returns at once. End your turn right after it; the answer "
        "arrives later as your next message.",
        _schema({"question": _TEXT, "options": _TEXTS}, ("question",)),
        _ask,
    ),
    Tool(
        "handoff",
        "Write your handoff note: what you did, what is left, and where things are, so that a "
        "later session can go on from it. Call it at the end of every turn, so the note stays "
        "current. A later call replaces it. A wake line 'handoff: ...' asks for a current note "
        "now: call handoff in that turn.",
        _schema({"note": _TEXT}, ("note",)),
        _handoff,
    ),
    Tool(
        HANDOFF_TOOL,
        "Read your handoff note, the one that your earlier session of this work stored with "
        "handoff. Call it first in a fresh session.",
        _schema({}, ()),
        _read_handoff,
    ),
    Tool(
        "request_wake",
        "Ask to be woken later, after after_secs seconds, for example to look at a slow CI "
        "run again. The wake message does not say why, so keep that in mind yourself.",
        _schema({"after_secs": {"type": "integer", "minimum": 1,
                                "maximum": MAX_WAKE_AFTER_SECS}}, ("after_secs",)),
        _request_wake,
    ),
    Tool(
        "subscribe",
        "Follow the events of a work item, such as a pull request or an issue that your work "
        "depends on. item is its URL or identifier; kinds are the event kinds that wake you, "
        "such as comment or ci_status.",
        _schema({"item": _TEXT, "kinds": _TEXTS}, ("item", "kinds")),
        _subscribe,
    ),
)

TOOLS: dict[str, Tool] = {tool.name: tool for tool in REGISTRY}


def register_routes(routes: Routes) -> None:
    routes.add("POST", f"{PREFIX}/hello", hello, auth=AUTH_WORKER)
    routes.add("POST", f"{PREFIX}/tool", call_tool, prefix=True, auth=AUTH_WORKER)
    routes.add("POST", f"{PREFIX}/outbox", take, auth=AUTH_WORKER)
    routes.add("POST", f"{PREFIX}/outbox/confirm", confirm, auth=AUTH_WORKER)
    routes.add("POST", f"{PREFIX}/lifecycle", lifecycle, prefix=True, auth=AUTH_WORKER)
    routes.add("POST", f"{PREFIX}/usage", record_usage, auth=AUTH_WORKER)


# ----- mapping a post to its worker --------------------------------------------------


def _runtimes() -> list[type[Runtime]]:
    """The runtimes whose sessions talk on the channel."""
    return [
        runtime
        for runtime in (runtime_mod.get(name) for name in runtime_mod.names())
        if runtime.capabilities.in_place_wake and runtime.channel_events
    ]


def _payload(request: Request) -> Mapping[str, Any]:
    payload = request.json()
    if not isinstance(payload, Mapping):
        raise HTTPError(400, "the body is not a JSON object")
    return payload


def _text(payload: Mapping[str, Any], key: str, limit: int) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    return sanitize.one_line(value, limit=limit) or None


def _worker(
    request: Request, payload: Mapping[str, Any] | None = None
) -> tuple[Ledger, Worker, type[Runtime]]:
    """The live worker whose token the post carries; 401 when it ended.

    With *payload*, the post must also come from the worker's own session (see
    :func:`chargehand.hooks.is_own_session`), else 404, as for a session no worker has.
    """
    ledger = request.ledger()
    caller = request.caller
    if caller is None:
        raise HTTPError(401, "a valid worker token is required")
    worker = ledger.get(caller.id)
    if worker is None or worker.is_terminal:
        raise HTTPError(401, "this worker has ended")
    runtime = next((r for r in _runtimes() if r.name == worker.runtime), None)
    if runtime is None:
        raise HTTPError(404, "this worker's runtime has no channel")
    if payload is not None:
        session = _text(payload, "session_id", 100)
        if not hooks.is_own_session(worker, session):
            log.info("%s: channel %s from session %s, which is not the worker's session",
                     worker.handle, sanitize.one_line(request.path, limit=80),
                     (session or "-")[:hooks.SESSION_PREFIX])
            raise HTTPError(404, "this is not the worker's session")
    return ledger, worker, runtime


def _session(payload: Mapping[str, Any]) -> str:
    session = _text(payload, "session_id", 100)
    if not session:
        raise HTTPError(400, "session_id is missing")
    return session


# ----- the routes --------------------------------------------------------------------


def hello(request: Request) -> Response:
    """Record that the session's mod runs, and give it the tools and the poll interval."""
    payload = _payload(request)
    session = _session(payload)
    ledger, worker, _ = _worker(request, payload)
    worker = delivery_mod.record_mod_hello(ledger, worker, session)
    log.info("%s: the chargehand mod of session %s said hello", worker.handle, session[:8])
    return Response.json({
        "tools": [tool.spec() for tool in REGISTRY],
        "outbox_poll_secs": delivery_mod.OUTBOX_POLL_SECS,
    })


def call_tool(request: Request) -> Response:
    name = request.path[len(f"{PREFIX}/tool"):].strip("/")
    tool = TOOLS.get(name)
    if tool is None:
        raise HTTPError(404, "no such tool")
    payload = _payload(request)
    arguments = payload.get("input", {})
    if not isinstance(arguments, Mapping):
        raise HTTPError(400, "input is not a JSON object")
    ledger, worker, _ = _worker(request)
    # The arguments are session output: the log names the tool, never what it was given.
    log.info("%s: tool %s", worker.handle, tool.name)
    try:
        result = tool.handler(ToolCall(request, ledger, worker, arguments))
    except Exception:
        log.exception("%s: tool %s failed", worker.handle, tool.name)
        result = ToolResult("chargehand could not handle this call.", is_error=True)
    return Response.json({"result": result.text, "is_error": result.is_error})


def take(request: Request) -> Response:
    """The worker's wake message for the mod to submit, or none."""
    payload = _payload(request)
    session = _session(payload)
    ended = request.ledger().get(request.caller.id) if request.caller is not None else None
    if ended is not None and ended.is_terminal:
        # The worker is done, failed or cancelled, and its session may live on. The answer
        # tells the mod to stop polling, so this is said once per load of the mod, instead
        # of a refusal every few seconds.
        log.info("%s: its session %s polled its outbox after the worker ended (%s); the mod "
                 "stops polling", ended.handle, session[:8], ended.state)
        return Response.json({"message": None, "ended": True})
    ledger, worker, _ = _worker(request)
    if worker.state in NO_WAKE_STATES:
        return Response.json({"message": None})
    message = delivery_mod.take_outbox(ledger, worker, session)
    if message is None:
        return Response.json({"message": None})
    log.info("%s: session %s took its wake message", worker.handle, session[:8])
    return Response.json({"message": {"id": message.id, "text": message.text}})


def confirm(request: Request) -> Response:
    """The mod's word that its submit started the turn, or that the session refused it."""
    payload = _payload(request)
    message_id = payload.get("id")
    submitted = payload.get("submitted")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise HTTPError(400, "id must be the id of the message")
    if not isinstance(submitted, bool):
        raise HTTPError(400, "submitted must be true or false")
    ledger, worker, _ = _worker(request)
    outcome, written = delivery_mod.confirm_outbox(ledger, worker, message_id, submitted=submitted)
    if outcome == delivery_mod.CONFIRM_UNKNOWN:
        raise HTTPError(404, "no such message for this worker")
    if outcome == delivery_mod.CONFIRM_RECORDED and written is not None:
        # Into the daemon's log in the tick's own format, as a wake by resume shows there.
        report = TickReport()
        report.woken.append(written.handle)
        report.transitions.append(f"{written.handle}: wake from the outbox -> {written.state}")
        request.daemon.report(report)
        request.daemon.tick_soon()
    return Response.json({"outcome": outcome})


def lifecycle(request: Request) -> Response:
    event = request.path[len(f"{PREFIX}/lifecycle"):].strip("/")
    payload = _payload(request)
    ledger, worker, runtime = _worker(request, payload)
    hook = runtime.parse_channel(event, payload) if event in runtime.channel_events else None
    if hook is None:
        raise HTTPError(404, "not found")
    report = TickReport()
    try:
        request.daemon.runner(ledger).apply_hook(worker, hook, report)
    except Exception:
        # The diff of the next tick catches up.
        log.exception("channel %s for %s failed", event, worker.handle)
        return Response.json({})
    request.daemon.report(report)
    return Response.json({})


def record_usage(request: Request) -> Response:
    """Write the session's weekly window to the usage file, and keep its context window."""
    payload = _payload(request)
    ledger, worker, runtime = _worker(request, payload)
    written = False
    if runtime.capabilities.usage_readings:
        written = usage.record_session_usage(
            request.daemon.config.serve.usage_file, payload.get("rate_limits")
        )
    context = payload.get("context")
    if isinstance(context, Mapping):
        tokens = context.get("tokens")
        ledger.update(
            worker,
            context_tokens=tokens if isinstance(tokens, int) and not isinstance(tokens, bool)
            and tokens >= 0 else None,
            context_percent=usage.percent(context.get("percent")),
        )
    return Response.json({"recorded": written})


__all__ = [
    "PREFIX",
    "REGISTRY",
    "TOOLS",
    "Tool",
    "ToolCall",
    "ToolResult",
    "register_routes",
]
