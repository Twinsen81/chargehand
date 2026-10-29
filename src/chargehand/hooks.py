"""The hook receiver: what worker sessions post to the daemon, applied to the ledger at once.

Each session is launched with settings hooks that post to ``POST /hooks/<event>`` with
the daemon's token: its start, the end of each turn, a turn that failed, its
notifications and its permission prompts. The runtime that owns the event reads the
payload; this module finds the worker and hands the post to the tick's own transition
code, so a hook and the session diff move a worker the same way and send the same
notification. The diff of each tick stays as the fallback for a hook that never came.

Every answer is an empty JSON object. A turn end gets no decision that would keep the
turn running, a session start gets no context for the model, and a permission prompt
gets no decision, so the session keeps its own dialog. A post that maps to no live
worker changes nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from chargehand import runtime as runtime_mod
from chargehand import sanitize
from chargehand.ledger import Ledger, PermissionPrompt, Worker
from chargehand.runtime import Hook, Runtime
from chargehand.serve import HTTPError, Request, Response, Routes
from chargehand.tick import TickReport

log = logging.getLogger(__name__)

PREFIX = "/hooks"

# How much of a session id a hook's log line carries.
SESSION_PREFIX = 8

# The answer to every hook. Anything else could reach the session's turn or its model.
NO_DECISION = Response.json({})


def register_routes(routes: Routes) -> None:
    routes.add("POST", PREFIX, receive, prefix=True)
    routes.extend_state(add_to_state)


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
    worker = find_worker(ledger, hook, runtime.name)
    session = session_prefix(hook)
    if worker is None:
        log.info("hook %s from session %s: no live worker has this session (cwd %s)",
                 event, session, sanitize.one_line(hook.cwd or "-", limit=300))
        return NO_DECISION
    log.info("%s: hook %s from session %s", worker.handle, event, session)
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
    return NO_DECISION


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


def _prompt(prompt: PermissionPrompt) -> dict[str, object]:
    return {
        "tool_name": sanitize.one_line(prompt.tool_name, limit=100),
        "tool_input": sanitize.clean(prompt.tool_input),
        "created_at": prompt.created_at,
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
            _prompt(prompt) for prompt in prompts if prompt.worker_id == worker.id
        ]


__all__ = ["NO_DECISION", "add_to_state", "find_worker", "hook_runtimes", "receive", "register_routes"]
