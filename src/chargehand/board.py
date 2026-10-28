"""The read-only board: one static page that the daemon serves, and the model it renders.

``GET /`` sends the page, and the page polls ``GET /api/board`` about once a second.
The model is built from the ledger, the role files and the usage file only: a request
makes no tracker call and lists no sessions, so a board left open costs nothing but a
few SQLite reads a second.

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
from chargehand import usage as usage_mod
from chargehand.config import MachineConfig
from chargehand.delivery import WAKE_CAP_WINDOW_SECS, wake_message
from chargehand.errors import ChargehandError
from chargehand.ledger import Ledger, Worker
from chargehand.roles import Role
from chargehand.serve import Request, Response, Routes
from chargehand.tick import template_values

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


def register_routes(routes: Routes) -> None:
    for path in ASSETS:
        routes.add("GET", path, asset)
    routes.add("GET", "/api/board", state)


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
