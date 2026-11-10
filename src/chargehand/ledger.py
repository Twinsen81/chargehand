"""The ledger: the write-ahead record that makes a launch crash-safe.

The row is written before any external side effect and its ``step`` advances after
each one, so a tick that dies mid-launch leaves enough behind for the next tick to
adopt, resume, or fail the worker loudly. Control commands also land here: a
mutating command records a request and the tick applies it, which keeps a single
writer of side effects.

A *worker* is one member of a role. A role may have any number of workers. A worker
usually has one work item, the pull request or issue it was created for, and it owns a
worktree and, over its life, one or more agent sessions; the ``sessions`` table keeps
them. A worker created by a start rule has ``origin`` ``source``; one created by hand
has ``board`` or ``clone``, and may have no work item at all.

SQLite under the state directory, not /tmp, because it has to survive a reboot.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from chargehand import paths, sanitize
from chargehand.sources.base import Event, WorkItem

SCHEMA_VERSION = 11

# Worker lifecycle. A worker that a start rule created occupies its (role, item) pair
# until it reaches a terminal state. `running` is a turn in progress. `idle`: the turn
# ended without a question and the worker waits for events. `queued`: events wait in its
# inbox for a wake that admission held back. `blocked` needs the operator, and delivery
# never wakes it: a stop and resume would discard an open prompt. `parked`: a usage limit
# ended the turn; it waits for the reset, and neither delivery, the watchdog nor the
# session diff moves it.
LAUNCHING = "launching"
RUNNING = "running"
IDLE = "idle"
QUEUED = "queued"
BLOCKED = "blocked"
PAUSED = "paused"
PARKED = "parked"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

NON_TERMINAL = (LAUNCHING, RUNNING, IDLE, QUEUED, BLOCKED, PAUSED, PARKED)
TERMINAL = (DONE, FAILED, CANCELLED)
WORKER_STATES = NON_TERMINAL + TERMINAL

# Session states as reported by Claude Code, plus `missing` for a session that has
# disappeared from the listing entirely.
SESSION_WORKING = "working"
SESSION_BLOCKED = "blocked"
SESSION_DONE = "done"
SESSION_FAILED = "failed"
SESSION_STOPPED = "stopped"
SESSION_MISSING = "missing"

LAST_STEP = 4

# Why a session row exists: the launch, an adoption, a continuation after a refresh,
# a retry, or a clone. Rows copied from a version 1 ledger carry `migrated`. A wake
# continues the same session, so each wake has its own `wake` row: the daily wake cap
# counts them. A clone in full mode starts with a copy of its original's conversation,
# which is a `fork` row.
SESSION_INITIAL = "initial"
SESSION_ADOPTED = "adopted"
SESSION_MIGRATED = "migrated"
SESSION_WAKE = "wake"
SESSION_FORK = "fork"

# How a worker came to exist. Only `source` workers are held to one per role and item.
ORIGIN_SOURCE = "source"
ORIGIN_BOARD = "board"
ORIGIN_CLONE = "clone"
ORIGINS = (ORIGIN_SOURCE, ORIGIN_BOARD, ORIGIN_CLONE)

# How much of its original's session a clone gets: nothing, or a copy of the conversation.
CLONE_FRESH = "fresh"
CLONE_FULL = "full"
CLONE_MODES = (CLONE_FRESH, CLONE_FULL)

# Where a control request came from: the command line or the board. Both record the same
# kinds and arguments; the tick applies them alike.
REQUEST_CLI = "cli"
REQUEST_BOARD = "board"
REQUEST_ORIGINS = (REQUEST_CLI, REQUEST_BOARD)

# A reply that the operator typed on the board waits in the worker's inbox as an event of
# this source and kind, with the line as its pointer. No configured source may have this
# name, so only the tick writes such a row; the line is trusted operator text, cleaned to
# one line of at most REPLY_LIMIT characters.
REPLY_SOURCE = "board"
REPLY_KIND = "reply"
REPLY_LIMIT = 500

# A wake that a worker asked for with request_wake waits in its inbox as an event of this
# source and kind, due at the time it named. Its pointer is chargehand's own identifier.
# One timer per worker waits at a time: a new request replaces the one that waits.
TIMER_SOURCE = "timer"
TIMER_KIND = "timer"
TIMER_POINTER = "request_wake"

# What a worker's session last reported with the report tool. `done` and `failed` end the
# worker at the end of the turn that reported them; the other states are card text only.
REPORT_WORKING = "working"
REPORT_DONE = "done"
REPORT_FAILED = "failed"
REPORT_NEEDS_INPUT = "needs-input"
REPORT_STATES = (REPORT_WORKING, REPORT_DONE, REPORT_FAILED, REPORT_NEEDS_INPUT)

# What the board keeps per repository and per worker: names and icons the operator chose,
# pins, and the folded and hidden repositories. The page reads it and nothing else does, so
# the configured names, the handles and the session names never change with it.
BOARD_REPOSITORY = "repository"
BOARD_WORKER = "worker"
BOARD_FIELDS: dict[str, tuple[str, ...]] = {
    BOARD_REPOSITORY: ("name", "icon", "pinned", "collapsed", "hidden"),
    BOARD_WORKER: ("name", "icon", "pinned"),
}

# A message for a live session, which the session's chargehand mod takes and submits as its
# next turn once it is idle: the wake into a live process. One open message per worker, at
# most: a new event while one is `pending` goes into it; once the mod has `taken` it, new
# events wait for the next wake. `submitted`: the mod's submit started the turn, and that
# is when the wake is recorded and its inbox entries are delivered. `dropped`: the message
# no longer goes out (its session changed, the mod refused it or never took it), and its
# entries wait in the inbox for the next wake.
OUTBOX_PENDING = "pending"
OUTBOX_TAKEN = "taken"
OUTBOX_SUBMITTED = "submitted"
OUTBOX_DROPPED = "dropped"
OUTBOX_OPEN = (OUTBOX_PENDING, OUTBOX_TAKEN)
OUTBOX_STATES = OUTBOX_OPEN + (OUTBOX_SUBMITTED, OUTBOX_DROPPED)

# A prompt a session shows the operator. A permission prompt is answered allow or deny; the
# record also holds a question with its own options, so that both use one card. A prompt is
# `open` until it is answered (`resolved`, with the decision and who decided) or the
# session stops showing it (`cancelled`). An open prompt is "released" when `released_at` is
# set: no hook waits for the board's answer any more (after the hook's timeout or a stop of
# the daemon), so only the dialog in the session can answer it. It stays `open` because the
# session still shows that dialog, and it is cancelled when the worker leaves `blocked`.
PROMPT_PERMISSION = "permission"
PROMPT_QUESTION = "question"
PROMPT_OPEN = "open"
PROMPT_RESOLVED = "resolved"
PROMPT_CANCELLED = "cancelled"
PROMPT_STATES = (PROMPT_OPEN, PROMPT_RESOLVED, PROMPT_CANCELLED)
ALLOW = "allow"
DENY = "deny"
# Who resolved or cancelled a prompt: a click on the board, a rule the operator recorded
# from the board, or the session itself (answered in the terminal, superseded, or stopped).
# A question is also resolved by a reply, from the board or the command line.
RESOLVED_BY_BOARD = "board"
RESOLVED_BY_REPLY = "reply"
RESOLVED_BY_RULE = "rule"
RESOLVED_BY_SESSION = "session"

# Slow work that runs on a thread of the daemon, outside the tick, so that neither the tick
# nor the board waits for it. Only an archive is such work today: its target is the worker
# id. `waiting`: a cancel of the live worker must apply first. `queued`: the thread starts
# it. `running`: the thread writes the archive; a restart of the daemon starts it again.
# `written`: the archive is complete and verified, and the tick removes the worktree and
# the session. `done` and `failed` are final.
JOB_ARCHIVE = "archive"
JOB_WAITING = "waiting"
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_WRITTEN = "written"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_ACTIVE = (JOB_WAITING, JOB_QUEUED, JOB_RUNNING, JOB_WRITTEN)
JOB_STATES = JOB_ACTIVE + (JOB_DONE, JOB_FAILED)

# A worker handle that names the worker by id: `role/item#id`, or `role/#id` without an item.
_ID_HANDLE = re.compile(r"(?:([^/#\s]+)/([^/#\s]*))?#([0-9]{1,12})")

# What a worker's `owns_label` says it shows its state on. One worker per item does it,
# so two workers cannot write the same label in turn. QUEUE: the queue label of its
# repository's tracker, for a worker the queue label started. SOURCE: its primary item
# on the `[[source]]` whose start rule started it, through that source's `mark`.
LABEL_NONE = 0
LABEL_QUEUE = 1
LABEL_SOURCE = 2
LABEL_OWNERS = (LABEL_NONE, LABEL_QUEUE, LABEL_SOURCE)

_WORKER_COLUMNS = (
    "id",
    "role",
    "origin",
    "start_rule",
    "runtime",
    "item",
    "item_id",
    "url",
    "repository",
    "repo",
    "worktree",
    "branch",
    "session_id",
    "session_uuid",
    "state",
    "label_state",
    "step",
    "launch_attempts",
    "session_state",
    "waiting_for",
    "notified_state",
    "working_since",
    "watchdog_notified",
    "last_error",
    "pr_url",
    "gc_done",
    "wakes",
    "last_wake_at",
    "created_at",
    "updated_at",
    "finished_at",
    "owns_label",
    "reset_at",
    "last_hook",
    "last_hook_at",
    "last_answer",
    "transcript_path",
    "archived_at",
    "parent_id",
    "clone_mode",
    "prompt",
    "model",
    "effort",
    "mod_session",
    "mod_seen_at",
    "context_tokens",
    "context_percent",
    "report_state",
    "report_summary",
    "report_links",
    "reported_at",
    "handoff_note",
    "handoff_at",
)

_LIVE = ", ".join(repr(s) for s in NON_TERMINAL)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS workers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    role             TEXT    NOT NULL,
    origin           TEXT    NOT NULL DEFAULT '{ORIGIN_SOURCE}',
    start_rule       TEXT,
    runtime          TEXT    NOT NULL DEFAULT 'claude',
    item             TEXT,
    item_id          TEXT,
    url              TEXT,
    repository       TEXT    NOT NULL,
    repo             TEXT    NOT NULL,
    worktree         TEXT    NOT NULL,
    branch           TEXT    NOT NULL,
    session_id       TEXT,
    session_uuid     TEXT,
    state            TEXT    NOT NULL,
    -- The queue status chargehand last *verified* on the tracker. Kept apart from
    -- `state` so an ambiguous label write is retried on the next tick rather than
    -- assumed to have landed.
    label_state      TEXT,
    step             INTEGER NOT NULL DEFAULT 0,
    launch_attempts  INTEGER NOT NULL DEFAULT 1,
    session_state    TEXT,
    waiting_for      TEXT,
    notified_state   TEXT,
    working_since    REAL,
    watchdog_notified INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    pr_url           TEXT,
    gc_done          INTEGER NOT NULL DEFAULT 0,
    wakes            INTEGER NOT NULL DEFAULT 0,
    last_wake_at     REAL,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    finished_at      REAL,
    -- What this worker shows its state on, one of LABEL_OWNERS: 0 nothing, 1 the
    -- tracker's queue label (the queue label started it; every worker before version 3
    -- was started that way), 2 its primary item on the [[source]] that started it.
    owns_label       INTEGER NOT NULL DEFAULT 1,
    -- When a parked worker's usage limit resets, as the failure said; NULL when unknown.
    reset_at         REAL,
    -- The last hook its session posted, when, and what it carried. Untrusted text,
    -- sanitized and capped when stored.
    last_hook        TEXT,
    last_hook_at     REAL,
    last_answer      TEXT,
    transcript_path  TEXT,
    -- When an archive of the worker was written and its worktree and session removed.
    archived_at      REAL,
    -- A clone: the worker it was made from, how much of that session it got (CLONE_MODES),
    -- and the prompt template, model and effort the operator chose. NULL is the role's.
    parent_id        INTEGER,
    clone_mode       TEXT,
    prompt           TEXT,
    model            TEXT,
    effort           TEXT,
    -- The session whose chargehand mod said hello last, and when: only that session takes
    -- its wakes from the outbox. And the session's context window as the mod last read it.
    mod_session      TEXT,
    mod_seen_at      REAL,
    context_tokens   INTEGER,
    context_percent  REAL,
    -- The last report of its session (REPORT_STATES, a summary, a JSON list of links), and
    -- its handoff note. Untrusted session text, cleaned and capped when stored.
    report_state     TEXT,
    report_summary   TEXT,
    report_links     TEXT,
    reported_at      REAL,
    handoff_note     TEXT,
    handoff_at       REAL
);

-- The structural guarantee against a duplicate launch: a start event arrives again on
-- every poll, so at most one live worker that a start rule created per role and work
-- item, enforced by the database rather than by a check the caller may skip. Workers
-- created by hand are not limited.
CREATE UNIQUE INDEX IF NOT EXISTS workers_one_live_per_role_item
    ON workers (role, item)
    WHERE origin = '{ORIGIN_SOURCE}' AND state IN ({_LIVE});

CREATE INDEX IF NOT EXISTS workers_state ON workers (state);
CREATE INDEX IF NOT EXISTS workers_worktree ON workers (worktree);
CREATE INDEX IF NOT EXISTS workers_item ON workers (item);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    INTEGER NOT NULL REFERENCES workers (id),
    session_id   TEXT,
    session_uuid TEXT,
    kind         TEXT    NOT NULL,
    started_at   REAL    NOT NULL,
    ended_at     REAL
);

CREATE INDEX IF NOT EXISTS sessions_worker ON sessions (worker_id);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    target      TEXT,
    args        TEXT    NOT NULL DEFAULT '{{}}',
    created_at  REAL    NOT NULL,
    applied_at  REAL,
    ok          INTEGER,
    message     TEXT,
    origin      TEXT    NOT NULL DEFAULT '{REQUEST_CLI}'
);

CREATE INDEX IF NOT EXISTS requests_pending ON requests (applied_at);

CREATE TABLE IF NOT EXISTS runner_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- A worker's interest in a work item: every live subscriber gets every event of a kind
-- in `kinds` (a JSON list) on that item. The primary item is the one the worker was
-- created for. A group topic later is a subscription to a group key.
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id   INTEGER NOT NULL REFERENCES workers (id),
    source      TEXT    NOT NULL,
    item        TEXT    NOT NULL,
    item_ref    TEXT    NOT NULL,
    kinds       TEXT    NOT NULL DEFAULT '[]',
    is_primary  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    UNIQUE (worker_id, source, item)
);

CREATE INDEX IF NOT EXISTS subscriptions_item ON subscriptions (source, item);

-- Per-worker queue of events not yet delivered. The unique key makes routing the same
-- event twice harmless, which a start event deferred by the caps relies on.
CREATE TABLE IF NOT EXISTS inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    INTEGER NOT NULL REFERENCES workers (id),
    source       TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    item         TEXT    NOT NULL,
    item_ref     TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    occurred_at  REAL    NOT NULL,
    dedupe_id    TEXT    NOT NULL,
    pointer      TEXT    NOT NULL,
    queued_at    REAL    NOT NULL,
    delivered_at REAL,
    -- An entry that waits for this time before a wake takes it, such as a timer.
    due_at       REAL,
    UNIQUE (worker_id, source, dedupe_id)
);

CREATE INDEX IF NOT EXISTS inbox_pending ON inbox (worker_id, delivered_at);

-- Every event the router has handled, for the dedupe filter, including events that
-- matched no subscriber. A start event that the caps deferred stays `start_pending`
-- and is offered to the start rules again on the next tick.
CREATE TABLE IF NOT EXISTS events (
    source        TEXT    NOT NULL,
    dedupe_id     TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    item          TEXT    NOT NULL,
    item_ref      TEXT    NOT NULL,
    item_url      TEXT,
    item_hint     TEXT,
    actor         TEXT    NOT NULL,
    occurred_at   REAL    NOT NULL,
    pointer       TEXT    NOT NULL,
    marked        INTEGER NOT NULL DEFAULT 0,
    seen_at       REAL    NOT NULL,
    start_pending INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, dedupe_id)
);

CREATE INDEX IF NOT EXISTS events_pending ON events (start_pending);

-- A prompt a session reported: a permission prompt, or a question with its options (a
-- JSON list). It is open until the board, a rule or the session resolves it, or the worker
-- leaves `blocked` and cancels it. The tool input is untrusted text; `input_digest` is a
-- hash of the whole input, which a rule matches.
CREATE TABLE IF NOT EXISTS permission_prompts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    INTEGER NOT NULL REFERENCES workers (id),
    tool_name    TEXT    NOT NULL,
    tool_input   TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL,
    resolved_at  REAL,
    kind         TEXT    NOT NULL DEFAULT 'permission',
    options      TEXT,
    input_digest TEXT,
    state        TEXT    NOT NULL DEFAULT 'open',
    decision     TEXT,
    resolved_by  TEXT,
    released_at  REAL
);

CREATE INDEX IF NOT EXISTS permission_prompts_open ON permission_prompts (worker_id, resolved_at);

-- "Always allow" for one worker: the daemon allows the same tool with the same input for
-- that worker by itself. Never a role's, a repository's or Claude Code's settings.
CREATE TABLE IF NOT EXISTS permission_rules (
    worker_id    INTEGER NOT NULL REFERENCES workers (id),
    tool_name    TEXT    NOT NULL,
    input_digest TEXT    NOT NULL,
    prompt_id    INTEGER REFERENCES permission_prompts (id),
    created_at   REAL    NOT NULL,
    PRIMARY KEY (worker_id, tool_name, input_digest)
);

-- The wake of a live session: a message its chargehand mod takes and submits; see
-- OUTBOX_STATES. `entries` is a JSON list of the inbox rows it delivers. The text is
-- chargehand's own wake message, built from kinds and checked pointers.
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    INTEGER NOT NULL REFERENCES workers (id),
    session_uuid TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    entries      TEXT    NOT NULL DEFAULT '[]',
    state        TEXT    NOT NULL,
    reason       TEXT,
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL,
    taken_at     REAL,
    submitted_at REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_open ON outbox (worker_id)
    WHERE state IN ('pending', 'taken');

-- Where each source's last poll stopped.
CREATE TABLE IF NOT EXISTS cursors (
    source     TEXT PRIMARY KEY,
    value      TEXT,
    updated_at REAL NOT NULL
);

-- Slow work for the daemon's job thread; see JOB_STATES. `args` is a JSON object, and
-- `result_path` the file the job writes.
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    target       TEXT    NOT NULL,
    args         TEXT    NOT NULL DEFAULT '{{}}',
    state        TEXT    NOT NULL,
    progress     TEXT,
    result_path  TEXT,
    error        TEXT,
    created_at   REAL    NOT NULL,
    started_at   REAL,
    updated_at   REAL    NOT NULL,
    finished_at  REAL
);

CREATE INDEX IF NOT EXISTS jobs_state ON jobs (state);
CREATE INDEX IF NOT EXISTS jobs_target ON jobs (kind, target);

-- How the board shows a repository or a worker. The key is the configured repository
-- name or the worker id; only the board reads it.
CREATE TABLE IF NOT EXISTS board_state (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    field      TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, key, field)
);
"""

# Version 1 kept one row per attempt at an issue. Every attempt becomes a worker in the
# `implementer` role bound to the issue, with its row id preserved so runner-state keys
# that name an id keep pointing at the right row. The old table is kept under another
# name for one release rather than dropped.
_MIGRATE_V1_TO_V2 = f"""
ALTER TABLE attempts RENAME TO attempts_v1;
{_SCHEMA}
INSERT INTO workers (id, role, origin, start_rule, runtime, item, item_id, url, repository,
                     repo, worktree, branch,
                     session_id, session_uuid, state, label_state, step, launch_attempts,
                     session_state, waiting_for, notified_state, working_since,
                     watchdog_notified, last_error, pr_url, gc_done, wakes, last_wake_at,
                     created_at, updated_at, finished_at)
SELECT id, 'implementer', '{ORIGIN_SOURCE}', 'queue', 'claude', identifier, issue_id, url,
       route, repo, worktree, branch,
       session_id, session_uuid, state, label_state, step, launch_attempts,
       session_state, waiting_for, notified_state, working_since,
       watchdog_notified, last_error, pr_url, gc_done, 0, NULL,
       created_at, updated_at, finished_at
FROM attempts_v1 ORDER BY id;
INSERT INTO sessions (worker_id, session_id, session_uuid, kind, started_at)
SELECT id, session_id, session_uuid, '{SESSION_MIGRATED}', created_at
FROM workers WHERE session_id IS NOT NULL;
"""

# Version 3 adds subscriptions, the inbox, the seen events and the source cursors, and the
# `owns_label` column. It only adds, so the existing rows and their ids stay as they are.
# The ALTER is not idempotent on its own; it runs only on a version 2 file, in the same
# transaction as the version bump. The one-live-worker index is dropped and created
# again, because version 3 has more live states and the index names them.
_MIGRATE_V2_TO_V3 = f"""
ALTER TABLE workers ADD COLUMN owns_label INTEGER NOT NULL DEFAULT 1;
DROP INDEX IF EXISTS workers_one_live_per_role_item;
{_SCHEMA}
"""

# Version 4 adds the `parked` state, what the session hooks report, and the permission
# prompts. It only adds, like version 3; the index is created again because `parked` is
# a live state.
_MIGRATE_V3_TO_V4 = f"""
ALTER TABLE workers ADD COLUMN reset_at REAL;
ALTER TABLE workers ADD COLUMN last_hook TEXT;
ALTER TABLE workers ADD COLUMN last_hook_at REAL;
ALTER TABLE workers ADD COLUMN last_answer TEXT;
ALTER TABLE workers ADD COLUMN transcript_path TEXT;
DROP INDEX IF EXISTS workers_one_live_per_role_item;
{_SCHEMA}
"""

# Version 5 records where each control request came from. Every earlier version has the
# requests table, so each one takes this step; the rows recorded before it came from the CLI.
_MIGRATE_V4_TO_V5 = f"""
ALTER TABLE requests ADD COLUMN origin TEXT NOT NULL DEFAULT '{REQUEST_CLI}';
{_SCHEMA}
"""

# Version 6 adds the board's own state. It only adds a table, which the schema creates.
_MIGRATE_V5_TO_V6 = _SCHEMA

# Version 7 makes a prompt a record with a state, a decision and who decided, and adds the
# per-worker rules. Only a version 4, 5 or 6 file has the old prompts table; an older one
# gets the table from the schema with every column. A prompt resolved before this version
# was never answered by chargehand, so its session cancelled it.
_MIGRATE_V6_TO_V7 = f"""
ALTER TABLE permission_prompts ADD COLUMN kind TEXT NOT NULL DEFAULT '{PROMPT_PERMISSION}';
ALTER TABLE permission_prompts ADD COLUMN options TEXT;
ALTER TABLE permission_prompts ADD COLUMN input_digest TEXT;
ALTER TABLE permission_prompts ADD COLUMN state TEXT NOT NULL DEFAULT '{PROMPT_OPEN}';
ALTER TABLE permission_prompts ADD COLUMN decision TEXT;
ALTER TABLE permission_prompts ADD COLUMN resolved_by TEXT;
ALTER TABLE permission_prompts ADD COLUMN released_at REAL;
UPDATE permission_prompts
SET state = '{PROMPT_CANCELLED}', resolved_by = '{RESOLVED_BY_SESSION}', released_at = resolved_at
WHERE resolved_at IS NOT NULL;
{_SCHEMA}
"""

# Version 8 adds the jobs and the worker's `archived_at`. Every version from 2 on has a
# workers table without the column; the version 1 copy creates the table with it.
_MIGRATE_V7_TO_V8 = f"""
ALTER TABLE workers ADD COLUMN archived_at REAL;
{_SCHEMA}
"""

# Version 9 adds what a clone keeps: its original, its mode, and the prompt, model and
# effort chosen for it. Like version 8, the version 1 copy creates the table with them.
_MIGRATE_V8_TO_V9 = f"""
ALTER TABLE workers ADD COLUMN parent_id INTEGER;
ALTER TABLE workers ADD COLUMN clone_mode TEXT;
ALTER TABLE workers ADD COLUMN prompt TEXT;
ALTER TABLE workers ADD COLUMN model TEXT;
ALTER TABLE workers ADD COLUMN effort TEXT;
{_SCHEMA}
"""

# Version 10 adds the outbox, which the schema creates, and what the chargehand mod reports
# on the worker. Like version 8, the version 1 copy creates the table with them.
_MIGRATE_V9_TO_V10 = f"""
ALTER TABLE workers ADD COLUMN mod_session TEXT;
ALTER TABLE workers ADD COLUMN mod_seen_at REAL;
ALTER TABLE workers ADD COLUMN context_tokens INTEGER;
ALTER TABLE workers ADD COLUMN context_percent REAL;
{_SCHEMA}
"""

# Version 11 adds the worker's last report and its handoff note, and the due time of an
# inbox entry. Every version from 2 on has a workers table without the new columns; only a
# version 3 file or a later one has an inbox, which a version 2 file gets whole from the
# schema of its step to version 3.
_V11_WORKER_COLUMNS = """
ALTER TABLE workers ADD COLUMN report_state TEXT;
ALTER TABLE workers ADD COLUMN report_summary TEXT;
ALTER TABLE workers ADD COLUMN report_links TEXT;
ALTER TABLE workers ADD COLUMN reported_at REAL;
ALTER TABLE workers ADD COLUMN handoff_note TEXT;
ALTER TABLE workers ADD COLUMN handoff_at REAL;
"""
_MIGRATE_V10_TO_V11 = f"""
{_V11_WORKER_COLUMNS}
ALTER TABLE inbox ADD COLUMN due_at REAL;
{_SCHEMA}
"""
_MIGRATE_V2_V10_TO_V11 = f"""
{_V11_WORKER_COLUMNS}
{_SCHEMA}
"""


@dataclass(frozen=True)
class Worker:
    id: int
    role: str
    origin: str
    start_rule: str | None
    runtime: str
    item: str | None
    item_id: str | None
    url: str | None
    repository: str
    repo: str
    worktree: str
    branch: str
    session_id: str | None
    session_uuid: str | None
    state: str
    label_state: str | None
    step: int
    launch_attempts: int
    session_state: str | None
    waiting_for: str | None
    notified_state: str | None
    working_since: float | None
    watchdog_notified: int
    last_error: str | None
    pr_url: str | None
    gc_done: int
    wakes: int
    last_wake_at: float | None
    created_at: float
    updated_at: float
    finished_at: float | None
    owns_label: int = 1
    reset_at: float | None = None
    last_hook: str | None = None
    last_hook_at: float | None = None
    last_answer: str | None = None
    transcript_path: str | None = None
    archived_at: float | None = None
    parent_id: int | None = None
    clone_mode: str | None = None
    prompt: str | None = None
    model: str | None = None
    effort: str | None = None
    mod_session: str | None = None
    mod_seen_at: float | None = None
    context_tokens: int | None = None
    context_percent: float | None = None
    report_state: str | None = None
    report_summary: str | None = None
    report_links: str | None = None
    reported_at: float | None = None
    handoff_note: str | None = None
    handoff_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def worktree_path(self) -> Path:
        return Path(self.worktree)

    @property
    def repo_path(self) -> Path:
        return Path(self.repo)

    @property
    def handle(self) -> str:
        """How a worker is named in logs, in reports, and as its session's display name.

        A start rule keeps one live worker per role and item, so `role/item` names it. A
        worker created by hand may share its role and item with others, so its handle
        carries its id: `role/item#id`, or `role/#id` without an item. The CLI and the
        tick accept every handle as a target.
        """
        if self.item and self.origin == ORIGIN_SOURCE:
            return f"{self.role}/{self.item}"
        return self.id_handle

    @property
    def id_handle(self) -> str:
        """The handle with the worker's id, which names it alone in every case."""
        return f"{self.role}/{self.item or ''}#{self.id}"

    @property
    def target(self) -> str:
        """The handle that chargehand prints for a command to name this worker.

        `role/item` for a live start-rule worker: the database keeps one live worker per
        role and item, and the resolution rule (:meth:`Ledger.find_worker`) prefers it.
        The id handle for every other worker, because a finished start-rule worker's
        `role/item` names its successor once a start rule starts one.
        """
        return self.id_handle if self.is_terminal else self.handle

    @property
    def name(self) -> str:
        """What a message to the operator calls the worker: its item, or its handle."""
        if self.item and self.origin == ORIGIN_SOURCE:
            return self.item
        return self.handle


@dataclass(frozen=True)
class SessionRecord:
    id: int
    worker_id: int
    session_id: str | None
    session_uuid: str | None
    kind: str
    started_at: float
    ended_at: float | None


@dataclass(frozen=True)
class PermissionPrompt:
    id: int
    worker_id: int
    tool_name: str
    tool_input: str
    created_at: float
    resolved_at: float | None
    kind: str = PROMPT_PERMISSION
    options: str | None = None
    input_digest: str | None = None
    state: str = PROMPT_OPEN
    decision: str | None = None
    resolved_by: str | None = None
    released_at: float | None = None


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    target: str
    args: Mapping[str, Any]
    state: str
    progress: str | None
    result_path: str | None
    error: str | None
    created_at: float
    started_at: float | None
    updated_at: float
    finished_at: float | None

    @property
    def is_active(self) -> bool:
        return self.state in JOB_ACTIVE

    @property
    def force(self) -> bool:
        return bool(self.args.get("force"))


def _row_to_job(row: sqlite3.Row) -> Job:
    data = dict(row)
    try:
        args = json.loads(data["args"])
    except (TypeError, ValueError):
        args = {}
    data["args"] = args if isinstance(args, dict) else {}
    return Job(**data)


@dataclass(frozen=True)
class Subscription:
    id: int
    worker_id: int
    source: str
    item: str
    item_ref: str
    kinds: tuple[str, ...]
    is_primary: bool
    created_at: float

    @property
    def work_item(self) -> WorkItem:
        return WorkItem(source=self.source, key=self.item, ref=self.item_ref)


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    worker_id: int
    session_uuid: str
    text: str
    entries: tuple[int, ...]
    state: str
    reason: str | None
    created_at: float
    updated_at: float
    taken_at: float | None
    submitted_at: float | None

    @property
    def is_open(self) -> bool:
        return self.state in OUTBOX_OPEN


def _row_to_outbox(row: sqlite3.Row) -> OutboxMessage:
    data = dict(row)
    try:
        entries = json.loads(data["entries"])
    except (TypeError, ValueError):
        entries = []
    if not isinstance(entries, list):
        entries = []
    data["entries"] = tuple(e for e in entries if isinstance(e, int) and not isinstance(e, bool))
    return OutboxMessage(**data)


@dataclass(frozen=True)
class InboxEntry:
    id: int
    worker_id: int
    source: str
    kind: str
    item: str
    item_ref: str
    actor: str
    occurred_at: float
    dedupe_id: str
    pointer: str
    queued_at: float
    delivered_at: float | None
    due_at: float | None = None


def _row_to_subscription(row: Mapping[str, Any]) -> Subscription:
    try:
        kinds = json.loads(row["kinds"])
    except (TypeError, ValueError):
        kinds = []
    return Subscription(
        id=row["id"],
        worker_id=row["worker_id"],
        source=row["source"],
        item=row["item"],
        item_ref=row["item_ref"],
        kinds=tuple(k for k in kinds if isinstance(k, str)) if isinstance(kinds, list) else (),
        is_primary=bool(row["is_primary"]),
        created_at=row["created_at"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        source=row["source"],
        kind=row["kind"],
        item=WorkItem(
            source=row["source"],
            key=row["item"],
            ref=row["item_ref"],
            url=row["item_url"],
            hint=row["item_hint"],
        ),
        actor=row["actor"],
        occurred_at=row["occurred_at"],
        dedupe_id=row["dedupe_id"],
        pointer=row["pointer"],
        marked=bool(row["marked"]),
    )


def _row_to_worker(row: sqlite3.Row) -> Worker:
    return Worker(**{column: row[column] for column in _WORKER_COLUMNS})


class Ledger:
    """A thin, explicit wrapper over the SQLite file. No ORM, no lazy state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.ledger_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path}: ledger schema version {version} is newer than this "
                f"chargehand understands ({SCHEMA_VERSION}); upgrade chargehand"
            )
        if version == SCHEMA_VERSION:
            return
        # One transaction for the schema, the copied rows and the version, because SQLite's
        # DDL and user_version are transactional: a migration that dies half way leaves the
        # old ledger as it was, instead of a renamed table that the next open cannot rename.
        # Every statement of the schema is idempotent: the version 1 copy includes the
        # whole schema, and a version 2 ledger only gains the tables it lacks.
        # A version 2 file takes every later step; the version 1 copy includes the whole
        # schema but keeps its own requests table, which gains the request origin. Up to
        # version 3 the schema creates the prompts table whole, so only 4 to 6 alter it.
        script = {
            1: _MIGRATE_V1_TO_V2 + _MIGRATE_V4_TO_V5 + _MIGRATE_V5_TO_V6,
            2: _MIGRATE_V2_TO_V3 + _MIGRATE_V3_TO_V4 + _MIGRATE_V4_TO_V5 + _MIGRATE_V5_TO_V6
            + _MIGRATE_V7_TO_V8 + _MIGRATE_V8_TO_V9 + _MIGRATE_V9_TO_V10 + _MIGRATE_V2_V10_TO_V11,
            3: _MIGRATE_V3_TO_V4 + _MIGRATE_V4_TO_V5 + _MIGRATE_V5_TO_V6 + _MIGRATE_V7_TO_V8
            + _MIGRATE_V8_TO_V9 + _MIGRATE_V9_TO_V10 + _MIGRATE_V10_TO_V11,
            4: _MIGRATE_V4_TO_V5 + _MIGRATE_V5_TO_V6 + _MIGRATE_V6_TO_V7 + _MIGRATE_V7_TO_V8
            + _MIGRATE_V8_TO_V9 + _MIGRATE_V9_TO_V10 + _MIGRATE_V10_TO_V11,
            5: _MIGRATE_V5_TO_V6 + _MIGRATE_V6_TO_V7 + _MIGRATE_V7_TO_V8 + _MIGRATE_V8_TO_V9
            + _MIGRATE_V9_TO_V10 + _MIGRATE_V10_TO_V11,
            6: _MIGRATE_V6_TO_V7 + _MIGRATE_V7_TO_V8 + _MIGRATE_V8_TO_V9 + _MIGRATE_V9_TO_V10
            + _MIGRATE_V10_TO_V11,
            7: _MIGRATE_V7_TO_V8 + _MIGRATE_V8_TO_V9 + _MIGRATE_V9_TO_V10 + _MIGRATE_V10_TO_V11,
            8: _MIGRATE_V8_TO_V9 + _MIGRATE_V9_TO_V10 + _MIGRATE_V10_TO_V11,
            9: _MIGRATE_V9_TO_V10 + _MIGRATE_V10_TO_V11,
            10: _MIGRATE_V10_TO_V11,
        }.get(version, _SCHEMA)
        try:
            self._conn.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
            )
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ----- workers ----------------------------------------------------------

    def create_worker(
        self,
        *,
        role: str,
        item: str | None,
        item_id: str | None,
        url: str | None,
        repository: str,
        repo: Path,
        worktree: Path,
        branch: str,
        origin: str = ORIGIN_SOURCE,
        start_rule: str | None = None,
        runtime: str = "claude",
        owns_label: int = LABEL_QUEUE,
        now: float | None = None,
    ) -> Worker:
        """Step 0 of the launch sequence: the row exists before any side effect."""
        if origin not in ORIGINS:
            raise ValueError(f"unknown worker origin {origin!r}")
        if int(owns_label) not in LABEL_OWNERS:
            raise ValueError(f"unknown label owner {owns_label!r}")
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            """
            INSERT INTO workers (role, origin, start_rule, runtime, item, item_id, url,
                                 repository, repo, worktree, branch, state, step,
                                 launch_attempts, created_at, updated_at, owns_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
            """,
            (role, origin, start_rule, runtime, item, item_id, url, repository, str(repo),
             str(worktree), branch, LAUNCHING, stamp, stamp, int(owns_label)),
        )
        worker = self.get(int(cursor.lastrowid))
        assert worker is not None
        return worker

    def get(self, worker_id: int) -> Worker | None:
        row = self._conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return _row_to_worker(row) if row else None

    def live_worker_for(self, item: str, role: str | None = None) -> Worker | None:
        """The live worker on *item*, in *role* when given, else in any role."""
        workers = self.live_workers_for(item)
        if role is not None:
            workers = [w for w in workers if w.role == role]
        return workers[0] if workers else None

    def live_workers_for(self, item: str) -> list[Worker]:
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE item = ? AND state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) ORDER BY id",
            (item, *NON_TERMINAL),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def latest_label_owner(self, item: str) -> Worker | None:
        """The newest worker on *item* that drives the queue label."""
        row = self._conn.execute(
            "SELECT * FROM workers WHERE item = ? AND owns_label = ? ORDER BY id DESC LIMIT 1",
            (item, LABEL_QUEUE),
        ).fetchone()
        return _row_to_worker(row) if row else None

    def latest_worker_for(
        self, item: str, role: str | None = None, *, origin: str | None = None
    ) -> Worker | None:
        query, values = "SELECT * FROM workers WHERE item = ?", [item]
        if role is not None:
            query, values = query + " AND role = ?", [*values, role]
        if origin is not None:
            query, values = query + " AND origin = ?", [*values, origin]
        row = self._conn.execute(query + " ORDER BY id DESC LIMIT 1", values).fetchone()
        return _row_to_worker(row) if row else None

    def find_worker(self, target: str, role: str | None = None) -> tuple[Worker | None, str]:
        """The one worker a control command means, or why it cannot tell.

        One rule, applied in this order:

        1. An id handle (`role/item#id`, `role/#id`, `#id`) names the worker with that id;
           its role and item, and *role* when given, must match.
        2. A `role/item` handle, or a bare item with *role*, is a handle. When a worker
           has exactly that handle, the handle wins: the live worker with it first, then
           the newest one with it. Only a start-rule worker has a handle without an id.
        3. Otherwise, by the item (in *role* when known): the one live worker on it; with
           several, the one start-rule worker among them that the bare item names; with
           none live, the newest recorded. A bare item whose one live worker was started
           by hand, while a start-rule worker on the item has ended, is refused: a
           notification about that worker carried the bare item. Anything else is refused,
           with each candidate as its id handle.

        Every handle chargehand prints (:attr:`Worker.target`, :meth:`target_for`)
        resolves back to its worker under this rule.
        """
        shown = sanitize.one_line(target, limit=100) if target else "''"
        by_id = _ID_HANDLE.fullmatch(target)
        if by_id is not None:
            worker = self.get(int(by_id.group(3)))
            if worker is None:
                return None, f"no worker #{by_id.group(3)}"
            named_role = by_id.group(1)
            if named_role is not None and (named_role, by_id.group(2) or None) != (
                worker.role, worker.item
            ):
                return None, f"worker #{worker.id} is {worker.id_handle}, not {shown}"
            if role is not None and role != worker.role:
                return None, f"worker #{worker.id} is in role {worker.role}, not {role}"
            return worker, ""
        item = target
        if "/" in target:
            named_role, _, item = target.partition("/")
            if role is not None and role != named_role:
                return None, f"{shown} is in role {named_role}, not {role}"
            role = named_role
        if role is not None:
            # Rule 2: an exact handle. The unique index allows one live worker with it.
            exact = next(
                (w for w in self.live_workers_for(item)
                 if w.role == role and w.origin == ORIGIN_SOURCE),
                None,
            ) or self.latest_worker_for(item, role, origin=ORIGIN_SOURCE)
            if exact is not None:
                return exact, ""
        live = self.live_workers_for(item)
        if role is not None:
            live = [w for w in live if w.role == role]
        if len(live) > 1:
            named = [w for w in live if w.name == target]
            if len(named) == 1:
                return named[0], ""
            handles = ", ".join(w.id_handle for w in live)
            roles = sorted({w.role for w in live})
            if len(roles) > 1:
                return None, (
                    f"{shown} has live workers in several roles ({', '.join(roles)}); pass "
                    f"--role, or name one of them: {handles}"
                )
            return None, f"{shown} has several live workers; name one of them: {handles}"
        if live:
            only = live[0]
            finished = (
                self.latest_worker_for(item, origin=ORIGIN_SOURCE)
                if role is None and only.origin != ORIGIN_SOURCE else None
            )
            if finished is not None:
                # A notification about that start-rule worker carried the bare item; the
                # one live worker started by hand is not the worker it named.
                return None, (
                    f"{shown} names a start-rule worker that has ended and a worker started "
                    f"by hand; name one of them: {finished.id_handle}, {only.id_handle}"
                )
            return only, ""
        latest = self.latest_worker_for(item, role, origin=ORIGIN_SOURCE) or self.latest_worker_for(
            item, role
        )
        if latest is None:
            return None, f"no worker recorded for {shown}" + (f" in role {role}" if role else "")
        return latest, ""

    def target_for(self, worker: Worker) -> str:
        """The shortest name a command can give *worker* now: its item, else its target.

        The item is used only when the rule resolves it to *worker*, so a hint stays as
        short as before for a worker alone on its item.
        """
        found, _ = self.find_worker(worker.name)
        return worker.name if found is not None and found.id == worker.id else worker.target

    def live_workers(self) -> list[Worker]:
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE state IN ({', '.join('?' * len(NON_TERMINAL))}) "
            f"ORDER BY id",
            NON_TERMINAL,
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def workers_in_state(self, states: Sequence[str]) -> list[Worker]:
        if not states:
            return []
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE state IN ({', '.join('?' * len(states))}) ORDER BY id",
            tuple(states),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def workers_needing_gc(self) -> list[Worker]:
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE gc_done = 0 AND state IN "
            f"({', '.join('?' * len(TERMINAL))}) ORDER BY id",
            TERMINAL,
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def archivable_workers(self, repository: str) -> list[Worker]:
        """The workers of *repository* that an archive would take: every live one, and each
        finished one whose worktree and session are not removed yet, unless archived."""
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE repository = ? AND archived_at IS NULL AND "
            f"(gc_done = 0 OR state IN ({', '.join('?' * len(NON_TERMINAL))})) ORDER BY id",
            (repository, *NON_TERMINAL),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def terminal_workers_since(self, seconds: float = 7 * 24 * 3600) -> list[Worker]:
        """Recently finished workers, for retrying a label write that did not land."""
        rows = self._conn.execute(
            f"SELECT * FROM workers WHERE state IN ({', '.join('?' * len(TERMINAL))}) "
            f"AND (finished_at IS NULL OR finished_at >= ?) ORDER BY id",
            (*TERMINAL, time.time() - seconds),
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def recent_workers(self, limit: int = 50) -> list[Worker]:
        rows = self._conn.execute(
            "SELECT * FROM workers ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_worker(row) for row in rows]

    def worker_by_session_id(self, session_id: str) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
        return _row_to_worker(row) if row else None

    def worker_by_worktree(self, worktree: str) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE worktree = ? ORDER BY id DESC LIMIT 1", (worktree,)
        ).fetchone()
        return _row_to_worker(row) if row else None

    def live_count_by_role(self) -> dict[str, int]:
        rows = self._conn.execute(
            f"SELECT role, COUNT(*) AS n FROM workers WHERE state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) GROUP BY role",
            NON_TERMINAL,
        ).fetchall()
        return {row["role"]: int(row["n"]) for row in rows}

    def update(self, worker: Worker, **changes: Any) -> Worker:
        """Write *changes* to the worker's row and return the updated value."""
        if not changes:
            return worker
        changes = self._checked(changes)
        assignments = ", ".join(f"{column} = ?" for column in changes)
        self._conn.execute(
            f"UPDATE workers SET {assignments} WHERE id = ?",
            (*changes.values(), worker.id),
        )
        self._after_state_write(worker, changes)
        return replace(worker, **changes)

    def update_if_unchanged(self, worker: Worker, **changes: Any) -> Worker | None:
        """Like :meth:`update`, but only while the row has *worker*'s state and last hook.

        None when the state changed, or a session hook was applied, since *worker* was
        read: the daemon applies hooks on its request threads while the tick runs.
        """
        if not changes:
            return worker
        changes = self._checked(changes)
        assignments = ", ".join(f"{column} = ?" for column in changes)
        cursor = self._conn.execute(
            f"UPDATE workers SET {assignments} WHERE id = ? AND state = ? AND last_hook_at IS ?",
            (*changes.values(), worker.id, worker.state, worker.last_hook_at),
        )
        if not cursor.rowcount:
            return None
        self._after_state_write(worker, changes)
        return replace(worker, **changes)

    def _after_state_write(self, worker: Worker, changes: Mapping[str, Any]) -> None:
        # Every state write but `blocked` means the session no longer shows the dialogs the
        # writer knew of: the worker left `blocked`, its turn or session ended, or a new
        # turn or session started (a wake, a continue). For a write of `running`, a prompt
        # recorded after *worker* was read came from the new turn while the action ran, and
        # stays open: a hook touches the row before it records a prompt, so a row read
        # later knows of it. The end of a launch (`launching` -> `running`) cancels
        # nothing: the first dialog of a session can come before the tick confirms it.
        # A question that the ask tool recorded is no dialog of the session: a write of
        # `running` cancels it only once the turn that asked it has ended (it is settled),
        # because a permission prompt answered later in the same turn also writes `running`.
        if "state" not in changes:
            return
        state = changes["state"]
        if state == BLOCKED or (worker.state == LAUNCHING and state == RUNNING):
            return
        if state != RUNNING:
            self.resolve_permission_prompts(worker, now=changes["updated_at"])
            return
        self.resolve_permission_prompts(
            worker, now=changes["updated_at"], recorded_by=worker.updated_at,
            kind=PROMPT_PERMISSION,
        )
        self._conn.execute(
            "UPDATE permission_prompts SET state = ?, resolved_by = ?, resolved_at = ? "
            "WHERE worker_id = ? AND kind = ? AND state = ? AND released_at IS NOT NULL",
            (PROMPT_CANCELLED, RESOLVED_BY_SESSION, changes["updated_at"], worker.id,
             PROMPT_QUESTION, PROMPT_OPEN),
        )

    @staticmethod
    def _checked(changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - set(_WORKER_COLUMNS)
        if unknown:
            raise ValueError(f"unknown worker column(s): {', '.join(sorted(unknown))}")
        if "state" in changes and changes["state"] not in WORKER_STATES:
            raise ValueError(f"unknown worker state: {changes['state']!r}")
        changes.setdefault("updated_at", time.time())
        if changes.get("state") in TERMINAL and "finished_at" not in changes:
            changes["finished_at"] = changes["updated_at"]
        if "state" in changes and changes["state"] != PARKED:
            # A reset time belongs to `parked`. A worker that leaves it by any path, such
            # as a turn that the operator started and that ended, keeps no old one.
            changes.setdefault("reset_at", None)
        return changes

    def live_worker_by_session_uuid(self, session_uuid: str) -> Worker | None:
        row = self._conn.execute(
            f"SELECT * FROM workers WHERE session_uuid = ? AND state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) ORDER BY id DESC LIMIT 1",
            (session_uuid, *NON_TERMINAL),
        ).fetchone()
        return _row_to_worker(row) if row else None

    # ----- permission prompts -----------------------------------------------

    def record_permission_prompt(
        self,
        worker: Worker,
        *,
        tool_name: str,
        tool_input: str,
        now: float | None = None,
        input_digest: str | None = None,
        resolved_by: str | None = None,
    ) -> int:
        """Record an open prompt, or with *resolved_by* one that is already allowed."""
        stamp = time.time() if now is None else now
        if resolved_by is None:
            cursor = self._conn.execute(
                "INSERT INTO permission_prompts (worker_id, tool_name, tool_input, created_at, "
                "input_digest) VALUES (?, ?, ?, ?, ?)",
                (worker.id, tool_name, tool_input, stamp, input_digest),
            )
        else:
            cursor = self._conn.execute(
                "INSERT INTO permission_prompts (worker_id, tool_name, tool_input, created_at, "
                "input_digest, state, decision, resolved_by, resolved_at, released_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (worker.id, tool_name, tool_input, stamp, input_digest, PROMPT_RESOLVED, ALLOW,
                 resolved_by, stamp, stamp),
            )
        return int(cursor.lastrowid)

    def get_permission_prompt(self, prompt_id: int) -> PermissionPrompt | None:
        row = self._conn.execute(
            "SELECT * FROM permission_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        return PermissionPrompt(**dict(row)) if row else None

    def open_permission_prompts(self, worker: Worker | None = None) -> list[PermissionPrompt]:
        """The open prompts, of *worker* or of every live worker."""
        if worker is not None:
            rows = self._conn.execute(
                "SELECT * FROM permission_prompts WHERE worker_id = ? AND state = ? ORDER BY id",
                (worker.id, PROMPT_OPEN),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT p.* FROM permission_prompts p JOIN workers w ON w.id = p.worker_id "
                f"WHERE p.state = ? AND w.state IN "
                f"({', '.join('?' * len(NON_TERMINAL))}) ORDER BY p.id",
                (PROMPT_OPEN, *NON_TERMINAL),
            ).fetchall()
        return [PermissionPrompt(**dict(row)) for row in rows]

    def resolve_permission_prompts(
        self,
        worker: Worker,
        *,
        now: float | None = None,
        recorded_by: float | None = None,
        kind: str | None = None,
    ) -> int:
        """Cancel the worker's open prompts, or those recorded by *recorded_by*, or those of
        *kind*: its session no longer shows them."""
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            "UPDATE permission_prompts SET state = ?, resolved_by = ?, resolved_at = ?, "
            "released_at = COALESCE(released_at, ?) WHERE worker_id = ? AND state = ? "
            "AND (? IS NULL OR created_at <= ?) AND (? IS NULL OR kind = ?)",
            (PROMPT_CANCELLED, RESOLVED_BY_SESSION, stamp, stamp, worker.id, PROMPT_OPEN,
             recorded_by, recorded_by, kind, kind),
        )
        return cursor.rowcount

    def record_question(
        self, worker: Worker, question: str, options: Sequence[str], *, now: float | None = None
    ) -> int:
        """Record an open question of *worker* with its *options*; it replaces an older one.

        The question is a prompt of kind `question`: the board shows it in the same card as
        a permission prompt, and the operator's answer resolves it. No hook waits for it.
        Its `released_at` says when the turn that asked it ended (:meth:`settle_questions`).
        """
        stamp = time.time() if now is None else now
        with self.transaction():
            self.resolve_permission_prompts(worker, now=stamp, kind=PROMPT_QUESTION)
            cursor = self._conn.execute(
                "INSERT INTO permission_prompts (worker_id, tool_name, tool_input, created_at, "
                "kind, options) VALUES (?, ?, ?, ?, ?, ?)",
                (worker.id, "ask", question, stamp, PROMPT_QUESTION, json.dumps(list(options))),
            )
        return int(cursor.lastrowid)

    def settle_questions(self, worker: Worker, *, now: float | None = None) -> None:
        """Record that the turn which asked the worker's open questions has ended."""
        self._conn.execute(
            "UPDATE permission_prompts SET released_at = ? WHERE worker_id = ? AND kind = ? "
            "AND state = ? AND released_at IS NULL",
            (time.time() if now is None else now, worker.id, PROMPT_QUESTION, PROMPT_OPEN),
        )

    def answer_questions(self, worker: Worker, *, by: str, now: float | None = None) -> int:
        """Resolve every open question of *worker*: a reply answers them."""
        cursor = self._conn.execute(
            "UPDATE permission_prompts SET state = ?, resolved_by = ?, resolved_at = ? "
            "WHERE worker_id = ? AND kind = ? AND state = ?",
            (PROMPT_RESOLVED, by, time.time() if now is None else now, worker.id,
             PROMPT_QUESTION, PROMPT_OPEN),
        )
        return cursor.rowcount

    def answer_question(self, prompt_id: int, *, by: str, now: float | None = None) -> bool:
        """Resolve an open question. False when it was answered or cancelled before."""
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            "UPDATE permission_prompts SET state = ?, resolved_by = ?, resolved_at = ? "
            "WHERE id = ? AND kind = ? AND state = ?",
            (PROMPT_RESOLVED, by, stamp, prompt_id, PROMPT_QUESTION, PROMPT_OPEN),
        )
        return cursor.rowcount == 1

    def cancel_permission_prompt(self, prompt_id: int, *, now: float | None = None) -> bool:
        """Cancel one open prompt that its session no longer shows. False when it was not open."""
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            "UPDATE permission_prompts SET state = ?, resolved_by = ?, resolved_at = ?, "
            "released_at = COALESCE(released_at, ?) WHERE id = ? AND state = ?",
            (PROMPT_CANCELLED, RESOLVED_BY_SESSION, stamp, stamp, prompt_id, PROMPT_OPEN),
        )
        return cursor.rowcount == 1

    def set_prompt_digest(self, prompt_id: int, input_digest: str) -> None:
        self._conn.execute(
            "UPDATE permission_prompts SET input_digest = ? WHERE id = ?", (input_digest, prompt_id)
        )

    def answer_permission_prompt(
        self,
        prompt_id: int,
        decision: str,
        *,
        by: str,
        rule: bool = False,
        now: float | None = None,
    ) -> bool:
        """Resolve an open prompt that a hook still holds. False when another answer came first.

        The first writer wins: the prompt must be open and not released, so a second click,
        an answer after a timeout and an answer after the session moved on change nothing.
        With *rule*, the same tool with the same input is allowed for this worker from now on.
        """
        stamp = time.time() if now is None else now
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE permission_prompts SET state = ?, decision = ?, resolved_by = ?, "
                "resolved_at = ? WHERE id = ? AND state = ? AND released_at IS NULL",
                (PROMPT_RESOLVED, decision, by, stamp, prompt_id, PROMPT_OPEN),
            )
            if cursor.rowcount != 1:
                return False
            if rule:
                prompt = self.get_permission_prompt(prompt_id)
                if prompt is None or not prompt.input_digest:
                    raise ValueError(f"prompt {prompt_id} has no input digest for a rule")
                self._conn.execute(
                    "INSERT OR IGNORE INTO permission_rules (worker_id, tool_name, input_digest, "
                    "prompt_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (prompt.worker_id, prompt.tool_name, prompt.input_digest, prompt_id, stamp),
                )
        return True

    def release_permission_prompt(self, prompt_id: int, *, now: float | None = None) -> bool:
        """Mark an open prompt as held by no hook. False when an answer came first."""
        cursor = self._conn.execute(
            "UPDATE permission_prompts SET released_at = ? "
            "WHERE id = ? AND state = ? AND released_at IS NULL",
            (time.time() if now is None else now, prompt_id, PROMPT_OPEN),
        )
        return cursor.rowcount == 1

    def has_permission_rule(self, worker: Worker, tool_name: str, input_digest: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM permission_rules WHERE worker_id = ? AND tool_name = ? "
            "AND input_digest = ?",
            (worker.id, tool_name, input_digest),
        ).fetchone()
        return row is not None

    def permission_rules(self, worker: Worker) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM permission_rules WHERE worker_id = ? ORDER BY created_at",
            (worker.id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----- sessions ---------------------------------------------------------

    def record_session(
        self,
        worker: Worker,
        *,
        kind: str,
        session_id: str | None,
        session_uuid: str | None,
        now: float | None = None,
    ) -> None:
        """Remember a session the worker owned. Idempotent per session id."""
        if session_id and self._conn.execute(
            "SELECT 1 FROM sessions WHERE worker_id = ? AND session_id = ?",
            (worker.id, session_id),
        ).fetchone():
            return
        self._conn.execute(
            "INSERT INTO sessions (worker_id, session_id, session_uuid, kind, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (worker.id, session_id, session_uuid, kind, time.time() if now is None else now),
        )

    def record_wake(
        self,
        worker: Worker,
        *,
        session_id: str | None,
        session_uuid: str | None,
        now: float | None = None,
    ) -> None:
        """One row per wake. Not idempotent: a resume keeps the session id."""
        self._conn.execute(
            "INSERT INTO sessions (worker_id, session_id, session_uuid, kind, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (worker.id, session_id, session_uuid, SESSION_WAKE, time.time() if now is None else now),
        )

    def wakes_since(self, worker: Worker, since: float) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE worker_id = ? AND kind = ? AND started_at >= ?",
                (worker.id, SESSION_WAKE, since),
            ).fetchone()[0]
        )

    def sessions_of(self, worker: Worker) -> list[SessionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE worker_id = ? ORDER BY id", (worker.id,)
        ).fetchall()
        return [SessionRecord(**dict(row)) for row in rows]

    # ----- subscriptions and the inbox --------------------------------------

    def subscribe(
        self,
        worker: Worker,
        item: WorkItem,
        kinds: Iterable[str],
        *,
        primary: bool = False,
        now: float | None = None,
    ) -> Subscription:
        """Subscribe *worker* to *item*. Subscribing again replaces the kinds."""
        self._conn.execute(
            """
            INSERT INTO subscriptions (worker_id, source, item, item_ref, kinds, is_primary,
                                       created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (worker_id, source, item) DO UPDATE SET
                kinds = excluded.kinds,
                item_ref = excluded.item_ref,
                is_primary = MAX(is_primary, excluded.is_primary)
            """,
            (worker.id, item.source, item.key, item.ref, json.dumps(list(kinds)),
             1 if primary else 0, time.time() if now is None else now),
        )
        row = self._conn.execute(
            "SELECT * FROM subscriptions WHERE worker_id = ? AND source = ? AND item = ?",
            (worker.id, item.source, item.key),
        ).fetchone()
        return _row_to_subscription(row)

    def subscriptions_of(self, worker: Worker) -> list[Subscription]:
        rows = self._conn.execute(
            "SELECT * FROM subscriptions WHERE worker_id = ? ORDER BY id", (worker.id,)
        ).fetchall()
        return [_row_to_subscription(row) for row in rows]

    def primary_subscription(self, worker: Worker) -> Subscription | None:
        row = self._conn.execute(
            "SELECT * FROM subscriptions WHERE worker_id = ? AND is_primary = 1 "
            "ORDER BY id LIMIT 1",
            (worker.id,),
        ).fetchone()
        return _row_to_subscription(row) if row else None

    def live_subscriptions(self, source: str, item: str) -> list[tuple[Subscription, Worker]]:
        """The subscriptions of live workers to one item, each with its worker."""
        rows = self._conn.execute(
            f"SELECT w.*, s.id AS s_id, s.worker_id AS s_worker_id, s.source AS s_source, "
            f"s.item AS s_item, s.item_ref AS s_item_ref, s.kinds AS s_kinds, "
            f"s.is_primary AS s_is_primary, s.created_at AS s_created_at "
            f"FROM subscriptions s JOIN workers w ON w.id = s.worker_id "
            f"WHERE s.source = ? AND s.item = ? AND w.state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) ORDER BY s.id",
            (source, item, *NON_TERMINAL),
        ).fetchall()
        return [
            (
                _row_to_subscription(
                    {key: row[f"s_{key}"] for key in (
                        "id", "worker_id", "source", "item", "item_ref", "kinds", "is_primary",
                        "created_at",
                    )}
                ),
                _row_to_worker(row),
            )
            for row in rows
        ]

    def subscribed_items(self, source: str) -> list[WorkItem]:
        """The union of the items that live workers subscribe to on one source."""
        rows = self._conn.execute(
            f"SELECT DISTINCT s.item, s.item_ref FROM subscriptions s "
            f"JOIN workers w ON w.id = s.worker_id WHERE s.source = ? AND w.state IN "
            f"({', '.join('?' * len(NON_TERMINAL))}) ORDER BY s.item",
            (source, *NON_TERMINAL),
        ).fetchall()
        return [WorkItem(source=source, key=row["item"], ref=row["item_ref"]) for row in rows]

    def enqueue(self, worker_id: int, event: Event, *, now: float | None = None) -> bool:
        """Put *event* into one worker's inbox. False when it is there already."""
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO inbox (worker_id, source, kind, item, item_ref, actor,
                                         occurred_at, dedupe_id, pointer, queued_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (worker_id, event.source, event.kind, event.item.key, event.item.ref, event.actor,
             event.occurred_at, event.dedupe_id, event.pointer,
             time.time() if now is None else now),
        )
        return cursor.rowcount > 0

    def inbox(
        self, worker: Worker, *, pending_only: bool = True, now: float | None = None
    ) -> list[InboxEntry]:
        """The worker's inbox; with *pending_only*, the entries that a wake may take now:
        not delivered, and due."""
        query = "SELECT * FROM inbox WHERE worker_id = ?"
        params: list[object] = [worker.id]
        if pending_only:
            query += " AND delivered_at IS NULL AND (due_at IS NULL OR due_at <= ?)"
            params.append(time.time() if now is None else now)
        rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
        return [InboxEntry(**dict(row)) for row in rows]

    def put_timer(
        self, worker: Worker, *, due_at: float, item: str, item_ref: str, now: float | None = None
    ) -> int:
        """Put a timer that is due at *due_at* into the worker's inbox, instead of the one
        that waits."""
        stamp = time.time() if now is None else now
        with self.transaction():
            self._conn.execute(
                "DELETE FROM inbox WHERE worker_id = ? AND source = ? AND kind = ? "
                "AND delivered_at IS NULL",
                (worker.id, TIMER_SOURCE, TIMER_KIND),
            )
            cursor = self._conn.execute(
                """
                INSERT INTO inbox (worker_id, source, kind, item, item_ref, actor, occurred_at,
                                   dedupe_id, pointer, queued_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (worker.id, TIMER_SOURCE, TIMER_KIND, item, item_ref, TIMER_SOURCE, stamp,
                 f"timer:{stamp!r}", TIMER_POINTER, stamp, due_at),
            )
        return int(cursor.lastrowid)

    def pending_timer(self, worker: Worker) -> InboxEntry | None:
        row = self._conn.execute(
            "SELECT * FROM inbox WHERE worker_id = ? AND source = ? AND kind = ? "
            "AND delivered_at IS NULL ORDER BY id DESC LIMIT 1",
            (worker.id, TIMER_SOURCE, TIMER_KIND),
        ).fetchone()
        return InboxEntry(**dict(row)) if row else None

    def delivered_at(self, worker: Worker, at: float) -> list[InboxEntry]:
        """The inbox entries of *worker* that one wake delivered: those marked at *at*."""
        rows = self._conn.execute(
            "SELECT * FROM inbox WHERE worker_id = ? AND delivered_at = ? ORDER BY id",
            (worker.id, at),
        ).fetchall()
        return [InboxEntry(**dict(row)) for row in rows]

    def mark_delivered(self, entries: Iterable[InboxEntry], *, now: float | None = None) -> None:
        ids = [entry.id for entry in entries]
        if not ids:
            return
        self._conn.execute(
            f"UPDATE inbox SET delivered_at = ? WHERE delivered_at IS NULL AND id IN "
            f"({', '.join('?' * len(ids))})",
            (time.time() if now is None else now, *ids),
        )

    # ----- the outbox ----------------------------------------------------------

    def outbox_message(self, worker: Worker) -> OutboxMessage | None:
        """The open outbox message of *worker*, pending or taken, or None."""
        row = self._conn.execute(
            "SELECT * FROM outbox WHERE worker_id = ? AND state IN (?, ?) ORDER BY id DESC LIMIT 1",
            (worker.id, *OUTBOX_OPEN),
        ).fetchone()
        return _row_to_outbox(row) if row else None

    def get_outbox(self, message_id: int) -> OutboxMessage | None:
        row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (message_id,)).fetchone()
        return _row_to_outbox(row) if row else None

    def put_outbox(
        self,
        worker: Worker,
        *,
        session_uuid: str,
        text: str,
        entries: Iterable[int],
        now: float | None = None,
    ) -> OutboxMessage | None:
        """The pending message for *worker*'s session, with *text* and *entries*.

        A pending message gets the new text and entries; an open message of another
        session is dropped first. None while the session's mod holds a taken one: new
        events wait for the next wake.
        """
        stamp = time.time() if now is None else now
        ids = json.dumps(sorted(set(entries)))
        with self.transaction():
            current = self.outbox_message(worker)
            if current is not None and current.session_uuid != session_uuid:
                self._drop(current.id, "the session changed", stamp)
                current = None
            if current is not None and current.state == OUTBOX_TAKEN:
                return None
            if current is not None:
                self._conn.execute(
                    "UPDATE outbox SET text = ?, entries = ?, updated_at = ? WHERE id = ?",
                    (text, ids, stamp, current.id),
                )
                message_id = current.id
            else:
                message_id = self._conn.execute(
                    "INSERT INTO outbox (worker_id, session_uuid, text, entries, state, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (worker.id, session_uuid, text, ids, OUTBOX_PENDING, stamp, stamp),
                ).lastrowid
        return self.get_outbox(int(message_id))

    def take_outbox(
        self,
        worker: Worker,
        *,
        session_uuid: str,
        retake_after: float,
        now: float | None = None,
    ) -> OutboxMessage | None:
        """Hand the open message of *session_uuid* to its mod, or None when there is none.

        A pending message becomes taken. A taken one is handed out again once
        *retake_after* seconds have passed without a confirm: the mod that took it may have
        been reloaded before it submitted the text.
        """
        stamp = time.time() if now is None else now
        # The mod of every live session polls; an empty outbox takes no write lock.
        current = self.outbox_message(worker)
        if current is None or current.session_uuid != session_uuid:
            return None
        if current.state == OUTBOX_TAKEN and (current.taken_at or 0.0) > stamp - retake_after:
            return None
        # Only as read: a delivery or a second poll that changed it in between wins.
        cursor = self._conn.execute(
            "UPDATE outbox SET state = ?, taken_at = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND taken_at IS ? AND updated_at = ?",
            (OUTBOX_TAKEN, stamp, stamp, current.id, current.state, current.taken_at,
             current.updated_at),
        )
        return self.get_outbox(current.id) if cursor.rowcount else None

    def submit_outbox(self, message_id: int, now: float | None = None) -> OutboxMessage | None:
        """Mark a taken message submitted. The message when this call did it, else None."""
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            "UPDATE outbox SET state = ?, submitted_at = ?, updated_at = ? "
            "WHERE id = ? AND state = ?",
            (OUTBOX_SUBMITTED, stamp, stamp, message_id, OUTBOX_TAKEN),
        )
        return self.get_outbox(message_id) if cursor.rowcount else None

    def drop_outbox(self, message_id: int, reason: str, now: float | None = None) -> bool:
        """Drop an open message, so its entries wait for the next wake; False when it was not open."""
        return self._drop(message_id, reason, time.time() if now is None else now)

    def _drop(self, message_id: int, reason: str, now: float) -> bool:
        cursor = self._conn.execute(
            "UPDATE outbox SET state = ?, reason = ?, updated_at = ? "
            "WHERE id = ? AND state IN (?, ?)",
            (OUTBOX_DROPPED, reason[:300], now, message_id, *OUTBOX_OPEN),
        )
        return bool(cursor.rowcount)

    def outbox_workers(self) -> set[int]:
        """The ids of the workers whose open outbox message may still start a turn.

        A paused or ended worker gets no wake from its outbox, so its message holds no
        working slot.
        """
        waking = [state for state in NON_TERMINAL if state != PAUSED]
        rows = self._conn.execute(
            f"SELECT DISTINCT o.worker_id FROM outbox o JOIN workers w ON w.id = o.worker_id "
            f"WHERE o.state IN (?, ?) AND w.state IN ({', '.join('?' * len(waking))})",
            (*OUTBOX_OPEN, *waking),
        ).fetchall()
        return {int(row["worker_id"]) for row in rows}

    def stale_outbox(self) -> list[OutboxMessage]:
        """The open messages of workers that are paused or ended: none of them goes out."""
        rows = self._conn.execute(
            f"SELECT o.* FROM outbox o JOIN workers w ON w.id = o.worker_id "
            f"WHERE o.state IN (?, ?) AND w.state IN ({', '.join('?' * (1 + len(TERMINAL)))}) "
            f"ORDER BY o.id",
            (*OUTBOX_OPEN, PAUSED, *TERMINAL),
        ).fetchall()
        return [_row_to_outbox(row) for row in rows]

    def inbox_entries(self, worker: Worker, ids: Iterable[int]) -> list[InboxEntry]:
        """The inbox rows of *worker* with *ids* that are not delivered yet."""
        wanted = list(ids)
        if not wanted:
            return []
        rows = self._conn.execute(
            f"SELECT * FROM inbox WHERE worker_id = ? AND delivered_at IS NULL AND id IN "
            f"({', '.join('?' * len(wanted))}) ORDER BY id",
            (worker.id, *wanted),
        ).fetchall()
        return [InboxEntry(**dict(row)) for row in rows]

    # ----- seen events and cursors ------------------------------------------

    def event_seen(self, source: str, dedupe_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM events WHERE source = ? AND dedupe_id = ?", (source, dedupe_id)
        ).fetchone() is not None

    def record_event(
        self, event: Event, *, start_pending: bool = False, now: float | None = None
    ) -> None:
        """Remember that the router handled *event*, so the dedupe filter drops it next time."""
        self._conn.execute(
            """
            INSERT INTO events (source, dedupe_id, kind, item, item_ref, item_url, item_hint,
                                actor, occurred_at, pointer, marked, seen_at, start_pending)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, dedupe_id) DO UPDATE SET start_pending = excluded.start_pending
            """,
            (event.source, event.dedupe_id, event.kind, event.item.key, event.item.ref,
             event.item.url, event.item.hint, event.actor, event.occurred_at, event.pointer,
             1 if event.marked else 0, time.time() if now is None else now,
             1 if start_pending else 0),
        )

    def pending_start_events(self) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE start_pending = 1 ORDER BY seen_at, rowid"
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def settle_start(self, event: Event) -> None:
        self._conn.execute(
            "UPDATE events SET start_pending = 0 WHERE source = ? AND dedupe_id = ?",
            (event.source, event.dedupe_id),
        )

    def prune_events(
        self, *, older_than_secs: float = 30 * 24 * 3600, pending_secs: float = 7 * 24 * 3600
    ) -> tuple[int, int]:
        """Forget old events; give up a start deferred for longer than *pending_secs*.

        Returns how many events were forgotten, and how many of them were deferred starts.
        """
        now = time.time()
        condition = "seen_at < ? OR (start_pending = 1 AND seen_at < ?)"
        values = (now - older_than_secs, now - pending_secs)
        given_up = self._conn.execute(
            f"SELECT COUNT(*) FROM events WHERE start_pending = 1 AND ({condition})", values
        ).fetchone()[0]
        cursor = self._conn.execute(f"DELETE FROM events WHERE {condition}", values)
        return cursor.rowcount, int(given_up)

    def get_cursor(self, source: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM cursors WHERE source = ?", (source,)
        ).fetchone()
        return row["value"] if row else None

    def set_cursor(self, source: str, value: str | None) -> None:
        self._conn.execute(
            "INSERT INTO cursors (source, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (source, value, time.time()),
        )

    # ----- control requests -------------------------------------------------

    def record_request(
        self, kind: str, target: str | None = None, *, origin: str = REQUEST_CLI, **args: Any
    ) -> int:
        if origin not in REQUEST_ORIGINS:
            raise ValueError(f"unknown request origin {origin!r}")
        cursor = self._conn.execute(
            "INSERT INTO requests (kind, target, args, created_at, origin) VALUES (?, ?, ?, ?, ?)",
            (kind, target, json.dumps(args, sort_keys=True), time.time(), origin),
        )
        return int(cursor.lastrowid)

    def record_applied_request(
        self, kind: str, target: str | None = None, *, origin: str, ok: bool, message: str,
        **args: Any,
    ) -> int:
        """A request that its caller applied itself, recorded for the audit trail only.

        It is complete from the start, so no tick applies it again.
        """
        with self.transaction():
            request_id = self.record_request(kind, target, origin=origin, **args)
            self.complete_request(request_id, ok=ok, message=message)
        return request_id

    def pending_requests(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE applied_at IS NULL ORDER BY id"
        ).fetchall()
        return [self._request_to_dict(row) for row in rows]

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._request_to_dict(row) if row else None

    def complete_request(self, request_id: int, *, ok: bool, message: str) -> None:
        self._conn.execute(
            "UPDATE requests SET applied_at = ?, ok = ?, message = ? WHERE id = ?",
            (time.time(), 1 if ok else 0, message, request_id),
        )

    @staticmethod
    def _request_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["args"] = json.loads(data["args"])
        except (TypeError, ValueError):
            data["args"] = {}
        return data

    def prune_requests(self, older_than_secs: float = 7 * 24 * 3600) -> int:
        cursor = self._conn.execute(
            "DELETE FROM requests WHERE applied_at IS NOT NULL AND applied_at < ?",
            (time.time() - older_than_secs,),
        )
        return cursor.rowcount

    # ----- jobs -------------------------------------------------------------

    def create_job(
        self,
        kind: str,
        target: str,
        *,
        state: str = JOB_QUEUED,
        result_path: str | None = None,
        progress: str | None = None,
        now: float | None = None,
        **args: Any,
    ) -> Job:
        if state not in JOB_ACTIVE:
            raise ValueError(f"a job starts in one of {', '.join(JOB_ACTIVE)}, not {state!r}")
        stamp = time.time() if now is None else now
        cursor = self._conn.execute(
            "INSERT INTO jobs (kind, target, args, state, progress, result_path, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target, json.dumps(args, sort_keys=True), state, progress, result_path,
             stamp, stamp),
        )
        job = self.get_job(int(cursor.lastrowid))
        assert job is not None
        return job

    def get_job(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def jobs_in_state(self, states: Sequence[str], kind: str | None = None) -> list[Job]:
        if not states:
            return []
        query = f"SELECT * FROM jobs WHERE state IN ({', '.join('?' * len(states))})"
        values: list[Any] = list(states)
        if kind is not None:
            query, values = query + " AND kind = ?", [*values, kind]
        rows = self._conn.execute(query + " ORDER BY id", values).fetchall()
        return [_row_to_job(row) for row in rows]

    def jobs_of(self, kind: str, target: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND target = ? ORDER BY id", (kind, target)
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def active_job(self, kind: str, target: str) -> Job | None:
        return next((job for job in self.jobs_of(kind, target) if job.is_active), None)

    def recent_jobs(self, since: float) -> list[Job]:
        """Every active job, and those that ended at or after *since*."""
        rows = self._conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({', '.join('?' * len(JOB_ACTIVE))}) "
            f"OR finished_at >= ? ORDER BY id",
            (*JOB_ACTIVE, since),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def update_job(self, job: Job, *, expect: str | None = None, **changes: Any) -> Job | None:
        """Write *changes* to the job. With *expect*, only while the job is in that state.

        None when *expect* did not match: the tick and the job thread both move jobs, and
        each move starts from a state that only one of them leaves.
        """
        unknown = set(changes) - {"state", "progress", "result_path", "error", "started_at",
                                  "finished_at", "args"}
        if unknown:
            raise ValueError(f"unknown job column(s): {', '.join(sorted(unknown))}")
        if "state" in changes and changes["state"] not in JOB_STATES:
            raise ValueError(f"unknown job state: {changes['state']!r}")
        changes.setdefault("updated_at", time.time())
        if changes.get("state") in (JOB_DONE, JOB_FAILED):
            changes.setdefault("finished_at", changes["updated_at"])
        values = {
            key: json.dumps(value, sort_keys=True) if key == "args" else value
            for key, value in changes.items()
        }
        assignments = ", ".join(f"{column} = ?" for column in values)
        query, params = f"UPDATE jobs SET {assignments} WHERE id = ?", [*values.values(), job.id]
        if expect is not None:
            query, params = query + " AND state = ?", [*params, expect]
        cursor = self._conn.execute(query, params)
        if not cursor.rowcount:
            return None
        return self.get_job(job.id)

    # ----- runner state -----------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO runner_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM runner_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def delete_state(self, key: str) -> None:
        self._conn.execute("DELETE FROM runner_state WHERE key = ?", (key,))

    def all_state(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM runner_state").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ----- pause ------------------------------------------------------------

    GLOBAL_PAUSE_KEY = "paused"

    def is_paused(self, repository: str | None = None) -> bool:
        if self.get_state(self.GLOBAL_PAUSE_KEY) == "1":
            return True
        if repository is not None:
            return self.get_state(f"paused:{repository}") == "1"
        return False

    def set_paused(self, paused: bool, repository: str | None = None) -> None:
        key = self.GLOBAL_PAUSE_KEY if repository is None else f"paused:{repository}"
        if paused:
            self.set_state(key, "1")
        else:
            self.delete_state(key)

    def paused_repositories(self) -> list[str]:
        return sorted(
            key.split(":", 1)[1]
            for key, value in self.all_state().items()
            if key.startswith("paused:") and value == "1"
        )

    # ----- board state --------------------------------------------------------

    @staticmethod
    def _board_field(kind: str, field: str) -> None:
        if kind not in BOARD_FIELDS:
            raise ValueError(f"unknown board state kind {kind!r}")
        if field not in BOARD_FIELDS[kind]:
            raise ValueError(f"unknown board state field {field!r} for a {kind}")

    def board_state(self) -> list[dict[str, Any]]:
        """Every board state row, as `kind`, `key`, `field`, `value` and `updated_at`."""
        rows = self._conn.execute(
            "SELECT kind, key, field, value, updated_at FROM board_state ORDER BY kind, key, field"
        ).fetchall()
        return [dict(row) for row in rows]

    def set_board_state(
        self, kind: str, key: str, field: str, value: str, *, now: float | None = None
    ) -> None:
        self._board_field(kind, field)
        self._conn.execute(
            "INSERT INTO board_state (kind, key, field, value, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key, field) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (kind, key, field, value, time.time() if now is None else now),
        )

    def delete_board_state(self, kind: str, key: str, field: str) -> None:
        self._board_field(kind, field)
        self._conn.execute(
            "DELETE FROM board_state WHERE kind = ? AND key = ? AND field = ?", (kind, key, field)
        )


def open_ledger(path: Path | None = None) -> Ledger:
    return Ledger(path)


def summarize(workers: Iterable[Worker]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for worker in workers:
        counts[worker.state] = counts.get(worker.state, 0) + 1
    return counts


__all__ = [
    "REQUEST_ORIGINS",
    "Worker",
    "SessionRecord",
    "Subscription",
    "InboxEntry",
    "OutboxMessage",
    "PermissionPrompt",
    "Job",
    "Ledger",
    "open_ledger",
    "summarize",
    "closing",
    *WORKER_STATES,
]
