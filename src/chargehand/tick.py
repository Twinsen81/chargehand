"""The tick: the only place side effects happen.

Order, once per tick:

1. reconcile interrupted launches, from three sides
2. apply recorded control requests
3. run the watchdog
4. subscribe each worker to the pull request of its branch, once one exists
5. poll the event sources and route their events: inboxes, closes, then start rules
6. diff session states against the ledger and notify on change
7. poll each repository's tracker and route its events the same way, up to the starts
8. deliver: wake each idle worker once for the events in its inbox
9. sync the tracker labels and the status of source items
10. admit the tracker's starts: the queue label is a start rule, launched within the
    caps, ledger row first
11. collect garbage

The tick reaches a tracker only as a source (:class:`TrackerSource`). Step 7 comes before
delivery, so a comment on a worker's issue wakes it in the same tick. Step 10 comes after
the state diff so that a slot freed by a finished worker is used in the same tick, and
after delivery, so a waiting worker gets a free slot before the queue starts a new one; a
start from a ``[[source]]`` launches earlier, in step 5.

A control command never calls a runtime, git, or a tracker itself; it records a
request and the tick applies it. Together with the tick lock that gives one writer of
side effects, so a cancel cannot race a launch in progress.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from chargehand import roles as roles_mod
from chargehand import config as config_mod
from chargehand import gitutil, ledger as ledger_mod, paths, sanitize, trackers
from chargehand import delivery as delivery_mod
from chargehand import router as router_mod
from chargehand import runtime as runtime_mod
from chargehand import sources as sources_mod
from chargehand.config import MachineConfig, RepoConfig, Repository
from chargehand.errors import (
    AgentRuntimeError,
    AmbiguousWrite,
    ChargehandError,
    ConfigError,
    DeliveryAborted,
    GitError,
    ItemGone,
    LaunchAborted,
    TickBusy,
    TrackerError,
)
from chargehand.ledger import Ledger, Worker
from chargehand.notify import Notification, Notifier
from chargehand.roles import Role, StartRule
from chargehand.runtime import Hook, LaunchSpec, Runtime, Session
from chargehand.sources import Event, Source, WorkItem
from chargehand.sources.tracker import TrackerSource
from chargehand.trackers import Status

log = logging.getLogger(__name__)

# Transient "this repository cannot be talked to right now" conditions: an unreachable API and
# an unreadable credential alike. Deliberately NOT ConfigError — a broken template or a
# bad setup command must fail the worker loudly rather than inherit the forgiveness an
# outage gets, or a typo would retry forever while holding a launch slot.
TRACKER_FAILURES = (TrackerError,)

# A repository's tracker keeps its poll cursor in the ledger under this prefix and the
# repository's name: several repositories may use trackers of one type.
TRACKER_CURSOR_PREFIX = "tracker:"

# watchdog_notified doubles as a small state: 0 nothing, 1 long-run notified,
# 2 a hard stop was attempted and did not take.
WATCHDOG_LONG_RUN = 1
WATCHDOG_STOP_FAILED = 2

# A worker whose branch got a pull request is subscribed to it once; this runner-state
# key, with the worker id, records that the check found one.
FOLLOWS_PULL_REQUEST_PREFIX = "follows-pr:"

# What wakes a worker on the pull request of its own branch. Not `pr_closed`: the pull
# request is not the worker's primary item, so its close does not end the worker.
PULL_REQUEST_KINDS = (roles_mod.COMMENT, "review", "ci_status")

# A role with `checkout = "item"` gets a second, detached checkout of its item's head, made
# by its setup step, at <worktree_root>/<ITEM_CHECKOUTS>/<role>/<item>. The session never
# runs there: the head is the item author's content, and Claude Code would load its
# settings, hooks and instructions from a working directory it trusts. A role name starts
# with a letter or digit, so this directory never holds a worker's worktree.
ITEM_CHECKOUTS = ".items"
# The commit the item checkout was on after the setup step is kept under this prefix and
# the worker id, so that garbage collection does not count the item's own commits as
# unpushed work.
CHECKOUT_HEAD_PREFIX = "checkout-head:"
# The files the runtime loads from the session's own worktree, as the setup step left
# them, for the same workers: a JSON object of path to content digest. A wake checks that
# the worktree is still on the worker's branch and that these files are as recorded, so a
# session that moved the item's head, or its settings or instructions, into its own
# working directory is not resumed there. `continue --force` records them again.
WORKSPACE_FILES_PREFIX = "workspace-files:"
WORKER_STATE_PREFIXES = (CHECKOUT_HEAD_PREFIX, WORKSPACE_FILES_PREFIX)

# A start event for a repository that no `[[repository]]` names is ignored. It is reported
# once per source and repository; this prefix marks the ones reported.
UNCONFIGURED_PREFIX = "unconfigured:"

# The notification detail of a worker that a usage limit parked.
USAGE_LIMIT = "usage limit reached; waits for the reset"

# How many times a session hook reads its worker again when the tick changed the row
# between the read and the write.
HOOK_WRITE_ATTEMPTS = 3

# The worker states a session hook never moves: a launch in progress sets its own state
# when its session appears, and a paused worker waits for the operator.
HOOK_FROZEN_STATES = (ledger_mod.LAUNCHING, ledger_mod.PAUSED)


class _RowChanged(Exception):
    """Another thread changed the worker's row between a read and the state write."""


# The notification detail of a worker whose turn ended without a question.
TURN_ENDED = "turn ended; waits for events"

# How much of a control request's outcome the CLI prints. An outcome that must carry a
# hint keeps within it.
REQUEST_MESSAGE_LIMIT = 300

# A finished worker's last notification, when its hook failed, waits in the runner state
# under this prefix and the worker id, and is sent again for up to this long.
PENDING_NOTIFICATION_PREFIX = "notify-pending:"
PENDING_NOTIFICATION_MAX_AGE_SECS = 24 * 3600

# Names no branch, because the session chose the name and this text reaches the terminal,
# the tick log, and whichever assistant ran the command.
KEPT_BRANCH = (
    "the worktree was on a branch the runner did not create, so that branch was kept; "
    "`git branch` in the repository lists it"
)

# The queue status each worker state should be reflected by on the tracker, or on the
# item of a `[[source]]` that marks items.
_STATUS_FOR_STATE = {
    ledger_mod.LAUNCHING: Status.RUNNING,
    ledger_mod.RUNNING: Status.RUNNING,
    # A turn ended: the operator may need to look, as before workers could wait for events.
    ledger_mod.IDLE: Status.BLOCKED,
    ledger_mod.QUEUED: Status.BLOCKED,
    ledger_mod.BLOCKED: Status.BLOCKED,
    ledger_mod.PAUSED: Status.BLOCKED,
    ledger_mod.PARKED: Status.BLOCKED,
    ledger_mod.FAILED: Status.BLOCKED,
    ledger_mod.CANCELLED: Status.BLOCKED,
    ledger_mod.DONE: Status.DONE,
}


def source_failure(name: str, exc: object) -> str:
    """*exc* as one line that names the source *name* once.

    A source's own error text often starts with its name already, and the tick adds the
    name in front of every source error.
    """
    text = sanitize.one_line(str(exc), limit=300)
    for prefix in (f"source {name}: ", f"{name}: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return f"source {name}: {text}"


def item_checkout_path(worker: Worker) -> Path:
    """Where a `checkout = "item"` role keeps its item's head, next to the worktree root."""
    worktree = worker.worktree_path
    return worktree.parent.parent / ITEM_CHECKOUTS / worktree.parent.name / worktree.name


@dataclass(frozen=True)
class WorkspaceCheck:
    """Whether a worker's own worktree may host its session again.

    *fault* is chargehand's own text and may reach a notification. *changed* names the
    recorded files that differ, for the operator; *acceptable* says whether recording the
    files as they are now, with `continue --force`, would clear the fault.
    """

    fault: str | None = None
    changed: tuple[str, ...] = ()
    acceptable: bool = False


def changed_entries(recorded: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    """The paths whose record differs from *current*, each with what happened to it."""
    changes = []
    for name in sorted(set(recorded) | set(current)):
        before, now = recorded.get(name), current.get(name)
        if before == now:
            continue
        if before is None:
            what = "new"
        elif now is None or now == "missing":
            what = "gone"
        else:
            what = "changed"
        changes.append(f"{name} ({what})")
    return changes


def trusted_program(name: str, roots: Sequence[Path]) -> str | None:
    """The program a setup command names, found where the operator's files are; None if not.

    A relative name is looked up under *roots* first. A bare name is then looked up in the
    absolute entries of PATH only; a relative name with a directory part is not looked up
    further, because the process would resolve it against its working directory.
    """
    program = Path(name)
    if program.is_absolute():
        return name
    for root in roots:
        candidate = root / program
        if candidate.exists():
            return str(candidate)
    if "/" in name or name in (".", ".."):
        return None
    search = os.pathsep.join(
        entry for entry in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(entry)
    )
    return shutil.which(name, path=search)


def hardened_git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """*environ* with git's hooks and fsmonitor turned off, through git's config env.

    For a subprocess that itself runs git, such as `gh pr checkout`, where a command-line
    `-c` cannot reach. A relative `core.hooksPath`, or a `core.fsmonitor` set to a
    program, would otherwise run code a checkout the session can write controls. Added on
    top of any `GIT_CONFIG_*` already in the environment.
    """
    env = dict(environ)
    try:
        index = max(int(env.get("GIT_CONFIG_COUNT") or 0), 0)
    except ValueError:
        index = 0
    for key, value in (("core.hooksPath", os.devnull), ("core.fsmonitor", "false")):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
        index += 1
    env["GIT_CONFIG_COUNT"] = str(index)
    return env


@contextmanager
def tick_lock(path: Path | None = None, *, wait_secs: float = 0.0):
    """Serialize ticks.

    The daemon holds it while each of its ticks runs, and `chargehand tick` and a control
    command with no daemon to apply it take it too, so the invariant is enforced here
    rather than assumed.
    The kernel releases a flock when its holder dies, so a killed tick needs no recovery.
    """
    target = path or paths.tick_lock_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + wait_secs
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TickBusy("another chargehand tick is running") from None
                time.sleep(0.2)
        try:
            os.write(handle, b"")
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


@dataclass
class TickReport:
    started_at: float = field(default_factory=time.time)
    launched: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    routed: list[str] = field(default_factory=list)
    woken: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The phase a stop request ended the tick before; None for a tick that ran every phase.
    stopped_before: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at,
            "duration_secs": round(time.time() - self.started_at, 3),
            "launched": self.launched,
            "adopted": self.adopted,
            "resumed": self.resumed,
            "failed": self.failed,
            "transitions": self.transitions,
            "notified": self.notified,
            "requests": self.requests,
            "routed": self.routed,
            "woken": self.woken,
            "collected": self.collected,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "errors": self.errors,
            "stopped_before": self.stopped_before,
        }


@dataclass
class RepositoryContext:
    repository: Repository
    # The repository's tracker, reached only as a source.
    source: TrackerSource | None = None
    repo_config: RepoConfig | None = None
    error: str | None = None
    roles: dict[str, Role | ConfigError] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.source is not None and self.repo_config is not None and self.error is None

    def item_of(self, worker: Worker) -> WorkItem:
        """The worker's own item as this repository's tracker names it."""
        return WorkItem(
            source=self.repository.tracker.type,
            key=worker.item or "",
            ref=worker.item_id or worker.item or "",
            url=worker.url,
            hint=self.repository.name,
        )

    def role(self, name: str) -> Role:
        """The role *name* as this repository defines it, loaded once per tick.

        A role that does not load is a configuration error for every worker in it, so
        the error is kept and raised again rather than the file being read on each call.
        """
        cached = self.roles.get(name)
        if cached is None:
            try:
                cached = roles_mod.load_role(
                    self.repository.path,
                    name,
                    repo_config=self.repo_config,
                    tracker_type=self.source.type if self.source else None,
                )
            except ConfigError as exc:
                cached = exc
            self.roles[name] = cached
        if isinstance(cached, ConfigError):
            raise cached
        return cached


@dataclass
class StatusFile:
    state: str | None = None
    stop_reason: str | None = None
    pr_url: str | None = None
    present: bool = False
    stale: bool = False

    @classmethod
    def read(cls, path: Path, *, not_before: float | None = None) -> "StatusFile":
        """The status-file protocol: how a repository disambiguates a `done` session.

        Anything the file says is untrusted; only the small closed vocabulary of
        `state` is acted on, and `stop_reason` is never printed by default.
        """
        try:
            # The path is keyed by issue, so a previous worker's file is still sitting
            # there. Acting on it would end a live run on a verdict about a dead one.
            if not_before is not None and path.stat().st_mtime < not_before:
                return cls(present=True, stale=True)
            raw = path.read_text(encoding="utf-8", errors="replace")[:64_000]
        except (OSError, ValueError):
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls(present=True)
        if not isinstance(data, dict):
            return cls(present=True)
        state = data.get("state")
        state = state.strip().lower() if isinstance(state, str) else None
        if state not in ("needs-input", "complete", "aborted"):
            state = None
        return cls(
            state=state,
            stop_reason=(
                sanitize.one_line(str(data["stop_reason"]))
                if isinstance(data.get("stop_reason"), str)
                else None
            ),
            pr_url=sanitize.safe_url(data.get("pr_url")),
            present=True,
        )


class Runner:
    """Holds the collaborators a tick needs. Every one of them is injectable for tests."""

    def __init__(
        self,
        config: MachineConfig,
        ledger: Ledger,
        *,
        runtimes: Mapping[str, Runtime] | None = None,
        git: gitutil.Git | None = None,
        notifier: Notifier | None = None,
        tracker_factory=trackers.build,
        source_factory=sources_mod.build,
        usage: Callable[[], delivery_mod.UsageReading | None] | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self._runtimes: dict[str, Runtime] = dict(runtimes or {})
        self.git = git or gitutil.Git(config.git_bin)
        self.notifier = notifier or Notifier(config.notify)
        self._tracker_factory = tracker_factory
        self._source_factory = source_factory
        # The provider's usage windows for the wake reserve. None until a reading exists.
        self._usage = usage or (lambda: None)
        self._sources: dict[str, Source | Exception | None] = {}
        self._contexts: dict[str, RepositoryContext] = {}
        # Which starts this step tries: a `[[source]]`'s in the route step, a tracker's in
        # the admission step. Each waiting start is tried once per tick.
        self._trackers_turn = False
        # The repositories whose tracker this tick polled. A queue start of any other
        # waits, with no tracker call: its poll failed and was reported once.
        self._polled_trackers: set[str] = set()
        self._sessions: dict[str, list[Session]] = {}
        # When each runtime's listing was asked for. A session hook that the daemon
        # applied after that time is newer than the listing, and the diff leaves it be.
        self._listed_at: dict[str, float] = {}

    # ----- collaborators ----------------------------------------------------

    def runtime(self, name: str) -> Runtime:
        """The runtime *name*, as the machine configuration sets it up. ConfigError when unknown."""
        cached = self._runtimes.get(name)
        if cached is None:
            cached = runtime_mod.create(name, self.config)
            self._runtimes[name] = cached
        return cached

    def runtime_names(self) -> list[str]:
        """Every runtime that may hold a session of ours: the default, the live workers', the roles'."""
        names = {runtime_mod.DEFAULT_RUNTIME, *self._runtimes}
        names.update(worker.runtime for worker in self.ledger.live_workers())
        for repository in self.config.repositories:
            context = self.context(repository)
            if context.repo_config is None:
                continue
            for role_name in repository.roles:
                try:
                    names.add(context.role(role_name).runtime)
                except ConfigError:
                    continue
        return sorted(names)

    def sessions(self, runtime: str, *, refresh: bool = False) -> list[Session]:
        if refresh or runtime not in self._sessions:
            asked_at = time.time()
            try:
                self._sessions[runtime] = self.runtime(runtime).list_sessions()
                self._listed_at[runtime] = asked_at
            except AgentRuntimeError as exc:
                log.error("could not list the sessions of runtime %s: %s", runtime, exc)
                raise
        return self._sessions[runtime]

    def invalidate_sessions(self, runtime: str | None = None) -> None:
        if runtime is None:
            self._sessions.clear()
        else:
            self._sessions.pop(runtime, None)

    def context(self, repository: Repository) -> RepositoryContext:
        cached = self._contexts.get(repository.name)
        if cached is not None:
            return cached
        context = RepositoryContext(repository=repository)
        try:
            context.source = TrackerSource(self._tracker_factory(repository), repository)
            context.repo_config = config_mod.load_repo_config(repository.path)
        except (ConfigError, TrackerError) as exc:
            context.error = str(exc)
        self._contexts[repository.name] = context
        return context

    def context_for_worker(self, worker: Worker) -> RepositoryContext | None:
        repository = self.config.repository_by_name(worker.repository)
        return self.context(repository) if repository else None

    def source(self, name: str) -> Source | None:
        """The configured source *name*, built once per tick; None when it is not usable."""
        if name not in self._sources:
            config = self.config.source_by_name(name)
            try:
                built = self._source_factory(config) if config else None
                if built is not None:
                    built.use_repositories(self.config.repositories)
                self._sources[name] = built
            except ChargehandError as exc:
                # One source that cannot be built must not keep the others from polling.
                # The route step reports it; that error line is its record.
                log.debug("source %s could not be built: %s", name, exc)
                self._sources[name] = exc
            except Exception as exc:
                log.exception("source %s could not be built", name)
                self._sources[name] = exc
        found = self._sources[name]
        return found if isinstance(found, Source) else None

    def _source_failed(
        self, report: TickReport, name: str, exc: Exception, doing: str = "poll"
    ) -> None:
        """Report a source's failure as one error line, with a traceback only when unexpected.

        An expected failure, such as an outage, a timeout or a command that exits
        non-zero, comes back on every tick while it lasts. The report's error line, which
        the tick prints, is its one record; a traceback would add nothing to it.
        """
        message = source_failure(name, exc)
        if isinstance(exc, ChargehandError):
            log.debug("%s", message)
        else:
            log.exception("source %s: %s failed", name, doing)
        report.errors.append(message)

    def tracker_context(self, event: Event) -> RepositoryContext | None:
        """The repository whose tracker produced *event*; None for a ``[[source]]`` event.

        A tracker's items carry the repository's name as their hint, and their source is
        the tracker type.
        """
        if not TrackerSource.produced(event):
            return None
        hint = event.item.hint
        repository = self.config.repository_by_name(hint) if hint else None
        if repository is None or repository.tracker.type != event.source:
            return None
        return self.context(repository)

    # ----- the tick ---------------------------------------------------------

    def tick(
        self,
        *,
        crash_after_step: int | None = None,
        crash_after_wake: bool = False,
        stop: Callable[[], bool] | None = None,
    ) -> TickReport:
        """Run every phase once.

        *stop* is asked before each phase. Once it answers true the phase in progress has
        already ended, and the remaining ones are skipped: the daemon shuts down between
        two phases rather than in the middle of a launch or a wake.
        """
        report = TickReport()
        for context in self._contexts.values():
            if context.source is not None:
                context.source.forget_queue()
        for name in self.runtime_names():
            try:
                self.sessions(name, refresh=True)
            except AgentRuntimeError as exc:
                # Without the session listing nothing below can be decided safely.
                report.errors.append(f"{name}: {exc}")
                return report
            except ConfigError as exc:
                # A worker or a role names a runtime this build does not have. Its own
                # workers fail where they use it; the others go on.
                report.errors.append(str(exc))

        phases = (
            ("reconcile", lambda: self._reconcile(report)),
            ("requests", lambda: self._apply_requests(report)),
            ("watchdog", lambda: self._watchdog(report)),
            ("follow", lambda: self._follow_pull_requests(report)),
            ("route", lambda: self._route_events(report, crash_after_step=crash_after_step)),
            ("supervise", lambda: self._diff_and_notify(report)),
            ("trackers", lambda: self._route_trackers(report)),
            ("deliver", lambda: self._deliver(report, crash_after_wake=crash_after_wake)),
            ("labels", lambda: self._sync_all_labels(report)),
            ("admit", lambda: self._admit(report, crash_after_step=crash_after_step)),
            ("collect", lambda: self._collect_garbage(report)),
        )
        for name, phase in phases:
            if stop is not None and stop():
                log.info("stopping: the tick ends before phase %s", name)
                report.stopped_before = name
                break
            try:
                phase()
            except (LaunchAborted, DeliveryAborted):
                raise
            except Exception as exc:
                log.exception("tick phase %s failed", name)
                report.errors.append(f"phase {name} failed: {exc}")
        self.ledger.prune_requests()
        return report

    # ----- 1. reconciliation ------------------------------------------------

    def _reconcile(self, report: TickReport) -> None:
        self._reconcile_ledger(report)
        self._reconcile_sessions(report)
        self._reconcile_tracker(report)

    def _reconcile_ledger(self, report: TickReport) -> None:
        """A row still `launching` when a tick starts is an interrupted launch."""
        for worker in self.ledger.workers_in_state([ledger_mod.LAUNCHING]):
            context = self.context_for_worker(worker)
            if context is None:
                self._fail_worker(
                    worker, None, f"repository '{worker.repository}' is no longer configured", report,
                    notify_detail="repository is no longer configured",
                )
                continue
            if not context.usable:
                report.errors.append(f"repository {worker.repository}: {context.error}")
                continue

            # Same guard the launch uses: a previous worker's stopped session keeps this
            # item's name and worktree, and must not be adopted as this one's.
            session = self._find_worker_session(
                worker, exclude=self._sessions_owned_by_other_workers(worker)
            )
            if session is not None:
                self._adopt(worker, session, report)
                continue

            if worker.launch_attempts >= self.config.max_launch_attempts:
                self._fail_worker(
                    worker,
                    context,
                    f"launch did not complete after {worker.launch_attempts} launch attempts "
                    f"(stopped at step {worker.step})",
                    report,
                    notify_detail="launch did not complete",
                )
                continue

            before = worker.launch_attempts
            worker = self.ledger.update(worker, launch_attempts=before + 1)
            report.resumed.append(f"{worker.item}@step{worker.step}")
            log.info(
                "resuming launch of %s from step %s (worker %s)",
                worker.item, worker.step, worker.launch_attempts,
            )
            try:
                self._run_launch_steps(context, worker, report)
            except LaunchAborted:
                raise
            except TRACKER_FAILURES as exc:
                # A tracker outage is not this worker's fault. Give the retry back,
                # otherwise three unlucky ticks fail a launch that never went wrong.
                self.ledger.update(
                    worker, launch_attempts=before, last_error=str(exc)[:500]
                )
                report.errors.append(f"{worker.item}: {exc}")
            except ChargehandError as exc:
                # Never silently re-queue: that would loop. Leave it launching and let
                # the worker counter fail it loudly.
                self.ledger.update(worker, last_error=str(exc)[:500])
                report.errors.append(f"{worker.item}: {exc}")

    def _reconcile_sessions(self, report: TickReport) -> None:
        """No session under the worktree root runs outside the watchdog."""
        for name in self.runtime_names():
            try:
                sessions = self.sessions(name)
            except ConfigError:
                continue
            self._reconcile_runtime_sessions(name, sessions, report)

    def _reconcile_runtime_sessions(
        self, runtime: str, sessions: list[Session], report: TickReport
    ) -> None:
        root = self.config.worktree_root
        live = [w for w in self.ledger.live_workers() if w.runtime == runtime]
        known = {str(Path(w.worktree)) for w in live} | {w.session_id for w in live if w.session_id}
        for session in sessions:
            if session.id in known or (session.cwd and str(Path(session.cwd)) in known):
                continue
            if not self._under(root, session.cwd):
                continue
            derived = runtime_mod.worker_from_session(session, root)
            by_id = self.ledger.worker_by_session_id(session.id)
            if by_id is not None and by_id.runtime != runtime:
                by_id = None
            previous = by_id or (
                self.ledger.latest_worker_for(derived[1], derived[0]) if derived else None
            )
            # `cancel` stops a session but keeps its worktree, so a stopped session
            # belonging to a worker that already ended is expected, not an orphan.
            # Only one that is still working needs supervising again.
            if (
                previous is not None
                and previous.is_terminal
                and session.state != runtime_mod.WORKING
            ):
                continue
            if derived is None:
                report.warnings.append(
                    f"unadopted session {session.id} under {root} with no derivable worker"
                )
                key = f"orphan-notified:{session.id}"
                if not self.ledger.get_state(key):
                    self.ledger.set_state(key, "1")
                    self._notify(report, "?", "orphan-session", None, None,
                                 detail=f"session {session.id}")
                continue
            role, item = derived
            if self.ledger.live_worker_for(item, role) is not None:
                continue
            adopted = self._adopt_orphan(runtime, role, item, session, report, previous=previous)
            if adopted is None:
                report.warnings.append(
                    f"session {session.id} looks like {role}/{item} but no configured "
                    f"repository and role own {session.cwd}"
                )

    def _reconcile_tracker(self, report: TickReport) -> None:
        """A "running" label with no live row means the ledger and the tracker disagree."""
        for repository in self.config.repositories:
            context = self.context(repository)
            if not context.usable:
                if context.error:
                    report.errors.append(f"repository {repository.name}: {context.error}")
                continue
            source = context.source
            assert source is not None
            try:
                running = source.marked(Status.RUNNING)
            except TRACKER_FAILURES as exc:
                report.errors.append(f"repository {repository.name}: {exc}")
                continue
            for item in running:
                if self.ledger.live_worker_for(item.key) is not None:
                    continue
                if self._label_sync_pending(item.key):
                    # The ledger knows what this label should be and has not managed to
                    # write it yet. That is a pending write, not a disagreement.
                    continue
                report.warnings.append(
                    f"{item.key} carries the running label but has no live worker"
                )
                try:
                    source.mark(item, Status.BLOCKED)
                except (*TRACKER_FAILURES, ItemGone) as exc:
                    report.errors.append(f"{item.key}: {exc}")
                    continue
                self._notify(report, item.key, "blocked", item.url, repository.name,
                             detail="running label without a live worker")

    def _stop_session(self, worker: Worker) -> bool:
        """Stop the session and confirm it stopped, by re-reading the listing.

        The exit code alone is not enough: a stop can report success and leave the
        session running. Everything destructive downstream — removing a worktree, hard
        stopping a run — depends on the session really being gone.
        """
        if not worker.session_id:
            return True
        self.runtime(worker.runtime).stop(worker.session_id)
        self.invalidate_sessions(worker.runtime)
        try:
            sessions = self.sessions(worker.runtime, refresh=True)
        except AgentRuntimeError:
            return False
        session = next((s for s in sessions if s.id == worker.session_id), None)
        if session is None:
            return True
        return self.runtime(worker.runtime).is_stopped(session)

    def _remove_session(self, worker: Worker) -> bool:
        if not worker.session_id:
            return True
        self.runtime(worker.runtime).remove(worker.session_id)
        self.invalidate_sessions(worker.runtime)
        try:
            sessions = self.sessions(worker.runtime, refresh=True)
        except AgentRuntimeError:
            return False
        return all(s.id != worker.session_id for s in sessions)

    def _adopt(self, worker: Worker, session: Session, report: TickReport) -> Worker:
        worker = self.ledger.update(
            worker,
            session_id=session.id,
            session_uuid=session.uuid,
            state=ledger_mod.RUNNING,
            step=ledger_mod.LAST_STEP,
            session_state=session.state,
            working_since=worker.working_since or time.time(),
            last_error=None,
        )
        self.ledger.record_session(
            worker, kind=ledger_mod.SESSION_ADOPTED, session_id=session.id, session_uuid=session.uuid
        )
        report.adopted.append(worker.item)
        log.info("adopted session %s for %s", session.id, worker.handle)
        return worker

    def _adopt_orphan(
        self,
        runtime: str,
        role_name: str,
        item: str,
        session: Session,
        report: TickReport,
        *,
        previous: Worker | None = None,
    ) -> Worker | None:
        """Bring a session started by a tick that died before recording ids under supervision.

        The new row inherits *previous*'s label ownership and primary subscription, so a
        worker that a source event started does not become the label's owner by being
        adopted. With no previous row, the role's own start rules decide. The subscription
        keeps its time, because the session has run since then and the router delivers no
        event from before a worker's start. The row gets the time of the adoption, so a
        status file that the earlier run wrote stays stale.
        """
        worktree = Path(session.cwd) if session.cwd else None
        if worktree is None:
            return None
        repository = self._repository_owning(worktree)
        if repository is None or role_name not in repository.roles:
            return None
        context = self.context(repository)
        if previous is not None:
            owns_label = previous.owns_label
        elif self._queue_starts(context, role_name):
            owns_label = ledger_mod.LABEL_QUEUE
        else:
            owns_label = ledger_mod.LABEL_NONE
        item_id, url = item, None
        if previous is not None:
            item_id, url = previous.item_id or item, previous.url
        elif owns_label == ledger_mod.LABEL_QUEUE and context.source is not None:
            try:
                found = context.source.find(item)
            except TRACKER_FAILURES:
                found = None
            if found is not None:
                item_id, url = found.ref, found.url
        branch = self.git.worktree_branch(repository.path, worktree) or self._branch_for(
            context.repo_config, role_name, item
        )
        primary = self.ledger.primary_subscription(previous) if previous else None
        with self.ledger.transaction():
            worker = self.ledger.create_worker(
                role=role_name,
                item=item,
                item_id=item_id,
                url=url,
                repository=repository.name,
                repo=repository.path,
                worktree=worktree,
                branch=branch,
                owns_label=owns_label,
                runtime=runtime,
            )
            if primary is not None:
                self.ledger.subscribe(
                    worker, primary.work_item, primary.kinds, primary=True,
                    now=primary.created_at,
                )
        worker = self._adopt(worker, session, report)
        report.warnings.append(f"adopted unsupervised session {session.id} as {worker.handle}")
        return worker

    @staticmethod
    def _queue_starts(context: RepositoryContext, role_name: str) -> bool:
        """Whether the queue label starts workers of this role. Unknown counts as no."""
        if not context.usable:
            return False
        try:
            role = context.role(role_name)
        except ConfigError:
            return False
        tracker_type = context.repository.tracker.type
        return role.start_rule_for(roles_mod.LABELED, tracker_type, context.repository.name) is not None

    def _repository_owning(self, worktree: Path) -> Repository | None:
        for repository in self.config.repositories:
            try:
                entries = self.git.list_worktrees(repository.path)
            except GitError:
                continue
            if str(worktree) in entries or str(worktree.resolve()) in entries:
                return repository
        return None

    @staticmethod
    def _under(root: Path, candidate: str | None) -> bool:
        if not candidate:
            return False
        try:
            Path(candidate).resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return False
        return True

    # ----- 2. control requests ----------------------------------------------

    def _apply_requests(self, report: TickReport) -> None:
        for request in self.ledger.pending_requests():
            kind = request["kind"]
            target = request["target"]
            args = request["args"]
            try:
                ok, message = self._apply_request(kind, target, args, report)
            except ChargehandError as exc:
                ok, message = False, str(exc)
            except Exception as exc:  # a bad request must not kill the tick
                log.exception("request %s failed", request["id"])
                ok, message = False, f"internal error: {exc}"
            self.ledger.complete_request(request["id"], ok=ok, message=message)
            report.requests.append(
                f"{kind}{f' {target}' if target else ''}: {'ok' if ok else 'refused'} — {message}"
            )

    def _apply_request(
        self, kind: str, target: str | None, args: dict, report: TickReport
    ) -> tuple[bool, str]:
        if kind == "tick":
            return True, "tick run"
        if kind in ("pause", "resume"):
            self.ledger.set_paused(kind == "pause", target)
            scope = f"repository {target}" if target else "all repositories"
            return True, f"{scope} {'paused' if kind == 'pause' else 'resumed'}"
        if target is None:
            return False, f"{kind} needs a work item"

        role = args.get("role") or None
        found, problem = self._resolve_target(target, role)
        if found is None:
            return False, problem or f"no worker recorded for {target}"
        worker = found
        context = self.context_for_worker(worker)

        if kind == "cancel":
            return self._do_cancel(worker, context, report)
        if kind == "stop":
            return self._do_stop(worker, context, report)
        if kind == "continue":
            return self._do_continue(worker, context, bool(args.get("force")), report)
        if kind == "retry":
            return self._do_retry(worker, context, bool(args.get("force")), report)
        if kind == "discard":
            return self._do_discard(worker, context, bool(args.get("force")), report)
        return False, f"unknown request '{kind}'"

    def _resolve_target(self, item: str, role: str | None) -> tuple[Worker | None, str | None]:
        """The one worker a control command means, or why it cannot tell.

        Several roles can hold the same item, so a bare item is enough only while one
        live worker has it; otherwise the command has to name the role.
        """
        live = self.ledger.live_workers_for(item)
        if role is not None:
            live = [w for w in live if w.role == role]
        if len(live) > 1:
            names = ", ".join(sorted(w.role for w in live))
            return None, f"{item} has live workers in several roles ({names}); pass --role"
        if live:
            return live[0], None
        latest = self.ledger.latest_worker_for(item, role)
        if latest is None:
            return None, f"no worker recorded for {item}" + (f" in role {role}" if role else "")
        return latest, None

    def _do_cancel(
        self, worker: Worker, context: RepositoryContext | None, report: TickReport
    ) -> tuple[bool, str]:
        if worker.is_terminal:
            return False, f"{worker.item} is already {worker.state}"
        if not self._stop_session(worker):
            return False, (
                f"{worker.item}: the session did not stop; it is still working. "
                f"Check it with `{self._attach_command(worker)}`."
            )
        worker = self.ledger.update(worker, state=ledger_mod.CANCELLED, session_state=None)
        self._sync_labels(worker, context, report)
        self._notify(report, worker.item, "cancelled", worker.url, worker.repository, role=worker.role,
                     detail="cancelled by operator")
        return True, "session stopped, worktree kept"

    def _do_stop(
        self, worker: Worker, context: RepositoryContext | None, report: TickReport
    ) -> tuple[bool, str]:
        if worker.is_terminal:
            return False, f"{worker.item} is already {worker.state}"
        if not worker.session_id:
            return False, f"{worker.item} has no session to stop"
        if not self._stop_session(worker):
            return False, f"{worker.item}: the session did not stop; it is still working"
        worker = self.ledger.update(worker, state=ledger_mod.PAUSED)
        self._sync_labels(worker, context, report)
        return True, "session stopped; `chargehand continue` respawns it"

    def _do_continue(
        self, worker: Worker, context: RepositoryContext | None, force: bool, report: TickReport
    ) -> tuple[bool, str]:
        """Respawn the session. *force* accepts the agent's files in the worktree as they are."""
        if not worker.session_id:
            return False, f"{worker.item} has no session to respawn"
        if worker.state not in (ledger_mod.PAUSED, ledger_mod.BLOCKED, ledger_mod.PARKED):
            return False, f"{worker.item} is {worker.state}, not paused, blocked or parked"
        check = self._workspace_check(worker, accept_files=force)
        if check.fault is not None:
            return False, self._refusal_message(worker, check)
        result = self.runtime(worker.runtime).respawn(worker.session_id)
        self.invalidate_sessions(worker.runtime)
        if result.returncode != 0:
            return False, f"respawn failed: {sanitize.one_line(result.stderr or result.stdout, limit=200)}"
        # Same rule as stopping: confirm by re-reading rather than trusting the exit code.
        session = next(
            (s for s in self.sessions(worker.runtime, refresh=True) if s.id == worker.session_id),
            None,
        )
        if session is None:
            return False, f"{worker.item}: respawn reported success but the session is gone"
        if self.runtime(worker.runtime).is_stopped(session):
            # A stopped session keeps the state of its last turn, often `done`, which says
            # nothing about why the respawn did not take.
            return False, (
                f"{worker.item}: respawn reported success but no process is running "
                f"(the session lists as {session.state})"
            )
        worker = self.ledger.update(
            worker, state=ledger_mod.RUNNING, working_since=time.time(), watchdog_notified=0,
            reset_at=None,
        )
        self.ledger.resolve_permission_prompts(worker)
        self._sync_labels(worker, context, report)
        return True, "session respawned"

    def _refusal_message(self, worker: Worker, check: WorkspaceCheck) -> str:
        """The `continue` refusal, within what the CLI prints, keeping the remedy whole.

        An acceptable fault points at `--force`; any other at `discard`, the only remedy
        for a worktree off its branch or an item checkout that is gone. The changed
        paths, which the session chose, are cleaned and take only the room that is left.
        """
        if check.acceptable:
            hint = (
                f"Check the worktree, then `chargehand continue {worker.item} --force` "
                f"to accept the files, or `chargehand discard {worker.item} --yes`."
            )
        else:
            hint = (
                f"Check the worktree, then `chargehand discard {worker.item} --yes`; the "
                f"next review request starts a fresh worker."
            )
        prefix, tail = f"{worker.item}: ", f". {hint}"
        budget = REQUEST_MESSAGE_LIMIT - len(prefix) - len(tail)
        detail = check.fault
        room = budget - len(detail) - len(" ()")
        if check.changed and room >= 20:
            detail = f"{detail} ({sanitize.one_line(', '.join(check.changed[:3]), limit=room)})"
        detail = sanitize.one_line(detail, limit=max(budget, 1))
        return sanitize.one_line(f"{prefix}{detail}{tail}", limit=REQUEST_MESSAGE_LIMIT)

    def _do_retry(
        self, worker: Worker, context: RepositoryContext | None, force: bool, report: TickReport
    ) -> tuple[bool, str]:
        """Re-arm the issue and let ordinary admission start a fresh worker."""
        if not worker.is_terminal:
            return False, (
                f"{worker.item} is {worker.state}; cancel or stop it before retrying"
            )
        if context is None or not context.usable:
            return False, f"repository '{worker.repository}' is unavailable"
        branches_removed = True
        if worker.worktree_path.exists():
            blocker = self._worktree_holds_work(worker)
            if blocker and not force:
                return False, (
                    f"{worker.item}: {blocker}. Push or discard it first "
                    f"(`chargehand discard {worker.item} --yes`)."
                )
            try:
                branches_removed = self._remove_worktree(worker, force=True)
            except GitError as exc:
                return False, f"could not remove the previous worktree: {exc}"
        # The previous session is kept by `stop`, and it still carries this issue's name
        # and working directory. Left in place it would be adopted as the retry's own
        # session, so the retry would report success without starting anything.
        if worker.session_id:
            self._stop_session(worker)
            if not self._remove_session(worker):
                return False, (
                    f"{worker.item}: the previous session {worker.session_id} could not be "
                    f"removed; a new worker would adopt it instead of starting. Remove it, "
                    f"then retry."
                )
            worker = self.ledger.update(worker, session_id=None, session_uuid=None)
        if not self._drives_label(worker):
            # The queue label would start the queue's role, not this one. A new start
            # event for the item starts a new worker of this role.
            self.ledger.update(worker, gc_done=1)
            outcome = "cleared; the next start event for the item starts a new worker"
            return True, outcome if branches_removed else f"{outcome}; {KEPT_BRANCH}"
        try:
            context.source.mark(context.item_of(worker), Status.QUEUED)  # type: ignore[union-attr]
        except ItemGone:
            return False, f"{worker.item} no longer exists on the tracker"
        except TRACKER_FAILURES as exc:
            return False, f"could not re-queue {worker.item}: {exc}"
        self.ledger.update(worker, gc_done=1)
        outcome = "re-queued; the next admission pass starts a new worker"
        return True, outcome if branches_removed else f"{outcome}; {KEPT_BRANCH}"

    def _do_discard(
        self, worker: Worker, context: RepositoryContext | None, force: bool, report: TickReport
    ) -> tuple[bool, str]:
        blocker = self._worktree_holds_work(worker)
        if blocker and not force:
            return False, f"{worker.item}: {blocker}. Re-run with --force to discard anyway."
        # Pulling the worktree out from under a session that is still running destroys
        # whatever it is doing, so the stop has to be confirmed first.
        if not self._stop_session(worker) and not force:
            return False, (
                f"{worker.item}: the session did not stop, so its worktree is still "
                f"in use. Check it with `{self._attach_command(worker)}`, or re-run with "
                f"--force."
            )
        removed = self._remove_session(worker)
        try:
            branches_removed = self._remove_worktree(worker, force=True)
        except GitError as exc:
            return False, f"could not remove the worktree: {exc}"
        if not worker.is_terminal:
            worker = self.ledger.update(worker, state=ledger_mod.CANCELLED)
            self._sync_labels(worker, context, report)
        self.ledger.update(worker, gc_done=1, session_id=None, session_uuid=None)
        kept = "" if branches_removed else f"; {KEPT_BRANCH}"
        if not removed:
            return True, (
                "worktree and branch deleted, but the session could not be removed; "
                f"remove session {worker.session_id} by hand{kept}"
            )
        return True, f"session removed, worktree and branch deleted{kept}"

    def _worktree_holds_work(self, worker: Worker) -> str | None:
        found = self._checkout_holds_work(worker.worktree_path, (), "its worktree")
        checkout = item_checkout_path(worker)
        if found is None and checkout.exists():
            # The item's checkout sits on its head commit, which no remote-tracking ref
            # may contain: a fork's commits, or a squash-merged branch that a fetch pruned.
            # Those commits are not the session's work, and neither is a newer head that
            # `gh pr checkout` fetched again.
            given = self.ledger.get_state(f"{CHECKOUT_HEAD_PREFIX}{worker.id}")
            fetched = self.git.head_commit(checkout, "FETCH_HEAD")
            found = self._checkout_holds_work(
                checkout, tuple(c for c in (given, fetched) if c), "its item checkout"
            )
        return found

    def _checkout_holds_work(self, path: Path, not_in: tuple[str, ...], name: str) -> str | None:
        if not path.exists():
            return None
        unpushed = self.git.unpushed_commits(path, not_in=not_in)
        if unpushed < 0:
            return f"{name} could not be inspected"
        if unpushed > 0:
            return f"{name} holds {unpushed} unpushed commit(s)"
        if self.git.is_dirty(path):
            return f"{name} has uncommitted changes"
        return None

    def _remove_worktree(self, worker: Worker, *, force: bool) -> bool:
        """Remove the worktree and every branch the runner can show is its own.

        A session can rename the branch it was given, typically after the tracker's naming
        convention, and the placeholder name then no longer exists. Git records a rename in
        the branch's reflog, and that record is what makes the branch the runner's to
        delete. A branch the session switched to in any other way is left alone. Returns
        False when such a branch was left behind.
        """
        repo = worker.repo_path
        current = None
        checkout = item_checkout_path(worker)
        if checkout.exists():
            self.git.remove_worktree(repo, checkout, force=force)
        if worker.worktree_path.exists():
            current = self.git.worktree_branch(repo, worker.worktree_path)
            self.git.remove_worktree(repo, worker.worktree_path, force=force)
        self.git.delete_branch(repo, worker.branch, force=True)
        if not current or current == worker.branch:
            return True
        if self.git.renamed_from(repo, current, worker.branch):
            self.git.delete_branch(repo, current, force=True)
            return True
        return False

    # ----- 3. watchdog ------------------------------------------------------

    def _watchdog(self, report: TickReport) -> None:
        now = time.time()
        max_run = self.config.max_run_hours * 3600
        hard_stop = self.config.hard_stop_hours * 3600
        for worker in self.ledger.live_workers():
            if worker.state != ledger_mod.RUNNING or not worker.working_since:
                continue
            elapsed = now - worker.working_since
            if hard_stop and elapsed > hard_stop:
                if not self._stop_session(worker):
                    # Leaving the worker `running` keeps working_since intact, so the
                    # next tick tries again instead of handing the run a fresh deadline.
                    report.errors.append(
                        f"{worker.item}: the watchdog could not stop the session"
                    )
                    if worker.watchdog_notified != WATCHDOG_STOP_FAILED:
                        self.ledger.update(worker, watchdog_notified=WATCHDOG_STOP_FAILED)
                        self._notify(report, worker.item, "watchdog-stop-failed",
                                     worker.url, worker.repository,
                                     detail=f"running for {elapsed / 3600:.1f}h")
                    continue
                updated = self.ledger.update(
                    worker,
                    state=ledger_mod.BLOCKED,
                    last_error=f"watchdog hard stop after {elapsed / 3600:.1f}h",
                )
                self._sync_labels(updated, self.context_for_worker(updated), report)
                self._notify(report, worker.item, "watchdog-stopped", worker.url, worker.repository, role=worker.role, detail=f"running for {elapsed / 3600:.1f}h")
                report.warnings.append(f"{worker.item}: watchdog stopped the session")
            elif max_run and elapsed > max_run and not worker.watchdog_notified:
                self.ledger.update(worker, watchdog_notified=WATCHDOG_LONG_RUN)
                self._notify(report, worker.item, "long-running", worker.url, worker.repository, role=worker.role, detail=f"running for {elapsed / 3600:.1f}h")

    # ----- 4. pull requests of the workers' branches ------------------------

    def _follow_pull_requests(self, report: TickReport) -> None:
        """Subscribe each worker to the pull request of its own branch, once one exists.

        Decided here in code, never by a model, so that a comment or a CI result on the
        pull request a worker opened wakes that worker. The pull request is not the
        worker's primary item. The source is asked only about a branch that was pushed,
        which a local ref shows, so a worker that never pushes costs no call.
        """
        live = self.ledger.live_workers()
        found = self._forget_ended_follows({worker.id for worker in live})
        sources = [
            (config.name, source)
            for config in self.config.sources
            if (source := self.source(config.name)) is not None and source.has_pull_requests
        ]
        if not sources:
            return
        for worker in live:
            if worker.state == ledger_mod.LAUNCHING:
                continue
            key = f"{FOLLOWS_PULL_REQUEST_PREFIX}{worker.id}"
            if key in found:
                continue
            repository = self.config.repository_by_name(worker.repository)
            if repository is None or not worker.worktree_path.exists():
                continue
            branch = self._pushed_branch(worker, repository)
            if branch is None:
                continue
            for name, source in sources:
                try:
                    item = source.pull_request_for(repository, branch)
                except Exception as exc:
                    self._source_failed(
                        report, name, exc, f"pull_request_for of {worker.handle}"
                    )
                    continue
                if item is None:
                    continue
                followed = {(s.source, s.item) for s in self.ledger.subscriptions_of(worker)}
                with self.ledger.transaction():
                    if (item.source, item.key) not in followed:
                        self.ledger.subscribe(worker, item, PULL_REQUEST_KINDS)
                        report.routed.append(f"{worker.handle}: follows {item.key}")
                    self.ledger.set_state(key, item.key)
                break

    def _forget_ended_follows(self, live: set[int]) -> set[str]:
        """Drop the follow records of workers that ended; return the keys that remain."""
        kept = set()
        for key in self.ledger.all_state():
            if not key.startswith(FOLLOWS_PULL_REQUEST_PREFIX):
                continue
            worker_id = key.removeprefix(FOLLOWS_PULL_REQUEST_PREFIX)
            if worker_id.isdigit() and int(worker_id) in live:
                kept.add(key)
            else:
                self.ledger.delete_state(key)
        return kept

    def _pushed_branch(self, worker: Worker, repository: Repository) -> str | None:
        """The worktree's branch when its remote has it, else None. A session may rename it."""
        context = self.context(repository)
        remote = gitutil.remote_of(context.repo_config.base) if context.repo_config else "origin"
        try:
            branch = self.git.worktree_branch(repository.path, worker.worktree_path) or worker.branch
        except GitError:
            return None
        if not self.git.ref_exists(repository.path, f"refs/remotes/{remote}/{branch}"):
            return None
        return branch

    # ----- 5. sources and routing -------------------------------------------

    def _route_events(self, report: TickReport, *, crash_after_step: int | None = None) -> None:
        """Poll every source, then filter, deliver and start, through the router.

        A source that fails is one error in the report and keeps its cursor, so its
        events arrive on the next tick; the other sources are routed as usual. The starts
        of a repository's tracker wait for the admission step.
        """
        self._trackers_turn = False
        router = router_mod.Router(
            self.ledger,
            start=lambda event: self._start_from_event(
                event, report, crash_after_step=crash_after_step
            ),
            close=lambda event: self._close_from_event(event, report),
        )
        # The tracker starts are kept for the admission step, so they are not counted here.
        later = sum(TrackerSource.produced(event) for event in self.ledger.pending_start_events())
        retried = router.retry_pending_starts()
        if retried.deferred > later:
            report.routed.append(f"{retried.deferred - later} start(s) still deferred")
        for config in self.config.sources:
            source = self.source(config.name)
            if source is None:
                problem = self._sources.get(config.name)
                report.errors.append(
                    sanitize.one_line(str(problem), limit=300)
                    if isinstance(problem, ConfigError)
                    else source_failure(config.name, problem)
                )
                continue
            try:
                signed_in = source.whoami()
                events, cursor = source.poll(
                    self.ledger.get_cursor(config.name),
                    self.ledger.subscribed_items(config.name),
                )
            except LaunchAborted:
                raise
            except Exception as exc:
                self._source_failed(report, config.name, exc)
                continue
            own = [event for event in events if event.source == config.name]
            if len(own) != len(events):
                report.warnings.append(
                    f"source {config.name}: ignored {len(events) - len(own)} event(s) "
                    f"that named another source"
                )
            result = router.route(own, signed_in=signed_in)
            self.ledger.set_cursor(config.name, cursor)
            if own:
                report.routed.append(
                    f"{config.name}: {len(own)} event(s), {result.duplicates} seen before, "
                    f"{result.own} by chargehand, {result.delivered} inbox row(s), "
                    f"{result.skipped_before_start} from before the worker's start, "
                    f"{result.closed} worker(s) closed, {result.deferred} start(s) deferred"
                )
        _, given_up = self.ledger.prune_events()
        if given_up:
            report.warnings.append(
                f"gave up {given_up} start(s) that waited longer than a week for a launch slot; "
                f"an issue still in a tracker's queue is offered again from the queue"
            )

    def _start_from_event(
        self, event: Event, report: TickReport, *, crash_after_step: int | None = None
    ) -> bool:
        """Run the start rules of the enabled roles for one event.

        Returns True when a rule wanted a worker and the launch has to wait, for the caps,
        a pause or a previous worker's worktree; the router then keeps the event.

        The queue label is a ``labeled`` event from the repository's own tracker. Only a
        worker it starts drives the label, and an issue that any live worker holds stays
        with it, whatever the rule's ``unless_subscribed`` says: a second worker driving
        the one label per issue would clear it when either finished. The start waits while
        that worker lives, and is dropped once the issue has left the queue.
        """
        if TrackerSource.produced(event) != self._trackers_turn:
            # Not this step's start: it is tried in the other one, once per tick.
            return True
        context = self.tracker_context(event)
        from_tracker = context is not None
        if context is not None:
            repository = context.repository
        elif event.kind == roles_mod.LABELED:
            # The queue label is the tracker's; another source's `labeled` starts nothing.
            return False
        else:
            source = self.source(event.source)
            if source is None:
                # A configured source that cannot be built this tick keeps its starts; the
                # weekly prune ends the wait. A source no longer configured drops them.
                return self.config.source_by_name(event.source) is not None
            found = source.repository_for(event.item, self.config.repositories)
            if found is None:
                self._note_unconfigured(event, report)
                return False
            repository, context = found, self.context(found)
        if not context.usable:
            # Reported by reconciliation; the start waits until the repository is usable.
            return True
        queue = event.kind == roles_mod.LABELED
        if queue and self.ledger.is_paused(repository.name):
            # Not polled while paused, so the queue is not read for it either.
            report.skipped.append(f"{event.item.key}: repository {repository.name} is paused")
            return True
        if queue:
            if repository.name not in self._polled_trackers:
                # This tick's poll failed and is reported; the queue cannot be checked.
                return True
            # A start that waited may have left the queue since. The trackers are polled
            # before the admission step, so this reads the listing of this tick's poll.
            try:
                if not context.source.still_queued(event.item):  # type: ignore[union-attr]
                    return False
            except TRACKER_FAILURES as exc:
                report.errors.append(f"repository {repository.name}: {exc}")
                return True
        item = event.item.key
        deferred = False
        for role_name in repository.roles:
            try:
                role = context.role(role_name)
            except ConfigError:
                # Reported by the tracker poll, once per tick. The role might start on this
                # event, so the start waits until the role file loads again.
                deferred = True
                continue
            rule = role.start_rule_for(event.kind, event.source, repository.name)
            if rule is None:
                continue
            if queue:
                if self.ledger.live_worker_for(item) is not None:
                    deferred = True
                    continue
            elif router_mod.suitable_worker_exists(self.ledger, rule, role.name, event.item):
                delivered, skipped = router_mod.deliver_to_holders(self.ledger, role.name, event)
                handle = f"{role.name}/{sanitize.scrub_identifier(item)}"
                if delivered:
                    report.routed.append(f"{handle}: {rule.on} goes to the worker that has it")
                elif skipped:
                    report.routed.append(
                        f"{handle}: {rule.on} is from before the start of the worker that has "
                        f"it, not delivered"
                    )
                continue
            if not sanitize.is_safe_identifier(item):
                report.warnings.append(
                    f"source {event.source}: skipping an item whose identifier is not usable "
                    f"as a path or branch name: {sanitize.scrub_identifier(item)!r}"
                )
                return False
            if self.ledger.is_paused(repository.name):
                report.skipped.append(f"{item}: repository {repository.name} is paused")
                deferred = True
                continue
            stale = self._stale_worktree_for(role.name, item)
            if stale is not None:
                hint = (
                    f"; `chargehand retry {item}` reuses it safely, "
                    f"`chargehand discard {item} --yes` removes it"
                    if queue else ""
                )
                report.skipped.append(
                    f"{item}: the previous {role.name} worker's worktree is still at {stale}{hint}"
                )
                deferred = True
                continue
            refusal = self._launch_refusal(repository, role, item)
            if refusal is not None:
                report.skipped.append(refusal)
                deferred = True
                continue
            if queue:
                owner = ledger_mod.LABEL_QUEUE
            elif from_tracker:
                owner = ledger_mod.LABEL_NONE
            else:
                owner = self._source_status_owner(event)
            try:
                self._launch(context, role, rule, event.item, report, owns_label=owner,
                             crash_after_step=crash_after_step)
            except LaunchAborted:
                raise
            except ChargehandError as exc:
                report.errors.append(f"{item}: {exc}")
        return deferred

    def _source_status_owner(self, event: Event) -> int:
        """Whether a worker that *event* starts marks its item on the event's source.

        Only when the source marks items, and only the first live worker on the item does
        it: the first role listed in the repository takes it, as with the queue label.
        """
        source = self.source(event.source)
        if source is None or not source.marks_items:
            return ledger_mod.LABEL_NONE
        for subscription, worker in self.ledger.live_subscriptions(event.source, event.item.key):
            if subscription.is_primary and worker.owns_label == ledger_mod.LABEL_SOURCE:
                return ledger_mod.LABEL_NONE
        return ledger_mod.LABEL_SOURCE

    def _note_unconfigured(self, event: Event, report: TickReport) -> None:
        """Report a start event for a repository that no `[[repository]]` names, once.

        Only when a role would start on the event's kind: a mention on some other
        repository is not news.
        """
        hint = sanitize.one_line(event.item.hint or "", limit=100).lower()
        if not hint or not self._some_role_starts_on(event):
            return
        key = f"{UNCONFIGURED_PREFIX}{event.source}:{hint}"
        if self.ledger.get_state(key):
            return
        self.ledger.set_state(key, str(int(time.time())))
        message = (
            f"source {event.source}: ignored a start event for {hint}, which no [[repository]] "
            f"names; reported once per repository"
        )
        log.warning("%s", message)
        report.warnings.append(message)

    def _some_role_starts_on(self, event: Event) -> bool:
        for repository in self.config.repositories:
            context = self.context(repository)
            if context.repo_config is None:
                continue
            for role_name in repository.roles:
                try:
                    role = context.role(role_name)
                except ConfigError:
                    continue
                if any(
                    router_mod.kind_matches((rule.on,), event.kind, event.source)
                    for rule in role.start
                ):
                    return True
        return False

    def _close_from_event(self, event: Event, report: TickReport) -> int:
        """End each worker whose primary item *event* closes, by its start rule's ``close_on``.

        The session is stopped first and the worker becomes done; garbage collection then
        removes the session and the worktree, never over unpushed commits. A worker that
        is working is left to finish its turn: once it is idle, the tick finds its item
        closed and ends it the same way.
        """
        closed = 0
        for subscription, worker in self.ledger.live_subscriptions(event.source, event.item.key):
            if not subscription.is_primary:
                continue
            rule = self._start_rule_of(worker)
            if rule is None or not router_mod.kind_matches(rule.close_on, event.kind, event.source):
                continue
            if worker.state in (ledger_mod.LAUNCHING, ledger_mod.RUNNING):
                report.skipped.append(
                    f"{worker.handle}: its item closed while it works; it ends after the turn"
                )
                continue
            if not self._stop_session(worker):
                report.warnings.append(f"{worker.item}: item closed but its session would not stop")
                continue
            updated = self.ledger.update(worker, state=ledger_mod.DONE, label_state=None)
            self._sync_labels(updated, self.context_for_worker(updated), report)
            report.transitions.append(f"{worker.item}: item closed -> done")
            closed += 1
        return closed

    def _start_rule_of(self, worker: Worker) -> StartRule | None:
        """The start rule that created *worker*, as its role file says now; None when unknown."""
        context = self.context_for_worker(worker)
        if context is None or not context.usable or not worker.start_rule:
            return None
        try:
            role = context.role(worker.role)
        except ConfigError:
            return None
        return next((rule for rule in role.start if rule.name == worker.start_rule), None)

    # ----- 6. state diff and notification -----------------------------------

    def _diff_and_notify(self, report: TickReport) -> None:
        # Before the diff, so a send that fails below waits for the next tick rather than
        # being tried twice in this one.
        self._retry_pending_notifications(report)
        listings: dict[str, dict[str, Session]] = {}
        for listed in self.ledger.live_workers():
            # Read again: the daemon applies session hooks on other threads while this
            # phase runs.
            worker = self.ledger.get(listed.id) or listed
            if worker.state == ledger_mod.LAUNCHING or worker.is_terminal:
                continue
            if self._hook_is_newer(worker):
                continue
            if worker.runtime not in listings:
                try:
                    listings[worker.runtime] = {s.id: s for s in self.sessions(worker.runtime)}
                except ConfigError as exc:
                    report.errors.append(f"{worker.item}: {exc}")
                    continue
            session = listings[worker.runtime].get(worker.session_id or "")
            if session is None:
                session = self._find_worker_session(worker)
                if session is not None:
                    worker = self.ledger.update(
                        worker, session_id=session.id, session_uuid=session.uuid
                    )
            observed = session.state if session else ledger_mod.SESSION_MISSING
            waiting = session.waiting_for if session else None
            pr_url = (session.pr_url if session else None) or worker.pr_url

            changes: dict[str, object] = {}
            if observed != worker.session_state:
                changes["session_state"] = observed
            if waiting != worker.waiting_for:
                changes["waiting_for"] = sanitize.one_line(waiting) if waiting else None
            if pr_url != worker.pr_url:
                changes["pr_url"] = pr_url
            if worker.state == ledger_mod.PARKED:
                # A usage limit ended the turn, and the listing shows that failed turn
                # until the session runs again. Only the operator or a hook moves it.
                changes.pop("session_state", None)
            if changes:
                worker = self.ledger.update(worker, **changes)

            try:
                worker = self._apply_session_state(worker, observed, report, session=session)
            except _RowChanged:
                # A hook wrote the row meanwhile, and it is newer than this listing.
                continue

    def _hook_is_newer(self, worker: Worker) -> bool:
        listed_at = self._listed_at.get(worker.runtime)
        return (
            worker.last_hook_at is not None
            and listed_at is not None
            and worker.last_hook_at >= listed_at
        )

    def apply_hook(
        self, worker: Worker, hook: Hook, report: TickReport | None = None, *, now: float | None = None
    ) -> Worker | None:
        """Apply what one session hook reports to *worker* at once, as the next diff would.

        The daemon calls this on a request thread while its tick may run, so the state is
        written only if the row still has the state and the last hook it was read with;
        otherwise the row is read again. The transition, the label and the notification
        are the session diff's own. Returns the worker as written, or None when it is no
        longer live.
        """
        report = report if report is not None else TickReport()
        stamp = time.time() if now is None else now
        current = self.ledger.get(worker.id)
        if current is None or current.is_terminal:
            return None
        changes: dict[str, object] = {"last_hook": hook.event, "last_hook_at": stamp}
        if hook.transcript_path:
            changes["transcript_path"] = hook.transcript_path
        if hook.last_answer is not None:
            changes["last_answer"] = hook.last_answer
        if hook.kind == runtime_mod.HOOK_TURN_FAILED:
            if hook.usage_limit:
                changes["reset_at"] = hook.reset_at
            changes["last_error"] = (
                f"{'usage limit' if hook.usage_limit else 'turn failed'}: {hook.error or 'unknown'}"
            )
        if hook.observed is not None and current.state not in HOOK_FROZEN_STATES:
            changes["session_state"] = hook.observed
        current = self.ledger.update(current, **changes)
        if hook.kind == runtime_mod.HOOK_PERMISSION:
            # A session shows one permission dialog at a time; an older open prompt was
            # answered in the session.
            self.ledger.resolve_permission_prompts(current, now=stamp)
            self.ledger.record_permission_prompt(
                current, tool_name=hook.tool_name or "unknown", tool_input=hook.tool_input or "",
                now=stamp,
            )
        if hook.observed is None:
            return current
        for _ in range(HOOK_WRITE_ATTEMPTS):
            if current.state in HOOK_FROZEN_STATES:
                return current
            try:
                return self._apply_session_state(current, hook.observed, report, hook=hook)
            except _RowChanged:
                current = self.ledger.get(worker.id)
                if current is None or current.is_terminal:
                    return None
        log.warning("%s: the hook %s lost every write to the tick; the next diff applies it",
                    current.handle, hook.event)
        return current

    def _apply_session_state(
        self,
        worker: Worker,
        observed: str,
        report: TickReport,
        *,
        session: Session | None = None,
        hook: Hook | None = None,
    ) -> Worker:
        """Move *worker* to the state that *observed* means, from the listing or a *hook*.

        The state write fails with _RowChanged when another thread changed the row's
        state or applied a hook since *worker* was read.
        """
        context = self.context_for_worker(worker)
        detail: str | None = None
        target_state = worker.state
        previous_state = worker.state

        if worker.state == ledger_mod.PARKED and hook is None:
            return worker
        if worker.state == ledger_mod.PAUSED and session is not None and self._is_stopped(
            worker, session
        ):
            # `stop` keeps the session, and a stopped Claude Code session still lists the
            # state of its last turn, often `done`. Only a live process ends the pause.
            return worker
        if observed == runtime_mod.WORKING:
            target_state = ledger_mod.RUNNING
            if worker.state != ledger_mod.RUNNING:
                # A reply landed in agent view; the run is live again.
                worker = self.ledger.update(
                    worker, working_since=time.time(), watchdog_notified=0, last_error=None
                )
        elif observed == runtime_mod.BLOCKED:
            # waiting_for comes from the session listing and can quote whatever the
            # agent was about to run. It stays in the ledger and in `status`; it never
            # reaches a notification.
            target_state, detail = ledger_mod.BLOCKED, "needs input"
        elif observed == runtime_mod.DONE:
            target_state, detail = self._resolve_done(worker, context)
            if target_state == ledger_mod.IDLE and worker.state == ledger_mod.QUEUED:
                # Still idle, with events that admission held back.
                target_state = ledger_mod.QUEUED
        elif observed == runtime_mod.FAILED:
            if hook is not None and hook.usage_limit:
                target_state, detail = ledger_mod.PARKED, USAGE_LIMIT
            else:
                target_state, detail = ledger_mod.BLOCKED, "session failed"
        elif observed == runtime_mod.STOPPED:
            if worker.state == ledger_mod.PAUSED:
                return worker
            target_state, detail = ledger_mod.BLOCKED, "session stopped"
        elif observed == ledger_mod.SESSION_MISSING:
            if worker.state == ledger_mod.PAUSED:
                return worker
            target_state, detail = ledger_mod.BLOCKED, "session is gone from the listing"
        else:
            target_state, detail = ledger_mod.BLOCKED, "session state not recognised — go look"

        if target_state != worker.state:
            written = self.ledger.update_if_unchanged(worker, state=target_state)
            if written is None:
                raise _RowChanged(worker.handle)
            worker = written
            report.transitions.append(f"{worker.item}: {observed} -> {target_state}")
            if previous_state == ledger_mod.BLOCKED:
                self.ledger.resolve_permission_prompts(worker)
        self._sync_labels(worker, context, report)

        if observed != worker.notified_state and observed != runtime_mod.WORKING:
            # Recorded only once delivered. A hook that failed, for example on the first
            # tick after a wake while the network is still down, is run again on the next
            # tick; otherwise the run would sit blocked with nobody told.
            if self._notify(report, worker.item, target_state, worker.url, worker.repository, role=worker.role, detail=detail):
                worker = self.ledger.update(worker, notified_state=observed)
            elif worker.is_terminal:
                # A finished worker leaves the live loop, so nothing would visit it again.
                self.ledger.set_state(f"{PENDING_NOTIFICATION_PREFIX}{worker.id}", detail or "")
        elif observed == runtime_mod.WORKING and worker.notified_state != observed:
            worker = self.ledger.update(worker, notified_state=observed)
        return worker

    def _retry_pending_notifications(self, report: TickReport) -> None:
        for key, detail in self.ledger.all_state().items():
            if not key.startswith(PENDING_NOTIFICATION_PREFIX):
                continue
            worker_id = key.removeprefix(PENDING_NOTIFICATION_PREFIX)
            worker = self.ledger.get(int(worker_id)) if worker_id.isdigit() else None
            if worker is None or not worker.finished_at:
                self.ledger.delete_state(key)
                continue
            if time.time() - worker.finished_at > PENDING_NOTIFICATION_MAX_AGE_SECS:
                self.ledger.delete_state(key)
                report.warnings.append(
                    f"gave up on the notification for {worker.item} ({worker.state})"
                )
                continue
            if self._notify(report, worker.item, worker.state, worker.url, worker.repository, role=worker.role, detail=detail or None):
                self.ledger.delete_state(key)

    def _resolve_done(
        self, worker: Worker, context: RepositoryContext | None
    ) -> tuple[str, str | None]:
        """A `done` session ended its turn. The status file, when the role has one, says more.

        Without a question in the status file, the worker is idle: it waits for events on
        the items it follows, and the operator may look at what the turn left. A status
        file that this worker did not write says nothing about this turn.
        """
        try:
            role = context.role(worker.role) if context and context.usable else None
        except ConfigError:
            role = None
        if role is None:
            return ledger_mod.BLOCKED, "finished or waiting; its role does not load, go look"
        if not role.status_file:
            return ledger_mod.IDLE, TURN_ENDED
        path = Path(
            config_mod.render(
                role.status_file,
                self._template_values(worker),
                where=f"{role.source or role.name}.status_file",
            )
        ).expanduser()
        status = StatusFile.read(path, not_before=worker.created_at)
        if status.stale or not status.present:
            return ledger_mod.IDLE, TURN_ENDED
        if status.pr_url and status.pr_url != worker.pr_url:
            self.ledger.update(worker, pr_url=status.pr_url)
        if status.state == "complete":
            return ledger_mod.DONE, "complete"
        if status.state == "needs-input":
            return ledger_mod.BLOCKED, "needs input"
        if status.state == "aborted":
            # stop_reason is written by the agent. It is kept for `--verbose-titles` and
            # deliberately left out of the notification, which usually crosses a relay.
            if status.stop_reason:
                self.ledger.update(worker, last_error=f"aborted: {status.stop_reason}"[:500])
            return ledger_mod.FAILED, "aborted"
        return ledger_mod.BLOCKED, "status file did not name a known state — go look"

    def _is_stopped(self, worker: Worker, session: Session) -> bool:
        try:
            return self.runtime(worker.runtime).is_stopped(session)
        except ConfigError:
            return False

    # ----- 7. delivery ------------------------------------------------------

    def _deliver(self, report: TickReport, *, crash_after_wake: bool = False) -> None:
        """Wake each idle worker once, for every event waiting in its inbox."""
        idle_before = {w.id for w in self.ledger.workers_in_state([ledger_mod.IDLE])}
        result = delivery_mod.Delivery(
            self.ledger,
            max_concurrent=self.config.max_concurrent,
            runtime=self.runtime,
            policy=self._policy_of,
            usage=self._usage,
            crash_after_wake=crash_after_wake,
            guard=lambda worker: self._workspace_check(worker).fault,
            prepare=self._refresh_item_checkout,
        ).run()
        report.woken.extend(worker.handle for worker in result.woken)
        report.skipped.extend(result.held)
        report.warnings.extend(result.warnings)
        report.errors.extend(result.errors)
        if result.woken or result.faults:
            self.invalidate_sessions()
        for worker in result.woken:
            # The tick log prints transitions, so a wake shows there too.
            report.transitions.append(f"{worker.item or worker.handle}: wake -> {worker.state}")
            log.info("woke %s in session %s", worker.handle, worker.session_id)
        for worker in self.ledger.workers_in_state([ledger_mod.QUEUED]):
            if worker.id in idle_before:
                report.transitions.append(f"{worker.item or worker.handle}: wake held -> queued")
        for worker, detail in result.faults:
            self._sync_labels(worker, self.context_for_worker(worker), report)
            self._notify(report, worker.item or worker.handle, "wake-fault", worker.url,
                         worker.repository, role=worker.role, detail=detail)

    def _workspace_check(self, worker: Worker, *, accept_files: bool = False) -> WorkspaceCheck:
        """Whether the session of a worker with an item checkout may start again where it is.

        A session that checked the item's head out in its own worktree, or copied the
        head's settings or instructions there, would have them loaded, trusted, when it
        is resumed. With *accept_files*, the files as they are now become the record, for
        an operator who has looked at them; the branch is checked either way.
        """
        if not self._has_item_checkout(worker):
            return WorkspaceCheck()
        checkout_fault = self._item_checkout_fault(worker)
        if checkout_fault is not None:
            return WorkspaceCheck(checkout_fault)
        try:
            branch = self.git.worktree_branch(worker.repo_path, worker.worktree_path)
        except GitError:
            branch = None
        if branch != worker.branch:
            return WorkspaceCheck("its worktree is no longer on its own branch")
        link_fault = self._gitlink_fault(worker.repo_path, worker.worktree_path, "its worktree")
        if link_fault is not None:
            return WorkspaceCheck(link_fault)
        if accept_files:
            return WorkspaceCheck(self._record_workspace_files(worker))
        recorded = self.ledger.get_state(f"{WORKSPACE_FILES_PREFIX}{worker.id}")
        try:
            record = json.loads(recorded) if recorded is not None else None
        except ValueError:
            record = None
        if not isinstance(record, dict):
            return WorkspaceCheck(
                "the agent's files in its worktree are not recorded", acceptable=True
            )
        try:
            pathspecs = self.runtime(worker.runtime).workspace_files
        except ConfigError:
            return WorkspaceCheck("its runtime is unknown")
        current = self.git.workspace_manifest(worker.worktree_path, pathspecs)
        if current is None:
            return WorkspaceCheck("its worktree could not be inspected")
        changed = changed_entries(record, current)
        if changed:
            log.warning("%s: the agent's files in its worktree changed: %s", worker.handle,
                        sanitize.one_line(", ".join(changed), limit=500))
            return WorkspaceCheck(
                "the agent's settings or instructions in its worktree changed",
                tuple(changed), acceptable=True,
            )
        return WorkspaceCheck()

    def _has_item_checkout(self, worker: Worker) -> bool:
        """Whether *worker* has, or had, an item checkout: the directory or a record of it."""
        return item_checkout_path(worker).exists() or any(
            self.ledger.get_state(f"{prefix}{worker.id}") is not None
            for prefix in WORKER_STATE_PREFIXES
        )

    def _item_checkout_fault(self, worker: Worker) -> str | None:
        """Why the worker's item checkout must not be read or written again, or None.

        A session that replaced the checkout with a symlink, or pointed its `.git` file
        elsewhere, would otherwise redirect the setup step the refresh runs, or the git
        calls that read it, into a directory of its choosing. So the path must be a plain
        directory that git lists, from the main checkout, as a detached worktree at that
        same path.
        """
        checkout = item_checkout_path(worker)
        if checkout.is_symlink():
            return "its item checkout was replaced with a symlink"
        if not checkout.exists():
            # The records say there was one. A session that removed it must not turn the
            # check off by doing so.
            return "its item checkout is gone"
        try:
            worktrees = self.git.list_worktrees(worker.repo_path)
        except GitError:
            return "its item checkout could not be inspected"
        entry = worktrees.get(str(checkout)) or worktrees.get(str(checkout.resolve()))
        if entry is None:
            return "its item checkout is not a registered worktree"
        if entry.get("branch"):
            # A detached worktree has no branch. One with a branch is the session's own
            # worktree, reached because the checkout path led there.
            return "its item checkout is not where it was"
        return self._gitlink_fault(worker.repo_path, checkout, "its item checkout")

    def _gitlink_fault(self, repo: Path, worktree: Path, name: str) -> str | None:
        """Why *worktree*'s ``.git`` must not be trusted, or None.

        A linked worktree's ``.git`` is a file that points to the admin directory the main
        checkout holds for it. A session that rewrites it to a directory of its own aims
        every later git read, and the refresh's `gh pr checkout`, at config it controls.
        So the link must still point to the admin directory the main checkout records,
        which the main checkout keeps out of the session's reach.
        """
        admin = self.git.recorded_admin_dir(repo, worktree)
        if admin is None:
            return f"{name} is not a registered worktree"
        if self.git.linked_gitdir(worktree) != admin:
            return f"{name} no longer points at its own git directory"
        return None

    def _record_workspace_files(self, worker: Worker) -> str | None:
        """Record the agent's files in the worktree as they are now; the reason when that fails."""
        try:
            pathspecs = self.runtime(worker.runtime).workspace_files
        except ConfigError:
            return "its runtime is unknown"
        manifest = self.git.workspace_manifest(worker.worktree_path, pathspecs)
        if manifest is None:
            return "its worktree could not be inspected"
        self.ledger.set_state(
            f"{WORKSPACE_FILES_PREFIX}{worker.id}", json.dumps(manifest, sort_keys=True)
        )
        return None

    def _refresh_item_checkout(self, worker: Worker) -> str | None:
        """Run the role's setup step again in the item checkout, so it follows the item's head.

        Before a wake only: the setup is what fetched the head at the launch, and a wake
        is when the session reads it again. Returns why the wake must wait, or None. The
        command's output may quote the item, so it stays in the ledger and the log.
        """
        checkout = item_checkout_path(worker)
        if not checkout.exists():
            return None
        context = self.context_for_worker(worker)
        if context is None or not context.usable or context.repo_config is None:
            return f"repository '{worker.repository}' is unavailable"
        try:
            role = context.role(worker.role)
        except ConfigError:
            return "its role does not load"
        if role.checkout != roles_mod.CHECKOUT_ITEM or not role.setup:
            return None
        # The worktree has been the session's working directory since the launch, so a
        # program there is not the operator's any more.
        failure = self._run_script(
            role.setup, worker, context.repo_config.setup_timeout_secs,
            f"{role.source or 'role ' + role.name}.setup", cwd=checkout, trust_worktree=False,
        )
        if failure is not None:
            detail = sanitize.one_line(f"item checkout not refreshed: {failure}", limit=400)
            self.ledger.update(worker, last_error=detail)
            log.warning("%s: %s", worker.handle, detail)
            return "its item checkout could not be refreshed"
        head = self.git.head_commit(checkout)
        if head is not None:
            self.ledger.set_state(f"{CHECKOUT_HEAD_PREFIX}{worker.id}", head)
        return None

    def _policy_of(self, worker: Worker) -> roles_mod.Policy:
        context = self.context_for_worker(worker)
        if context is None or not context.usable:
            raise ConfigError(f"repository '{worker.repository}' is unavailable")
        return context.role(worker.role).policy

    # ----- labels -----------------------------------------------------------

    def _sync_all_labels(self, report: TickReport) -> None:
        # Terminal workers are included: the last label write an worker makes is the
        # one at its final transition, and if that write fails nothing else would ever
        # retry it. An overnight credential outage would otherwise leave every run that
        # finished during it wearing the wrong label.
        for worker in (*self.ledger.live_workers(), *self.ledger.terminal_workers_since()):
            if worker.state == ledger_mod.LAUNCHING and worker.step < 1:
                continue
            self._sync_labels(worker, self.context_for_worker(worker), report)

    def _label_sync_pending(self, item: str) -> bool:
        worker = self.ledger.latest_label_owner(item)
        if worker is None:
            return False
        desired = _STATUS_FOR_STATE.get(worker.state)
        return desired is not None and worker.label_state != desired.value

    @staticmethod
    def _drives_label(worker: Worker) -> bool:
        """Only a worker that the queue label started changes the queue label.

        The label is one per issue and several workers may follow the issue, so a worker
        that another event started only reads it. Recorded when the worker is created,
        so a later edit of the role file cannot hand the label to another worker.
        """
        return worker.owns_label == ledger_mod.LABEL_QUEUE

    def _status_target(
        self, worker: Worker, context: RepositoryContext | None
    ) -> tuple[Source, WorkItem] | None:
        """The source that shows *worker*'s state, and the item there; None for no call now.

        A queue worker marks its issue on the repository's tracker. A worker that a
        `[[source]]` that marks items started marks its primary item on that source, which
        needs no usable repository. A source that cannot be built this tick is tried again
        on the next one.
        """
        if self._drives_label(worker):
            if context is None or not context.usable:
                return None
            assert context.source is not None
            return context.source, context.item_of(worker)
        if worker.owns_label != ledger_mod.LABEL_SOURCE:
            return None
        primary = self.ledger.primary_subscription(worker)
        source = self.source(primary.source) if primary is not None else None
        if primary is None or source is None:
            return None
        return source, primary.work_item

    def _sync_labels(
        self, worker: Worker, context: RepositoryContext | None, report: TickReport
    ) -> Worker:
        """Drive the tracker, or the source item the worker marks, towards the worker state.

        `label_state` records what was last *verified*, so a write that could not be
        confirmed is simply retried on the next tick rather than assumed to have landed.
        """
        desired = _STATUS_FOR_STATE.get(worker.state)
        if desired is None or worker.label_state == desired.value:
            return worker
        target = self._status_target(worker, context)
        if target is None:
            return worker
        source, item = target
        try:
            source.mark(item, desired)
        except ItemGone:
            report.warnings.append(f"{worker.item} no longer exists on the tracker")
            return self.ledger.update(worker, label_state=desired.value)
        except AmbiguousWrite as exc:
            report.errors.append(f"{worker.item}: {exc}")
            return self.ledger.update(worker, last_error=str(exc)[:500])
        except TRACKER_FAILURES as exc:
            report.errors.append(f"{worker.item}: {exc}")
            return worker
        # Deliberately does not clear last_error: a successful label write says nothing
        # about why the worker failed, and that reason is what `--verbose-titles` shows.
        return self.ledger.update(worker, label_state=desired.value)

    # ----- 9. admission and launch ------------------------------------------

    def _counts(self) -> tuple[int, int, dict[str, int]]:
        """(working, open, per-repository working) as the admission rules define them."""
        working = 0
        open_workers = 0
        per_route: dict[str, int] = {}
        for worker in self.ledger.live_workers():
            open_workers += 1
            is_working = worker.state in (ledger_mod.LAUNCHING, ledger_mod.RUNNING)
            if is_working:
                working += 1
                per_route[worker.repository] = per_route.get(worker.repository, 0) + 1
        return working, open_workers, per_route

    def _route_trackers(self, report: TickReport) -> None:
        """Poll each repository's tracker as a source, and route its events before delivery.

        The filters, the inboxes and the closes run here, so a comment on a worker's issue
        wakes it in this tick's delivery, as an event from a ``[[source]]`` does. The
        tracker's starts are not this step's turn: the router keeps them as waiting
        starts, and the admission step offers them to the start rules. A paused
        repository is not polled.
        """
        self._trackers_turn = False
        router = router_mod.Router(
            self.ledger,
            start=lambda event: self._start_from_event(event, report),
            close=lambda event: self._close_from_event(event, report),
        )
        polled = self._poll_trackers(report)
        for repository, events, cursor in polled:
            source = self.context(repository).source
            assert source is not None
            try:
                # Asked only when the marker filter needs it, so a quiet poll stays one call.
                signed_in = source.whoami() if any(event.marked for event in events) else ""
            except TRACKER_FAILURES as exc:
                report.errors.append(f"repository {repository.name}: {exc}")
                continue
            result = router.route(events, signed_in=signed_in)
            self.ledger.set_cursor(f"{TRACKER_CURSOR_PREFIX}{repository.name}", cursor)
            if result.delivered or result.own or result.closed or result.skipped_before_start:
                report.routed.append(
                    f"{repository.name}: {len(events)} tracker event(s), {result.own} by "
                    f"chargehand, {result.delivered} inbox row(s), "
                    f"{result.skipped_before_start} from before the worker's start, "
                    f"{result.closed} worker(s) closed"
                )

    def _admit(self, report: TickReport, *, crash_after_step: int | None = None) -> None:
        """Offer the tracker's waiting starts to the start rules, oldest first, under the caps.

        After the state diff, which may have freed a slot, and after delivery, so a
        waiting worker gets a free slot before the queue starts a new one. Each tracker
        was polled earlier in the tick, so a queue start checks the queue against this
        tick's listing.
        """
        self._trackers_turn = True
        router_mod.Router(
            self.ledger,
            start=lambda event: self._start_from_event(
                event, report, crash_after_step=crash_after_step
            ),
        ).retry_pending_starts()

    def _poll_trackers(
        self, report: TickReport
    ) -> list[tuple[Repository, list[Event], str | None]]:
        polled: list[tuple[Repository, list[Event], str | None]] = []
        for repository in self.config.repositories:
            if self.ledger.is_paused(repository.name):
                report.skipped.append(f"repository {repository.name} is paused")
                continue
            context = self.context(repository)
            if not context.usable:
                if context.error:
                    report.errors.append(f"repository {repository.name}: {context.error}")
                continue
            for role_name in repository.roles:
                try:
                    context.role(role_name)
                except ConfigError as exc:
                    report.errors.append(f"repository {repository.name}: {exc}")
            source = context.source
            assert source is not None
            try:
                events, cursor = source.poll(
                    self.ledger.get_cursor(f"{TRACKER_CURSOR_PREFIX}{repository.name}"),
                    self._followed_items(context),
                )
            except ChargehandError as exc:
                # Expected, and back on every tick while it lasts: the report's error
                # line is its one record.
                report.errors.append(f"repository {repository.name}: {exc}")
                continue
            except Exception as exc:
                log.exception("the tracker of repository %s failed", repository.name)
                report.errors.append(
                    f"repository {repository.name}: {sanitize.one_line(str(exc), limit=300)}"
                )
                continue
            polled.append((repository, events, cursor))
        self._polled_trackers = {repository.name for repository, _, _ in polled}
        return polled

    def _followed_items(self, context: RepositoryContext) -> list[WorkItem]:
        """The items of this repository's tracker that live workers subscribe to.

        Every repository's tracker of one type is one source by name, so the ledger's list
        is narrowed to the workers of this repository. A ``[[source]]`` with the tracker's
        name, such as ``github`` next to a GitHub Issues queue, already polls these items,
        so the tracker leaves them to it.
        """
        name = context.repository.tracker.type
        if self.config.source_by_name(name) is not None:
            return []
        return [
            item
            for item in self.ledger.subscribed_items(name)
            if any(
                worker.repository == context.repository.name
                for _, worker in self.ledger.live_subscriptions(name, item.key)
            )
        ]

    def _launch_refusal(self, repository: Repository, role: Role, item: str) -> str | None:
        """Why the caps hold a new launch back now, or None when it may start."""
        working, open_workers, per_repository = self._counts()
        if working >= self.config.max_concurrent:
            return f"{item}: launch throttle ({working}/{self.config.max_concurrent})"
        if open_workers >= self.config.max_open_workers:
            return f"{item}: {open_workers}/{self.config.max_open_workers} open workers"
        if (
            repository.max_concurrent is not None
            and per_repository.get(repository.name, 0) >= repository.max_concurrent
        ):
            return (
                f"{item}: repository {repository.name} is at its own limit "
                f"({repository.max_concurrent})"
            )
        in_role = self.ledger.live_count_by_role().get(role.name, 0)
        if role.policy.max_workers is not None and in_role >= role.policy.max_workers:
            return f"{item}: role {role.name} is at its limit ({role.policy.max_workers} workers)"
        return None

    def _stale_worktree_for(self, role: str, item: str) -> Path | None:
        """A re-armed item starts a new worker only once the old worktree is gone.

        Reusing it would start a run that is supposed to be fresh on top of the previous
        worker's changes, and, if that launch were interrupted, leave its session as the
        obvious thing for reconciliation to adopt.
        """
        previous = self.ledger.latest_worker_for(item, role)
        if previous is None or not previous.is_terminal:
            return None
        return previous.worktree_path if previous.worktree_path.exists() else None

    def _launch(
        self,
        context: RepositoryContext,
        role: Role,
        rule: StartRule,
        item: WorkItem,
        report: TickReport,
        *,
        owns_label: int = ledger_mod.LABEL_NONE,
        crash_after_step: int | None = None,
    ) -> None:
        """Start a worker of *role* on *item*; *owns_label* says what it marks its state on."""
        repository = context.repository
        repo_config = context.repo_config
        assert repo_config is not None
        worktree = self.config.worktree_root / role.name / item.key
        branch = self._branch_for(repo_config, role.name, item.key)

        # Step 0: the row, and the subscription to its own item, exist before any
        # external side effect. The url reaches the prompt as {item_url} and comes from the
        # source, so only a plain URL is kept. Only a worker the queue label starts drives
        # the label; a worker that marks its item on a `[[source]]` does it after the launch.
        with self.ledger.transaction():
            worker = self.ledger.create_worker(
                role=role.name,
                item=item.key,
                item_id=item.ref,
                url=sanitize.safe_url(item.url),
                owns_label=owns_label,
                repository=repository.name,
                repo=repository.path,
                worktree=worktree,
                branch=branch,
                start_rule=rule.name,
                runtime=role.runtime,
            )
            self.ledger.subscribe(worker, item, rule.subscribe, primary=True)
        log.info("launching %s in %s", worker.handle, worktree)
        self._check_crash(0, crash_after_step)
        self._run_launch_steps(context, worker, report, crash_after_step=crash_after_step)

    def _run_launch_steps(
        self,
        context: RepositoryContext,
        worker: Worker,
        report: TickReport,
        *,
        crash_after_step: int | None = None,
    ) -> Worker:
        repo_config = context.repo_config
        source = context.source
        assert repo_config is not None and source is not None
        # A role that fails to load is a ChargehandError: the caller records it and the
        # launch retry counter fails the worker loudly rather than looping.
        role = context.role(worker.role)

        # Step 1: swap the labels, then verify by re-reading. Only for a worker that the
        # queue label started; another worker on the item leaves the label alone.
        if worker.step < 1 and not self._drives_label(worker):
            worker = self.ledger.update(worker, step=1)
            self._check_crash(1, crash_after_step)
        if worker.step < 1:
            # A tracker or credential failure while resuming is caught by
            # _reconcile_ledger, which hands the worker its launch retry back rather than
            # spending one.
            try:
                source.mark(context.item_of(worker), Status.RUNNING)
            except ItemGone:
                return self._fail_worker(
                    worker, context, "the issue no longer exists on the tracker", report,
                    notify_detail="issue no longer exists",
                )
            except AmbiguousWrite as exc:
                # It may have landed. Record why and stay at step 0, then let the caller
                # decide what it costs. A resume hands its launch retry back, because an
                # unverifiable write is a fact about the tracker and not a fault in this
                # worker: a tracker that answers a correct write with a stale read would
                # otherwise fail a launch that never went wrong, in three ticks.
                self.ledger.update(worker, last_error=str(exc)[:500])
                raise
            worker = self.ledger.update(worker, step=1, label_state=Status.RUNNING.value)
            self._check_crash(1, crash_after_step)

        # Step 2: fetch, then create the worktree on a placeholder branch. A role that
        # reviews its item gets a second, detached worktree for the item's head.
        if worker.step < 2:
            self.git.fetch(context.repository.path, gitutil.remote_of(repo_config.base))
            self.git.add_worktree(
                context.repository.path, worker.worktree_path, worker.branch, repo_config.base
            )
            if role.checkout == roles_mod.CHECKOUT_ITEM:
                self.git.add_detached_worktree(
                    context.repository.path, item_checkout_path(worker), repo_config.base
                )
            worker = self.ledger.update(worker, step=2)
            self._check_crash(2, crash_after_step)

        # Step 3: the repository's own setup script, then the role's. A role that reviews
        # its item runs its setup in the item checkout, which the setup moves to the
        # item's head.
        if worker.step < 3:
            item_checkout = role.checkout == roles_mod.CHECKOUT_ITEM
            for template, where, cwd in (
                (repo_config.setup, f"{repo_config.source or 'repo config'}.setup", None),
                (role.setup, f"{role.source or 'role ' + role.name}.setup",
                 item_checkout_path(worker) if item_checkout else None),
            ):
                failure = self._run_script(
                    template, worker, repo_config.setup_timeout_secs, where, cwd=cwd
                )
                if failure is not None:
                    # `failure` can quote the script's stderr, so it stays local.
                    return self._fail_worker(
                        worker, context, failure, report, notify_detail="setup script failed"
                    )
            if item_checkout:
                head = self.git.head_commit(item_checkout_path(worker))
                if head is not None:
                    self.ledger.set_state(f"{CHECKOUT_HEAD_PREFIX}{worker.id}", head)
                # Recorded after the setup scripts: what a repository's own setup writes,
                # such as local settings, is not a change the wake check should see.
                problem = self._record_workspace_files(worker)
                if problem is not None:
                    return self._fail_worker(
                        worker, context,
                        f"the agent's files in the worktree could not be recorded: {problem}",
                        report, notify_detail="the worktree could not be inspected",
                    )
            worker = self.ledger.update(worker, step=3)
            self._check_crash(3, crash_after_step)

        # Step 4: start the background session and record its ids.
        if worker.step < 4:
            worker = self._start_session(context, role, worker, report)
            self._check_crash(4, crash_after_step)

        return worker

    def _run_script(
        self, template: str | None, worker: Worker, timeout_secs: float, where: str,
        *, cwd: Path | None = None, trust_worktree: bool = True,
    ) -> str | None:
        """Run a setup command in the worktree, or in *cwd*, an item checkout.

        The program is found in the worktree, the main checkout or the absolute entries
        of PATH, never relative to *cwd*: an item checkout holds the item author's files.
        Without *trust_worktree* the worktree is left out too: once a session has worked
        in it, a program there may be the session's. A command in an item checkout runs
        with git hooks turned off, because a repository's relative `core.hooksPath` would
        name the author's hooks there.
        """
        if not template:
            return None
        argv = config_mod.render_argv(template, self._template_values(worker), where=where)
        roots = (
            *(((worker.worktree_path, "the worktree"),) if trust_worktree else ()),
            (worker.repo_path, "the main checkout"),
        )
        program = trusted_program(argv[0], [path for path, _ in roots])
        if program is None:
            named = ", ".join(name for _, name in roots)
            return f"setup program {argv[0]!r} is not in {named}, or an absolute PATH entry"
        argv[0] = program
        env = None if cwd is None else hardened_git_env(os.environ)
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd or worker.worktree_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_secs,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"setup script timed out after {timeout_secs:.0f}s"
        except OSError as exc:
            return f"setup script could not be run: {exc}"
        if result.returncode != 0:
            tail = sanitize.one_line(result.stderr or result.stdout, limit=300)
            return f"setup script exited {result.returncode}: {tail}"
        return None

    def _start_session(
        self,
        context: RepositoryContext,
        role: Role,
        worker: Worker,
        report: TickReport,
    ) -> Worker:
        # Adoption first: a previous run of this step may have started the session and
        # died before recording its ids.
        if role.runtime != worker.runtime:
            raise ConfigError(
                f"{worker.handle}: the role now names runtime '{role.runtime}', but this "
                f"worker was created on '{worker.runtime}'"
            )
        runtime = self.runtime(worker.runtime)
        owned_elsewhere = self._sessions_owned_by_other_workers(worker)
        self.sessions(worker.runtime, refresh=True)
        existing = self._find_worker_session(worker, exclude=owned_elsewhere)
        if existing is None:
            # Loading the role checked the main checkout, which may hold files that are
            # not committed. The session starts in the worktree, a checkout of `base`, so
            # what it needs, such as an agent file, must be there or in the user's files.
            try:
                runtime.check_role(role, worker.worktree_path)
            except ConfigError as exc:
                return self._fail_worker(
                    worker, context,
                    f"the session starts in a checkout of the base branch, so a file it "
                    f"needs must be committed there: {exc}",
                    report,
                    notify_detail="the role's files are not in the worktree",
                )
            prompt = config_mod.render(
                role.prompt,
                self._template_values(worker),
                where=f"{role.source or 'role ' + role.name}.prompt",
            )
            result = runtime.launch(
                LaunchSpec(
                    worktree=worker.worktree_path,
                    name=worker.handle,
                    prompt=prompt,
                    options=role.options,
                    model=role.model,
                    effort=role.effort,
                    read_paths=(
                        (item_checkout_path(worker),)
                        if role.checkout == roles_mod.CHECKOUT_ITEM else ()
                    ),
                ),
                warn=report.warnings.append,
            )
            self.sessions(worker.runtime, refresh=True)
            existing = self._find_worker_session(worker, exclude=owned_elsewhere)
            if existing is None:
                raise AgentRuntimeError(
                    f"started a session for {worker.handle} but it did not appear in the "
                    f"session listing of runtime {worker.runtime}; the launch printed: "
                    f"{sanitize.one_line(result.output, limit=200)!r}"
                )
        worker = self.ledger.update(
            worker,
            step=ledger_mod.LAST_STEP,
            state=ledger_mod.RUNNING,
            session_id=existing.id,
            session_uuid=existing.uuid,
            session_state=existing.state,
            working_since=time.time(),
            last_error=None,
        )
        self.ledger.record_session(
            worker, kind=ledger_mod.SESSION_INITIAL, session_id=existing.id, session_uuid=existing.uuid
        )
        report.launched.append(worker.item)
        return worker

    def _find_worker_session(self, worker: Worker, *, exclude: Collection[str] = ()) -> Session | None:
        """The worker's session in the current listing, by worktree or by name.

        A session is named after its worker (`role/item`). Sessions from before roles
        existed were named after the issue alone, so that name is tried second.
        """
        runtime = self.runtime(worker.runtime)
        sessions = self.sessions(worker.runtime)
        found = runtime.find_session(
            sessions, name=worker.handle, cwd=worker.worktree_path, exclude=exclude
        )
        if found is None:
            found = runtime.find_session(sessions, name=worker.item, exclude=exclude)
        return found

    def _sessions_owned_by_other_workers(self, worker: Worker) -> set[str]:
        return {
            other.session_id
            for other in self.ledger.recent_workers(200)
            if other.session_id and other.id != worker.id and other.runtime == worker.runtime
        }

    def _attach_command(self, worker: Worker) -> str:
        try:
            return self.runtime(worker.runtime).attach_command(worker.session_id or "")
        except ConfigError:
            return f"session {worker.session_id} of runtime {worker.runtime}"

    def _branch_for(self, repo_config: RepoConfig | None, role: str, item: str) -> str:
        # Two roles can take the same item, and git refuses to check one branch out in
        # two worktrees, so the role is part of the placeholder name.
        prefix = repo_config.branch_prefix if repo_config else "chargehand/"
        return f"{prefix}{role}/{item}"

    def _template_values(self, worker: Worker) -> dict[str, str]:
        checkout = item_checkout_path(worker)
        values = {
            "item": worker.item,
            "item_url": worker.url or "",
            "item_checkout": str(checkout if checkout.exists() else worker.worktree_path),
            "worktree": str(worker.worktree_path),
            "branch": worker.branch,
            "repo": str(worker.repo_path),
        }
        # The names from before work items existed.
        values["issue"] = values["item"]
        values["url"] = values["item_url"]
        return values

    @staticmethod
    def _check_crash(step: int, crash_after_step: int | None) -> None:
        if crash_after_step is not None and step == crash_after_step:
            raise LaunchAborted(step)

    def _fail_worker(
        self,
        worker: Worker,
        context: RepositoryContext | None,
        reason: str,
        report: TickReport,
        *,
        notify_detail: str = "worker failed",
    ) -> Worker:
        """*reason* may quote a setup script or a tracker, so it stays local.

        It is recorded in the ledger and shown by `--verbose-titles`; the notification
        carries only *notify_detail*, which this module writes.
        """
        worker = self.ledger.update(
            worker, state=ledger_mod.FAILED, last_error=sanitize.one_line(reason, limit=400)
        )
        self._sync_labels(worker, context, report)
        report.failed.append(f"{worker.item}: {reason}")
        log.error("%s failed: %s", worker.item, reason)
        self._notify(report, worker.item, "failed", worker.url, worker.repository, role=worker.role,
                     detail=notify_detail)
        return worker

    # ----- 10. garbage collection -------------------------------------------

    def _collect_garbage(self, report: TickReport) -> None:
        self._check_disk(report)
        self._close_out_finished_issues(report)
        for worker in self.ledger.workers_needing_gc():
            context = self.context_for_worker(worker)
            if context is None or not context.usable:
                continue
            if not self._item_closed(worker, context):
                continue
            blocker = self._worktree_holds_work(worker)
            if blocker:
                report.warnings.append(f"{worker.item}: not collected, {blocker}")
                continue
            if worker.session_id and not self._stop_session(worker):
                report.warnings.append(
                    f"{worker.item}: not collected, its session is still working"
                )
                continue
            self._remove_session(worker)
            try:
                branches_removed = self._remove_worktree(worker, force=False)
            except GitError as exc:
                report.warnings.append(f"{worker.item}: could not collect: {exc}")
                continue
            if not branches_removed:
                report.warnings.append(f"{worker.item}: collected, but {KEPT_BRANCH}")
            self.ledger.update(worker, gc_done=1, session_id=None, session_uuid=None)
            report.collected.append(worker.item)
        self._forget_checkout_heads()

    def _forget_checkout_heads(self) -> None:
        """Drop the records of each worker whose worktree is gone, by any path."""
        for key in self.ledger.all_state():
            prefix = next((p for p in WORKER_STATE_PREFIXES if key.startswith(p)), None)
            if prefix is None:
                continue
            worker_id = key.removeprefix(prefix)
            worker = self.ledger.get(int(worker_id)) if worker_id.isdigit() else None
            if worker is None or worker.gc_done:
                self.ledger.delete_state(key)

    def _close_out_finished_issues(self, report: TickReport) -> None:
        """A closed issue ends its worker, even one that is idle or parked as blocked.

        Without this, a repository with no status file never reaches a terminal state: `done`
        alone makes a worker idle, so its worktree would sit there until someone discarded it
        by hand. A run that is still working is reported rather than stopped: the issue
        closing is not a reason to destroy work in progress.
        """
        for worker in self.ledger.live_workers():
            if worker.state in (ledger_mod.LAUNCHING, ledger_mod.RUNNING):
                continue
            context = self.context_for_worker(worker)
            if context is None or not context.usable:
                continue
            if not self._item_closed(worker, context):
                continue
            if not self._stop_session(worker):
                report.warnings.append(
                    f"{worker.item}: issue is closed but its session would not stop"
                )
                continue
            updated = self.ledger.update(worker, state=ledger_mod.DONE, label_state=None)
            self._sync_labels(updated, context, report)
            report.transitions.append(f"{worker.item}: issue closed -> done")

    def _item_closed(self, worker: Worker, context: RepositoryContext) -> bool:
        """True once the worker's own item is closed or gone; False when open or unknown.

        Asked of the source of the worker's primary item: a ``[[source]]`` by that name,
        otherwise the repository's tracker. A worker that the queue label started asks
        the tracker; a worker from before subscriptions existed has no primary item.
        """
        source: Source | None
        item: WorkItem
        subscription = None if self._drives_label(worker) else self.ledger.primary_subscription(worker)
        if subscription is not None:
            item = subscription.work_item
            source = self.source(subscription.source)
            if source is None and subscription.source == context.repository.tracker.type:
                source = context.source
        elif self._drives_label(worker):
            item, source = context.item_of(worker), context.source
        else:
            return False
        if source is None:
            return False
        try:
            return not source.item_open(item)
        except ChargehandError as exc:
            # An outage: the item counts as open until a later tick can ask again.
            log.debug("%s", source_failure(source.name, exc))
            return False
        except Exception:
            # One source failure must not end the phase for every other worker.
            log.exception("source %s: item_open failed for %s", source.name, worker.handle)
            return False

    def _check_disk(self, report: TickReport) -> None:
        if not self.config.min_free_disk_gb:
            return
        target = self.config.worktree_root
        probe = target if target.exists() else target.parent
        try:
            free_gb = shutil.disk_usage(probe).free / 1024**3
        except OSError:
            return
        if free_gb < self.config.min_free_disk_gb:
            report.warnings.append(
                f"low disk: {free_gb:.1f} GB free under {target}, "
                f"below the {self.config.min_free_disk_gb:.0f} GB threshold"
            )

    # ----- notification -----------------------------------------------------

    def _notify(
        self,
        report: TickReport,
        item: str,
        state: str,
        url: str | None,
        repository: str | None,
        *,
        role: str | None = None,
        detail: str | None = None,
    ) -> bool:
        """*detail* must be a phrase this module writes.

        A notification usually crosses a third-party relay, so nothing authored by an
        agent, a setup script, or the tracker belongs in it: not an issue title, not a
        session's `waitingFor`, not a status file's `stop_reason`, not a script's stderr.
        Those are kept in the ledger and reached through `--verbose-titles` and `logs`.

        Returns False only when a configured command failed.
        """
        notification = Notification(
            item=item, state=state, url=url, repository=repository, role=role, detail=detail
        )
        if self.notifier.send(notification) or not self.notifier.enabled:
            report.notified.append(f"{item}: {state}")
            return True
        report.warnings.append(f"notification for {item} ({state}) failed; see the log")
        return False



def run_tick(
    config: MachineConfig,
    ledger: Ledger,
    *,
    crash_after_step: int | None = None,
    **kwargs,
) -> TickReport:
    return Runner(config, ledger, **kwargs).tick(crash_after_step=crash_after_step)


__all__ = ["Runner", "TickReport", "RepositoryContext", "StatusFile", "run_tick", "tick_lock"]
