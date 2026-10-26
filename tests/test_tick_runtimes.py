"""The tick on two runtimes: what must not depend on which runtime a role uses.

Each test runs once on the claude runtime, through the fake binary, and once on the
fake runtime, which has fewer capabilities. ``driver`` is the handle a test uses to
set what a session does next, and it offers the same calls for both.
"""

from __future__ import annotations

import pytest

from chargehand import ledger as ledger_mod
from chargehand.cli import build_status
from chargehand.config import SourceConfig
from chargehand.errors import LaunchAborted
from chargehand.gitutil import Git
from chargehand.ledger import Ledger
from chargehand.notify import Notifier
from chargehand.runtime import LaunchSpec
from chargehand.tick import Runner
from conftest import Harness, make_config
from support import fake_runtime, fake_source

ROLES = {
    "claude": 'prompt = "work on {item}"\npermission_mode = "auto"\n',
    "fake": 'runtime = "fake"\nprompt = "work on {item}"\nsandbox = true\n',
}
START = '\n[[start]]\non = "labeled"\n'


def write_role(repo, name, text):
    target = repo / ".chargehand" / "roles" / f"{name}.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture(params=sorted(ROLES))
def runtime_name(request, harness) -> str:
    write_role(harness.repo, "implementer", ROLES[request.param] + START)
    return request.param


@pytest.fixture
def driver(runtime_name, harness):
    return harness.claude_state if runtime_name == "claude" else fake_runtime.world()


def launch(harness, item="ABC-1"):
    harness.board.add(item)
    report = harness.tick()
    return report, harness.worker(item)


def apply_request(harness, kind, target=None, **args):
    request_id = harness.ledger.record_request(kind, target, **args)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


def test_a_queued_item_starts_a_worker_on_the_roles_runtime(harness, runtime_name, driver):
    report, worker = launch(harness)

    assert report.launched == ["ABC-1"]
    assert report.errors == []
    assert worker.state == ledger_mod.RUNNING
    assert worker.runtime == runtime_name
    assert worker.session_id
    assert len(driver.launches) == 1
    assert harness.labels("ABC-1") == ("chargehand-running",)


def test_a_blocked_session_blocks_the_worker_and_notifies(harness, runtime_name, driver):
    launch(harness)
    driver.set_session_state("ABC-1", "blocked", waitingFor="a question")

    harness.next_tick()

    worker = harness.worker("ABC-1")
    assert worker.state == ledger_mod.BLOCKED
    assert worker.waiting_for == "a question"
    assert harness.labels("ABC-1") == ("chargehand-blocked",)
    assert ("ABC-1", "blocked") in harness.notifications()


def test_a_session_that_disappears_blocks_the_worker(harness, runtime_name, driver):
    launch(harness)
    driver.drop_sessions()

    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    assert harness.worker("ABC-1").session_state == ledger_mod.SESSION_MISSING


def test_cancel_stops_the_session_on_its_own_runtime(harness, runtime_name, driver):
    _, worker = launch(harness)

    result = apply_request(harness, "cancel", "ABC-1")

    assert result["ok"] == 1
    assert ["stop", worker.session_id] in driver.commands
    assert harness.worker("ABC-1").state == ledger_mod.CANCELLED
    assert harness.worktree("ABC-1").is_dir()


def test_stop_then_continue_respawns_the_session(harness, runtime_name, driver):
    _, worker = launch(harness)

    assert apply_request(harness, "stop", "ABC-1")["ok"] == 1
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED
    assert apply_request(harness, "continue", "ABC-1")["ok"] == 1

    assert ["respawn", worker.session_id] in driver.commands
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_an_interrupted_launch_adopts_the_session_it_started(harness, runtime_name, driver):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=3)
    # The session came up, but the tick died before it recorded the ids.
    harness.runner.runtime(runtime_name).launch(
        LaunchSpec(
            worktree=harness.worktree("ABC-1"), name="implementer/ABC-1", prompt="work"
        )
    )

    report = harness.next_tick()

    assert report.adopted == ["ABC-1"]
    assert len(driver.launches) == 1
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_discard_removes_the_session_and_the_worktree(harness, runtime_name, driver):
    _, worker = launch(harness)

    result = apply_request(harness, "discard", "ABC-1", force=True)

    assert result["ok"] == 1
    assert ["rm", worker.session_id] in driver.commands
    assert not harness.worktree("ABC-1").exists()
    assert all(s.get("id") != worker.session_id for s in driver.sessions)


def test_an_orphan_session_under_the_worktree_root_is_adopted(harness, runtime_name, driver):
    harness.worktree("ABC-9").mkdir(parents=True)
    harness.runner.runtime(runtime_name).launch(
        LaunchSpec(
            worktree=harness.worktree("ABC-9"), name="implementer/ABC-9", prompt="work"
        )
    )

    report = harness.next_tick()

    assert any("ABC-9" in warning for warning in report.warnings)
    # The worktree is not one of the repository's, so no repository owns it; the
    # session is reported, not adopted, the same way on both runtimes.
    assert harness.ledger.live_worker_for("ABC-9") is None


@pytest.fixture(params=sorted(ROLES))
def routed(request, tmp_path, repo, claude_state, board):
    write_role(
        repo, "reviewer",
        ROLES[request.param] + '\n[[start]]\non = "fake.review_requested"\nsubscribe = ["comment"]\n',
    )
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    source = SourceConfig(name="fake", type="fake", options={"board": "default", "repository": "test"})
    config = make_config(repo, worktree_root, roles=("implementer", "reviewer"), sources=(source,))
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    harness = Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root,
                      notifier)
    harness.runtime_name = request.param
    yield harness
    ledger.close()


def test_a_source_event_starts_a_worker_on_the_roles_runtime(routed):
    driver = routed.claude_state if routed.runtime_name == "claude" else fake_runtime.world()
    routed.board.add("ABC-1", labels=())
    fake_source.feed().emit("review_requested", "ABC-1")

    report = routed.tick()

    assert report.launched == ["ABC-1"]
    assert report.errors == []
    reviewer = routed.worker("ABC-1", "reviewer")
    assert reviewer.runtime == routed.runtime_name
    assert reviewer.state == ledger_mod.RUNNING
    assert reviewer.session_id
    assert len(driver.launches) == 1


# ----- a claude role and a fake-runtime role in one repository ---------------


@pytest.fixture
def mixed(tmp_path, repo, claude_state, board) -> Harness:
    write_role(repo, "implementer", ROLES["claude"] + "\n[policy]\nmax_workers = 1\n" + START)
    write_role(repo, "reviewer", ROLES["fake"] + START)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    config = make_config(repo, worktree_root, roles=("implementer", "reviewer"))
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    harness = Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root,
                      notifier)
    yield harness
    ledger.close()


def test_one_tick_supervises_a_claude_role_and_a_fake_runtime_role(mixed):
    world = fake_runtime.world()
    mixed.board.add("ABC-1")
    mixed.board.add("ABC-2")

    report = mixed.tick()

    assert sorted(report.launched) == ["ABC-1", "ABC-2"]
    assert report.errors == []
    on_claude = mixed.worker("ABC-1")
    on_fake = mixed.worker("ABC-2")
    assert (on_claude.role, on_claude.runtime) == ("implementer", "claude")
    assert (on_fake.role, on_fake.runtime) == ("reviewer", "fake")
    assert len(mixed.claude_state.launches) == 1
    assert [launch["name"] for launch in world.launches] == ["reviewer/ABC-2"]
    assert world.launches[0]["options"] == {"sandbox": True}

    mixed.claude_state.set_session_state("ABC-1", "blocked")
    world.set_session_state("ABC-2", "blocked")
    mixed.next_tick()

    assert mixed.worker("ABC-1").state == ledger_mod.BLOCKED
    assert mixed.worker("ABC-2").state == ledger_mod.BLOCKED

    status = build_status(mixed.config, mixed.ledger, include_queue=False)
    by_item = {w["item"]: w for w in status["workers"]}
    assert by_item["ABC-1"]["attach"] == f"claude attach {on_claude.session_id}"
    assert by_item["ABC-2"]["attach"] == f"fake-agent resume {on_fake.session_id}"
    assert {s["runtime"] for s in status["sessions"]} == {"claude", "fake"}

    mixed.ledger.record_request("cancel", "ABC-1")
    mixed.ledger.record_request("cancel", "ABC-2")
    mixed.next_tick()

    assert ["stop", on_claude.session_id] in mixed.claude_state.commands
    assert ["stop", on_fake.session_id] in world.commands
    assert ["stop", on_fake.session_id] not in mixed.claude_state.commands
    assert mixed.worker("ABC-1").state == ledger_mod.CANCELLED
    assert mixed.worker("ABC-2").state == ledger_mod.CANCELLED


def test_a_runtime_whose_listing_fails_stops_the_tick(mixed):
    world = fake_runtime.world()
    world.set(list_fails=True)
    mixed.board.add("ABC-1")

    report = mixed.tick()

    assert report.launched == []
    assert any(error.startswith("fake:") for error in report.errors)
