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
the session start, each turn end and the session end, through the same transitions as the
session hooks. ``/channel/usage`` carries the session's rate-limit windows and its context
window after each turn.

Every route is a POST, so it needs the daemon's token. A post names its session by id and
working directory and maps to a worker as a hook does; a post that maps to no live worker
changes nothing. Everything a post carries is untrusted session output: it is cleaned and
capped, and no tool argument reaches a log line, a notification or a prompt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from chargehand import delivery as delivery_mod
from chargehand import hooks
from chargehand import ledger as ledger_mod
from chargehand import runtime as runtime_mod
from chargehand import sanitize, usage
from chargehand.ledger import Ledger, Worker
from chargehand.runtime import Hook, Runtime
from chargehand.serve import HTTPError, Request, Response, Routes
from chargehand.tick import TickReport

log = logging.getLogger(__name__)

PREFIX = "/channel"

# A worker in one of these states gets no wake from its outbox: the operator paused it, or
# it ended.
NO_WAKE_STATES = (ledger_mod.PAUSED, *ledger_mod.TERMINAL)

# The most event kinds one subscribe names.
MAX_KINDS = 20

# What a tool answers whose effect a later release brings. chargehand's own text.
NOT_AVAILABLE = (
    "chargehand records nothing for this tool yet. Go on with your work; end your turn as "
    "you would without it."
)


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


def _not_available(call: ToolCall) -> ToolResult:
    return ToolResult(NOT_AVAILABLE)


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
        "Report your progress to the operator. state is one of working, done, failed or "
        "needs-input; summary is one short sentence; links is an optional list of URLs, such "
        "as your pull request.",
        _schema({
            "state": {"type": "string", "enum": ["working", "done", "failed", "needs-input"]},
            "summary": _TEXT,
            "links": _TEXTS,
        }, ("state", "summary")),
        _not_available,
    ),
    Tool(
        "ask",
        "Ask the operator a question, with optional answer options. It returns at once. End "
        "your turn after it; the answer arrives later as a new message.",
        _schema({"question": _TEXT, "options": _TEXTS}, ("question",)),
        _not_available,
    ),
    Tool(
        "handoff",
        "Write your handoff note: what you did, what is left, and where things are, so that a "
        "later session can go on from it.",
        _schema({"note": _TEXT}, ("note",)),
        _not_available,
    ),
    Tool(
        "request_wake",
        "Ask to be woken later, after after_secs seconds, with a short reason.",
        _schema({"after_secs": {"type": "integer", "minimum": 1}, "reason": _TEXT},
                ("after_secs",)),
        _not_available,
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
    routes.add("POST", f"{PREFIX}/hello", hello)
    routes.add("POST", f"{PREFIX}/tool", call_tool, prefix=True)
    routes.add("POST", f"{PREFIX}/outbox", take)
    routes.add("POST", f"{PREFIX}/outbox/confirm", confirm)
    routes.add("POST", f"{PREFIX}/lifecycle", lifecycle, prefix=True)
    routes.add("POST", f"{PREFIX}/usage", record_usage)


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


def _worker(request: Request, payload: Mapping[str, Any]) -> tuple[Ledger, Worker, type[Runtime]]:
    """The live worker of the posting session, as a hook finds it; 404 when there is none."""
    ledger = request.ledger()
    probe = Hook(
        kind=runtime_mod.HOOK_NOTIFICATION, event="channel",
        session_uuid=_text(payload, "session_id", 100), cwd=_text(payload, "cwd", 1000),
    )
    for runtime in _runtimes():
        worker = hooks.find_worker(ledger, probe, runtime.name)
        if worker is not None:
            return ledger, worker, runtime
    log.info("channel %s from session %s: no live worker has this session",
             sanitize.one_line(request.path, limit=80), hooks.session_prefix(probe))
    raise HTTPError(404, "no live worker has this session")


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
    ledger, worker, _ = _worker(request, payload)
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
    ledger, worker, _ = _worker(request, payload)
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
    ledger, worker, _ = _worker(request, payload)
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
    runtimes = [runtime for runtime in _runtimes() if event in runtime.channel_events]
    for runtime in runtimes:
        hook = runtime.parse_channel(event, payload)
        if hook is not None:
            break
    else:
        raise HTTPError(404, "not found")
    ledger, worker, _ = _worker(request, payload)
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
    "NOT_AVAILABLE",
    "PREFIX",
    "REGISTRY",
    "TOOLS",
    "Tool",
    "ToolCall",
    "ToolResult",
    "register_routes",
]
