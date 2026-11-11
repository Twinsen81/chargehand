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
    # A plain-text question ended the turn: `state: blocked`, `status: idle`.
    harness.claude_state.set_session_state("ABC-1", "blocked", status="idle")
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert result["message"] == (
        "ABC-1 asked a question, and its session waits for the answer: send the answer as a reply"
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


def test_a_held_answer_does_not_notify_the_question_again(harness, monkeypatch):
    from chargehand import claude
    from chargehand.runtime import WakeResult

    launch(harness)
    harness.claude_state.set_session_state("ABC-1", "blocked", status="idle")
    harness.next_tick()
    monkeypatch.setattr(
        claude.ClaudeRuntime, "wake",
        lambda self, *args, **kwargs: WakeResult(False, "a turn started", busy=True),
    )

    apply_request(harness, "reply", "ABC-1", text="yes")
    harness.next_tick()

    worker = harness.worker("ABC-1")
    assert (worker.state, worker.notified_state) == (ledger_mod.BLOCKED, "blocked")
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
