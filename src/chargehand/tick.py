"""The tick: the only place side effects happen.

Order, once per tick:

1. reconcile interrupted launches, from three sides
2. apply recorded control requests
3. reap void leases and run the watchdog
4. diff session states against the ledger and notify on change
5. admit new issues within the launch throttle
6. launch them, ledger row first
7. collect garbage

A control command never calls Claude Code, git, or a tracker itself; it records a
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
from collections.abc import Collection, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from chargehand import claude as claude_mod
from chargehand import roles as roles_mod
from chargehand import config as config_mod
from chargehand import gitutil, ledger as ledger_mod, paths, sanitize, trackers
from chargehand.claude import ClaudeCLI, Session
from chargehand.config import MachineConfig, RepoConfig, Repository
from chargehand.errors import (
    AmbiguousWrite,
    ChargehandError,
    ClaudeError,
    ConfigError,
    GitError,
    LaunchAborted,
    PoolError,
    TickBusy,
    TrackerError,
)
from chargehand.ledger import Ledger, Worker
from chargehand.notify import Notification, Notifier
from chargehand.pool import NoopPool, Pool
from chargehand.roles import Role, StartRule
from chargehand.trackers import Issue, Status, Tracker

log = logging.getLogger(__name__)

# Transient "this repository cannot be talked to right now" conditions: an unreachable API and
# an unreadable credential alike. Deliberately NOT ConfigError — a broken template or a
# bad setup command must fail the worker loudly rather than inherit the forgiveness an
# outage gets, or a typo would retry forever while holding a launch slot.
TRACKER_FAILURES = (TrackerError,)

# watchdog_notified doubles as a small state: 0 nothing, 1 long-run notified,
# 2 a hard stop was attempted and did not take.
WATCHDOG_LONG_RUN = 1
WATCHDOG_STOP_FAILED = 2

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

# The queue status each worker state should be reflected by on the tracker.
_STATUS_FOR_STATE = {
    ledger_mod.LAUNCHING: Status.RUNNING,
    ledger_mod.RUNNING: Status.RUNNING,
    ledger_mod.BLOCKED: Status.BLOCKED,
    ledger_mod.PAUSED: Status.BLOCKED,
    ledger_mod.FAILED: Status.BLOCKED,
    ledger_mod.CANCELLED: Status.BLOCKED,
    ledger_mod.DONE: Status.DONE,
}


@contextmanager
def tick_lock(path: Path | None = None, *, wait_secs: float = 0.0):
    """Serialize ticks.

    launchd will not run two instances of one job, but `chargehand tick` and a control
    command can both want to act, so the invariant is enforced here rather than assumed.
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
    collected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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
            "collected": self.collected,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class RepositoryContext:
    repository: Repository
    tracker: Tracker | None = None
    repo_config: RepoConfig | None = None
    error: str | None = None
    roles: dict[str, Role | ConfigError] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.tracker is not None and self.repo_config is not None and self.error is None

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
                    tracker_type=self.tracker.type if self.tracker else None,
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
        claude: ClaudeCLI | None = None,
        git: gitutil.Git | None = None,
        pool: Pool | None = None,
        notifier: Notifier | None = None,
        tracker_factory=trackers.build,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.claude = claude or ClaudeCLI(config.claude_bin)
        self.git = git or gitutil.Git(config.git_bin)
        self.pool = pool or NoopPool()
        self.notifier = notifier or Notifier(config.notify)
        self._tracker_factory = tracker_factory
        self._contexts: dict[str, RepositoryContext] = {}
        self._sessions: list[Session] | None = None

    # ----- collaborators ----------------------------------------------------

    def sessions(self, *, refresh: bool = False) -> list[Session]:
        if refresh or self._sessions is None:
            try:
                self._sessions = self.claude.list_sessions()
            except ClaudeError as exc:
                log.error("could not list sessions: %s", exc)
                raise
        return self._sessions

    def invalidate_sessions(self) -> None:
        self._sessions = None

    def context(self, repository: Repository) -> RepositoryContext:
        cached = self._contexts.get(repository.name)
        if cached is not None:
            return cached
        context = RepositoryContext(repository=repository)
        try:
            context.tracker = self._tracker_factory(repository)
            context.repo_config = config_mod.load_repo_config(repository.path)
        except (ConfigError, TrackerError) as exc:
            context.error = str(exc)
        self._contexts[repository.name] = context
        return context

    def context_for_worker(self, worker: Worker) -> RepositoryContext | None:
        repository = self.config.repository_by_name(worker.repository)
        return self.context(repository) if repository else None

    # ----- the tick ---------------------------------------------------------

    def tick(self, *, crash_after_step: int | None = None) -> TickReport:
        report = TickReport()
        try:
            self.sessions(refresh=True)
        except ClaudeError as exc:
            # Without the session listing nothing below can be decided safely.
            report.errors.append(f"claude: {exc}")
            return report

        phases = (
            ("reconcile", lambda: self._reconcile(report)),
            ("requests", lambda: self._apply_requests(report)),
            ("watchdog", lambda: self._reap_and_watchdog(report)),
            ("supervise", lambda: self._diff_and_notify(report)),
            ("labels", lambda: self._sync_all_labels(report)),
            ("admit", lambda: self._admit(report, crash_after_step=crash_after_step)),
            ("collect", lambda: self._collect_garbage(report)),
        )
        for name, phase in phases:
            try:
                phase()
            except LaunchAborted:
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
        root = self.config.worktree_root
        live = self.ledger.live_workers()
        known = {str(Path(w.worktree)) for w in live} | {w.session_id for w in live if w.session_id}
        for session in self.sessions():
            if session.id in known or (session.cwd and str(Path(session.cwd)) in known):
                continue
            if not self._under(root, session.cwd):
                continue
            derived = claude_mod.worker_from_session(session, root)
            previous = self.ledger.worker_by_session_id(session.id) or (
                self.ledger.latest_worker_for(derived[1], derived[0]) if derived else None
            )
            # `cancel` stops a session but keeps its worktree, so a stopped session
            # belonging to a worker that already ended is expected, not an orphan.
            # Only one that is still working needs supervising again.
            if (
                previous is not None
                and previous.is_terminal
                and session.state != claude_mod.WORKING
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
            adopted = self._adopt_orphan(role, item, session, report)
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
            try:
                running = context.tracker.list(Status.RUNNING)  # type: ignore[union-attr]
            except TRACKER_FAILURES as exc:
                report.errors.append(f"repository {repository.name}: {exc}")
                continue
            for issue in running:
                if self.ledger.live_worker_for(issue.identifier) is not None:
                    continue
                if self._label_sync_pending(issue.identifier):
                    # The ledger knows what this label should be and has not managed to
                    # write it yet. That is a pending write, not a disagreement.
                    continue
                report.warnings.append(
                    f"{issue.identifier} carries the running label but has no live worker"
                )
                try:
                    context.tracker.mark(issue, Status.BLOCKED)  # type: ignore[union-attr]
                except (*TRACKER_FAILURES, AmbiguousWrite) as exc:
                    report.errors.append(f"{issue.identifier}: {exc}")
                    continue
                self._notify(report, issue.identifier, "blocked", issue.url, repository.name,
                             detail="running label without a live worker")

    def _stop_session(self, worker: Worker) -> bool:
        """Stop the session and confirm it stopped, by re-reading the listing.

        The exit code alone is not enough: a stop can report success and leave the
        session running. Everything destructive downstream — removing a worktree, hard
        stopping a run — depends on the session really being gone.
        """
        if not worker.session_id:
            return True
        self.claude.stop(worker.session_id)
        self.invalidate_sessions()
        try:
            sessions = self.sessions(refresh=True)
        except ClaudeError:
            return False
        session = next((s for s in sessions if s.id == worker.session_id), None)
        if session is None:
            return True
        return session.state in (claude_mod.DONE, claude_mod.STOPPED, claude_mod.FAILED)

    def _remove_session(self, worker: Worker) -> bool:
        if not worker.session_id:
            return True
        self.claude.remove(worker.session_id)
        self.invalidate_sessions()
        try:
            sessions = self.sessions(refresh=True)
        except ClaudeError:
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
        self, role_name: str, item: str, session: Session, report: TickReport
    ) -> Worker | None:
        """Bring a session started by a tick that died before recording ids under supervision."""
        worktree = Path(session.cwd) if session.cwd else None
        if worktree is None:
            return None
        repository = self._repository_owning(worktree)
        if repository is None or role_name not in repository.roles:
            return None
        context = self.context(repository)
        item_id, url = item, None
        if context.usable:
            try:
                issue = context.tracker.get(item)  # type: ignore[union-attr]
            except TRACKER_FAILURES:
                issue = None
            if issue is not None:
                item_id, url = issue.id, issue.url
        branch = self.git.worktree_branch(repository.path, worktree) or self._branch_for(
            context.repo_config, role_name, item
        )
        worker = self.ledger.create_worker(
            role=role_name,
            item=item,
            item_id=item_id,
            url=url,
            repository=repository.name,
            repo=repository.path,
            worktree=worktree,
            branch=branch,
        )
        worker = self._adopt(worker, session, report)
        report.warnings.append(f"adopted unsupervised session {session.id} as {worker.handle}")
        return worker

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
            return self._do_continue(worker, context, report)
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
                f"Check it with `claude attach {worker.session_id}`."
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
        self, worker: Worker, context: RepositoryContext | None, report: TickReport
    ) -> tuple[bool, str]:
        if not worker.session_id:
            return False, f"{worker.item} has no session to respawn"
        if worker.state not in (ledger_mod.PAUSED, ledger_mod.BLOCKED):
            return False, f"{worker.item} is {worker.state}, not paused or blocked"
        result = self.claude.respawn(worker.session_id)
        self.invalidate_sessions()
        if result.returncode != 0:
            return False, f"respawn failed: {sanitize.one_line(result.stderr or result.stdout, limit=200)}"
        # Same rule as stopping: confirm by re-reading rather than trusting the exit code.
        session = next(
            (s for s in self.sessions(refresh=True) if s.id == worker.session_id), None
        )
        if session is None or session.state in (claude_mod.STOPPED, claude_mod.FAILED):
            return False, (
                f"{worker.item}: respawn reported success but the session is "
                f"{session.state if session else 'gone'}"
            )
        worker = self.ledger.update(
            worker, state=ledger_mod.RUNNING, working_since=time.time(), watchdog_notified=0
        )
        self._sync_labels(worker, context, report)
        return True, "session respawned"

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
                    f"{worker.item}: the previous session could not be removed; "
                    f"a new worker would adopt it instead of starting. Remove it with "
                    f"`claude rm {worker.session_id}`."
                )
            worker = self.ledger.update(worker, session_id=None, session_uuid=None)
        try:
            issue = context.tracker.get(worker.item_id)  # type: ignore[union-attr]
            if issue is None:
                return False, f"{worker.item} no longer exists on the tracker"
            context.tracker.mark(issue, Status.QUEUED)  # type: ignore[union-attr]
        except (*TRACKER_FAILURES, AmbiguousWrite) as exc:
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
                f"in use. Check it with `claude attach {worker.session_id}`, or re-run with "
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
                f"remove it with `claude rm`{kept}"
            )
        return True, f"session removed, worktree and branch deleted{kept}"

    def _worktree_holds_work(self, worker: Worker) -> str | None:
        worktree = worker.worktree_path
        if not worktree.exists():
            return None
        unpushed = self.git.unpushed_commits(worktree)
        if unpushed < 0:
            return "its worktree could not be inspected"
        if unpushed > 0:
            return f"its worktree holds {unpushed} unpushed commit(s)"
        if self.git.is_dirty(worktree):
            return "its worktree has uncommitted changes"
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

    # ----- 3. reap and watchdog ---------------------------------------------

    def _reap_and_watchdog(self, report: TickReport) -> None:
        try:
            reaped = self.pool.reap()
        except PoolError as exc:
            # The watchdog below must run however the pool fails.
            report.errors.append(f"pool: {exc}")
            reaped = []
        for line in reaped:
            report.warnings.append(f"reaped lease {line}")

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

    # ----- 4. state diff and notification -----------------------------------

    def _diff_and_notify(self, report: TickReport) -> None:
        # Before the diff, so a send that fails below waits for the next tick rather than
        # being tried twice in this one.
        self._retry_pending_notifications(report)
        sessions = {s.id: s for s in self.sessions()}
        for worker in self.ledger.live_workers():
            if worker.state == ledger_mod.LAUNCHING:
                continue
            session = sessions.get(worker.session_id or "")
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
            if changes:
                worker = self.ledger.update(worker, **changes)

            worker = self._apply_session_state(worker, observed, report)

    def _apply_session_state(
        self, worker: Worker, observed: str, report: TickReport
    ) -> Worker:
        context = self.context_for_worker(worker)
        detail: str | None = None
        target_state = worker.state

        if observed == claude_mod.WORKING:
            target_state = ledger_mod.RUNNING
            if worker.state != ledger_mod.RUNNING:
                # A reply landed in agent view; the run is live again.
                worker = self.ledger.update(
                    worker, working_since=time.time(), watchdog_notified=0, last_error=None
                )
        elif observed == claude_mod.BLOCKED:
            # waiting_for comes from the session listing and can quote whatever the
            # agent was about to run. It stays in the ledger and in `status`; it never
            # reaches a notification.
            target_state, detail = ledger_mod.BLOCKED, "needs input"
        elif observed == claude_mod.DONE:
            target_state, detail = self._resolve_done(worker, context)
        elif observed == claude_mod.FAILED:
            target_state, detail = ledger_mod.BLOCKED, "session failed"
        elif observed == claude_mod.STOPPED:
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
            worker = self.ledger.update(worker, state=target_state)
            report.transitions.append(f"{worker.item}: {observed} -> {target_state}")
        self._sync_labels(worker, context, report)

        if observed != worker.notified_state and observed != claude_mod.WORKING:
            # Recorded only once delivered. A hook that failed, for example on the first
            # tick after a wake while the network is still down, is run again on the next
            # tick; otherwise the run would sit blocked with nobody told.
            if self._notify(report, worker.item, target_state, worker.url, worker.repository, role=worker.role, detail=detail):
                worker = self.ledger.update(worker, notified_state=observed)
            elif worker.is_terminal:
                # A finished worker leaves the live loop, so nothing would visit it again.
                self.ledger.set_state(f"{PENDING_NOTIFICATION_PREFIX}{worker.id}", detail or "")
        elif observed == claude_mod.WORKING and worker.notified_state != observed:
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
        """`done` is ambiguous: a session that asked in plain text also reads done."""
        try:
            role = context.role(worker.role) if context and context.usable else None
        except ConfigError:
            role = None
        if role is None or not role.status_file:
            return ledger_mod.BLOCKED, "finished or waiting — no status file, go look"
        path = Path(
            config_mod.render(
                role.status_file,
                self._template_values(worker),
                where=f"{role.source or role.name}.status_file",
            )
        ).expanduser()
        status = StatusFile.read(path, not_before=worker.created_at)
        if status.stale:
            return ledger_mod.BLOCKED, "its status file predates this worker — go look"
        if not status.present:
            return ledger_mod.BLOCKED, "finished but wrote no status file — go look"
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
        worker = self.ledger.latest_worker_for(item)
        if worker is None:
            return False
        desired = _STATUS_FOR_STATE.get(worker.state)
        return desired is not None and worker.label_state != desired.value

    def _sync_labels(
        self, worker: Worker, context: RepositoryContext | None, report: TickReport
    ) -> Worker:
        """Drive the tracker towards the label the worker state implies.

        `label_state` records what was last *verified*, so a write that could not be
        confirmed is simply retried on the next tick rather than assumed to have landed.
        """
        desired = _STATUS_FOR_STATE.get(worker.state)
        if desired is None or worker.label_state == desired.value:
            return worker
        if context is None or not context.usable:
            return worker
        try:
            issue = context.tracker.get(worker.item_id)  # type: ignore[union-attr]
            if issue is None:
                report.warnings.append(f"{worker.item} no longer exists on the tracker")
                return self.ledger.update(worker, label_state=desired.value)
            context.tracker.mark(issue, desired)  # type: ignore[union-attr]
        except AmbiguousWrite as exc:
            report.errors.append(f"{worker.item}: {exc}")
            return self.ledger.update(worker, last_error=str(exc)[:500])
        except TRACKER_FAILURES as exc:
            report.errors.append(f"{worker.item}: {exc}")
            return worker
        # Deliberately does not clear last_error: a successful label write says nothing
        # about why the worker failed, and that reason is what `--verbose-titles` shows.
        return self.ledger.update(worker, label_state=desired.value)

    # ----- 5/6. admission and launch ----------------------------------------

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

    def _admit(self, report: TickReport, *, crash_after_step: int | None = None) -> None:
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
                    role = context.role(role_name)
                except ConfigError as exc:
                    report.errors.append(f"repository {repository.name}: {exc}")
                    continue
                tracker_type = context.tracker.type  # type: ignore[union-attr]
                rule = role.start_rule_for(roles_mod.LABELED, tracker_type, repository.name)
                if rule is None:
                    # Started by some other event source, which the tracker queue is not.
                    continue
                self._admit_role(context, role, rule, report, crash_after_step=crash_after_step)

    def _admit_role(
        self,
        context: RepositoryContext,
        role: Role,
        rule: StartRule,
        report: TickReport,
        *,
        crash_after_step: int | None,
    ) -> None:
        repository = context.repository
        try:
            queued = context.tracker.list(Status.QUEUED)  # type: ignore[union-attr]
        except TRACKER_FAILURES as exc:
            report.errors.append(f"repository {repository.name}: {exc}")
            return

        for issue in queued:
            item = issue.identifier
            if not sanitize.is_safe_identifier(item):
                report.warnings.append(
                    f"skipping an issue whose identifier is not usable as a path or "
                    f"branch name: {sanitize.scrub_identifier(item)!r}"
                )
                continue
            # The queue label is one per issue, and every worker the queue starts drives that
            # label, so a second one would clear it when either finished. An issue that any
            # live worker holds stays with it, whatever the rule's `unless_subscribed` says;
            # re-adding the label while it runs must not start another role on it.
            if self.ledger.live_worker_for(item) is not None:
                continue
            stale = self._stale_worktree_for(role.name, item)
            if stale is not None:
                report.skipped.append(
                    f"{item}: the previous worker's worktree is still at {stale}; "
                    f"`chargehand retry {item}` reuses it safely, "
                    f"`chargehand discard {item} --yes` removes it"
                )
                continue

            working, open_workers, per_repository = self._counts()
            if working >= self.config.max_concurrent:
                report.skipped.append(
                    f"{item}: launch throttle ({working}/{self.config.max_concurrent})"
                )
                break
            if open_workers >= self.config.max_open_workers:
                report.skipped.append(
                    f"{item}: {open_workers}/{self.config.max_open_workers} open workers"
                )
                break
            if (
                repository.max_concurrent is not None
                and per_repository.get(repository.name, 0) >= repository.max_concurrent
            ):
                report.skipped.append(
                    f"{item}: repository {repository.name} is at its own limit "
                    f"({repository.max_concurrent})"
                )
                break
            in_role = self.ledger.live_count_by_role().get(role.name, 0)
            if role.policy.max_workers is not None and in_role >= role.policy.max_workers:
                report.skipped.append(
                    f"{item}: role {role.name} is at its limit ({role.policy.max_workers} workers)"
                )
                break

            try:
                self._launch(context, role, rule, issue, report,
                             crash_after_step=crash_after_step)
            except LaunchAborted:
                raise
            except ChargehandError as exc:
                report.errors.append(f"{item}: {exc}")

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
        issue: Issue,
        report: TickReport,
        *,
        crash_after_step: int | None = None,
    ) -> None:
        repository = context.repository
        repo_config = context.repo_config
        assert repo_config is not None
        worktree = self.config.worktree_root / role.name / issue.identifier
        branch = self._branch_for(repo_config, role.name, issue.identifier)

        # Step 0: the row exists before any external side effect.
        worker = self.ledger.create_worker(
            role=role.name,
            item=issue.identifier,
            item_id=issue.id,
            url=issue.url,
            repository=repository.name,
            repo=repository.path,
            worktree=worktree,
            branch=branch,
            start_rule=rule.name,
            runtime=role.runtime,
        )
        log.info("launching %s in %s", worker.handle, worktree)
        self._check_crash(0, crash_after_step)
        self._run_launch_steps(context, worker, report, crash_after_step=crash_after_step,
                               issue=issue)

    def _run_launch_steps(
        self,
        context: RepositoryContext,
        worker: Worker,
        report: TickReport,
        *,
        crash_after_step: int | None = None,
        issue: Issue | None = None,
    ) -> Worker:
        repo_config = context.repo_config
        tracker = context.tracker
        assert repo_config is not None and tracker is not None
        # A role that fails to load is a ChargehandError: the caller records it and the
        # launch retry counter fails the worker loudly rather than looping.
        role = context.role(worker.role)

        # Step 1: swap the labels, then verify by re-reading.
        if worker.step < 1:
            if issue is None:
                # Only reached when resuming an interrupted launch; a tracker or
                # credential failure here is caught by _reconcile_ledger, which hands
                # the worker its launch retry back rather than spending one.
                issue = tracker.get(worker.item_id)
                if issue is None:
                    return self._fail_worker(
                        worker, context, "the issue no longer exists on the tracker", report,
                        notify_detail="issue no longer exists",
                    )

            try:
                tracker.mark(issue, Status.RUNNING)
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

        # Step 2: fetch, then create the worktree on a placeholder branch.
        if worker.step < 2:
            self.git.fetch(context.repository.path, gitutil.remote_of(repo_config.base))
            self.git.add_worktree(
                context.repository.path, worker.worktree_path, worker.branch, repo_config.base
            )
            worker = self.ledger.update(worker, step=2)
            self._check_crash(2, crash_after_step)

        # Step 3: the repository's own setup script, then the role's.
        if worker.step < 3:
            for template, where in (
                (repo_config.setup, f"{repo_config.source or 'repo config'}.setup"),
                (role.setup, f"{role.source or 'role ' + role.name}.setup"),
            ):
                failure = self._run_script(template, worker, repo_config.setup_timeout_secs, where)
                if failure is not None:
                    # `failure` can quote the script's stderr, so it stays local.
                    return self._fail_worker(
                        worker, context, failure, report, notify_detail="setup script failed"
                    )
            worker = self.ledger.update(worker, step=3)
            self._check_crash(3, crash_after_step)

        # Step 4: start the background session and record its ids.
        if worker.step < 4:
            worker = self._start_session(context, role, worker, report)
            self._check_crash(4, crash_after_step)

        return worker

    def _run_script(
        self, template: str | None, worker: Worker, timeout_secs: float, where: str
    ) -> str | None:
        if not template:
            return None
        argv = config_mod.render_argv(template, self._template_values(worker), where=where)
        program = Path(argv[0])
        if not program.is_absolute():
            # The worktree is a full checkout, so its own copy of the script is the one
            # the run should use; the main checkout is only a fallback.
            for root in (worker.worktree_path, worker.repo_path):
                candidate = root / program
                if candidate.exists():
                    argv[0] = str(candidate)
                    break
        try:
            result = subprocess.run(
                argv,
                cwd=str(worker.worktree_path),
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
        owned_elsewhere = self._sessions_owned_by_other_workers(worker)
        self.sessions(refresh=True)
        existing = self._find_worker_session(worker, exclude=owned_elsewhere)
        if existing is None:
            prompt = config_mod.render(
                role.prompt,
                self._template_values(worker),
                where=f"{role.source or 'role ' + role.name}.prompt",
            )
            # Claude Code will not start a background session in a directory nobody has
            # accepted a trust dialog for, and every worktree here was made seconds ago.
            # There is no dialog a runner can answer, so the flag is written instead.
            if self.config.trust_worktrees and not self.claude.trust_worktree(
                worker.worktree_path
            ):
                report.warnings.append(
                    f"{worker.handle}: could not record {worker.worktree_path} as a "
                    f"trusted workspace; the launch may be refused"
                )
            result = self.claude.launch(
                worktree=worker.worktree_path,
                name=worker.handle,
                prompt=prompt,
                permission_mode=role.permission_mode,
                agent=role.agent,
                model=role.model,
                effort=role.effort,
                settings=self._launch_settings(role),
            )
            self.sessions(refresh=True)
            existing = self._find_worker_session(worker, exclude=owned_elsewhere)
            if existing is None:
                raise ClaudeError(
                    f"started a session for {worker.handle} but it did not appear in "
                    f"`claude agents --json --all`; the launch printed: "
                    f"{sanitize.one_line(result.stdout or result.stderr, limit=200)!r}"
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
        sessions = self.sessions()
        found = self.claude.find_session(
            sessions, name=worker.handle, cwd=worker.worktree_path, exclude=exclude
        )
        if found is None:
            found = self.claude.find_session(sessions, name=worker.item, exclude=exclude)
        return found

    def _sessions_owned_by_other_workers(self, worker: Worker) -> set[str]:
        return {
            other.session_id
            for other in self.ledger.recent_workers(200)
            if other.session_id and other.id != worker.id
        }

    def _launch_settings(self, role: Role) -> dict[str, object] | None:
        """Deny rules for runner-launched sessions, and the role's own allow rules.

        A guardrail, not a sandbox: sessions run as the operator's user, so this only
        stops the easy case of a session steering the runner or stopping its siblings.
        """
        deny = self.config.deny_rules if self.config.pass_deny_rules_at_launch else ()
        return role.launch_settings(tuple(deny))

    def _branch_for(self, repo_config: RepoConfig | None, role: str, item: str) -> str:
        # Two roles can take the same item, and git refuses to check one branch out in
        # two worktrees, so the role is part of the placeholder name.
        prefix = repo_config.branch_prefix if repo_config else "chargehand/"
        return f"{prefix}{role}/{item}"

    def _template_values(self, worker: Worker) -> dict[str, str]:
        values = {
            "item": worker.item,
            "item_url": worker.url or "",
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

    # ----- 7. garbage collection --------------------------------------------

    def _collect_garbage(self, report: TickReport) -> None:
        self._check_disk(report)
        self._close_out_finished_issues(report)
        for worker in self.ledger.workers_needing_gc():
            context = self.context_for_worker(worker)
            if context is None or not context.usable:
                continue
            try:
                issue = context.tracker.get(worker.item_id)  # type: ignore[union-attr]
            except TRACKER_FAILURES:
                continue
            if issue is not None and not issue.closed:
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

    def _close_out_finished_issues(self, report: TickReport) -> None:
        """A closed issue ends its worker, even one parked as blocked.

        Without this, a repository with no status file never reaches a terminal state — `done`
        alone means "go look" — so its worktree would sit there until someone discarded it
        by hand. A run that is still working is reported rather than stopped: the issue
        closing is not a reason to destroy work in progress.
        """
        for worker in self.ledger.live_workers():
            if worker.state in (ledger_mod.LAUNCHING, ledger_mod.RUNNING):
                continue
            context = self.context_for_worker(worker)
            if context is None or not context.usable:
                continue
            try:
                issue = context.tracker.get(worker.item_id)  # type: ignore[union-attr]
            except TRACKER_FAILURES:
                continue
            if issue is not None and not issue.closed:
                continue
            if not self._stop_session(worker):
                report.warnings.append(
                    f"{worker.item}: issue is closed but its session would not stop"
                )
                continue
            updated = self.ledger.update(worker, state=ledger_mod.DONE, label_state=None)
            self._sync_labels(updated, context, report)
            report.transitions.append(f"{worker.item}: issue closed -> done")

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
