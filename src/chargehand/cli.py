"""The control surface: a CLI, reached over SSH. There is no server.

Two rules shape this module. A mutating command never calls a runtime, git, or a
tracker itself — it records a request and lets a tick apply it, so a cancel cannot
race a launch in progress. And nothing that originated outside chargehand is printed
without going through :mod:`chargehand.sanitize`; issue titles and agent output are
attacker-influenced, so they stay out of default output entirely.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from chargehand import __version__, install, paths, sanitize, trackers
from chargehand import roles as roles_mod
from chargehand import runtime as runtime_mod
from chargehand.config import MachineConfig, load_machine_config, load_repo_config
from chargehand.errors import ChargehandError, ConfigError, TickBusy
from chargehand.gitutil import Git
from chargehand.ledger import Ledger, Worker, open_ledger
from chargehand.notify import Notifier
from chargehand.tick import Runner, tick_lock
from chargehand.trackers import Status

DESCRIPTION = (
    "Supervised Claude Code workers in fresh git worktrees on a machine you own: "
    "label an issue, and a worker in the role you configured picks it up."
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3

# How long a control command waits for the scheduled job to apply its request, and how
# often it kick-starts the job meanwhile.
SCHEDULED_JOB_WAIT_SECS = 30.0
KICKSTART_RETRY_SECS = 2.0

# The worker states whose session `status` offers to attach to.
ATTACH_STATES = ("blocked", "idle", "queued")

# The parts of a tick report that `tick` prints, in this order. `skipped` holds the
# launches and wakes that a cap or a pause held back.
TICK_FIELDS = (
    "launched", "adopted", "resumed", "woken", "failed", "transitions", "requests",
    "collected", "skipped", "warnings", "errors",
)

log = logging.getLogger("chargehand")


# ----- shared plumbing ------------------------------------------------------


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else (logging.INFO if verbosity == 1 else logging.DEBUG)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr
    )


def _load_config(args: argparse.Namespace) -> MachineConfig:
    return load_machine_config(Path(args.config).expanduser() if args.config else None)


def _runner(config: MachineConfig, ledger: Ledger) -> Runner:
    return Runner(
        config,
        ledger,
        git=Git(config.git_bin),
        notifier=Notifier(config.notify),
    )


def _print(text: str = "") -> None:
    print(text)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _described(kind: str, target: str | None) -> str:
    return f"{kind} {target}" if target else kind


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


# ----- status ---------------------------------------------------------------


def _elapsed(since: float | None, now: float | None = None) -> str:
    if not since:
        return "-"
    return _span((now or time.time()) - since)


def _span(secs: float) -> str:
    delta = max(0.0, secs)
    if delta < 90:
        return f"{delta:.0f}s"
    if delta < 5400:
        return f"{delta / 60:.0f}m"
    if delta < 172800:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.1f}d"


def _branch_is_ours(worker: Worker) -> bool:
    # A session can rename its branch after the tracker's convention, which puts a slug of
    # the issue title into the name, and an adopted session is recorded under the branch
    # it is on. The runner's own placeholder ends in the item.
    return worker.branch.endswith(worker.item)


def _worker_dict(
    worker: Worker, *, verbose: bool, runner: Runner | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "item": worker.item,
        "role": worker.role,
        "runtime": worker.runtime,
        "repository": worker.repository,
        "state": worker.state,
        "session_state": worker.session_state,
        # Whether it is waiting is a state; why it says it is waiting is session text.
        "waiting": bool(worker.waiting_for),
        "step": worker.step,
        "launch_attempts": worker.launch_attempts,
        "session_id": worker.session_id,
        "worktree": worker.worktree,
        "branch": worker.branch if verbose or _branch_is_ours(worker) else None,
        "url": worker.url,
        "pr_url": worker.pr_url,
        "label_state": worker.label_state,
        "wakes": worker.wakes,
        "elapsed_secs": round(time.time() - worker.created_at, 1),
        "working_since": worker.working_since,
        "created_at": worker.created_at,
        "updated_at": worker.updated_at,
    }
    # An idle or queued worker ended a turn whose result the operator may want to read.
    if worker.state in ATTACH_STATES and worker.session_id and runner is not None:
        try:
            payload["attach"] = runner.runtime(worker.runtime).attach_command(worker.session_id)
        except ConfigError:
            pass
    if verbose:
        if worker.waiting_for:
            payload["waiting_for"] = sanitize.one_line(worker.waiting_for)
        if worker.last_error:
            payload["last_error"] = sanitize.one_line(worker.last_error, limit=300)
    return payload


def build_status(
    config: MachineConfig,
    ledger: Ledger,
    *,
    verbose: bool = False,
    include_queue: bool = True,
) -> dict[str, object]:
    now = time.time()
    workers = ledger.live_workers()
    runner = _runner(config, ledger)

    sessions_payload: list[dict[str, object]] = []
    session_errors: list[str] = []
    for runtime_name in runner.runtime_names():
        try:
            sessions = runner.runtime(runtime_name).list_sessions()
        except ChargehandError as exc:
            session_errors.append(f"{runtime_name}: {exc}")
            continue
        for session in sessions:
            entry: dict[str, object] = {
                "runtime": runtime_name,
                "id": session.id,
                "state": session.state,
                "raw_state": sanitize.one_line(session.raw_state, limit=40),
                "cwd": session.cwd,
                "age_secs": round(session.age_secs(now) or 0.0, 1) if session.started_at else None,
            }
            if verbose:
                entry["name"] = sanitize.one_line(session.name or "", limit=64)
                entry["waiting_for"] = sanitize.one_line(session.waiting_for or "", limit=120)
            sessions_payload.append(entry)
    session_error = "; ".join(session_errors) or None

    repositories_payload = []
    for repository in config.repositories:
        entry: dict[str, object] = {
            "name": repository.name,
            "path": str(repository.path),
            "tracker": repository.tracker.type,
            "paused": ledger.is_paused(repository.name),
            "labels": repository.labels.as_dict(),
        }
        if include_queue:
            try:
                tracker = trackers.build(repository)
                queued = tracker.list(Status.QUEUED)
                entry["queued"] = len(queued)
                entry["queued_issues"] = [issue.identifier for issue in queued]
                if verbose:
                    entry["queued_titles"] = [sanitize.one_line(issue.title) for issue in queued]
            except ChargehandError as exc:
                entry["queue_error"] = str(exc)
        repo_config = None
        try:
            repo_config = load_repo_config(repository.path)
        except ConfigError as exc:
            entry["config_error"] = str(exc)
        roles_payload: dict[str, object] = {}
        for name in repository.roles:
            try:
                role = roles_mod.load_role(
                    repository.path, name, repo_config=repo_config,
                    tracker_type=repository.tracker.type,
                )
            except ConfigError as exc:
                roles_payload[name] = {"error": str(exc)}
                continue
            roles_payload[name] = {
                "runtime": role.runtime,
                "prompt": role.prompt,
                "permission_mode": role.permission_mode,
                "agent": role.agent,
                "model": role.model,
                "effort": role.effort,
                "source": str(role.source) if role.source else None,
            }
        entry["roles"] = roles_payload
        repositories_payload.append(entry)

    root = config.worktree_root
    try:
        usage = shutil.disk_usage(root if root.exists() else root.parent)
        free_gb: float | None = round(usage.free / 1024**3, 1)
    except OSError:
        free_gb = None

    return {
        "generated_at": now,
        "version": __version__,
        "machine": {
            "hostname": socket.gethostname(),
            "worktree_root": str(root),
            "free_disk_gb": free_gb,
            "min_free_disk_gb": config.min_free_disk_gb,
            "launchd_loaded": install.is_loaded(),
            "poll_interval_secs": config.poll_interval_secs,
        },
        "runner": {
            "paused": ledger.is_paused(),
            "paused_repositories": ledger.paused_repositories(),
            "max_concurrent": config.max_concurrent,
            "max_open_workers": config.max_open_workers,
            "working": sum(1 for w in workers if w.state in ("launching", "running")),
            "open": len(workers),
            "pending_requests": [
                {"kind": r["kind"], "target": r["target"], "created_at": r["created_at"]}
                for r in ledger.pending_requests()
            ],
        },
        "repositories": repositories_payload,
        "workers": [_worker_dict(w, verbose=verbose, runner=runner) for w in workers],
        "recent": [
            _worker_dict(w, verbose=verbose, runner=runner)
            for w in ledger.recent_workers(10)
            if w.is_terminal
        ],
        "sessions": sessions_payload,
        "session_error": session_error,
    }


def _render_table(status: dict[str, object], *, verbose: bool) -> str:
    lines: list[str] = []
    machine = status["machine"]  # type: ignore[index]
    runner = status["runner"]  # type: ignore[index]
    paused_note = " PAUSED" if runner["paused"] else ""
    lines.append(
        f"chargehand {status['version']} on {machine['hostname']}{paused_note} — "
        f"{runner['working']}/{runner['max_concurrent']} working, "
        f"{runner['open']}/{runner['max_open_workers']} open, "
        f"{machine['free_disk_gb']} GB free"
    )
    if runner["paused_repositories"]:
        lines.append(f"  paused repositories: {', '.join(runner['paused_repositories'])}")
    if status.get("session_error"):
        lines.append(f"  ! {sanitize.one_line(str(status['session_error']), limit=160)}")

    lines.append("")
    lines.append("repositories")
    for repository in status["repositories"]:  # type: ignore[index]
        queued = repository.get("queued")
        detail = f"queued {queued}" if queued is not None else repository.get("queue_error", "")
        flag = " (paused)" if repository["paused"] else ""
        roles = repository.get("roles") or {}
        lines.append(
            f"  {repository['name']:<16}{flag} {repository['tracker']:<8} "
            f"{', '.join(roles) or '-':<24} {sanitize.one_line(str(detail), limit=60)}"
        )
        if repository.get("config_error"):
            lines.append(f"    ! {sanitize.one_line(repository['config_error'], limit=160)}")
        for name, role in roles.items():
            if isinstance(role, dict) and role.get("error"):
                lines.append(f"    ! role {name}: {sanitize.one_line(str(role['error']), limit=150)}")

    workers = status["workers"]  # type: ignore[index]
    lines.append("")
    if not workers:
        lines.append("no live workers")
    else:
        header = (
            f"  {'ITEM':<14} {'ROLE':<12} {'STATE':<10} {'SESSION':<9} {'AGE':>6} {'STEP':>4}  DETAIL"
        )
        lines.append(header)
        for worker in workers:
            detail = worker.get("waiting_for") or worker.get("pr_url") or (
                "waiting" if worker.get("waiting") else ""
            )
            if worker.get("attach"):
                detail = f"{detail} · {worker['attach']}".strip(" ·")
            lines.append(
                f"  {sanitize.scrub_identifier(str(worker['item'])):<14} "
                f"{sanitize.scrub_identifier(str(worker['role'])):<12} {worker['state']:<10} "
                f"{str(worker['session_state'] or '-'):<9} "
                f"{_elapsed(worker['created_at']):>6} {worker['step']:>4}  "
                f"{sanitize.one_line(str(detail), limit=70)}"
            )
            if verbose and worker.get("last_error"):
                lines.append(f"      ! {worker['last_error']}")

    if runner["pending_requests"]:
        lines.append("")
        lines.append("pending control requests")
        for request in runner["pending_requests"]:
            lines.append(f"  {request['kind']} {request['target'] or ''}".rstrip())
    return "\n".join(lines)


# ----- commands -------------------------------------------------------------


def cmd_tick(args: argparse.Namespace) -> int:
    # Every line is stamped. The scheduled job appends this output to its log, where a
    # line that does not say when it was written cannot show whether ticks run on time or
    # when an error began.
    try:
        config = _load_config(args)
        with open_ledger() as ledger, tick_lock(wait_secs=args.wait):
            report = _runner(config, ledger).tick(crash_after_step=args.crash_after_step)
    except TickBusy as exc:
        print(f"{_stamp()} {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ChargehandError as exc:
        print(f"{_stamp()} chargehand: {sanitize.one_line(str(exc), limit=500)}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        _emit_json(report.as_dict())
    else:
        stamp = _stamp()
        for field in TICK_FIELDS:
            for entry in getattr(report, field):
                _print(f"{stamp} {field[:-1] if field.endswith('s') else field}: "
                       f"{sanitize.one_line(str(entry), limit=200)}")
        if not any(getattr(report, f) for f in TICK_FIELDS):
            _print(f"{stamp} nothing to do")
    return EXIT_ERROR if report.errors else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with open_ledger() as ledger:
        status = build_status(
            config, ledger, verbose=args.verbose_titles, include_queue=not args.no_queue
        )
    if args.json:
        _emit_json(status)
    else:
        _print(_render_table(status, verbose=args.verbose_titles))
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    config = _load_config(args)
    interval = args.interval or max(5, min(config.poll_interval_secs, 30))
    try:
        while True:
            with open_ledger() as ledger:
                status = build_status(
                    config, ledger, verbose=args.verbose_titles, include_queue=not args.no_queue
                )
            # Our own output, so the escape here is ours to write.
            sys.stdout.write("\x1b[H\x1b[2J")
            sys.stdout.write(_render_table(status, verbose=args.verbose_titles))
            sys.stdout.write(f"\n\nrefreshing every {interval}s — ctrl-c to stop\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        _print()
        return EXIT_OK


def _find_worker(ledger: Ledger, item: str, role: str | None) -> tuple[Worker | None, str]:
    live = ledger.live_workers_for(item)
    if role is not None:
        live = [w for w in live if w.role == role]
    if len(live) > 1:
        names = ", ".join(sorted(w.role for w in live))
        return None, f"{sanitize.scrub_identifier(item)} has live workers in several roles ({names}); pass --role"
    worker = live[0] if live else ledger.latest_worker_for(item, role)
    if worker is None:
        return None, f"no worker recorded for {sanitize.scrub_identifier(item)}"
    return worker, ""


def cmd_logs(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with open_ledger() as ledger:
        worker, problem = _find_worker(ledger, args.item, args.role)
    if worker is None:
        print(problem, file=sys.stderr)
        return EXIT_ERROR
    if not worker.session_id:
        print(f"{worker.handle} has no session", file=sys.stderr)
        return EXIT_ERROR
    try:
        output = runtime_mod.create(worker.runtime, config).logs(worker.session_id, lines=args.lines)
    except ChargehandError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    # Agent output is untrusted: strip anything that could act on this terminal.
    _print(sanitize.clean(output))
    return EXIT_OK


def _wait_for_request(ledger: Ledger, request_id: int, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        request = ledger.get_request(request_id)
        remaining = deadline - time.monotonic()
        if (request and request["applied_at"] is not None) or remaining <= 0:
            return request
        # Never past the deadline, or a short wait between kick-starts becomes a long one.
        time.sleep(min(0.25, remaining))


def _apply_through_the_scheduled_job(ledger: Ledger, request_id: int, timeout: float) -> dict | None:
    """Kick-start the loaded job until one of its ticks applies the request.

    `launchctl kickstart` without `-k` does nothing while the job is already running, and
    that tick may be past the point where it reads requests. A command issued while the
    previous command's tick is still finishing lands in exactly that window, so asking once
    would leave the request to an inline tick instead: outside the job's environment and
    missing from its log.
    """
    deadline = time.monotonic() + timeout
    while True:
        install.kickstart()
        remaining = max(0.0, deadline - time.monotonic())
        request = _wait_for_request(ledger, request_id, min(KICKSTART_RETRY_SECS, remaining))
        if (request and request["applied_at"] is not None) or time.monotonic() >= deadline:
            return request


def _runs_the_scheduled_job(args: argparse.Namespace) -> bool:
    """Whether the loaded launchd job would tick *this* configuration and ledger.

    The plist carries no path overrides, so the job always runs the default instance.
    A process given `--config`, `CHARGEHAND_CONFIG` or `CHARGEHAND_STATE_DIR` is a
    different instance sharing the binary, and kick-starting the job from it would run
    a tick against a ledger it is not waiting on.
    """
    return paths.is_default_instance() and not args.config


def _request(args: argparse.Namespace, kind: str, target: str | None = None, **extra) -> int:
    """Record a control request and let a tick apply it."""
    config = _load_config(args)
    with open_ledger() as ledger:
        request_id = ledger.record_request(kind, target, **extra)
        if args.no_wait:
            _print(f"recorded: {_described(kind, target)}")
            return EXIT_OK

        # Prefer the scheduled job when it is loaded: it runs inside the GUI session,
        # where the keychain and Claude Code's credentials are reachable.
        #
        # Only when this process is the instance that job runs, though. The plist carries
        # no path overrides, so a second instance started with CHARGEHAND_CONFIG or
        # CHARGEHAND_STATE_DIR would be kick-starting a tick against somebody else's
        # ledger, and then waiting for a request that tick will never see.
        applied = None
        if _runs_the_scheduled_job(args) and install.is_loaded():
            applied = _apply_through_the_scheduled_job(
                ledger, request_id, min(args.wait, SCHEDULED_JOB_WAIT_SECS)
            )
        if applied is None or applied["applied_at"] is None:
            try:
                with tick_lock(wait_secs=args.wait):
                    _runner(config, ledger).tick()
            except TickBusy:
                pass
            applied = _wait_for_request(ledger, request_id, args.wait)

    if applied is None or applied["applied_at"] is None:
        _print(f"recorded: {_described(kind, target)} — the next tick will apply it")
        return EXIT_OK
    message = sanitize.one_line(applied["message"] or "", limit=300)
    if applied["ok"]:
        _print(f"{_described(kind, target)}: {message}")
        return EXIT_OK
    print(f"refused: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _role_args(args: argparse.Namespace) -> dict[str, object]:
    return {"role": args.role} if getattr(args, "role", None) else {}


def cmd_pause(args: argparse.Namespace) -> int:
    return _request(args, "pause", args.repository)


def cmd_resume(args: argparse.Namespace) -> int:
    return _request(args, "resume", args.repository)


def cmd_cancel(args: argparse.Namespace) -> int:
    return _request(args, "cancel", args.item, **_role_args(args))


def cmd_stop(args: argparse.Namespace) -> int:
    return _request(args, "stop", args.item, **_role_args(args))


def cmd_continue(args: argparse.Namespace) -> int:
    return _request(args, "continue", args.item, force=args.force, **_role_args(args))


def cmd_retry(args: argparse.Namespace) -> int:
    return _request(args, "retry", args.item, force=args.force, **_role_args(args))


def cmd_discard(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "discard removes the session, the worktree, and the local branch. "
            "Re-run with --yes.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return _request(args, "discard", args.item, force=args.force, **_role_args(args))


def cmd_install(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("install writes a launchd LaunchAgent, which is macOS only. On other "
              "platforms schedule `chargehand tick` with your own timer.", file=sys.stderr)
        return EXIT_ERROR
    config = _load_config(args)
    plist = install.write_plist(interval_secs=args.interval or config.poll_interval_secs)
    _print(f"wrote {plist}")
    for check in install.job_checks(config, install.installed_job(plist) or {}):
        if check.status in (install.WARN, install.FAIL):
            print(f"warning: {check.detail}", file=sys.stderr)
    if not args.load:
        _print("")
        _print("Not loaded. To start it:")
        _print(f"  launchctl bootstrap {install.domain()} {plist}")
        _print(f"  launchctl kickstart {install.service_target()}   # run a tick now")
        _print("")
        _print("Re-run with --load to do that now.")
        return EXIT_OK
    if install.is_loaded():
        install.bootout()
    result = install.bootstrap(plist)
    if result.returncode != 0:
        print((result.stderr or result.stdout).strip(), file=sys.stderr)
        return EXIT_ERROR
    _print(f"loaded {paths.LAUNCHD_LABEL}")
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    if install.is_loaded():
        install.bootout()
        _print(f"unloaded {paths.LAUNCHD_LABEL}")
    plist = paths.launch_agent_plist()
    if plist.exists() and args.remove_plist:
        plist.unlink()
        _print(f"removed {plist}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        config: MachineConfig | None = _load_config(args)
    except ConfigError as exc:
        config = None
        if args.json:
            _emit_json([{"name": "config", "status": "fail", "detail": str(exc)}])
            return EXIT_ERROR
        print(f"config: {exc}", file=sys.stderr)
    checks = install.doctor(config, check_trackers=not args.no_tracker)
    if args.json:
        _emit_json([check.as_dict() for check in checks])
    else:
        for check in checks:
            marker = {"ok": "  ok  ", "info": " info ", "warn": " warn ", "fail": " FAIL "}.get(
                check.status, check.status
            )
            _print(f"[{marker}] {check.name:<22} {sanitize.one_line(check.detail, limit=140)}")
    return EXIT_ERROR if install.worst(checks) == install.FAIL else EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    if args.machine:
        target = Path(args.output).expanduser() if args.output else paths.config_file()
        if target.exists() and not args.force:
            print(f"{target} already exists; pass --force to overwrite", file=sys.stderr)
            return EXIT_REFUSED
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            (paths.templates_dir() / "config.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        _print(f"wrote {target}")
        notify = target.parent / "notify.sh"
        wrote_notify = not notify.exists()
        if wrote_notify:
            notify.write_text(
                (paths.templates_dir() / "notify.sh").read_text(encoding="utf-8"), encoding="utf-8"
            )
            notify.chmod(0o755)
            _print(f"wrote {notify}")
        _print("")
        _print(f"Edit {target.name}, then run `chargehand doctor`.")
        if wrote_notify:
            # The hook as shipped only prints, and the runner captures what it prints, so
            # without this a new setup notifies nobody and looks as if it works.
            _print(f"{notify.name} only prints until you enable a method in it: a macOS "
                   "notification, email, or a push service. Its comments and the "
                   "Notifications section of the README show how.")
        return EXIT_OK

    repo = Path(args.repo or ".").expanduser().resolve()
    try:
        written = install.init_repo(repo, force=args.force, roles=args.role)
    except ChargehandError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    for target in written:
        _print(f"wrote {target}")
    if args.role:
        _print(f"Add {', '.join(args.role)} to the repository's `roles` in your machine "
               "configuration. Commit an agent file to the base branch, or copy it to "
               "~/.claude/agents: sessions start in a checkout of the base branch. The "
               "comments at the top of each role file say what else it needs.")
    else:
        _print("Edit `base` in the repository configuration and `prompt` in the role, then add "
               "the repository to your machine configuration.")
    return EXIT_OK


def cmd_templates(args: argparse.Namespace) -> int:
    directory = paths.templates_dir()
    if args.name:
        candidate = directory / args.name
        if not candidate.is_file():
            print(f"no template named '{args.name}'", file=sys.stderr)
            return EXIT_ERROR
        sys.stdout.write(candidate.read_text(encoding="utf-8"))
        return EXIT_OK
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            _print(str(path.relative_to(directory)))
    return EXIT_OK


# ----- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # No abbreviated flags. A permission rule that gates a flag matches the command text,
    # so `status --verbose` must not quietly mean `status --verbose-titles`.
    parser = argparse.ArgumentParser(
        prog="chargehand", description=DESCRIPTION, allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"chargehand {__version__}")
    parser.add_argument("--config", help=f"machine configuration file (default: {paths.config_file()})")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="log more; repeat for debug")
    subparsers = parser.add_subparsers(dest="command")

    def add(name: str, handler, help_text: str, *, aliases: Sequence[str] = ()):
        sub = subparsers.add_parser(
            name, help=help_text, aliases=list(aliases), allow_abbrev=False
        )
        sub.set_defaults(handler=handler)
        return sub

    def add_item(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("item", help="the work item, such as an issue identifier")
        sub.add_argument("--role", help="the worker's role, when several roles hold the item")

    tick = add("tick", cmd_tick, "run one tick now")
    tick.add_argument("--json", action="store_true")
    tick.add_argument("--wait", type=float, default=0.0,
                      help="seconds to wait for a running tick to finish")
    tick.add_argument("--crash-after-step", type=int, metavar="N",
                      help="fault injection: abort a launch right after step N (0-4)")

    for name, handler, help_text in (
        ("status", cmd_status, "queue, workers, sessions, machine"),
        ("watch", cmd_watch, "self-refreshing status table"),
    ):
        sub = add(name, handler, help_text)
        sub.add_argument("--json", action="store_true")
        sub.add_argument("--verbose-titles", action="store_true",
                         help="include untrusted issue titles and session names")
        sub.add_argument("--no-queue", action="store_true", help="skip tracker calls")
        if name == "watch":
            sub.add_argument("--interval", type=float, help="refresh seconds")

    logs = add("logs", cmd_logs, "tail one session's output (untrusted text)")
    add_item(logs)
    logs.add_argument("--lines", type=int, default=200)

    for name, handler, help_text in (
        ("pause", cmd_pause, "stop admitting new launches"),
        ("resume", cmd_resume, "resume admitting new launches"),
    ):
        sub = add(name, handler, help_text)
        sub.add_argument("--repository", "--route", dest="repository", help="limit to one repository")
        _add_request_flags(sub)

    for name, handler, help_text in (
        ("cancel", cmd_cancel, "stop the session and mark the item blocked; keep the worktree"),
        ("stop", cmd_stop, "pause one worker"),
    ):
        sub = add(name, handler, help_text)
        add_item(sub)
        _add_request_flags(sub)

    continue_cmd = add("continue", cmd_continue, "respawn a stopped or blocked worker")
    add_item(continue_cmd)
    continue_cmd.add_argument(
        "--force", action="store_true",
        help="accept the agent's files in the worktree as they are now, after you checked them",
    )
    _add_request_flags(continue_cmd)

    retry = add("retry", cmd_retry, "start a new worker for a failed or cancelled one")
    add_item(retry)
    retry.add_argument("--force", action="store_true", help="discard an unpushed worktree first")
    _add_request_flags(retry)

    discard = add("discard", cmd_discard, "remove the session, worktree, and local branch")
    add_item(discard)
    discard.add_argument("--yes", action="store_true", help="required: this destroys work")
    discard.add_argument("--force", action="store_true", help="discard even with unpushed commits")
    _add_request_flags(discard)

    install_cmd = add("install", cmd_install, "write the launchd job that runs the tick")
    install_cmd.add_argument("--load", action="store_true", help="also bootstrap it now")
    install_cmd.add_argument("--interval", type=int, help="seconds between ticks")

    uninstall = add("uninstall", cmd_uninstall, "unload the launchd job")
    uninstall.add_argument("--remove-plist", action="store_true")

    doctor = add("doctor", cmd_doctor, "check this machine's setup")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--no-tracker", action="store_true", help="skip tracker calls")

    init = add("init", cmd_init, "write a starter configuration")
    init.add_argument("--machine", action="store_true", help="machine config instead of repo config")
    init.add_argument("--repo", help="repository root (default: the current directory)")
    init.add_argument("--output", help="where to write the machine config")
    init.add_argument("--force", action="store_true")
    init.add_argument(
        "--role", action="append", default=[], metavar="NAME",
        help="write this starter role and its agent file instead of the implementer; "
             "repeatable (e.g. pr-reviewer)",
    )

    templates = add("templates", cmd_templates, "print a bundled template")
    templates.add_argument("name", nargs="?", help="e.g. skill/SKILL.md, roles/implementer.toml")

    return parser


def _add_request_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--wait", type=float, default=90.0,
                     help="seconds to wait for a tick to apply the request")
    sub.add_argument("--no-wait", action="store_true", help="record the request and return")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    try:
        return handler(args)
    except ChargehandError as exc:
        print(f"chargehand: {sanitize.one_line(str(exc), limit=500)}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
