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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

from chargehand import __version__, paths
from chargehand import roles as roles_mod
from chargehand import runtime as runtime_mod
from chargehand.config import MachineConfig, load_repo_config
from chargehand.errors import ChargehandError, ConfigError
from chargehand.gitutil import Git
from chargehand.trackers import build as build_tracker

OK = "ok"
# Not a fault: something worth knowing that needs no change for the machine to work.
INFO = "info"
WARN = "warn"
FAIL = "fail"

# launchd starts jobs with a minimal PATH, so everything the tick shells out to has
# to be reachable through the job's own PATH rather than through a login shell's.
DEFAULT_JOB_PATH = (
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)
# What launchd gives a job whose definition sets no PATH.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Everything the tick starts inherits the job's process type, including the Claude Code
# daemon that a launch starts, and that daemon then hosts every background session on
# the machine, the operator's own as well. Background would hold all of them in the
# throttled band, where CPU-bound work measured about three times slower than from a
# terminal. Interactive gives them the priority a terminal gives.
JOB_PROCESS_TYPE = "Interactive"


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
        "ProcessType": JOB_PROCESS_TYPE,
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


def installed_job(plist: Path | None = None) -> dict[str, object] | None:
    try:
        with (plist or paths.launch_agent_plist()).open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, ExpatError):
        return None
    return data if isinstance(data, dict) else None


def job_checks(config: MachineConfig, job: Mapping[str, object]) -> list[Check]:
    """What the scheduled job can reach, rather than what the shell running doctor can.

    The job has only the PATH its definition sets. A tool that resolves in a shell but not
    there shows up as a "not found" inside a tick, long after setup looked fine.
    """
    checks: list[Check] = []
    argv = job.get("ProgramArguments")
    program = argv[0] if isinstance(argv, list) and argv else job.get("Program")
    if not isinstance(program, str) or not os.access(program, os.X_OK):
        checks.append(
            Check("launchd program", FAIL,
                  f"the job runs {program}, which is not an executable file; "
                  "re-run `chargehand install --load`")
        )
    environment = job.get("EnvironmentVariables")
    job_path = environment.get("PATH") if isinstance(environment, Mapping) else None
    found: list[str] = []
    missing: list[str] = []
    for tool in (config.claude_bin, config.git_bin):
        where = shutil.which(os.path.expanduser(tool), path=job_path or LAUNCHD_PATH)
        if where:
            found.append(f"{tool} at {where}")
        else:
            missing.append(tool)
    if missing:
        checks.append(
            Check("launchd PATH", FAIL,
                  f"the job's PATH does not reach {', '.join(missing)}; re-run "
                  "`chargehand install --load` from a shell where it resolves, or set an "
                  "absolute claude_bin or git_bin")
        )
    else:
        checks.append(Check("launchd PATH", OK, ", ".join(found)))
    return checks


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


def starter_roles() -> list[str]:
    """The roles chargehand ships a template for."""
    return sorted(path.stem for path in (paths.templates_dir() / "roles").glob("*.toml"))


def init_repo(repo: Path, *, force: bool = False, roles: Sequence[str] = ()) -> list[Path]:
    """Write the repository configuration and a starter `implementer` role.

    With *roles*, write those starter roles instead, each with the agent file it names
    when chargehand ships one, and the repository configuration only when it is missing.
    """
    unknown = sorted(set(roles) - set(starter_roles()))
    if unknown:
        raise ChargehandError(
            f"no starter role named {', '.join(unknown)}; available: {', '.join(starter_roles())}"
        )
    targets: list[tuple[Path, str]] = []
    config = repo / paths.REPO_CONFIG_NAME
    if not roles or not config.exists():
        targets.append((config, "chargehand.toml"))
    for name in roles or (roles_mod.DEFAULT_ROLE,):
        targets.append((repo / ".chargehand" / "roles" / f"{name}.toml", f"roles/{name}.toml"))
        if (paths.templates_dir() / "agents" / f"{name}.md").is_file():
            targets.append((repo / ".claude" / "agents" / f"{name}.md", f"agents/{name}.md"))
    for target, _ in targets:
        if target.exists() and not force:
            raise ChargehandError(f"{target} already exists; pass --force to overwrite")
    written = []
    for target, template in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((paths.templates_dir() / template).read_text(encoding="utf-8"), encoding="utf-8")
        written.append(target)
    return written


def _runtimes_in_use(config: MachineConfig) -> list[str]:
    """The default runtime and every runtime an enabled role names. A role that fails to load is reported later."""
    names = {roles_mod.DEFAULT_RUNTIME}
    for repository in config.repositories:
        try:
            repo_config = load_repo_config(repository.path)
        except ConfigError:
            continue
        for name in repository.roles:
            try:
                role = roles_mod.load_role(
                    repository.path, name, repo_config=repo_config,
                    tracker_type=repository.tracker.type, check_agent=False,
                )
            except ConfigError:
                continue
            names.add(role.runtime)
    return sorted(names)


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
    for key in config.ignored_keys:
        checks.append(Check("config", INFO, f"{key} is ignored and can be removed"))

    for name in _runtimes_in_use(config):
        try:
            runtime = runtime_mod.create(name, config)
            results = runtime.health_checks()
        except ChargehandError as exc:
            checks.append(Check(f"runtime {name}", FAIL, str(exc)))
            continue
        checks.extend(Check(result.name, result.status, result.detail) for result in results)

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

    for repository in config.repositories:
        label = f"repository {repository.name}"
        if not repository.path.exists():
            checks.append(Check(label, FAIL, f"{repository.path} does not exist"))
            continue
        if not git.is_repo(repository.path):
            checks.append(Check(label, FAIL, f"{repository.path} is not a git repository"))
            continue
        try:
            repo_config = load_repo_config(repository.path)
        except ConfigError as exc:
            checks.append(Check(label, FAIL, str(exc)))
            continue
        checks.append(Check(label, OK, f"{repository.path} · base {repo_config.base}"))
        if not git.ref_exists(repository.path, repo_config.base):
            checks.append(
                Check(f"{label} base", WARN, f"{repo_config.base} is not present; fetch first")
            )
        for name in repository.roles:
            role_label = f"{label} role {name}"
            try:
                role = roles_mod.load_role(
                    repository.path, name, repo_config=repo_config,
                    tracker_type=repository.tracker.type,
                )
            except ConfigError as exc:
                checks.append(Check(role_label, FAIL, str(exc)))
                continue
            parts = [f"runtime {role.runtime}"] if role.runtime != roles_mod.DEFAULT_RUNTIME else []
            if role.permission_mode:
                parts.append(role.permission_mode)
            if role.agent:
                parts.append(f"agent {role.agent}")
            if role.model:
                parts.append(f"model {role.model}")
            if role.effort:
                parts.append(f"effort {role.effort}")
            detail = " · ".join(parts)
            if role.permission_mode == "bypassPermissions":
                checks.append(
                    Check(
                        role_label,
                        WARN,
                        detail + " · bypassPermissions is intended for a dedicated machine, "
                        "user account, or VM",
                    )
                )
            elif not role.start:
                checks.append(Check(role_label, WARN, detail + " · no start rule, so nothing starts it on its own"))
            else:
                checks.append(Check(role_label, OK, detail))
        if check_trackers:
            try:
                tracker = build_tracker(repository)
                checks.append(Check(f"{label} tracker", OK, f"{tracker.describe()} as {tracker.whoami()}"))
            except ChargehandError as exc:
                checks.append(Check(f"{label} tracker", FAIL, str(exc)))

    job: dict[str, object] | None = None
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
        if plist.exists():
            job = installed_job(plist)
            if job is None:
                checks.append(Check("launchd", FAIL, f"{plist} cannot be read as a property list"))
            else:
                checks.extend(job_checks(config, job))
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
