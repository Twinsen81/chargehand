"""The board: one static page that the daemon serves, the model it renders, and its actions.

``GET /`` sends the page, and the page polls ``GET /api/board`` about once a second.
The model is built from the ledger, the role files and the usage file only: a request
makes no tracker call and lists no sessions, so a board left open costs nothing but a
few SQLite reads a second.

Every action is a ``POST`` with the daemon's token, and records the same control request
as the command line, with origin ``board``; the tick applies it, so the daemon stays the
one writer of side effects. An action waits a moment for its outcome and otherwise
answers with the request pending; ``GET /api/requests/<id>`` reads the outcome. Attach
is the exception: it opens a terminal window on this machine at once, because the tick
can be busy with a launch for minutes, and records its request already applied. The
board has no chat: a conversation with a session goes through attach.

Everything a session, a tracker or a hook wrote is untrusted. The model passes it
through :mod:`chargehand.sanitize`, keeps a link only when it is a plain http(s) URL, and
escapes ``<``, ``>`` and ``&`` in the JSON; the page renders it as text and never as
markup. The page loads nothing from another origin, and its content security policy
lets it load only its own script, its own style sheet and this API.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from chargehand import config as config_mod
from chargehand import ledger as ledger_mod
from chargehand import paths, sanitize
from chargehand import roles as roles_mod
from chargehand import terminal as terminal_mod
from chargehand import usage as usage_mod
from chargehand.config import MachineConfig
from chargehand.delivery import WAKE_CAP_WINDOW_SECS, wake_message
from chargehand.errors import ChargehandError
from chargehand.ledger import Ledger, Worker
from chargehand.roles import Role
from chargehand.serve import HTTPError, Request, Response, Routes
from chargehand.tick import NEW_WORKER, REPLY, REQUEST_MESSAGE_LIMIT, template_values

# The columns in page order: key, title, hint, and the worker states each one shows.
COLUMNS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("needs_you", "Needs you", "A question or a permission prompt waits",
     (ledger_mod.BLOCKED,)),
    ("working", "Working", "Launching, or running a turn",
     (ledger_mod.LAUNCHING, ledger_mod.RUNNING)),
    ("queued", "Queued", "Events wait for a cap or a usage reset",
     (ledger_mod.QUEUED, ledger_mod.PARKED)),
    ("idle", "Idle", "The last turn ended, or paused",
     (ledger_mod.IDLE, ledger_mod.PAUSED)),
    ("closed", "Closed", "Finished in the last 24 hours", ledger_mod.TERMINAL),
)
COLUMN_OF = {state: key for key, _, _, states in COLUMNS for state in states}

CLOSED_WINDOW_SECS = 24 * 3600
CLOSED_LIMIT = 30

PROMPT_LIMIT = 2000
ANSWER_LIMIT = 2000
LINE_LIMIT = 200
# A transcript holds about this many bytes per token. The estimate is approximate.
BYTES_PER_TOKEN = 4

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
PAGE_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)

# Path to the file under templates/board/ and its content type. Nothing else is served.
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/board.css": ("board.css", "text/css; charset=utf-8"),
    "/board.js": ("board.js", "text/javascript; charset=utf-8"),
}


# How long an action waits for the tick to apply its request before it answers with the
# request pending, and how often it looks meanwhile.
REQUEST_WAIT_SECS = 3.0
REQUEST_POLL_SECS = 0.1

# A worker action in the URL, the request kind the command line records for it, and
# whether it takes the command line's --force.
WORKER_ACTIONS: dict[str, tuple[str, bool]] = {
    "pause": ("stop", False),
    "cancel": ("cancel", False),
    "continue": ("continue", True),
    "retry": ("retry", True),
    "discard": ("discard", True),
    "reply": (REPLY, False),
}
ATTACH = "attach"
REPOSITORY_ACTIONS = ("pause", "resume")

# What a new worker request may carry: an item as its tracker names it, and the event
# kinds it follows on that item.
ITEM_LIMIT = 200
SUBSCRIBE_LIMIT = 10
# A reply is one line; a longer body is refused rather than cut without notice.
REPLY_BODY_LIMIT = 4 * ledger_mod.REPLY_LIMIT


def register_routes(routes: Routes) -> None:
    for path in ASSETS:
        routes.add("GET", path, asset)
    routes.add("GET", "/api/board", state)
    routes.add("GET", "/api/requests", request_outcome, prefix=True)
    routes.add("POST", "/api/workers", new_worker)
    routes.add("POST", "/api/workers", worker_action, prefix=True)
    routes.add("POST", "/api/repositories", repository_action, prefix=True)
    routes.add("POST", "/api/pause", pause_all)
    routes.add("POST", "/api/resume", resume_all)


def asset_dir() -> Path:
    return paths.templates_dir() / "board"


def asset(request: Request) -> Response:
    name, content_type = ASSETS[request.path]
    return Response(
        body=(asset_dir() / name).read_bytes(), content_type=content_type, headers=PAGE_HEADERS
    )


def state(request: Request) -> Response:
    daemon = request.daemon
    ledger = request.ledger()
    runners = []

    def attach(worker: Worker) -> str | None:
        # The runtime only formats a command here; it runs nothing.
        if not runners:
            runners.append(daemon.runner(ledger))
        try:
            return runners[0].runtime(worker.runtime).attach_command(worker.session_id)
        except ChargehandError:
            return None

    model = build(daemon.config, ledger, health=daemon.health(), attach=attach)
    return Response(body=to_json(model), headers=PAGE_HEADERS)


# ----- actions -------------------------------------------------------------------


def _body(request: Request) -> dict[str, object]:
    if not request.body.strip():
        return {}
    payload = request.json()
    if not isinstance(payload, dict):
        raise HTTPError(400, "the body must be a JSON object")
    return payload


def _flag(body: dict[str, object], name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise HTTPError(400, f"{name} must be true or false")
    return value


def _segments(request: Request, prefix: str) -> list[str]:
    return [part for part in request.path[len(prefix):].split("/") if part]


def outcome(row: dict[str, object]) -> dict[str, object]:
    """A request as an action answers it. Its message is chargehand's own, in one line."""
    message = row.get("message")
    applied = row.get("applied_at") is not None
    target = row.get("target")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "target": sanitize.one_line(str(target), limit=LINE_LIMIT) if target else None,
        "origin": row.get("origin"),
        "created_at": row.get("created_at"),
        "applied_at": row.get("applied_at"),
        "pending": not applied,
        "ok": bool(row.get("ok")) if applied else None,
        "message": (
            sanitize.one_line(str(message), limit=REQUEST_MESSAGE_LIMIT) if message else None
        ),
    }


def _answer(ledger: Ledger, request_id: int) -> Response:
    """The request's outcome once the tick applied it, or the request pending after a wait."""
    deadline = time.monotonic() + REQUEST_WAIT_SECS
    while True:
        row = ledger.get_request(request_id)
        assert row is not None
        if row["applied_at"] is not None or time.monotonic() >= deadline:
            return Response.json(outcome(row), status=200 if row["applied_at"] is not None else 202)
        time.sleep(REQUEST_POLL_SECS)


def _record(request: Request, kind: str, target: str | None, **args: object) -> Response:
    ledger = request.ledger()
    request_id = ledger.record_request(kind, target, origin=ledger_mod.REQUEST_BOARD, **args)
    return _answer(ledger, request_id)


def request_outcome(request: Request) -> Response:
    """``GET /api/requests/<id>``: what became of a request. Needs no token, as every read."""
    parts = _segments(request, "/api/requests")
    if len(parts) != 1 or not parts[0].isdigit():
        raise HTTPError(404, "not found")
    row = request.ledger().get_request(int(parts[0]))
    if row is None:
        raise HTTPError(404, f"no request {parts[0]}")
    return Response.json(outcome(row))


def target_of(ledger: Ledger, worker: Worker) -> str:
    """What the request names the worker by: its handle, as on the command line.

    A `role/item` handle names the live worker, or the newest one, of that role and
    item, when the tick applies the request. A finished worker is named by the handle
    with its id, because a start rule may start a new live worker on its item before
    then; so is any worker that the plain handle does not name now.
    """
    by_id = f"{worker.role}/{worker.item or ''}#{worker.id}"
    if worker.is_terminal:
        return by_id
    found, _ = ledger.find_worker(worker.handle)
    if found is not None and found.id == worker.id:
        return worker.handle
    return by_id


def worker_action(request: Request) -> Response:
    """``POST /api/workers/<id>/<action>``: the request of a CLI command, or attach."""
    parts = _segments(request, "/api/workers")
    if len(parts) != 2 or not parts[0].isdigit():
        raise HTTPError(404, "not found")
    action = parts[1]
    if action != ATTACH and action not in WORKER_ACTIONS:
        raise HTTPError(404, f"no worker action {sanitize.one_line(action, limit=40)}")
    body = _body(request)
    ledger = request.ledger()
    worker = ledger.get(int(parts[0]))
    if worker is None:
        raise HTTPError(404, f"no worker {parts[0]}")
    target = target_of(ledger, worker)
    if action == ATTACH:
        return _attach(request, ledger, worker, target, body)
    kind, takes_force = WORKER_ACTIONS[action]
    if kind == REPLY:
        return _record(request, kind, target, text=_reply_text(body))
    if takes_force:
        return _record(request, kind, target, force=_flag(body, "force"))
    return _record(request, kind, target)


def _reply_text(body: dict[str, object]) -> str:
    """The reply as one cleaned line. Operator text, but a page could send anything."""
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPError(400, "a reply needs text")
    if len(text) > REPLY_BODY_LIMIT:
        raise HTTPError(400, f"a reply is one line of at most {ledger_mod.REPLY_LIMIT} characters")
    line = sanitize.one_line(text, limit=ledger_mod.REPLY_LIMIT)
    if not line:
        raise HTTPError(400, "the reply is empty")
    return line


def _attach(
    request: Request, ledger: Ledger, worker: Worker, target: str, body: dict[str, object]
) -> Response:
    """Open a terminal window that attaches to the worker's session, here and now.

    The request row is the audit record; it is complete when written, so no tick acts on it.
    """
    name = body.get("terminal", terminal_mod.DEFAULT_TERMINAL)
    if name not in terminal_mod.TERMINALS:
        raise HTTPError(400, f"terminal must be one of {', '.join(terminal_mod.TERMINALS)}")
    if worker.is_terminal or not worker.session_id:
        raise HTTPError(409, f"{worker.handle} has no live session to attach to")
    if not sanitize.is_safe_identifier(worker.session_id):
        raise HTTPError(409, f"{worker.handle}: the session id is not a plain identifier")
    try:
        command = request.daemon.runner(ledger).runtime(worker.runtime).attach_command(
            worker.session_id
        )
    except ChargehandError as exc:
        raise HTTPError(409, f"{worker.handle}: {sanitize.one_line(str(exc), limit=200)}") from None
    ok, message = terminal_mod.open_window(str(name), command)
    request_id = ledger.record_applied_request(
        ATTACH, target, origin=ledger_mod.REQUEST_BOARD, ok=ok, message=message, terminal=name
    )
    row = ledger.get_request(request_id)
    assert row is not None
    return Response.json(outcome(row))


def new_worker(request: Request) -> Response:
    """``POST /api/workers``: a worker of a role, on an optional item, started by hand."""
    body = _body(request)
    repository = request.daemon.config.repository_by_name(str(body.get("repository") or ""))
    if repository is None:
        raise HTTPError(400, "repository must name a configured repository")
    role = body.get("role")
    if not isinstance(role, str) or role not in repository.roles:
        raise HTTPError(400, f"role must be one of the roles of {repository.name}: "
                             f"{', '.join(repository.roles)}")
    args: dict[str, object] = {"role": role}
    item = body.get("item")
    if item is not None and not isinstance(item, str):
        raise HTTPError(400, "item must be text")
    item = (item or "").strip()
    if item:
        if len(item) > ITEM_LIMIT or item != sanitize.one_line(item, limit=ITEM_LIMIT) or " " in item:
            raise HTTPError(400, "item must be one word, as the tracker names the item")
        args["item"] = item
    kinds = body.get("subscribe", [])
    if not isinstance(kinds, list) or len(kinds) > SUBSCRIBE_LIMIT or not all(
        isinstance(kind, str) and sanitize.is_event_kind(kind) for kind in kinds
    ):
        raise HTTPError(400, "subscribe must be a list of event kinds, such as [\"comment\"]")
    if kinds and not item:
        raise HTTPError(400, "subscribe needs an item to follow")
    if kinds:
        args["subscribe"] = list(dict.fromkeys(kinds))
    return _record(request, NEW_WORKER, repository.name, **args)


def repository_action(request: Request) -> Response:
    """``POST /api/repositories/<name>/<pause|resume>``."""
    parts = _segments(request, "/api/repositories")
    if len(parts) != 2 or parts[1] not in REPOSITORY_ACTIONS:
        raise HTTPError(404, "not found")
    repository = request.daemon.config.repository_by_name(parts[0])
    if repository is None:
        raise HTTPError(404, f"no repository {sanitize.one_line(parts[0], limit=64)}")
    return _record(request, parts[1], repository.name)


def pause_all(request: Request) -> Response:
    return _record(request, "pause", None)


def resume_all(request: Request) -> Response:
    return _record(request, "resume", None)


def to_json(model: object) -> bytes:
    """JSON in which no character can open or close markup, should it ever reach some."""
    text = json.dumps(model, sort_keys=True, default=str)
    return (
        text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    ).encode("utf-8")


# ----- the model ---------------------------------------------------------------


def build(
    config: MachineConfig,
    ledger: Ledger,
    *,
    health: dict[str, object],
    attach: Callable[[Worker], str | None] = lambda worker: None,
    now: float | None = None,
) -> dict[str, object]:
    """The page model: the top bar and the cards of every column."""
    current = time.time() if now is None else now
    live = ledger.live_workers()
    closed = sorted(
        ledger.terminal_workers_since(CLOSED_WINDOW_SECS),
        key=lambda worker: worker.finished_at or worker.updated_at,
        reverse=True,
    )[:CLOSED_LIMIT]
    roles = _Roles(config)
    prompts = ledger.open_permission_prompts()
    cards = [
        card(worker, ledger, roles, attach=attach, prompts=prompts, now=current)
        for worker in (*live, *closed)
    ]
    columns = []
    for key, title, hint, _ in COLUMNS:
        chosen = [entry for entry in cards if entry["column"] == key]
        if key != "closed":
            chosen.sort(key=lambda entry: entry["updated_at"], reverse=True)
        columns.append({"key": key, "title": title, "hint": hint, "cards": chosen})
    return {
        "generated_at": current,
        "usage": usage_mod.read_usage(config.serve.usage_file, current),
        "throttle": {
            "working": sum(1 for w in live if w.state in (ledger_mod.LAUNCHING, ledger_mod.RUNNING)),
            "max_concurrent": config.max_concurrent,
            "open": len(live),
            "max_open_workers": config.max_open_workers,
            "paused": ledger.is_paused(),
            "paused_repositories": ledger.paused_repositories(),
        },
        "health": _health(health),
        "columns": columns,
        "repositories": [
            {
                "name": repository.name,
                "roles": list(repository.roles),
                "paused": ledger.is_paused(repository.name),
            }
            for repository in config.repositories
        ],
        "terminals": list(terminal_mod.TERMINALS),
        "token_file": str(config.serve.token_file),
    }


def _health(health: dict[str, object]) -> dict[str, object]:
    started = health.get("last_tick_started_at")
    finished = health.get("last_tick_finished_at")
    duration = (
        round(finished - started, 2)
        if isinstance(started, (int, float)) and isinstance(finished, (int, float))
        and finished >= started
        else None
    )
    error = health.get("last_tick_error")
    return {
        "version": health.get("version"),
        "started_at": health.get("started_at"),
        "tick_interval_secs": health.get("tick_interval_secs"),
        "ticks": health.get("ticks"),
        "failed_ticks": health.get("failed_ticks"),
        "ticking": health.get("ticking"),
        "last_tick_at": finished,
        "last_tick_secs": duration,
        "last_tick_error": sanitize.one_line(error, limit=300) if isinstance(error, str) else None,
    }


class _Roles:
    """The role of each worker, read once per request. None when it does not load."""

    def __init__(self, config: MachineConfig) -> None:
        self._config = config
        self._roles: dict[tuple[str, str], Role | None] = {}

    def of(self, worker: Worker) -> Role | None:
        key = (worker.repository, worker.role)
        if key not in self._roles:
            self._roles[key] = self._load(*key)
        return self._roles[key]

    def _load(self, repository_name: str, role: str) -> Role | None:
        repository = self._config.repository_by_name(repository_name)
        if repository is None:
            return None
        try:
            try:
                repo_config = config_mod.load_repo_config(repository.path)
            except ChargehandError:
                repo_config = None
            return roles_mod.load_role(
                repository.path, role, repo_config=repo_config,
                tracker_type=repository.tracker.type, check_agent=False,
            )
        except ChargehandError:
            return None


def card(
    worker: Worker,
    ledger: Ledger,
    roles: _Roles,
    *,
    attach: Callable[[Worker], str | None],
    prompts: Iterable[ledger_mod.PermissionPrompt] = (),
    now: float,
) -> dict[str, object]:
    role = roles.of(worker)
    live = not worker.is_terminal
    prompt = next((p for p in prompts if p.worker_id == worker.id), None) if live else None
    cap = role.policy.max_wakes_per_day if role else None
    return {
        "id": worker.id,
        "handle": worker.handle,
        "origin": worker.origin,
        "actions": actions(worker),
        "column": COLUMN_OF[worker.state],
        "state": worker.state,
        "session_state": worker.session_state,
        "role": worker.role,
        "repository": worker.repository,
        "item": sanitize.scrub_identifier(worker.item) if worker.item else f"#{worker.id}",
        "item_url": sanitize.safe_url(worker.url),
        "pr_url": sanitize.safe_url(worker.pr_url),
        "branch": sanitize.one_line(worker.branch, limit=LINE_LIMIT),
        "worktree": sanitize.one_line(worker.worktree, limit=LINE_LIMIT),
        "model": role.model if role else None,
        "effort": role.effort if role else None,
        "last_prompt": last_prompt(worker, ledger, role),
        "last_answer": _text(worker.last_answer, ANSWER_LIMIT),
        "waiting_for": (
            sanitize.one_line(worker.waiting_for, limit=LINE_LIMIT)
            if live and worker.waiting_for else None
        ),
        "permission_prompt": (
            {
                "tool_name": sanitize.one_line(prompt.tool_name, limit=100),
                "tool_input": sanitize.one_line(prompt.tool_input, limit=LINE_LIMIT),
            }
            if prompt else None
        ),
        "problem": (
            sanitize.one_line(worker.last_error, limit=300)
            if worker.last_error and worker.state in (ledger_mod.PAUSED, ledger_mod.FAILED)
            else None
        ),
        "wakes_today": ledger.wakes_since(worker, now - WAKE_CAP_WINDOW_SECS),
        "wake_cap": cap,
        "tokens_estimate": tokens_estimate(worker.transcript_path),
        "attach": attach(worker) if live and worker.session_id else None,
        "reset_at": worker.reset_at,
        "last_hook": worker.last_hook,
        "created_at": worker.created_at,
        "updated_at": worker.updated_at,
        "finished_at": worker.finished_at,
    }


def actions(worker: Worker) -> list[str]:
    """The buttons a card offers: the actions that the worker's state can take now."""
    if worker.is_terminal:
        return ["retry"] + ([] if worker.gc_done else ["discard"])
    offered = []
    if worker.state in (ledger_mod.PAUSED, ledger_mod.BLOCKED, ledger_mod.PARKED):
        offered.append("continue")
    if worker.session_id and worker.state != ledger_mod.PAUSED:
        offered.append("pause")
    offered += ["cancel", "reply"]
    if worker.session_id:
        offered.append(ATTACH)
    offered.append("discard")
    return offered


def _text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    cleaned = sanitize.clean(value).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or None


def last_prompt(worker: Worker, ledger: Ledger, role: Role | None) -> str | None:
    """The wake message of the last wake, or the role's first prompt before any wake.

    The wake message is rebuilt from the inbox rows that wake delivered, with the code
    that built it, so the card shows what the session read.
    """
    if worker.last_wake_at is not None:
        message, _ = wake_message(ledger.delivered_at(worker, worker.last_wake_at))
        return _text(message, PROMPT_LIMIT)
    if role is None:
        return None
    try:
        # A worker without an item has None for {item}; the card shows it empty.
        values = {name: value or "" for name, value in template_values(worker).items()}
        rendered = config_mod.render(role.prompt, values, where=f"role {role.name}")
    except ChargehandError:
        return None
    return _text(rendered, PROMPT_LIMIT)


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


__all__ = [
    "ASSETS",
    "WORKER_ACTIONS",
    "actions",
    "outcome",
    "target_of",
    "COLUMNS",
    "COLUMN_OF",
    "CSP",
    "build",
    "card",
    "last_prompt",
    "register_routes",
    "to_json",
    "tokens_estimate",
]
