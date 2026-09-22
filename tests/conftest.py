"""Fixtures. Nothing here needs a real `claude`, a real tracker, or the network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from chargehand import paths  # noqa: E402
from chargehand.claude import ClaudeCLI  # noqa: E402
from chargehand.config import Labels, MachineConfig, NotifyConfig, Route, TrackerConfig  # noqa: E402
from chargehand.gitutil import Git  # noqa: E402
from chargehand.ledger import Ledger  # noqa: E402
from chargehand.notify import Notifier  # noqa: E402
from chargehand.pool import NoopPool  # noqa: E402
from chargehand.tick import Runner  # noqa: E402
from support import fake_tracker  # noqa: E402

FAKE_CLAUDE = Path(__file__).parent / "support" / "fake_claude.py"

REPO_CONFIG = """\
base = "origin/main"
prompt = "work on {issue} ({url})"
permission_mode = "auto"
branch_prefix = "chargehand/"
"""


def run_git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env
    )
    return result.stdout


class FakeClaudeState:
    """Reads and writes the fake binary's state file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.write({})

    def read(self) -> dict:
        return json.loads(self.path.read_text() or "{}")

    def write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state, indent=2))

    def set(self, **values) -> None:
        state = self.read()
        state.update(values)
        self.write(state)

    @property
    def sessions(self) -> list[dict]:
        return self.read().get("sessions", [])

    @property
    def launches(self) -> list[dict]:
        return self.read().get("launches", [])

    @property
    def commands(self) -> list[list[str]]:
        return self.read().get("commands", [])

    def set_session_state(self, name: str, session_state: str, **extra) -> None:
        state = self.read()
        for session in state.get("sessions", []):
            if session.get("name") == name or session.get("id") == name:
                session["state"] = session_state
                session.update(extra)
        self.write(state)

    def drop_sessions(self) -> None:
        self.set(sessions=[])


@dataclass
class Harness:
    config: MachineConfig
    ledger: Ledger
    runner: Runner
    claude_state: FakeClaudeState
    board: "fake_tracker.Board"
    repo: Path
    origin: Path
    worktree_root: Path
    notifier: Notifier

    def tick(self, **kwargs):
        self.runner.invalidate_sessions()
        return self.runner.tick(**kwargs)

    def fresh_runner(self) -> Runner:
        """A new Runner, as a new tick would build. Clears per-tick caches."""
        self.runner = Runner(
            self.config,
            self.ledger,
            claude=ClaudeCLI(self.config.claude_bin),
            git=Git(self.config.git_bin),
            pool=NoopPool(),
            notifier=self.notifier,
        )
        return self.runner

    def next_tick(self, **kwargs):
        self.fresh_runner()
        return self.runner.tick(**kwargs)

    def attempt(self, identifier: str):
        return self.ledger.live_attempt_for(identifier) or self.ledger.latest_attempt_for(
            identifier
        )

    def labels(self, identifier: str) -> tuple[str, ...]:
        return self.board.labels_of(identifier)

    def notifications(self) -> list[tuple[str, str]]:
        return [(n.issue, n.state) for n in self.notifier.sent]


@pytest.fixture(autouse=True)
def never_touch_launchd(monkeypatch):
    """Keep the suite away from a real LaunchAgent.

    A control command kick-starts the scheduled job when one is loaded. On a machine
    where chargehand is actually installed that would make `pytest` start the real
    runner, against the real configuration, and then wait 30 seconds on a ledger it
    never touches. Tests that exercise this path stub it themselves.
    """
    monkeypatch.setattr("chargehand.install.is_loaded", lambda: False)
    monkeypatch.setattr(
        "chargehand.install.launchctl",
        lambda *args: pytest.fail(f"a test tried to run launchctl {' '.join(args)}"),
    )


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(state))
    monkeypatch.setenv(paths.LOG_FILE_ENV, str(tmp_path / "chargehand.log"))
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path / "config.toml"))
    fake_tracker.reset()
    yield state
    fake_tracker.reset()


@pytest.fixture
def origin_repo(tmp_path) -> Path:
    origin = tmp_path / "origin.git"
    run_git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)
    return origin


@pytest.fixture
def repo(tmp_path, origin_repo) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run_git("init", "--initial-branch=main", cwd=path)
    run_git("remote", "add", "origin", str(origin_repo), cwd=path)
    (path / "README.md").write_text("# test repo\n")
    (path / paths.REPO_CONFIG_NAME).write_text(REPO_CONFIG)
    run_git("add", "-A", cwd=path)
    run_git("commit", "-m", "initial", cwd=path)
    run_git("push", "-u", "origin", "main", cwd=path)
    return path


@pytest.fixture
def claude_state(tmp_path, monkeypatch) -> FakeClaudeState:
    path = tmp_path / "claude-state.json"
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(path))
    return FakeClaudeState(path)


@pytest.fixture
def board() -> "fake_tracker.Board":
    return fake_tracker.board("default")


def make_config(repo: Path, worktree_root: Path, **overrides) -> MachineConfig:
    labels = Labels(queued="chargehand", running="chargehand-running", blocked="chargehand-blocked")
    route = Route(
        name="test",
        repo=repo,
        tracker=TrackerConfig(type="fake", options={"board": "default"}),
        labels=labels,
        max_concurrent=overrides.pop("route_max_concurrent", None),
    )
    defaults = dict(
        routes=(route,),
        worktree_root=worktree_root,
        max_concurrent=2,
        max_open_attempts=4,
        max_run_hours=6.0,
        hard_stop_hours=10.0,
        min_free_disk_gb=0.0,
        labels=labels,
        notify=NotifyConfig(),
        claude_bin=str(FAKE_CLAUDE),
        git_bin="git",
    )
    defaults.update(overrides)
    return MachineConfig(**defaults)


@pytest.fixture
def harness(tmp_path, repo, origin_repo, claude_state, board) -> Harness:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    config = make_config(repo, worktree_root)
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(
        config,
        ledger,
        claude=ClaudeCLI(config.claude_bin),
        git=Git(config.git_bin),
        pool=NoopPool(),
        notifier=notifier,
    )
    harness = Harness(
        config=config,
        ledger=ledger,
        runner=runner,
        claude_state=claude_state,
        board=board,
        repo=repo,
        origin=origin_repo,
        worktree_root=worktree_root,
        notifier=notifier,
    )
    yield harness
    ledger.close()


def write_machine_config(path: Path, repo: Path, worktree_root: Path, **extra) -> Path:
    lines = [
        f'worktree_root = "{worktree_root}"',
        f'claude_bin = "{FAKE_CLAUDE}"',
        "max_concurrent = 2",
        "max_open_attempts = 4",
        "min_free_disk_gb = 0",
    ]
    for key, value in extra.items():
        lines.append(f"{key} = {json.dumps(value)}")
    lines += [
        "",
        "[[route]]",
        'name = "test"',
        f'repo = "{repo}"',
        'tracker = { type = "fake", board = "default" }',
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def config_file(tmp_path, repo, harness) -> Path:
    return write_machine_config(tmp_path / "config.toml", repo, harness.worktree_root)
