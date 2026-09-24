"""Installation helpers: the launchd job, `doctor`, and `init`.

Nothing here runs as part of a tick. `install` writes the job definition and, only
when asked, loads it; `doctor` reports and never repairs.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from chargehand import __version__, paths
from chargehand.claude import ClaudeCLI
from chargehand.config import MachineConfig, load_repo_config
from chargehand.errors import ChargehandError, ConfigError
from chargehand.gitutil import Git
from chargehand.trackers import build as build_tracker

OK = "ok"
WARN = "warn"
FAIL = "fail"

# launchd starts jobs with a minimal PATH, so everything the tick shells out to has
# to be reachable through the job's own PATH rather than through a login shell's.
DEFAULT_JOB_PATH = (
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def build_plist(
    *,
    program: Iterable[str] | None = None,
    interval_secs: int = 180,
    job_path: str | None = None,
    log_path: Path | None = None,
) -> dict[str, object]:
    argv = list(program) if program else chargehand_argv()
    extra_path = os.environ.get("PATH", "")
    return {
        "Label": paths.LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "StartInterval": int(interval_secs),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": job_path or _merge_path(DEFAULT_JOB_PATH, extra_path)},
        "StandardOutPath": str(log_path or paths.log_file()),
        "StandardErrorPath": str(log_path or paths.log_file()),
    }


def _merge_path(base: str, extra: str) -> str:
    seen: list[str] = []
    for entry in base.split(":") + extra.split(":"):
        if entry and entry not in seen:
            seen.append(entry)
    return ":".join(seen)


def chargehand_argv() -> list[str]:
    """How launchd should invoke a tick, as a full argument list.

    The console script when one is on PATH, and the module form otherwise. Both halves
    matter: a virtual environment that was not activated puts neither `chargehand` nor
    its `python` on PATH, so the interpreter running this call is the only reliable way
    back to the package. Returning the whole argv rather than a program keeps the two
    forms from being recombined wrongly by a caller.
    """
    script = shutil.which("chargehand")
    if script:
        return [script, "tick"]
    return [sys.executable, "-m", "chargehand", "tick"]


def write_plist(
    destination: Path | None = None,
    *,
    interval_secs: int = 180,
    job_path: str | None = None,
) -> Path:
    target = destination or paths.launch_agent_plist()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = build_plist(interval_secs=interval_secs, job_path=job_path)
    with target.open("wb") as handle:
        plistlib.dump(data, handle)
    return target


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False, timeout=60
    )


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain()}/{paths.LAUNCHD_LABEL}"


def is_loaded() -> bool:
    if sys.platform != "darwin":
        return False
    return launchctl("print", service_target()).returncode == 0


def bootstrap(plist: Path) -> subprocess.CompletedProcess[str]:
    return launchctl("bootstrap", domain(), str(plist))


def bootout() -> subprocess.CompletedProcess[str]:
    return launchctl("bootout", service_target())


def kickstart(*, force: bool = False) -> subprocess.CompletedProcess[str]:
    """Ask launchd to run the job now.

    `-k` kills the running instance first, which would abort a tick in the middle of a
    git fetch or a setup script and burn one of that attempt's launch retries. Control
    commands therefore never force: if a tick is already running they wait for it, and
    the caller falls back to running a tick itself under the tick lock.
    """
    args = ["kickstart", "-k", service_target()] if force else ["kickstart", service_target()]
    return launchctl(*args)


def init_repo(repo: Path, *, force: bool = False) -> Path:
    target = repo / paths.REPO_CONFIG_NAME
    if target.exists() and not force:
        raise ChargehandError(f"{target} already exists; pass --force to overwrite")
    template = (paths.templates_dir() / "chargehand.toml").read_text(encoding="utf-8")
    target.write_text(template, encoding="utf-8")
    return target


def doctor(config: MachineConfig | None, *, check_trackers: bool = True) -> list[Check]:
    checks: list[Check] = [
        Check("chargehand", OK, f"version {__version__}"),
        Check(
            "python",
            OK if sys.version_info >= (3, 11) else FAIL,
            f"{sys.version.split()[0]} at {sys.executable}",
        ),
    ]

    if config is None:
        checks.append(
            Check("config", FAIL, f"no configuration at {paths.config_file()}; run `chargehand init --machine`")
        )
        return checks
    checks.append(Check("config", OK, str(config.source or "(in memory)")))

    claude = ClaudeCLI(config.claude_bin)
    if not claude.is_available():
        checks.append(Check("claude", FAIL, f"'{config.claude_bin}' not found on PATH"))
    else:
        try:
            checks.append(Check("claude", OK, claude.version()))
        except ChargehandError as exc:
            checks.append(Check("claude", FAIL, str(exc)))
        else:
            try:
                sessions = claude.list_sessions()
                checks.append(
                    Check("claude agents", OK, f"{len(sessions)} session(s) listed")
                )
            except ChargehandError as exc:
                checks.append(Check("claude agents", FAIL, str(exc)))
            if config.pass_deny_rules_at_launch:
                supported = claude.supports_flag("--settings")
                checks.append(
                    Check(
                        "launch deny rules",
                        OK if supported else WARN,
                        "passed per launch with --settings"
                        if supported
                        else "this Claude Code build does not advertise --settings; put the "
                        "deny rules in user settings instead, or set "
                        "pass_deny_rules_at_launch = false",
                    )
                )

    git = Git(config.git_bin)
    checks.append(
        Check("git", OK if shutil.which(config.git_bin) else FAIL, config.git_bin)
    )

    root = config.worktree_root
    if root.exists():
        checks.append(
            Check("worktree root", OK if os.access(root, os.W_OK) else FAIL, str(root))
        )
    else:
        parent = root.parent
        checks.append(
            Check(
                "worktree root",
                OK if parent.exists() and os.access(parent, os.W_OK) else FAIL,
                f"{root} (will be created)",
            )
        )
    try:
        free_gb = shutil.disk_usage(root if root.exists() else root.parent).free / 1024**3
        checks.append(
            Check(
                "disk",
                OK if free_gb >= config.min_free_disk_gb else WARN,
                f"{free_gb:.1f} GB free",
            )
        )
    except OSError as exc:
        checks.append(Check("disk", WARN, str(exc)))

    if config.notify.command:
        program = Path(os.path.expanduser(config.notify.command.split()[0]))
        runnable = program.exists() and os.access(program, os.X_OK)
        checks.append(
            Check("notify", OK if runnable else FAIL, f"{program}{'' if runnable else ' is not executable'}")
        )
    else:
        checks.append(Check("notify", WARN, "no notify.command configured; nothing will reach you"))

    for route in config.routes:
        label = f"route {route.name}"
        if not route.repo.exists():
            checks.append(Check(label, FAIL, f"{route.repo} does not exist"))
            continue
        if not git.is_repo(route.repo):
            checks.append(Check(label, FAIL, f"{route.repo} is not a git repository"))
            continue
        try:
            repo_config = load_repo_config(route.repo)
        except ConfigError as exc:
            checks.append(Check(label, FAIL, str(exc)))
            continue
        detail = f"{route.repo} · base {repo_config.base} · {repo_config.permission_mode}"
        if repo_config.permission_mode == "bypassPermissions":
            checks.append(
                Check(
                    label,
                    WARN,
                    detail + " — bypassPermissions is intended for a dedicated machine, "
                    "user account, or VM",
                )
            )
        else:
            checks.append(Check(label, OK, detail))
        if not git.ref_exists(route.repo, repo_config.base):
            checks.append(
                Check(f"{label} base", WARN, f"{repo_config.base} is not present; fetch first")
            )
        if check_trackers:
            try:
                tracker = build_tracker(route)
                checks.append(Check(f"{label} tracker", OK, f"{tracker.describe()} as {tracker.whoami()}"))
            except ChargehandError as exc:
                checks.append(Check(f"{label} tracker", FAIL, str(exc)))

    if sys.platform == "darwin":
        plist = paths.launch_agent_plist()
        if not plist.exists():
            checks.append(Check("launchd", WARN, f"{plist} not installed; run `chargehand install`"))
        elif is_loaded():
            checks.append(Check("launchd", OK, f"{paths.LAUNCHD_LABEL} is loaded"))
        else:
            checks.append(
                Check("launchd", WARN, f"{plist} exists but is not loaded; run `chargehand install --load`")
            )
    else:
        checks.append(
            Check("launchd", WARN, "not macOS: schedule `chargehand tick` with your own timer")
        )

    return checks


def worst(checks: Iterable[Check]) -> str:
    statuses = {check.status for check in checks}
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return OK
