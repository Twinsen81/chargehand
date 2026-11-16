"""Control requests. A mutating command records a request; only the tick acts."""

from __future__ import annotations

import json
import time

import pytest

from chargehand import ledger as ledger_mod
from chargehand import tick as tick_mod
from chargehand.errors import LaunchAborted
from conftest import run_git


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    return harness.worker(identifier)


def apply_request(harness, kind, target=None, **args):
    request_id = harness.ledger.record_request(kind, target, **args)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


def test_pause_stops_admitting_and_resume_restores_it(harness):
    harness.board.add("ABC-1")

    assert apply_request(harness, "pause")["ok"] == 1
    assert harness.worker("ABC-1") is None

    harness.next_tick()
    assert harness.worker("ABC-1") is None

    assert apply_request(harness, "resume")["ok"] == 1
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_pause_can_be_scoped_to_one_route(harness):
    harness.board.add("ABC-1")
    apply_request(harness, "pause", "test")

    assert harness.worker("ABC-1") is None
    assert harness.ledger.paused_repositories() == ["test"]


def test_a_running_session_keeps_working_while_the_queue_is_paused(harness):
    launch(harness)
    apply_request(harness, "pause")

    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_cancel_stops_the_session_marks_blocked_and_keeps_the_worktree(harness):
    attempt = launch(harness)

    result = apply_request(harness, "cancel", "ABC-1")

    assert result["ok"] == 1
    assert ["stop", attempt.session_id] in harness.claude_state.commands
    assert harness.worker("ABC-1").state == ledger_mod.CANCELLED
    assert harness.labels("ABC-1") == ("chargehand-blocked",)
    assert (harness.worktree("ABC-1")).is_dir()
    assert ("ABC-1", "cancelled") in harness.notifications()


def test_a_cancel_recorded_during_a_launch_is_applied_cleanly(harness):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=2)
    harness.ledger.record_request("cancel", "ABC-1")

    harness.next_tick()

    attempt = harness.worker("ABC-1")
    # Reconciliation runs first so the session is known, then the cancel stops it.
    # Applying the cancel earlier would leave that session running unsupervised.
    assert attempt.state == ledger_mod.CANCELLED
    assert attempt.session_id
    assert ["stop", attempt.session_id] in harness.claude_state.commands
    assert harness.labels("ABC-1") == ("chargehand-blocked",)


def test_stop_then_continue_round_trips_a_run(harness):
    attempt = launch(harness)

    assert apply_request(harness, "stop", "ABC-1")["ok"] == 1
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED
    assert ["stop", attempt.session_id] in harness.claude_state.commands

    assert apply_request(harness, "continue", "ABC-1")["ok"] == 1
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING
    assert ["respawn", attempt.session_id] in harness.claude_state.commands


def test_a_paused_run_is_not_reported_as_a_new_problem(harness):
    launch(harness)
    apply_request(harness, "stop", "ABC-1")
    before = len(harness.notifier.sent)

    harness.next_tick()

    assert len(harness.notifier.sent) == before
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED


def test_continue_refuses_a_run_that_is_not_paused_or_blocked(harness):
    launch(harness)

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert "not paused, blocked or parked" in result["message"]


def test_retry_starts_a_new_attempt_for_a_failed_run(harness, repo):
    (repo / "setup.sh").write_text("#!/bin/sh\nexit 3\n")
    (repo / "setup.sh").chmod(0o755)
    (repo / ".chargehand.toml").write_text(
        'base = "origin/main"\nprompt = "{issue}"\nsetup = "setup.sh {worktree}"\n'
    )
    harness.board.add("ABC-1")
    harness.tick()
    assert harness.worker("ABC-1").state == ledger_mod.FAILED
    first_id = harness.worker("ABC-1").id

    (repo / "setup.sh").write_text("#!/bin/sh\nexit 0\n")
    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 1
    attempt = harness.worker("ABC-1")
    assert attempt.id != first_id
    assert attempt.state == ledger_mod.RUNNING
    assert harness.labels("ABC-1") == ("chargehand-running",)


def test_retry_refuses_a_live_run(harness):
    launch(harness)

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 0
    assert "cancel or stop it" in result["message"]


def test_retry_refuses_to_throw_away_unpushed_work(harness):
    launch(harness)
    worktree = harness.worktree("ABC-1")
    (worktree / "work.txt").write_text("hours of it\n")
    run_git("add", "-A", cwd=worktree)
    run_git("commit", "-m", "wip", cwd=worktree)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 0
    assert "unpushed" in result["message"]
    assert (worktree / "work.txt").exists()


def test_discard_removes_the_session_worktree_and_branch(harness):
    attempt = launch(harness)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert not (harness.worktree("ABC-1")).exists()
    assert ["rm", attempt.session_id] in harness.claude_state.commands
    assert not harness.runner.git.branch_exists(harness.repo, "chargehand/implementer/ABC-1")


def test_discard_deletes_a_branch_the_session_renamed(harness):
    """A session may rename its placeholder after the tracker's convention."""
    launch(harness)
    worktree = harness.worktree("ABC-1")
    run_git("branch", "-m", "chargehand/implementer/ABC-1", "abc-1-a-title-slug", cwd=worktree)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert not harness.runner.git.branch_exists(harness.repo, "abc-1-a-title-slug")
    assert "kept" not in result["message"]


def test_discard_follows_a_branch_renamed_more_than_once(harness):
    launch(harness)
    worktree = harness.worktree("ABC-1")
    run_git("branch", "-m", "chargehand/implementer/ABC-1", "abc-1-first", cwd=worktree)
    run_git("branch", "-m", "abc-1-first", "abc-1-second", cwd=worktree)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert not harness.runner.git.branch_exists(harness.repo, "abc-1-second")


def test_discard_keeps_a_branch_the_runner_did_not_create(harness):
    """Only a recorded rename proves a branch is the runner's; any other may be work."""
    launch(harness)
    worktree = harness.worktree("ABC-1")
    run_git("checkout", "-b", "made-by-the-session", cwd=worktree)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert not worktree.exists()
    assert harness.runner.git.branch_exists(harness.repo, "made-by-the-session")
    assert not harness.runner.git.branch_exists(harness.repo, "chargehand/implementer/ABC-1")
    assert "branch was kept" in result["message"]
    # The session chose that name, and this message can reach an assistant's session.
    assert "made-by-the-session" not in result["message"]


def test_discard_keeps_a_renamed_branch_when_the_repository_keeps_no_reflog(harness):
    """Without a reflog there is no proof of the rename, so the branch stays."""
    run_git("config", "core.logAllRefUpdates", "false", cwd=harness.repo)
    launch(harness)
    worktree = harness.worktree("ABC-1")
    run_git("branch", "-m", "chargehand/implementer/ABC-1", "abc-1-a-title-slug", cwd=worktree)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert harness.runner.git.branch_exists(harness.repo, "abc-1-a-title-slug")
    assert "branch was kept" in result["message"]


def test_retry_deletes_a_branch_the_session_renamed(harness):
    launch(harness)
    worktree = harness.worktree("ABC-1")
    run_git("branch", "-m", "chargehand/implementer/ABC-1", "abc-1-a-title-slug", cwd=worktree)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 1
    assert not harness.runner.git.branch_exists(harness.repo, "abc-1-a-title-slug")


def test_discard_refuses_when_commits_are_unpushed(harness):
    launch(harness)
    worktree = harness.worktree("ABC-1")
    (worktree / "work.txt").write_text("hours of it\n")
    run_git("add", "-A", cwd=worktree)
    run_git("commit", "-m", "wip", cwd=worktree)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "unpushed" in result["message"]
    assert (worktree / "work.txt").exists()


def test_discard_force_overrides_the_refusal(harness):
    launch(harness)
    worktree = harness.worktree("ABC-1")
    (worktree / "work.txt").write_text("hours of it\n")
    run_git("add", "-A", cwd=worktree)
    run_git("commit", "-m", "wip", cwd=worktree)

    result = apply_request(harness, "discard", "ABC-1", force=True)

    assert result["ok"] == 1
    assert not worktree.exists()


def test_discard_refuses_when_the_worktree_is_dirty(harness):
    launch(harness)
    (harness.worktree("ABC-1") / "scratch.txt").write_text("uncommitted\n")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "uncommitted" in result["message"]


def test_a_request_for_an_unknown_issue_is_refused_not_ignored(harness):
    result = apply_request(harness, "cancel", "NOPE-1")

    assert result["ok"] == 0
    assert "no worker recorded" in result["message"]


def test_an_unknown_request_kind_does_not_kill_the_tick(harness):
    harness.board.add("ABC-1")
    harness.ledger.record_request("frobnicate", "ABC-1")

    harness.next_tick()

    assert harness.worker("ABC-1") is None or harness.worker("ABC-1").state


def test_a_cancelled_runs_leftover_session_is_not_re_adopted(harness):
    """cancel keeps the worktree, so its stopped session must not look like an orphan."""
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")

    report = harness.next_tick()

    assert report.adopted == []
    assert harness.ledger.live_workers() == []
    assert harness.worker("ABC-1").state == ledger_mod.CANCELLED


def test_a_cancelled_run_whose_session_kept_working_is_supervised_again(harness):
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")
    harness.claude_state.set_session_state("ABC-1", "working")

    report = harness.next_tick()

    assert "implementer/ABC-1" in report.adopted
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_cancel_refuses_when_the_session_will_not_stop(harness):
    """An exit code is not proof: the listing has to show the session stopped."""
    launch(harness)
    harness.claude_state.set(stop_returncode=3)

    result = apply_request(harness, "cancel", "ABC-1")

    assert result["ok"] == 0
    assert "did not stop" in result["message"]
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_discard_refuses_to_pull_a_worktree_out_from_under_a_live_session(harness):
    launch(harness)
    harness.claude_state.set(stop_returncode=3)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "still in use" in result["message"]
    assert (harness.worktree("ABC-1")).is_dir()


def test_discard_force_overrides_a_session_that_will_not_stop(harness):
    launch(harness)
    harness.claude_state.set(stop_returncode=3)

    result = apply_request(harness, "discard", "ABC-1", force=True)

    assert result["ok"] == 1
    assert not (harness.worktree("ABC-1")).exists()


def test_discard_says_so_when_the_session_could_not_be_removed(harness):
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")
    harness.claude_state.set(rm_returncode=4)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert "could not be removed" in result["message"]
    assert not (harness.worktree("ABC-1")).exists()


def hand_worker(harness):
    """A worker started by hand, with no item, then cancelled: its worktree stays."""
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer"
    )
    harness.next_tick()
    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 1, outcome["message"]
    worker = harness.ledger.get(int(outcome["message"].rsplit("#", 1)[1]))
    assert apply_request(harness, "cancel", worker.id_handle)["ok"] == 1
    return harness.ledger.get(worker.id)


def add_listed_session(harness, session_id, cwd, **extra):
    """A session that only the listing shows, such as a copy that `claude --resume` started."""
    sessions = harness.claude_state.sessions
    sessions.append({
        "id": session_id, "uuid": f"uuid-{session_id}", "cwd": str(cwd),
        "state": "working", "status": "busy", "pid": 4242, **extra,
    })
    harness.claude_state.set(sessions=sessions)


@pytest.mark.parametrize("where", ["worktree", "below", "link"])
def test_discard_refuses_a_session_with_no_row_in_the_worktree_and_force_removes_it(
    harness, tmp_path, where
):
    worker = hand_worker(harness)
    worktree = worker.worktree_path
    cwd = {"worktree": worktree, "below": worktree / "src", "link": tmp_path / "link"}[where]
    if where == "below":
        cwd.mkdir()
    if where == "link":
        cwd.symlink_to(worktree, target_is_directory=True)
    add_listed_session(harness, "47076c61", cwd, name=f"implementer/{worker.id_handle}")
    target = harness.ledger.target_for(worker)

    refused = apply_request(harness, "discard", target)

    assert refused["ok"] == 0
    assert "other sessions are in its worktree: 47076c61. Remove them" in refused["message"]
    assert refused["reason"] == {"needs_force": True, "other_sessions": ["47076c61"]}
    assert worktree.is_dir()
    assert ["stop", "47076c61"] not in harness.claude_state.commands
    assert ["stop", worker.session_id] in harness.claude_state.commands  # by the cancel only
    assert harness.claude_state.commands.count(["stop", worker.session_id]) == 1
    assert "47076c61" in [s["id"] for s in harness.claude_state.sessions]
    assert harness.ledger.get(worker.id).gc_done == 0

    forced = apply_request(harness, "discard", target, force=True)

    assert forced["ok"] == 1
    assert forced["message"].endswith(
        "; other sessions in its worktree stopped and removed: 47076c61"
    )
    commands = harness.claude_state.commands
    assert commands.index(["stop", "47076c61"]) < commands.index(["rm", "47076c61"])
    assert harness.claude_state.sessions == []
    assert not worktree.exists()
    assert harness.ledger.get(worker.id).gc_done == 1


def test_a_stopped_session_in_the_worktree_refuses_discard_until_it_is_removed(harness):
    worker = hand_worker(harness)
    # A stopped copy stays listed, without a process.
    add_listed_session(harness, "c0ffee01", worker.worktree_path, state="done")
    sessions = harness.claude_state.sessions
    for session in sessions:
        if session["id"] == "c0ffee01":
            session.pop("pid")
            session.pop("status")
    harness.claude_state.set(sessions=sessions)
    target = harness.ledger.target_for(worker)

    refused = apply_request(harness, "discard", target)

    assert refused["ok"] == 0
    assert refused["reason"] == {"needs_force": True, "other_sessions": ["c0ffee01"]}

    harness.claude_state.set(
        sessions=[s for s in harness.claude_state.sessions if s["id"] != "c0ffee01"]
    )
    assert apply_request(harness, "discard", target)["ok"] == 1
    assert not worker.worktree_path.exists()


def earlier_session(harness, worker, session_id, **listed):
    """A session that a refresh replaced: it keeps its row, and the listing shows it."""
    harness.ledger.record_session(
        worker, kind=ledger_mod.SESSION_REFRESH, session_id=session_id,
        session_uuid=f"uuid-{session_id}", now=1.0,
    )
    add_listed_session(harness, session_id, worker.worktree_path, **listed)


def test_an_earlier_session_that_runs_again_in_the_worktree_refuses_discard(harness):
    worker = hand_worker(harness)
    # The operator resumed the replaced session in place: the worker's own stop misses it.
    earlier_session(harness, worker, "e0a1e5d1")
    target = harness.ledger.target_for(worker)

    refused = apply_request(harness, "discard", target)

    assert refused["ok"] == 0
    assert refused["reason"] == {"needs_force": True, "other_sessions": ["e0a1e5d1"]}
    assert ["rm", "e0a1e5d1"] not in harness.claude_state.commands
    assert worker.worktree_path.is_dir()

    forced = apply_request(harness, "discard", target, force=True)

    assert forced["ok"] == 1
    assert "other sessions in its worktree stopped and removed: e0a1e5d1" in forced["message"]
    commands = harness.claude_state.commands
    assert commands.index(["stop", "e0a1e5d1"]) < commands.index(["rm", "e0a1e5d1"])
    assert not worker.worktree_path.exists()


def test_a_stopped_earlier_session_is_removed_with_the_worker(harness):
    worker = hand_worker(harness)
    earlier_session(harness, worker, "e0a1e5d2", state="done")
    sessions = harness.claude_state.sessions
    for session in sessions:
        if session["id"] == "e0a1e5d2":
            session.pop("pid")
            session.pop("status")
    harness.claude_state.set(sessions=sessions)

    result = apply_request(harness, "discard", harness.ledger.target_for(worker))

    assert result["ok"] == 1
    assert "other sessions" not in result["message"]
    assert ["rm", "e0a1e5d2"] in harness.claude_state.commands
    assert "e0a1e5d2" not in [s["id"] for s in harness.claude_state.sessions]


def test_discard_names_each_session_that_could_not_be_removed(harness):
    worker = hand_worker(harness)
    earlier_session(harness, worker, "e0a1e5d3", state="done")
    sessions = harness.claude_state.sessions
    for session in sessions:
        if session["id"] == "e0a1e5d3":
            session.pop("pid")
            session.pop("status")
    harness.claude_state.set(sessions=sessions, rm_returncode=4)

    result = apply_request(harness, "discard", harness.ledger.target_for(worker))

    assert result["ok"] == 1
    assert "could not be removed; remove sessions e0a1e5d3, s1 by hand" in result["message"]


def test_a_refusal_counts_the_other_sessions_it_does_not_name(harness):
    worker = hand_worker(harness)
    ids = [f"copy{n}" for n in range(7)]
    for session_id in ids:
        add_listed_session(harness, session_id, worker.worktree_path)

    refused = apply_request(harness, "discard", harness.ledger.target_for(worker))

    assert refused["ok"] == 0
    assert f"{', '.join(ids[:5])} and 2 more." in refused["message"]
    assert refused["reason"] == {
        "needs_force": True, "other_sessions": ids[:5], "other_sessions_more": 2,
    }


def test_discard_leaves_a_session_outside_the_worktree_alone(harness, tmp_path):
    worker = hand_worker(harness)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A sibling whose name starts with the worktree's name is not below it.
    sibling = worker.worktree_path.with_name(worker.worktree_path.name + "-2")
    sibling.mkdir()
    add_listed_session(harness, "aaaa1111", elsewhere)
    add_listed_session(harness, "bbbb2222", sibling)

    result = apply_request(harness, "discard", harness.ledger.target_for(worker))

    assert result["ok"] == 1 and "other sessions" not in result["message"]
    assert [s["id"] for s in harness.claude_state.sessions] == ["aaaa1111", "bbbb2222"]
    assert not any(c[1] in ("aaaa1111", "bbbb2222") for c in harness.claude_state.commands)


def test_forced_discard_names_another_session_that_did_not_stop(harness):
    worker = hand_worker(harness)
    add_listed_session(harness, "47076c61", worker.worktree_path)
    harness.claude_state.set(stop_state="working")

    result = apply_request(harness, "discard", harness.ledger.target_for(worker), force=True)

    assert result["ok"] == 1
    assert result["message"].endswith(
        "; other sessions in its worktree could not be stopped and removed: 47076c61; "
        "remove them by hand"
    )
    assert ["rm", "47076c61"] not in harness.claude_state.commands


def test_discard_never_stops_the_session_of_another_live_worker_in_the_worktree(harness):
    live = launch(harness)
    worker = hand_worker(harness)
    sessions = harness.claude_state.sessions
    for session in sessions:
        if session["id"] == live.session_id:
            session["cwd"] = str(worker.worktree_path)
    harness.claude_state.set(sessions=sessions)

    for force in (False, True):
        result = apply_request(harness, "discard", harness.ledger.target_for(worker), force=force)

        assert result["ok"] == 0 and result["reason"] is None
        assert f"the session of live worker {live.id_handle} works in its worktree" in result["message"]
    assert ["stop", live.session_id] not in harness.claude_state.commands
    assert worker.worktree_path.is_dir()
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_discard_refuses_without_force_when_the_listing_cannot_be_read(harness, monkeypatch):
    worker = hand_worker(harness)
    target = harness.ledger.target_for(worker)
    apply = tick_mod.Runner._apply_request

    def listing_fails_from_the_request_on(self, kind, *args):
        # The tick applies no request when its own first listing fails.
        if kind == "discard":
            harness.claude_state.set(agents_fail=True)
        try:
            return apply(self, kind, *args)
        finally:
            harness.claude_state.set(agents_fail=False)

    monkeypatch.setattr(tick_mod.Runner, "_apply_request", listing_fails_from_the_request_on)

    refused = apply_request(harness, "discard", target)

    assert refused["ok"] == 0 and "listing could not be read" in refused["message"]
    assert refused["reason"] == {"needs_force": True, "sessions_unknown": True}
    assert worker.worktree_path.is_dir()

    forced = apply_request(harness, "discard", target, force=True)

    assert forced["ok"] == 1
    assert "other sessions were not looked for" in forced["message"]
    assert not worker.worktree_path.exists()


def new_workers(harness, worker):
    return [w for w in harness.ledger.live_workers() if w.id != worker.id]


def test_retry_refuses_a_session_with_no_row_in_the_worktree_and_force_removes_it(harness):
    worker = hand_worker(harness)
    add_listed_session(harness, "47076c61", worker.worktree_path / "src")
    (worker.worktree_path / "src").mkdir()
    target = harness.ledger.target_for(worker)

    refused = apply_request(harness, "retry", target)

    assert refused["ok"] == 0
    assert "other sessions are in its worktree: 47076c61. Remove them" in refused["message"]
    assert refused["reason"] == {"needs_force": True, "other_sessions": ["47076c61"]}
    assert worker.worktree_path.is_dir()
    assert ["stop", "47076c61"] not in harness.claude_state.commands
    assert harness.claude_state.commands.count(["stop", worker.session_id]) == 1  # the cancel
    assert ["rm", worker.session_id] not in harness.claude_state.commands
    assert harness.ledger.get(worker.id).gc_done == 0
    assert new_workers(harness, worker) == []

    forced = apply_request(harness, "retry", target, force=True)

    assert forced["ok"] == 1, forced["message"]
    assert forced["message"].startswith("cleared; ")
    assert forced["message"].endswith(
        "; other sessions in its worktree stopped and removed: 47076c61"
    )
    commands = harness.claude_state.commands
    assert commands.index(["stop", "47076c61"]) < commands.index(["rm", "47076c61"])
    assert "47076c61" not in [s["id"] for s in harness.claude_state.sessions]
    assert not worker.worktree_path.exists()
    assert harness.ledger.get(worker.id).gc_done == 1
    (again,) = new_workers(harness, worker)
    assert again.role == worker.role


def test_an_earlier_session_that_runs_again_in_the_worktree_refuses_retry(harness):
    worker = hand_worker(harness)
    earlier_session(harness, worker, "e0a1e5d4")

    refused = apply_request(harness, "retry", harness.ledger.target_for(worker))

    assert refused["ok"] == 0
    assert refused["reason"] == {"needs_force": True, "other_sessions": ["e0a1e5d4"]}
    assert worker.worktree_path.is_dir()


def test_a_forced_retry_names_another_session_that_did_not_stop(harness):
    worker = hand_worker(harness)
    add_listed_session(harness, "47076c62", worker.worktree_path)
    harness.claude_state.set(stop_state="working")

    result = apply_request(harness, "retry", harness.ledger.target_for(worker), force=True)

    assert "; other sessions in its worktree could not be stopped and removed: 47076c62; " \
        "remove them by hand" in result["message"]
    assert ["rm", "47076c62"] not in harness.claude_state.commands


def test_retry_never_stops_the_session_of_another_live_worker_in_the_worktree(harness):
    live = launch(harness)
    worker = hand_worker(harness)
    sessions = harness.claude_state.sessions
    for session in sessions:
        if session["id"] == live.session_id:
            session["cwd"] = str(worker.worktree_path)
    harness.claude_state.set(sessions=sessions)

    for force in (False, True):
        result = apply_request(harness, "retry", harness.ledger.target_for(worker), force=force)

        assert result["ok"] == 0 and result["reason"] is None
        assert f"the session of live worker {live.id_handle} works in its worktree" in result["message"]
    assert ["stop", live.session_id] not in harness.claude_state.commands
    assert worker.worktree_path.is_dir()
    assert harness.ledger.get(worker.id).gc_done == 0
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING


def test_retry_leaves_a_session_outside_the_worktree_alone(harness, tmp_path):
    worker = hand_worker(harness)
    sibling = worker.worktree_path.with_name(worker.worktree_path.name + "-2")
    sibling.mkdir()
    add_listed_session(harness, "bbbb2223", sibling)

    result = apply_request(harness, "retry", harness.ledger.target_for(worker))

    assert result["ok"] == 1 and "other sessions" not in result["message"]
    assert "bbbb2223" in [s["id"] for s in harness.claude_state.sessions]
    assert not any(c[1] == "bbbb2223" for c in harness.claude_state.commands)
    assert not worker.worktree_path.exists()


def test_retry_refuses_without_force_when_the_listing_cannot_be_read(harness, monkeypatch):
    worker = hand_worker(harness)
    target = harness.ledger.target_for(worker)
    other_sessions = tick_mod.Runner._other_sessions

    def listing_fails(self, *args):
        # Only this look fails: the retry's own stop and removal read the listing too.
        harness.claude_state.set(agents_fail=True)
        try:
            return other_sessions(self, *args)
        finally:
            harness.claude_state.set(agents_fail=False)

    monkeypatch.setattr(tick_mod.Runner, "_other_sessions", listing_fails)

    refused = apply_request(harness, "retry", target)

    assert refused["ok"] == 0 and "listing could not be read" in refused["message"]
    assert "Re-run with --force to retry anyway." in refused["message"]
    assert refused["reason"] == {"needs_force": True, "sessions_unknown": True}
    assert worker.worktree_path.is_dir()
    assert new_workers(harness, worker) == []

    forced = apply_request(harness, "retry", target, force=True)

    assert forced["ok"] == 1
    assert "other sessions were not looked for" in forced["message"]
    assert not worker.worktree_path.exists()


def test_retry_starts_a_new_session_instead_of_adopting_the_old_one(harness):
    """`stop` keeps the session, and it still carries this issue's name and worktree."""
    launch(harness)
    launches_before = len(harness.claude_state.launches)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 1
    assert len(harness.claude_state.launches) == launches_before + 1
    attempt = harness.worker("ABC-1")
    assert attempt.state == ledger_mod.RUNNING
    assert attempt.session_state == "working"


def test_retry_refuses_when_the_previous_session_cannot_be_removed(harness):
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")
    harness.claude_state.set(rm_returncode=4)

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 0
    assert "would adopt it instead of starting" in result["message"]


def test_a_launch_never_adopts_a_session_another_attempt_owns(harness):
    launch(harness)
    first = harness.worker("ABC-1")
    apply_request(harness, "cancel", "ABC-1")
    # The old session is still listed, with this issue's name and working directory.
    assert any(s["id"] == first.session_id for s in harness.claude_state.sessions)

    apply_request(harness, "retry", "ABC-1")

    assert harness.worker("ABC-1").session_id != first.session_id


def test_a_control_request_survives_an_unusable_tracker_credential(harness, monkeypatch):
    """A locked keychain must not brick the tick."""
    from chargehand.errors import TrackerAuthError
    from support import fake_tracker

    launch(harness)
    monkeypatch.setattr(
        fake_tracker.FakeTracker,
        "list",
        lambda self, status: (_ for _ in ()).throw(TrackerAuthError("linear: no API key")),
    )

    result = apply_request(harness, "cancel", "ABC-1")

    assert result["applied_at"] is not None
    assert result["ok"] == 1
    assert harness.worker("ABC-1").state == ledger_mod.CANCELLED


def test_one_broken_phase_does_not_stop_the_rest_of_the_tick(harness, monkeypatch):
    launch(harness)
    monkeypatch.setattr(
        harness.runner, "_reconcile",
        lambda report: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    request_id = harness.ledger.record_request("cancel", "ABC-1")

    report = harness.runner.tick()

    assert any("phase reconcile failed" in error for error in report.errors)
    assert harness.ledger.get_request(request_id)["applied_at"] is not None


def test_continue_refuses_when_the_respawn_did_not_take(harness):
    launch(harness)
    apply_request(harness, "stop", "ABC-1")
    harness.claude_state.set(respawn_returncode=5)

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED


@pytest.mark.parametrize("process_left", [True, False])
def test_an_unrecognised_session_state_never_counts_as_stopped(harness, process_left):
    """`unknown` means "go look" everywhere else; it must not authorise deleting a worktree.

    It fails closed also when the entry has no pid and no status, which for a known state
    means the process is gone.
    """
    launch(harness)
    if process_left:
        # The stop reports success, but the listing keeps the process and says something
        # unrecognised.
        harness.claude_state.set(stop_state="reticulating-splines")
    else:
        # The stop drops pid and status, and the state is one chargehand does not know.
        harness.claude_state.set_session_state("ABC-1", "reticulating-splines")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "still in use" in result["message"]
    assert (harness.worktree("ABC-1")).is_dir()


@pytest.fixture
def short_respawn_wait(monkeypatch):
    monkeypatch.setattr(tick_mod, "RESPAWN_WAIT_SECS", 0.3)
    monkeypatch.setattr(tick_mod, "RESPAWN_POLL_SECS", 0.01)


def test_continue_refuses_a_respawn_that_started_no_process(harness, short_respawn_wait):
    launch(harness)
    harness.claude_state.set_session_state("ABC-1", "done")
    apply_request(harness, "stop", "ABC-1")
    harness.claude_state.set(respawn_starts_no_process=True)

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED
    # The listing still says `done`, the state of the last turn; the reason is the process.
    assert result["message"] == (
        "ABC-1: respawn reported success but no process is running after 0.3 s (the session "
        "lists as done); if one starts later, the next tick shows it"
    )


def test_continue_waits_for_the_respawned_process_to_show(harness, monkeypatch):
    """The respawn returns before its process is listed; the answer is what happened."""
    monkeypatch.setattr(tick_mod, "RESPAWN_POLL_SECS", 0.01)
    attempt = launch(harness)
    harness.claude_state.set_session_state("ABC-1", "done")
    apply_request(harness, "stop", "ABC-1")
    harness.claude_state.set(respawn_listed_after=3)

    result = apply_request(harness, "continue", "ABC-1")

    assert (result["ok"], result["message"]) == (1, "session respawned")
    assert harness.worker("ABC-1").state == ledger_mod.RUNNING
    assert harness.claude_state.commands.count(["respawn", attempt.session_id]) == 1
    assert "respawn_pending" not in harness.claude_state.read()


def test_continue_points_a_question_at_a_reply_and_leaves_the_session(harness):
    attempt = launch(harness)
    # The ask tool asked, and the listing shows the turn ended: `blocked`, `status: idle`.
    harness.ledger.record_question(harness.worker("ABC-1"), "Which file next?", [])
    harness.claude_state.set_session_state("ABC-1", "blocked", status="idle")
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert result["message"] == (
        "ABC-1: its turn ended and its session waits, so there is nothing to respawn: send a reply"
    )
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    assert ["respawn", attempt.session_id] not in harness.claude_state.commands
    assert harness.claude_state.sessions[0]["status"] == "idle"


def test_continue_points_a_dialog_at_its_answer_and_leaves_the_session(harness):
    attempt = launch(harness)
    harness.claude_state.set_session_state(
        "ABC-1", "blocked", status="waiting", waitingFor="permission prompt"
    )
    harness.next_tick()

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert result["message"] == (
        f"ABC-1: its session waits for the answer to a prompt. Answer a permission prompt on "
        f"its card, or any prompt with `claude attach {attempt.session_id}`"
    )
    assert ["respawn", attempt.session_id] not in harness.claude_state.commands


def test_continue_points_an_open_permission_prompt_at_its_answer_when_no_dialog_is_listed(
    harness,
):
    attempt = launch(harness)
    harness.claude_state.set_session_state(
        "ABC-1", "blocked", status="waiting", waitingFor="permission prompt"
    )
    harness.next_tick()
    prompt = harness.ledger.record_permission_prompt(
        harness.worker("ABC-1"), tool_name="Bash", tool_input='{"command": "date"}'
    )
    harness.ledger.release_permission_prompt(prompt)
    # The listing shows no dialog, while the ledger still holds the open prompt: a reply
    # would wait in the inbox behind it.
    harness.claude_state.set_session_state("ABC-1", "blocked", status="idle", waitingFor=None)

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert result["message"] == (
        f"ABC-1: its session waits for the answer to a prompt. Answer a permission prompt on "
        f"its card, or any prompt with `claude attach {attempt.session_id}`"
    )
    assert harness.ledger.get_permission_prompt(prompt).state == "open"
    assert ["respawn", attempt.session_id] not in harness.claude_state.commands


def with_status_file(repo, tmp_path):
    status_dir = tmp_path / "status"
    status_dir.mkdir(exist_ok=True)
    (repo / ".chargehand.toml").write_text(
        f'base = "origin/main"\nprompt = "{{issue}}"\nstatus_file = "{status_dir}/{{issue}}.json"\n'
    )
    return status_dir


def asked_in_the_status_file(harness, repo, tmp_path):
    """The status file says needs-input; the session's turn ended `done` and it lives on."""
    status_dir = with_status_file(repo, tmp_path)
    attempt = launch(harness)
    (status_dir / "ABC-1.json").write_text(json.dumps({"state": "needs-input"}))
    harness.claude_state.set_session_state("ABC-1", "done", status="idle")
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    return attempt


def test_a_reply_answers_a_question_that_the_status_file_asked(harness, repo, tmp_path):
    asked_in_the_status_file(harness, repo, tmp_path)

    result = apply_request(harness, "reply", "ABC-1", text="yes")

    assert result["ok"] == 1 and "answers the worker's question" in result["message"]
    worker = harness.worker("ABC-1")
    assert (worker.state, worker.wakes) == (ledger_mod.RUNNING, 1)
    (resume,) = harness.claude_state.read()["resumes"]
    assert "reply from the operator: yes" in resume["prompt"].splitlines()


def test_continue_points_a_status_file_question_at_a_reply(harness, repo, tmp_path):
    attempt = asked_in_the_status_file(harness, repo, tmp_path)

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert result["message"] == (
        "ABC-1: its turn ended and its session waits, so there is nothing to respawn: send a reply"
    )
    assert ["respawn", attempt.session_id] not in harness.claude_state.commands


def test_a_prompt_during_the_answer_to_a_question_is_notified(harness, monkeypatch):
    """No SessionStart hook comes first, so the record of the notified question must go."""
    from chargehand import claude

    attempt = launch(harness)
    harness.ledger.record_question(harness.worker("ABC-1"), "Which file next?", [])
    harness.claude_state.set_session_state("ABC-1", "blocked", status="idle")
    harness.next_tick()
    assert harness.notifications().count(("ABC-1", "blocked")) == 1
    real = claude.ClaudeRuntime.wake

    def wake_then_prompt(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        hook = claude.parse_hook("PermissionRequest", {
            "session_id": attempt.session_uuid, "tool_name": "Bash",
            "tool_input": {"command": "date"},
        })
        harness.runner.apply_hook(harness.worker("ABC-1"), hook)
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "wake", wake_then_prompt)

    assert apply_request(harness, "reply", "ABC-1", text="yes")["ok"] == 1

    worker = harness.worker("ABC-1")
    assert (worker.state, worker.wakes) == (ledger_mod.BLOCKED, 1)
    assert len(harness.ledger.open_permission_prompts(worker)) == 1
    assert harness.notifications().count(("ABC-1", "blocked")) == 2


def test_a_held_answer_does_not_notify_the_question_again(harness, repo, tmp_path, monkeypatch):
    from chargehand import claude
    from chargehand.runtime import WakeResult

    asked_in_the_status_file(harness, repo, tmp_path)
    monkeypatch.setattr(
        claude.ClaudeRuntime, "wake",
        lambda self, *args, **kwargs: WakeResult(False, "a turn started", busy=True),
    )

    apply_request(harness, "reply", "ABC-1", text="yes")
    harness.next_tick()

    worker = harness.worker("ABC-1")
    assert (worker.state, worker.notified_state) == (ledger_mod.BLOCKED, "done")
    assert harness.notifications().count(("ABC-1", "blocked")) == 1


def test_continue_respawns_a_blocked_worker_whose_session_was_stopped(harness):
    """A stopped session lists the state of its last turn; `continue` brings its process back."""
    attempt = launch(harness)
    state = harness.claude_state.read()
    (entry,) = state["sessions"]
    entry["state"] = "blocked"
    del entry["pid"], entry["status"]
    harness.claude_state.write(state)
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED

    result = apply_request(harness, "continue", "ABC-1")

    assert (result["ok"], result["message"]) == (1, "session respawned")
    assert ["respawn", attempt.session_id] in harness.claude_state.commands


def test_a_stopped_idle_session_keeps_its_worker_paused(harness):
    """A stopped Claude Code session still lists `done`, the state of its last turn."""
    attempt = launch(harness)
    harness.claude_state.set_session_state("ABC-1", "done")
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.IDLE

    assert apply_request(harness, "stop", "ABC-1")["ok"] == 1
    (entry,) = harness.claude_state.sessions
    assert entry["state"] == "done" and "pid" not in entry
    harness.next_tick()
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.PAUSED
    assert harness.worker("ABC-1").id == attempt.id


def test_a_permission_prompt_of_the_respawned_session_stays_open(harness, monkeypatch):
    """The respawned session can ask for permission before the continue writes `running`."""
    from chargehand import claude

    attempt = launch(harness)
    assert apply_request(harness, "stop", "ABC-1")["ok"] == 1
    old = harness.ledger.record_permission_prompt(
        harness.worker("ABC-1"), tool_name="Bash", tool_input="{}"
    )
    harness.ledger.update(harness.worker("ABC-1"), last_hook="PermissionRequest")
    real = claude.ClaudeRuntime.respawn

    def respawn_then_prompt(self, session_id):
        result = real(self, session_id)
        hook = claude.parse_hook("PermissionRequest", {
            "session_id": attempt.session_uuid, "tool_name": "Bash",
            "tool_input": {"command": "date"},
        })
        harness.runner.apply_hook(harness.worker("ABC-1"), hook)
        return result

    monkeypatch.setattr(claude.ClaudeRuntime, "respawn", respawn_then_prompt)
    # The respawned session shows the dialog, so the listing says blocked too.
    harness.claude_state.set(respawn_state="blocked")

    assert apply_request(harness, "continue", "ABC-1")["ok"] == 1

    # A hook does not move a paused worker, so the continue writes the state the new
    # session asked for: the card is answerable, not in Working.
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    [prompt] = harness.ledger.open_permission_prompts(harness.worker("ABC-1"))
    assert json.loads(prompt.tool_input) == {"command": "date"}
    assert harness.ledger.get_permission_prompt(old).state == "cancelled"


# ----- refusals that force overrides: the reason for the board -------------------------------


def cancelled(harness, identifier="ABC-1"):
    worker = launch(harness, identifier)
    assert apply_request(harness, "cancel", identifier)["ok"] == 1
    return harness.ledger.get(worker.id)


def record(harness, worker):
    raw = harness.ledger.get_state(f"{tick_mod.WORK_AT_RISK_PREFIX}{worker.id}")
    return json.loads(raw) if raw else None


def at_risk(harness, worker):
    """The facts the tick recorded, or None when it found nothing to lose."""
    found = record(harness, worker)
    if found is None:
        return None
    found.pop("checked_at")
    return found if any(found[k] for k in ("uncommitted_changes", "unpushed_commits", "uninspected")) else None


DIRTY = {"place": "worktree", "uncommitted_changes": True, "unpushed_commits": 0, "uninspected": False}


def test_a_refused_discard_retry_and_archive_carry_the_reason_and_keep_the_cli_text(harness):
    worker = cancelled(harness)
    (worker.worktree_path / "progress.log").write_text("notes\n")
    texts = {
        "discard": f"{worker.name}: its worktree has uncommitted changes. Re-run with --force to "
                   f"discard anyway.",
        "retry": f"{worker.name}: its worktree has uncommitted changes. Push or discard it first "
                 f"(`chargehand discard {harness.ledger.target_for(worker)} --yes`).",
        "archive": f"{worker.name}: its worktree has uncommitted changes. Commit and push that work "
                   f"first, or archive with force: the archive keeps the files, not the commits.",
    }

    for kind, text in texts.items():
        result = apply_request(harness, kind, "ABC-1")
        assert (result["ok"], result["message"]) == (0, text), kind
        assert result["reason"] == {"needs_force": True, "work": DIRTY}, kind

    run_git("add", "-A", cwd=worker.worktree_path)
    run_git("commit", "-m", "wip", cwd=worker.worktree_path)
    result = apply_request(harness, "discard", "ABC-1")
    assert result["message"].endswith(
        "its worktree holds 1 unpushed commit(s). Re-run with --force to discard anyway."
    )
    assert result["reason"]["work"] == {
        "place": "worktree", "uncommitted_changes": False, "unpushed_commits": 1, "uninspected": False,
    }
    assert worker.worktree_path.exists()


def test_a_discard_whose_session_did_not_stop_needs_force(harness):
    launch(harness)
    harness.claude_state.set(stop_returncode=1)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0 and "re-run with --force" in result["message"]
    assert result["reason"] == {"needs_force": True, "session_running": True}


def test_a_refusal_that_force_does_not_override_has_no_reason(harness):
    launch(harness)

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 0 and result["reason"] is None
    assert apply_request(harness, "pause")["reason"] is None


def test_the_tick_looks_at_an_ended_worker_once_it_ended_and_then_once_per_interval(harness, monkeypatch):
    worker = launch(harness)
    (worker.worktree_path / "progress.log").write_text("notes\n")
    assert apply_request(harness, "cancel", "ABC-1")["ok"] == 1
    assert at_risk(harness, worker) == DIRTY
    checked_at = record(harness, worker)["checked_at"]

    looks = []
    look = tick_mod.Runner._work_at_risk
    # Each tick of the harness builds a new runner, so the class is patched.
    monkeypatch.setattr(
        tick_mod.Runner, "_work_at_risk", lambda self, w: looks.append(w.id) or look(self, w)
    )
    (worker.worktree_path / "progress.log").unlink()
    for _ in range(3):
        harness.next_tick()
    assert looks == [] and at_risk(harness, worker) == DIRTY

    harness.runner._record_work_at_risk(now=checked_at + tick_mod.WORK_AT_RISK_REFRESH_SECS + 1)
    assert looks == [worker.id]
    assert at_risk(harness, worker) is None and record(harness, worker) is not None

    # A refusal looks at once, whatever the interval.
    (worker.worktree_path / "progress.log").write_text("notes\n")
    assert apply_request(harness, "discard", "ABC-1")["ok"] == 0
    assert at_risk(harness, worker) == DIRTY
    assert apply_request(harness, "discard", "ABC-1", force=True)["ok"] == 1
    assert record(harness, worker) is None


def test_the_record_of_a_worker_that_left_the_closed_window_is_dropped(harness):
    worker = cancelled(harness)
    assert record(harness, worker) is not None
    harness.ledger._conn.execute(
        "UPDATE workers SET finished_at = ? WHERE id = ?",
        (time.time() - tick_mod.WORK_AT_RISK_WINDOW_SECS - 60, worker.id),
    )

    harness.runner._record_work_at_risk()

    assert record(harness, worker) is None


def test_a_live_worker_is_looked_at_only_after_a_refusal(harness):
    worker = launch(harness)
    (worker.worktree_path / "scratch.txt").write_text("in progress\n")
    harness.next_tick()
    assert record(harness, worker) is None

    assert apply_request(harness, "archive", "ABC-1")["ok"] == 0
    assert at_risk(harness, worker) == DIRTY
    checked_at = record(harness, worker)["checked_at"]

    (worker.worktree_path / "scratch.txt").unlink()
    harness.runner._record_work_at_risk(now=checked_at + tick_mod.WORK_AT_RISK_REFRESH_SECS + 1)
    assert record(harness, worker) is None
    (worker.worktree_path / "scratch.txt").write_text("in progress\n")
    harness.next_tick()
    assert record(harness, worker) is None
