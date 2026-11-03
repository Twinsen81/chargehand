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

A permission prompt is answered on its card, also at once: the session's hook waits in
the daemon for the answer (see :mod:`chargehand.hooks`), so a request for the tick would
come too late. The answer writes only the prompt's ledger row; the waiting hook is the
one that tells the session.

The operator can also rename, give an icon to and pin repositories and workers, and fold
or hide repositories. That is the board's own state in the ledger: a ``POST`` with the
token writes it at once, as it has no side effect outside the ledger, and nothing but
the model reads it. A hidden repository's workers run, are supervised and notify as
before; only their cards leave the page until the operator asks to see them.

An archive of a worker, or of every worker of a repository, is a request like the others;
the zip is written by the daemon's job thread, and the model shows each job on its card and
in the top bar with its state and progress, read from the ledger as the rest.

Everything a session, a tracker or a hook wrote is untrusted. The model passes it
through :mod:`chargehand.sanitize`, keeps a link only when it is a plain http(s) URL, and
escapes ``<``, ``>`` and ``&`` in the JSON; the page renders it as text and never as
markup. The page loads nothing from another origin, and its content security policy
lets it load only its own script, its own style sheet and this API.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from chargehand import config as config_mod
from chargehand import hooks as hooks_mod
from chargehand import ledger as ledger_mod
from chargehand import paths, sanitize
from chargehand import roles as roles_mod
from chargehand import terminal as terminal_mod
from chargehand import usage as usage_mod
from chargehand.config import MachineConfig
from chargehand.delivery import QUEUE_REASON_PREFIX, WAKE_CAP_WINDOW_SECS, wake_message
from chargehand.errors import ChargehandError
from chargehand.ledger import Ledger, Worker
from chargehand.roles import Role
from chargehand.serve import HTTPError, Request, Response, Routes
from chargehand.tick import (
    ARCHIVE,
    ARCHIVE_REPOSITORY,
    MODEL_LIMIT_KEY,
    NEW_WORKER,
    REPLY,
    REQUEST_MESSAGE_LIMIT,
    UNARCHIVE_REPOSITORY,
    template_values,
)

log = logging.getLogger(__name__)

# The columns in page order: key, title, hint, and the worker states each one shows.
COLUMNS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("needs_you", "Needs you", "A question or a permission prompt waits",
     (ledger_mod.BLOCKED,)),
    ("working", "Working", "Launching, or running a turn",
     (ledger_mod.LAUNCHING, ledger_mod.RUNNING)),
    ("queued", "Queued", "Events wait for a cap or the usage reserve",
     (ledger_mod.QUEUED,)),
    ("parked", "Parked", "A usage limit ended the turn; resumes after the reset",
     (ledger_mod.PARKED,)),
    ("idle", "Idle", "The last turn ended, or paused",
     (ledger_mod.IDLE, ledger_mod.PAUSED)),
    ("closed", "Closed", "Finished in the last 24 hours", ledger_mod.TERMINAL),
)
COLUMN_OF = {state: key for key, _, _, states in COLUMNS for state in states}

CLOSED_WINDOW_SECS = 24 * 3600
# How long the model-limit badge shows when the failure gave no reset time.
MODEL_LIMIT_SHOWN_SECS = 5 * 3600
CLOSED_LIMIT = 30

PROMPT_LIMIT = 2000
# How much of a permission prompt's tool input a card shows.
PROMPT_INPUT_LIMIT = 1000
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
    "archive": (ARCHIVE, True),
}
ATTACH = "attach"
# The answer to an open prompt: `{"prompt": <id>, "answer": "allow" | "deny" | "always"}`.
ANSWER = "answer"
# A repository action in the URL, the request kind, and whether it takes force.
REPOSITORY_ACTIONS: dict[str, tuple[str, bool]] = {
    "pause": ("pause", False),
    "resume": ("resume", False),
    "archive": (ARCHIVE_REPOSITORY, True),
    "unarchive": (UNARCHIVE_REPOSITORY, False),
}
# How long the top bar shows a job that ended, and how many jobs it shows at most.
JOB_SHOWN_SECS = 3600
JOB_LIMIT = 20
# The top bar shows these jobs in full, and counts the others.
JOB_SHOWN_IN_FULL = (ledger_mod.JOB_RUNNING, ledger_mod.JOB_FAILED)
JOB_COUNTED = (ledger_mod.JOB_WAITING, ledger_mod.JOB_QUEUED, ledger_mod.JOB_WRITTEN,
               ledger_mod.JOB_DONE)

# What a new worker request may carry: an item as its tracker names it, and the event
# kinds it follows on that item.
ITEM_LIMIT = 200
SUBSCRIBE_LIMIT = 10
# A reply is one line; a longer body is refused rather than cut without notice.
REPLY_BODY_LIMIT = 4 * ledger_mod.REPLY_LIMIT

# The action that writes the board's own state of a repository or a worker.
PERSONALISE = "board"
# A display name is one line of at most this many characters; a longer one is refused.
NAME_LIMIT = 60
# An icon is one emoji, which can take several code points (a flag, a family, a skin tone),
# or a name from this set. Nothing is uploaded and nothing is fetched.
ICON_CODE_POINTS = 10
ICONS: dict[str, str] = {
    "bell": "\U0001f514",
    "book": "\U0001f4d8",
    "bug": "\U0001f41b",
    "chart": "\U0001f4c8",
    "desktop": "\U0001f5a5\ufe0f",
    "eyes": "\U0001f440",
    "fire": "\U0001f525",
    "flag": "\U0001f6a9",
    "gear": "\u2699\ufe0f",
    "globe": "\U0001f310",
    "lock": "\U0001f512",
    "note": "\U0001f4dd",
    "package": "\U0001f4e6",
    "phone": "\U0001f4f1",
    "rocket": "\U0001f680",
    "star": "\u2b50",
    "test": "\U0001f9ea",
    "tools": "\U0001f6e0\ufe0f",
}
# One emoji: a keycap, a flag of two regional indicators, a subdivision flag of tag
# characters, or symbols joined by the zero-width joiner, each with an optional variation
# selector and skin tone.
_ZWJ = "\u200d"
_KEYCAP = re.compile(r"[#*0-9]\ufe0f?\u20e3")
_REGIONAL = re.compile(r"[\U0001f1e6-\U0001f1ff]{2}")
_TAG_FLAG = re.compile(r"\U0001f3f4[\U000e0020-\U000e007e]+\U000e007f")
_REGIONAL_ONE = re.compile(r"[\U0001f1e6-\U0001f1ff]")
_MODIFIERS = re.compile(r"[\ufe0e\ufe0f]?[\U0001f3fb-\U0001f3ff]?[\ufe0e\ufe0f]?")
_FLAGS = ("pinned", "collapsed", "hidden")


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
    """``GET /api/board``; ``?show_hidden=1`` keeps the cards of the hidden repositories."""
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

    show_hidden = request.query.get("show_hidden", [""])[-1] == "1"
    model = build(
        daemon.config, ledger, health=daemon.health(), attach=attach, show_hidden=show_hidden,
        held=lambda prompt_id: hooks_mod.is_held(daemon, prompt_id),
    )
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


def target_of(worker: Worker) -> str:
    """What a board request names the worker by: the handle the card shows.

    It is :attr:`Worker.target`, which the resolution rule in :meth:`Ledger.find_worker`
    resolves back to this worker: `role/item` for a live start-rule worker, the id
    handle for every other one.
    """
    return worker.target


def worker_action(request: Request) -> Response:
    """``POST /api/workers/<id>/<action>``: the request of a CLI command, or attach."""
    parts = _segments(request, "/api/workers")
    if len(parts) != 2 or not parts[0].isdigit():
        raise HTTPError(404, "not found")
    action = parts[1]
    if action not in (ATTACH, PERSONALISE, ANSWER) and action not in WORKER_ACTIONS:
        raise HTTPError(404, f"no worker action {sanitize.one_line(action, limit=40)}")
    body = _body(request)
    ledger = request.ledger()
    worker = ledger.get(int(parts[0]))
    if worker is None:
        raise HTTPError(404, f"no worker {parts[0]}")
    if action == PERSONALISE:
        return personalise(request, ledger_mod.BOARD_WORKER, str(worker.id), body, name=worker.target)
    if action == ANSWER:
        return Response.json(
            hooks_mod.answer_prompt(request, ledger, worker, body.get("prompt"), body.get("answer"))
        )
    target = target_of(worker)
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
    too_long = f"a reply is one line of at most {ledger_mod.REPLY_LIMIT} characters"
    if len(text) > REPLY_BODY_LIMIT:
        raise HTTPError(400, too_long)
    line = sanitize.one_line(text, limit=REPLY_BODY_LIMIT)
    if len(line) > ledger_mod.REPLY_LIMIT:
        raise HTTPError(400, too_long)
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
    """``POST /api/repositories/<name>/<pause|resume|archive|unarchive|board>``."""
    parts = _segments(request, "/api/repositories")
    if len(parts) != 2 or (parts[1] not in REPOSITORY_ACTIONS and parts[1] != PERSONALISE):
        raise HTTPError(404, "not found")
    repository = request.daemon.config.repository_by_name(parts[0])
    if repository is None:
        raise HTTPError(404, f"no repository {sanitize.one_line(parts[0], limit=64)}")
    if parts[1] == PERSONALISE:
        return personalise(
            request, ledger_mod.BOARD_REPOSITORY, repository.name, _body(request),
            name=f"repository {repository.name}",
        )
    kind, takes_force = REPOSITORY_ACTIONS[parts[1]]
    if takes_force:
        return _record(request, kind, repository.name, force=_flag(_body(request), "force"))
    return _record(request, kind, repository.name)


# ----- the board's own state -------------------------------------------------------


def is_emoji(text: str) -> bool:
    """Whether *text* is one short emoji sequence and nothing else, so never markup."""
    if not text or len(text) > ICON_CODE_POINTS:
        return False
    if any(pattern.fullmatch(text) for pattern in (_KEYCAP, _REGIONAL, _TAG_FLAG)):
        return True
    return all(_emoji_part(part) for part in text.split(_ZWJ))


def _emoji_part(part: str) -> bool:
    """One symbol, which is not a regional indicator, and its optional modifiers."""
    if not part or unicodedata.category(part[0]) != "So" or _REGIONAL_ONE.fullmatch(part[0]):
        return False
    return _MODIFIERS.fullmatch(part[1:]) is not None


def icon_glyph(value: str | None) -> str | None:
    """What the page shows for a stored icon: the glyph of a bundled name, or the emoji."""
    if not value:
        return None
    if value in ICONS:
        return ICONS[value]
    return value if is_emoji(value) else None


def _name_setting(value: object) -> str | None:
    """A display name as one cleaned line, or None to drop it. Operator text, but capped."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPError(400, "name must be text, or null to drop it")
    too_long = f"a name is one line of at most {NAME_LIMIT} characters"
    if len(value) > 4 * NAME_LIMIT:
        raise HTTPError(400, too_long)
    line = sanitize.one_line(value, limit=4 * NAME_LIMIT)
    if len(line) > NAME_LIMIT:
        raise HTTPError(400, too_long)
    return line or None


def _icon_setting(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPError(400, "icon must be text, or null to drop it")
    icon = unicodedata.normalize("NFC", value.strip())
    if not icon:
        return None
    if icon in ICONS or is_emoji(icon):
        return icon
    raise HTTPError(400, f"icon must be one emoji or one of: {', '.join(sorted(ICONS))}")


def _setting(field: str, value: object) -> str | None:
    """The stored text of *field*, or None when the field goes back to its default."""
    if field == "name":
        return _name_setting(value)
    if field == "icon":
        return _icon_setting(value)
    if not isinstance(value, bool):
        raise HTTPError(400, f"{field} must be true or false")
    return "1" if value else None


def personalise(
    request: Request, kind: str, key: str, body: Mapping[str, object], *, name: str
) -> Response:
    """Set or drop each board setting in *body* for a repository or a worker.

    *name* starts the log line: the worker's handle, or the repository.

    It changes how the board shows the thing and nothing else, so it needs no request row
    and no tick: the configured name, the handle and the session name stay as they are.
    """
    allowed = ledger_mod.BOARD_FIELDS[kind]
    if not body:
        raise HTTPError(400, f"send one or more of: {', '.join(allowed)}")
    changes: dict[str, str | None] = {}
    for field, value in body.items():
        if field not in allowed:
            raise HTTPError(400, f"a {kind} has no board setting "
                                 f"{sanitize.one_line(str(field), limit=40)}; "
                                 f"it takes {', '.join(allowed)}")
        changes[field] = _setting(field, value)
    ledger = request.ledger()
    with ledger.transaction():
        for field, value in changes.items():
            if value is None:
                ledger.delete_board_state(kind, key, field)
            else:
                ledger.set_board_state(kind, key, field, value)
    log.info("%s: board settings set: %s", name, ", ".join(changes))
    settings = BoardState(ledger.board_state()).of(kind, key)
    return Response.json({"kind": kind, "key": key, "settings": settings})


class BoardState:
    """The board state rows, by kind and key."""

    def __init__(self, rows: Iterable[Mapping[str, object]]) -> None:
        self._values: dict[tuple[str, str], dict[str, str]] = {}
        for row in rows:
            entry = self._values.setdefault((str(row["kind"]), str(row["key"])), {})
            entry[str(row["field"])] = str(row["value"])

    def of(self, kind: str, key: str) -> dict[str, str]:
        return dict(self._values.get((kind, key), {}))

    def shown(self, kind: str, key: str) -> dict[str, object]:
        """How the page shows *key*: its name, its icon and its flags.

        The values are cleaned again here, because an import can write them too.
        """
        values = self._values.get((kind, key), {})
        name = sanitize.one_line(values.get("name", ""), limit=NAME_LIMIT) or None
        icon = values.get("icon")
        glyph = icon_glyph(icon)
        shown: dict[str, object] = {
            "display_name": name, "icon": icon if glyph else None, "icon_glyph": glyph,
        }
        for flag in _FLAGS:
            if flag in ledger_mod.BOARD_FIELDS[kind]:
                shown[flag] = values.get(flag) == "1"
        return shown


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
    show_hidden: bool = False,
    held: Callable[[int], bool] = lambda prompt_id: False,
) -> dict[str, object]:
    """The page model: the top bar, and the cards of every column in repository groups.

    *held* tells whether a hook still waits for the board's answer to a prompt.

    In a group the pinned cards come first, then the others by their last activity; the
    groups are ordered the same way by the repository's pin. A collapsed repository shows
    only its pinned cards. A hidden repository's cards are left out unless *show_hidden*.
    """
    current = time.time() if now is None else now
    live = ledger.live_workers()
    closed = sorted(
        ledger.terminal_workers_since(CLOSED_WINDOW_SECS),
        key=lambda worker: worker.finished_at or worker.updated_at,
        reverse=True,
    )[:CLOSED_LIMIT]
    roles = _Roles(config)
    prompts = ledger.open_permission_prompts()
    jobs = ledger.recent_jobs(current - max(JOB_SHOWN_SECS, CLOSED_WINDOW_SECS))
    # The newest archive of each worker, for its card.
    archives = {
        job.target: job for job in jobs if job.kind == ledger_mod.JOB_ARCHIVE
    }
    personal = BoardState(ledger.board_state())
    repositories = {
        name: personal.shown(ledger_mod.BOARD_REPOSITORY, name)
        for name in dict.fromkeys(
            [repository.name for repository in config.repositories]
            + [worker.repository for worker in (*live, *closed)]
        )
    }
    cards = []
    for worker in (*live, *closed):
        entry = card(
            worker, ledger, roles, attach=attach, prompts=prompts, now=current, held=held,
            job=archives.get(str(worker.id)),
        )
        entry.update(personal.shown(ledger_mod.BOARD_WORKER, str(worker.id)))
        repository = repositories[worker.repository]
        entry["repository_label"] = repository["display_name"] or worker.repository
        entry["hidden"] = repository["hidden"]
        cards.append(entry)
    columns = [
        _column(key, title, hint, [entry for entry in cards if entry["column"] == key],
                repositories, show_hidden=show_hidden)
        for key, title, hint, _ in COLUMNS
    ]
    hidden = [name for name, shown in repositories.items() if shown["hidden"]]
    workers = {worker.id: worker for worker in (*live, *closed)}
    shown_jobs = [
        job_entry(job, workers.get(int(job.target)) if job.target.isdigit() else None, ledger)
        for job in jobs if job.is_active or (job.finished_at or 0) >= current - JOB_SHOWN_SECS
    ][-JOB_LIMIT:]
    return {
        "generated_at": current,
        "usage": usage_mod.read_usage(config.serve.usage_file, current),
        "model_limit": model_limit(ledger, current),
        "throttle": {
            "working": sum(1 for w in live if w.state in (ledger_mod.LAUNCHING, ledger_mod.RUNNING)),
            "max_concurrent": config.max_concurrent,
            "open": len(live),
            "max_open_workers": config.max_open_workers,
            "paused": ledger.is_paused(),
            "paused_repositories": ledger.paused_repositories(),
        },
        "health": _health(health),
        "jobs": shown_jobs,
        "job_summary": job_summary(shown_jobs),
        "columns": columns,
        "repositories": [
            {
                "name": repository.name,
                "roles": list(repository.roles),
                "paused": ledger.is_paused(repository.name),
                **repositories[repository.name],
            }
            for repository in config.repositories
        ],
        "hidden": {
            "repositories": hidden,
            "count": len(hidden),
            "cards": sum(1 for entry in cards if entry["hidden"]),
            "shown": show_hidden,
        },
        "icons": dict(ICONS),
        "terminals": list(terminal_mod.TERMINALS),
        "token_file": str(config.serve.token_file),
    }


def job_entry(job: ledger_mod.Job, worker: Worker | None, ledger: Ledger) -> dict[str, object]:
    """A job as the top bar and a card show it. Its texts are chargehand's own."""
    if worker is None and job.kind == ledger_mod.JOB_ARCHIVE and job.target.isdigit():
        worker = ledger.get(int(job.target))
    return {
        "id": job.id,
        "kind": job.kind,
        "worker_id": worker.id if worker else None,
        "handle": worker.target if worker else None,
        "repository": worker.repository if worker else None,
        "state": job.state,
        "active": job.is_active,
        "progress": sanitize.one_line(job.progress, limit=LINE_LIMIT) if job.progress else None,
        "result_path": sanitize.one_line(job.result_path, limit=LINE_LIMIT) if job.result_path else None,
        "error": sanitize.one_line(job.error, limit=300) if job.error else None,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }


def job_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    """What the top bar shows of the jobs: a running or failed job in full, the others as
    a count per state, so that an archive of a whole repository stays readable."""
    return {
        "shown": [entry for entry in entries if entry["state"] in JOB_SHOWN_IN_FULL],
        "counts": {
            state: sum(1 for entry in entries if entry["state"] == state)
            for state in JOB_COUNTED
        },
    }


def model_limit(ledger: Ledger, now: float) -> dict[str, object] | None:
    """The top bar's badge: the model family whose own limit failed the last such turn.

    The usage windows have no per-model window, so the failed turn is the only sign. The
    badge shows until the reset time, or for one five-hour window when none was given.
    """
    raw = ledger.get_state(MODEL_LIMIT_KEY)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("model"), str):
        return None
    at = data.get("at")
    reset_at = data.get("reset_at")
    if not isinstance(at, (int, float)):
        return None
    until = reset_at if isinstance(reset_at, (int, float)) else at + MODEL_LIMIT_SHOWN_SECS
    if until <= now:
        return None
    provider = data.get("provider")
    return {
        "provider": sanitize.scrub_identifier(provider) if isinstance(provider, str) else None,
        "model": sanitize.scrub_identifier(data["model"])[:40],
        "at": at,
        "reset_at": reset_at if isinstance(reset_at, (int, float)) else None,
    }


def _activity(entry: Mapping[str, object]) -> float:
    """When the card last changed: its finish for a closed card, its last update otherwise."""
    value = entry["updated_at"]
    if entry["column"] == "closed" and entry["finished_at"]:
        value = entry["finished_at"]
    return float(value) if isinstance(value, (int, float)) else 0.0


def _column(
    key: str,
    title: str,
    hint: str,
    chosen: list[dict[str, object]],
    repositories: Mapping[str, Mapping[str, object]],
    *,
    show_hidden: bool,
) -> dict[str, object]:
    """A column: a heading per repository that has cards in it, and the cards in page order."""
    members: dict[str, list[dict[str, object]]] = {}
    for entry in chosen:
        if show_hidden or not entry["hidden"]:
            members.setdefault(str(entry["repository"]), []).append(entry)
    for group in members.values():
        group.sort(key=lambda entry: (not entry["pinned"], -_activity(entry)))
    order = sorted(
        members,
        key=lambda name: (
            not repositories[name]["pinned"], -max(_activity(entry) for entry in members[name]), name,
        ),
    )
    groups, cards = [], []
    for name in order:
        shown = repositories[name]
        visible = [entry for entry in members[name] if not shown["collapsed"] or entry["pinned"]]
        cards += visible
        groups.append({
            "repository": name,
            **shown,
            "count": len(members[name]),
            "folded": len(members[name]) - len(visible),
        })
    return {
        "key": key,
        "title": title,
        "hint": hint,
        "count": sum(len(group) for group in members.values()),
        "groups": groups,
        "cards": cards,
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
    held: Callable[[int], bool] = lambda prompt_id: False,
    job: ledger_mod.Job | None = None,
) -> dict[str, object]:
    role = roles.of(worker)
    live = not worker.is_terminal
    # The newest: a session shows one dialog at a time.
    prompt = (
        next((p for p in reversed(list(prompts)) if p.worker_id == worker.id), None)
        if live else None
    )
    cap = role.policy.max_wakes_per_day if role else None
    return {
        "id": worker.id,
        "handle": worker.target,
        "origin": worker.origin,
        "actions": actions(worker, job),
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
        "permission_prompt": permission_prompt(prompt, held) if prompt else None,
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
        "queue_reason": (
            sanitize.one_line(reason, limit=LINE_LIMIT)
            if worker.state in (ledger_mod.QUEUED, ledger_mod.PARKED)
            and (reason := ledger.get_state(f"{QUEUE_REASON_PREFIX}{worker.id}"))
            else None
        ),
        "last_hook": worker.last_hook,
        "job": job_entry(job, worker, ledger) if job else None,
        "archived_at": worker.archived_at,
        "created_at": worker.created_at,
        "updated_at": worker.updated_at,
        "finished_at": worker.finished_at,
    }


def permission_prompt(
    prompt: ledger_mod.PermissionPrompt, held: Callable[[int], bool]
) -> dict[str, object]:
    """A prompt as its card shows it. *answers* is empty when only the session can answer."""
    answerable = prompt.kind == ledger_mod.PROMPT_PERMISSION and held(prompt.id)
    return {
        "id": prompt.id,
        "kind": prompt.kind,
        "tool_name": sanitize.one_line(prompt.tool_name, limit=100),
        "tool_input": sanitize.one_line(prompt.tool_input, limit=PROMPT_INPUT_LIMIT),
        "created_at": prompt.created_at,
        "answers": list(hooks_mod.ANSWERS) if answerable else [],
    }


def actions(worker: Worker, job: ledger_mod.Job | None = None) -> list[str]:
    """The buttons a card offers: the actions that the worker's state can take now.

    While an archive of the worker is under way, it offers none that would remove or
    replace what the archive reads.
    """
    archiving = job is not None and job.is_active
    if worker.is_terminal:
        if archiving or worker.archived_at is not None:
            return []
        return ["retry"] + ([] if worker.gc_done else ["discard", "archive"])
    offered = []
    if worker.state in (ledger_mod.PAUSED, ledger_mod.BLOCKED, ledger_mod.PARKED):
        offered.append("continue")
    if worker.session_id and worker.state != ledger_mod.PAUSED:
        offered.append("pause")
    offered += ["cancel", "reply"]
    if worker.session_id:
        offered.append(ATTACH)
    if not archiving:
        offered += ["discard", "archive"]
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
    "ICONS",
    "BoardState",
    "icon_glyph",
    "is_emoji",
    "personalise",
    "WORKER_ACTIONS",
    "actions",
    "outcome",
    "permission_prompt",
    "target_of",
    "COLUMNS",
    "COLUMN_OF",
    "CSP",
    "build",
    "card",
    "last_prompt",
    "model_limit",
    "register_routes",
    "to_json",
    "tokens_estimate",
]
