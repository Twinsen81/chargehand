"""Roles in the tick: what a role file changes about a launch, and several roles at once."""

from __future__ import annotations

import json
import stat

from conftest import FAKE_CLAUDE, Harness, make_config
from chargehand import ledger as ledger_mod
from chargehand.claude import ClaudeCLI
from chargehand.config import DEFAULT_DENY_RULES
from chargehand.gitutil import Git
from chargehand.ledger import Ledger
from chargehand.notify import Notifier
from chargehand.pool import NoopPool
from chargehand.tick import Runner


def write_role(repo, name, text):
    target = repo / ".chargehand" / "roles" / f"{name}.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def write_agent(repo, name):
    target = repo / ".claude" / "agents" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nname: {name}\n---\nBe the {name}.\n")


def harness_with(tmp_path, repo, claude_state, board, **overrides) -> Harness:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir(exist_ok=True)
    config = make_config(repo, worktree_root, **overrides)
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, claude=ClaudeCLI(config.claude_bin), git=Git(),
                    pool=NoopPool(), notifier=notifier)
    return Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root, notifier)


def test_a_role_file_drives_the_launch(harness):
    write_agent(harness.repo, "builder")
    write_role(harness.repo, "implementer", """\
agent = "builder"
model = "sonnet"
effort = "low"
prompt = "/work {item} at {item_url}"
permission_mode = "acceptEdits"
allow = ["Bash(gh pr *)"]

[[start]]
on = "labeled"
""")
    harness.board.add("ABC-1")

    report = harness.tick()

    assert report.launched == ["ABC-1"]
    argv = harness.claude_state.launches[0]["argv"]
    assert argv[argv.index("-n") + 1] == "implementer/ABC-1"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--agent") + 1] == "builder"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[-1] == "/work ABC-1 at https://tracker.invalid/ABC-1"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["permissions"]["allow"] == ["Bash(gh pr *)"]
    assert set(DEFAULT_DENY_RULES) <= set(settings["permissions"]["deny"])
    worker = harness.worker("ABC-1")
    assert worker.role == "implementer"
    assert (worker.origin, worker.start_rule, worker.runtime) == ("source", "labeled", "claude")
    assert worker.worktree_path == harness.worktree_root / "implementer" / "ABC-1"
    assert worker.branch == "chargehand/implementer/ABC-1"
    assert [s.kind for s in harness.ledger.sessions_of(worker)] == [ledger_mod.SESSION_INITIAL]


def test_the_old_prompt_variables_still_render(harness):
    # The conftest repository configuration is the pre-roles shape: `prompt` inline.
    harness.board.add("ABC-1")

    harness.tick()

    argv = harness.claude_state.launches[0]["argv"]
    assert argv[-1] == "work on ABC-1 (https://tracker.invalid/ABC-1)"
    assert "--agent" not in argv and "--model" not in argv and "--effort" not in argv


def test_the_roles_setup_runs_after_the_repositorys(harness, tmp_path):
    script = tmp_path / "mark.sh"
    script.write_text('#!/bin/sh\necho "$1" >> "$2/order.txt"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    (harness.repo / ".chargehand.toml").write_text(
        f'base = "origin/main"\nsetup = "{script} repo {{worktree}}"\n'
    )
    write_role(harness.repo, "implementer", f"""\
prompt = "{{item}}"
setup = "{script} role {{worktree}}"

[[start]]
on = "labeled"
""")
    harness.board.add("ABC-1")

    report = harness.tick()

    assert report.launched == ["ABC-1"]
    order = (harness.worktree("ABC-1") / "order.txt").read_text().split()
    assert order == ["repo", "role"]


def test_a_role_not_started_by_the_queue_is_left_alone(harness):
    write_role(harness.repo, "implementer", """\
prompt = "/pr-review {item_url}"

[[start]]
on = "github.review_requested"
""")
    harness.board.add("ABC-1")

    report = harness.tick()

    assert report.launched == []
    assert report.errors == []
    assert harness.worker("ABC-1") is None


def test_a_missing_role_is_reported_not_fatal(tmp_path, repo, claude_state, board):
    harness = harness_with(tmp_path, repo, claude_state, board, roles=("ghost",))
    board.add("ABC-1")

    report = harness.tick()

    assert report.launched == []
    assert any("no role named 'ghost'" in error for error in report.errors)
    harness.ledger.close()


def test_roles_that_share_the_queue_do_not_both_take_an_item(tmp_path, repo, claude_state, board):
    """The queue label is one per item. The first role listed takes it; the label flips, and
    the second role no longer sees the item as queued."""
    write_role(repo, "second", 'prompt = "second look at {item}"\n\n[[start]]\non = "labeled"\n')
    harness = harness_with(tmp_path, repo, claude_state, board, roles=("implementer", "second"))
    board.add("ABC-1")

    report = harness.tick()

    assert report.launched == ["ABC-1"]
    assert harness.worker("ABC-1").role == "implementer"
    assert harness.worker("ABC-1", "second") is None
    harness.ledger.close()


def test_a_control_command_needs_the_role_when_several_roles_hold_the_item(harness):
    harness.board.add("ABC-1")
    harness.tick()
    first = harness.worker("ABC-1")
    # A second role holding the same item, as another event source would start it.
    second = harness.ledger.create_worker(
        role="second", item="ABC-1", item_id=first.item_id, url=first.url, repository="test",
        repo=harness.repo, worktree=harness.worktree("ABC-1", "second"),
        branch="chargehand/second/ABC-1",
    )
    harness.ledger.update(second, state=ledger_mod.RUNNING, session_id="other", session_uuid="u-other")

    request = harness.ledger.record_request("cancel", "ABC-1")
    harness.next_tick()
    outcome = harness.ledger.get_request(request)
    assert outcome["ok"] == 0
    assert "several roles" in outcome["message"]

    request = harness.ledger.record_request("cancel", "ABC-1", role="implementer")
    harness.next_tick()
    assert harness.ledger.get_request(request)["ok"] == 1
    assert harness.worker("ABC-1", "implementer").state == ledger_mod.CANCELLED


def test_a_roles_worker_cap_holds_across_items(harness):
    write_role(harness.repo, "implementer", """\
prompt = "{item}"

[[start]]
on = "labeled"

[policy]
max_workers = 1
""")
    harness.board.add("ABC-1")
    harness.board.add("ABC-2")

    report = harness.tick()

    assert report.launched == ["ABC-1"]
    assert any("role implementer is at its limit" in note for note in report.skipped)


def test_a_sessions_name_from_before_roles_is_still_found(harness):
    """A ledger migrated from version 1 points at sessions named after the issue alone."""
    harness.board.add("ABC-1")
    harness.tick()
    worker = harness.worker("ABC-1")
    state = harness.claude_state.read()
    state["sessions"][0]["name"] = "ABC-1"
    state["sessions"][0]["cwd"] = "/somewhere/else"
    harness.claude_state.write(state)
    harness.ledger.update(worker, session_id=None, session_uuid=None)

    harness.next_tick()

    assert harness.worker("ABC-1").session_id == worker.session_id


def test_a_start_rule_can_skip_an_item_that_any_role_has(tmp_path, repo, claude_state, board):
    write_role(repo, "second", """\
prompt = "second look at {item}"

[[start]]
on = "labeled"
unless_subscribed = "any"
""")
    harness = harness_with(tmp_path, repo, claude_state, board, roles=("second",))
    board.add("ABC-1")
    # A worker of another role already has the item.
    other = harness.ledger.create_worker(
        role="implementer", item="ABC-1", item_id="id-ABC-1", url=None, repository="test",
        repo=repo, worktree=harness.worktree("ABC-1"), branch="chargehand/implementer/ABC-1",
        origin=ledger_mod.ORIGIN_BOARD,
    )
    harness.ledger.update(other, state=ledger_mod.RUNNING)

    report = harness.tick()

    assert report.launched == []
    assert report.errors == []
    assert harness.worker("ABC-1", "second") is None
    harness.ledger.close()


def test_a_start_rule_can_be_limited_to_some_repositories(harness):
    write_role(harness.repo, "implementer", """\
prompt = "{item}"

[[start]]
on = "labeled"
repositories = ["somewhere-else"]
""")
    harness.board.add("ABC-1")

    report = harness.tick()

    assert report.launched == []
    assert report.errors == []

