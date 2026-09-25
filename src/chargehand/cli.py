"""The control surface: a CLI, reached over SSH. There is no server.

Two rules shape this module. A mutating command never calls Claude Code, git, or a
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
from chargehand.claude import ClaudeCLI
from chargehand.config import MachineConfig, load_machine_config, load_repo_config
from chargehand.errors import ChargehandError, ConfigError, TickBusy
from chargehand.gitutil import Git
from chargehand.ledger import Attempt, Ledger, open_ledger
from chargehand.notify import Notifier
from chargehand.tick import Runner, tick_lock
from chargehand.trackers import Status

DESCRIPTION = (
    "Label an issue, get a supervised Claude Code session in a fresh git worktree "
    "on a machine you own."
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3

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
        claude=ClaudeCLI(config.claude_bin),
        git=Git(config.git_bin),
        notifier=Notifier(config.notify),
    )


def _print(text: str = "") -> None:
    print(text)


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


# ----- status ---------------------------------------------------------------


def _elapsed(since: float | None, now: float | None = None) -> str:
    if not since:
        return "-"
    delta = max(0.0, (now or time.time()) - since)
    if delta < 90:
        return f"{delta:.0f}s"
    if delta < 5400:
        return f"{delta / 60:.0f}m"
    if delta < 172800:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.1f}d"


def _attempt_dict(attempt: Attempt, *, verbose: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "issue": attempt.identifier,
        "route": attempt.route,
        "state": attempt.state,
        "session_state": attempt.session_state,
        # Whether it is waiting is a state; why it says it is waiting is session text.
        "waiting": bool(attempt.waiting_for),
        "step": attempt.step,
        "launch_attempts": attempt.launch_attempts,
        "session_id": attempt.session_id,
        "worktree": attempt.worktree,
        "branch": attempt.branch,
        "url": attempt.url,
        "pr_url": attempt.pr_url,
        "label_state": attempt.label_state,
        "elapsed_secs": round(time.time() - attempt.created_at, 1),
        "working_since": attempt.working_since,
        "created_at": attempt.created_at,
        "updated_at": attempt.updated_at,
    }
    if attempt.state == "blocked" and attempt.session_id:
        payload["attach"] = f"claude attach {attempt.session_id}"
    if verbose:
        if attempt.waiting_for:
            payload["waiting_for"] = sanitize.one_line(attempt.waiting_for)
        if attempt.last_error:
            payload["last_error"] = sanitize.one_line(attempt.last_error, limit=300)
    return payload


def build_status(
    config: MachineConfig,
    ledger: Ledger,
    *,
    verbose: bool = False,
    include_queue: bool = True,
) -> dict[str, object]:
    now = time.time()
    attempts = ledger.live_attempts()
    runner = _runner(config, ledger)

    sessions_payload: list[dict[str, object]] = []
    session_error: str | None = None
    try:
        for session in runner.claude.list_sessions():
            entry: dict[str, object] = {
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
    except ChargehandError as exc:
        session_error = str(exc)

    routes_payload = []
    for route in config.routes:
        entry: dict[str, object] = {
            "name": route.name,
            "repo": str(route.repo),
            "tracker": route.tracker.type,
            "paused": ledger.is_paused(route.name),
            "labels": route.labels.as_dict(),
        }
        if include_queue:
            try:
                tracker = trackers.build(route)
                queued = tracker.list(Status.QUEUED)
                entry["queued"] = len(queued)
                entry["queued_issues"] = [issue.identifier for issue in queued]
                if verbose:
                    entry["queued_titles"] = [sanitize.one_line(issue.title) for issue in queued]
            except ChargehandError as exc:
                entry["queue_error"] = str(exc)
        try:
            entry["prompt"] = load_repo_config(route.repo).prompt
            entry["permission_mode"] = load_repo_config(route.repo).permission_mode
        except ConfigError as exc:
            entry["config_error"] = str(exc)
        routes_payload.append(entry)

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
            "paused_routes": ledger.paused_routes(),
            "max_concurrent": config.max_concurrent,
            "max_open_attempts": config.max_open_attempts,
            "working": sum(1 for a in attempts if a.state in ("launching", "running")),
            "open": len(attempts),
            "pending_requests": [
                {"kind": r["kind"], "target": r["target"], "created_at": r["created_at"]}
                for r in ledger.pending_requests()
            ],
        },
        "routes": routes_payload,
        "attempts": [_attempt_dict(a, verbose=verbose) for a in attempts],
        "recent": [
            _attempt_dict(a, verbose=verbose)
            for a in ledger.recent_attempts(10)
            if a.is_terminal
        ],
        "sessions": sessions_payload,
        "session_error": session_error,
        "pool": {"enabled": runner.pool.enabled, "leases": [l.__dict__ for l in runner.pool.status()]},
    }


def _render_table(status: dict[str, object], *, verbose: bool) -> str:
    lines: list[str] = []
    machine = status["machine"]  # type: ignore[index]
    runner = status["runner"]  # type: ignore[index]
    paused_note = " PAUSED" if runner["paused"] else ""
    lines.append(
        f"chargehand {status['version']} on {machine['hostname']}{paused_note} — "
        f"{runner['working']}/{runner['max_concurrent']} working, "
        f"{runner['open']}/{runner['max_open_attempts']} open, "
        f"{machine['free_disk_gb']} GB free"
    )
    if runner["paused_routes"]:
        lines.append(f"  paused routes: {', '.join(runner['paused_routes'])}")
    if status.get("session_error"):
        lines.append(f"  ! claude: {sanitize.one_line(str(status['session_error']), limit=160)}")

    lines.append("")
    lines.append("routes")
    for route in status["routes"]:  # type: ignore[index]
        queued = route.get("queued")
        detail = f"queued {queued}" if queued is not None else route.get("queue_error", "")
        flag = " (paused)" if route["paused"] else ""
        lines.append(
            f"  {route['name']:<16}{flag} {route['tracker']:<8} "
            f"{sanitize.one_line(str(detail), limit=80)}"
        )
        if route.get("config_error"):
            lines.append(f"    ! {sanitize.one_line(route['config_error'], limit=160)}")

    attempts = status["attempts"]  # type: ignore[index]
    lines.append("")
    if not attempts:
        lines.append("no live attempts")
    else:
        header = f"  {'ISSUE':<14} {'STATE':<10} {'SESSION':<9} {'AGE':>6} {'STEP':>4}  DETAIL"
        lines.append(header)
        for attempt in attempts:
            detail = attempt.get("waiting_for") or attempt.get("pr_url") or (
                "waiting" if attempt.get("waiting") else ""
            )
            if attempt.get("attach"):
                detail = f"{detail} · {attempt['attach']}".strip(" ·")
            lines.append(
                f"  {sanitize.scrub_identifier(str(attempt['issue'])):<14} {attempt['state']:<10} "
                f"{str(attempt['session_state'] or '-'):<9} "
                f"{_elapsed(attempt['created_at']):>6} {attempt['step']:>4}  "
                f"{sanitize.one_line(str(detail), limit=70)}"
            )
            if verbose and attempt.get("last_error"):
                lines.append(f"      ! {attempt['last_error']}")

    if runner["pending_requests"]:
        lines.append("")
        lines.append("pending control requests")
        for request in runner["pending_requests"]:
            lines.append(f"  {request['kind']} {request['target'] or ''}".rstrip())
    return "\n".join(lines)


# ----- commands -------------------------------------------------------------


def cmd_tick(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with open_ledger() as ledger:
        try:
            with tick_lock(wait_secs=args.wait):
                report = _runner(config, ledger).tick(crash_after_step=args.crash_after_step)
        except TickBusy as exc:
            print(f"{exc}", file=sys.stderr)
            return EXIT_ERROR
    if args.json:
        _emit_json(report.as_dict())
    else:
        for field in ("launched", "adopted", "resumed", "failed", "transitions", "requests",
                      "collected", "warnings", "errors"):
            for entry in getattr(report, field):
                _print(f"{field[:-1] if field.endswith('s') else field}: "
                       f"{sanitize.one_line(str(entry), limit=200)}")
        if not any(getattr(report, f) for f in ("launched", "adopted", "resumed", "failed",
                                                "transitions", "requests", "collected",
                                                "warnings", "errors")):
            _print("nothing to do")
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


def cmd_logs(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with open_ledger() as ledger:
        attempt = ledger.live_attempt_for(args.issue) or ledger.latest_attempt_for(args.issue)
    if attempt is None:
        print(f"no attempt recorded for {sanitize.scrub_identifier(args.issue)}", file=sys.stderr)
        return EXIT_ERROR
    if not attempt.session_id:
        print(f"{attempt.identifier} has no session", file=sys.stderr)
        return EXIT_ERROR
    try:
        output = ClaudeCLI(config.claude_bin).logs(attempt.session_id, lines=args.lines)
    except ChargehandError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    # Agent output is untrusted: strip anything that could act on this terminal.
    _print(sanitize.clean(output))
    return EXIT_OK


def _wait_for_request(ledger: Ledger, request_id: int, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = ledger.get_request(request_id)
        if request and request["applied_at"] is not None:
            return request
        time.sleep(0.25)
    return ledger.get_request(request_id)


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
            _print(f"recorded: {kind} {target or ''}".rstrip())
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
            install.kickstart()
            applied = _wait_for_request(ledger, request_id, min(args.wait, 30.0))
        if applied is None or applied["applied_at"] is None:
            try:
                with tick_lock(wait_secs=args.wait):
                    _runner(config, ledger).tick()
            except TickBusy:
                pass
            applied = _wait_for_request(ledger, request_id, args.wait)

    if applied is None or applied["applied_at"] is None:
        _print(f"recorded: {kind} {target or ''} — the next tick will apply it".rstrip())
        return EXIT_OK
    message = sanitize.one_line(applied["message"] or "", limit=300)
    if applied["ok"]:
        _print(f"{kind} {target or ''}: {message}".strip())
        return EXIT_OK
    print(f"refused: {message}", file=sys.stderr)
    return EXIT_REFUSED


def cmd_pause(args: argparse.Namespace) -> int:
    return _request(args, "pause", args.route)


def cmd_resume(args: argparse.Namespace) -> int:
    return _request(args, "resume", args.route)


def cmd_cancel(args: argparse.Namespace) -> int:
    return _request(args, "cancel", args.issue)


def cmd_stop(args: argparse.Namespace) -> int:
    return _request(args, "stop", args.issue)


def cmd_continue(args: argparse.Namespace) -> int:
    return _request(args, "continue", args.issue)


def cmd_retry(args: argparse.Namespace) -> int:
    return _request(args, "retry", args.issue, force=args.force)


def cmd_discard(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "discard removes the session, the worktree, and the local branch. "
            "Re-run with --yes.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return _request(args, "discard", args.issue, force=args.force)


def cmd_install(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("install writes a launchd LaunchAgent, which is macOS only. On other "
              "platforms schedule `chargehand tick` with your own timer.", file=sys.stderr)
        return EXIT_ERROR
    config = _load_config(args)
    plist = install.write_plist(interval_secs=args.interval or config.poll_interval_secs)
    _print(f"wrote {plist}")
    if not args.load:
        _print("")
        _print("Not loaded. To start it:")
        _print(f"  launchctl bootstrap {install.domain()} {plist}")
        _print(f"  launchctl kickstart -k {install.service_target()}   # run a tick now")
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
            marker = {"ok": "  ok  ", " warn ": " warn ", "warn": " warn ", "fail": " FAIL "}.get(
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
        target = install.init_repo(repo, force=args.force)
    except ChargehandError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    _print(f"wrote {target}")
    _print("Edit `base` and `prompt`, then add the route to your machine configuration.")
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
    parser = argparse.ArgumentParser(prog="chargehand", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"chargehand {__version__}")
    parser.add_argument("--config", help=f"machine configuration file (default: {paths.config_file()})")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="log more; repeat for debug")
    subparsers = parser.add_subparsers(dest="command")

    def add(name: str, handler, help_text: str, *, aliases: Sequence[str] = ()):
        sub = subparsers.add_parser(name, help=help_text, aliases=list(aliases))
        sub.set_defaults(handler=handler)
        return sub

    tick = add("tick", cmd_tick, "run one tick now")
    tick.add_argument("--json", action="store_true")
    tick.add_argument("--wait", type=float, default=0.0,
                      help="seconds to wait for a running tick to finish")
    tick.add_argument("--crash-after-step", type=int, metavar="N",
                      help="fault injection: abort a launch right after step N (0-4)")

    for name, handler, help_text in (
        ("status", cmd_status, "queue, attempts, sessions, machine"),
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
    logs.add_argument("issue")
    logs.add_argument("--lines", type=int, default=200)

    for name, handler, help_text in (
        ("pause", cmd_pause, "stop admitting new launches"),
        ("resume", cmd_resume, "resume admitting new launches"),
    ):
        sub = add(name, handler, help_text)
        sub.add_argument("--route", help="limit to one route")
        _add_request_flags(sub)

    for name, handler, help_text in (
        ("cancel", cmd_cancel, "stop the session and mark the issue blocked; keep the worktree"),
        ("stop", cmd_stop, "pause one run"),
        ("continue", cmd_continue, "respawn a stopped or blocked run"),
    ):
        sub = add(name, handler, help_text)
        sub.add_argument("issue")
        _add_request_flags(sub)

    retry = add("retry", cmd_retry, "start a new attempt for a failed or cancelled run")
    retry.add_argument("issue")
    retry.add_argument("--force", action="store_true", help="discard an unpushed worktree first")
    _add_request_flags(retry)

    discard = add("discard", cmd_discard, "remove the session, worktree, and local branch")
    discard.add_argument("issue")
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

    templates = add("templates", cmd_templates, "print a bundled template")
    templates.add_argument("name", nargs="?", help="e.g. skill/SKILL.md, assistant-settings.json")

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
