"""Control requests. A mutating command records a request; only the tick acts."""

from __future__ import annotations

import pytest

from chargehand import ledger as ledger_mod
from chargehand.errors import LaunchAborted
from conftest import run_git


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    return harness.attempt(identifier)


def apply_request(harness, kind, target=None, **args):
    request_id = harness.ledger.record_request(kind, target, **args)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


def test_pause_stops_admitting_and_resume_restores_it(harness):
    harness.board.add("ABC-1")

    assert apply_request(harness, "pause")["ok"] == 1
    assert harness.attempt("ABC-1") is None

    harness.next_tick()
    assert harness.attempt("ABC-1") is None

    assert apply_request(harness, "resume")["ok"] == 1
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_pause_can_be_scoped_to_one_route(harness):
    harness.board.add("ABC-1")
    apply_request(harness, "pause", "test")

    assert harness.attempt("ABC-1") is None
    assert harness.ledger.paused_routes() == ["test"]


def test_a_running_session_keeps_working_while_the_queue_is_paused(harness):
    launch(harness)
    apply_request(harness, "pause")

    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_cancel_stops_the_session_marks_blocked_and_keeps_the_worktree(harness):
    attempt = launch(harness)

    result = apply_request(harness, "cancel", "ABC-1")

    assert result["ok"] == 1
    assert ["stop", attempt.session_id] in harness.claude_state.commands
    assert harness.attempt("ABC-1").state == ledger_mod.CANCELLED
    assert harness.labels("ABC-1") == ("chargehand-blocked",)
    assert (harness.worktree_root / "ABC-1").is_dir()
    assert ("ABC-1", "cancelled") in harness.notifications()


def test_a_cancel_recorded_during_a_launch_is_applied_cleanly(harness):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=2)
    harness.ledger.record_request("cancel", "ABC-1")

    harness.next_tick()

    attempt = harness.attempt("ABC-1")
    # Reconciliation runs first so the session is known, then the cancel stops it.
    # Applying the cancel earlier would leave that session running unsupervised.
    assert attempt.state == ledger_mod.CANCELLED
    assert attempt.session_id
    assert ["stop", attempt.session_id] in harness.claude_state.commands
    assert harness.labels("ABC-1") == ("chargehand-blocked",)


def test_stop_then_continue_round_trips_a_run(harness):
    attempt = launch(harness)

    assert apply_request(harness, "stop", "ABC-1")["ok"] == 1
    assert harness.attempt("ABC-1").state == ledger_mod.PAUSED
    assert ["stop", attempt.session_id] in harness.claude_state.commands

    assert apply_request(harness, "continue", "ABC-1")["ok"] == 1
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING
    assert ["respawn", attempt.session_id] in harness.claude_state.commands


def test_a_paused_run_is_not_reported_as_a_new_problem(harness):
    launch(harness)
    apply_request(harness, "stop", "ABC-1")
    before = len(harness.notifier.sent)

    harness.next_tick()

    assert len(harness.notifier.sent) == before
    assert harness.attempt("ABC-1").state == ledger_mod.PAUSED


def test_continue_refuses_a_run_that_is_not_paused_or_blocked(harness):
    launch(harness)

    result = apply_request(harness, "continue", "ABC-1")

    assert result["ok"] == 0
    assert "not paused or blocked" in result["message"]


def test_retry_starts_a_new_attempt_for_a_failed_run(harness, repo):
    (repo / "setup.sh").write_text("#!/bin/sh\nexit 3\n")
    (repo / "setup.sh").chmod(0o755)
    (repo / ".chargehand.toml").write_text(
        'base = "origin/main"\nprompt = "{issue}"\nsetup = "setup.sh {worktree}"\n'
    )
    harness.board.add("ABC-1")
    harness.tick()
    assert harness.attempt("ABC-1").state == ledger_mod.FAILED
    first_id = harness.attempt("ABC-1").id

    (repo / "setup.sh").write_text("#!/bin/sh\nexit 0\n")
    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 1
    attempt = harness.attempt("ABC-1")
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
    worktree = harness.worktree_root / "ABC-1"
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
    assert not (harness.worktree_root / "ABC-1").exists()
    assert ["rm", attempt.session_id] in harness.claude_state.commands
    assert not harness.runner.git.branch_exists(harness.repo, "chargehand/ABC-1")


def test_discard_refuses_when_commits_are_unpushed(harness):
    launch(harness)
    worktree = harness.worktree_root / "ABC-1"
    (worktree / "work.txt").write_text("hours of it\n")
    run_git("add", "-A", cwd=worktree)
    run_git("commit", "-m", "wip", cwd=worktree)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "unpushed" in result["message"]
    assert (worktree / "work.txt").exists()


def test_discard_force_overrides_the_refusal(harness):
    launch(harness)
    worktree = harness.worktree_root / "ABC-1"
    (worktree / "work.txt").write_text("hours of it\n")
    run_git("add", "-A", cwd=worktree)
    run_git("commit", "-m", "wip", cwd=worktree)

    result = apply_request(harness, "discard", "ABC-1", force=True)

    assert result["ok"] == 1
    assert not worktree.exists()


def test_discard_refuses_when_the_worktree_is_dirty(harness):
    launch(harness)
    (harness.worktree_root / "ABC-1" / "scratch.txt").write_text("uncommitted\n")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "uncommitted" in result["message"]


def test_a_request_for_an_unknown_issue_is_refused_not_ignored(harness):
    result = apply_request(harness, "cancel", "NOPE-1")

    assert result["ok"] == 0
    assert "no attempt recorded" in result["message"]


def test_an_unknown_request_kind_does_not_kill_the_tick(harness):
    harness.board.add("ABC-1")
    harness.ledger.record_request("frobnicate", "ABC-1")

    harness.next_tick()

    assert harness.attempt("ABC-1") is None or harness.attempt("ABC-1").state


def test_a_cancelled_runs_leftover_session_is_not_re_adopted(harness):
    """cancel keeps the worktree, so its stopped session must not look like an orphan."""
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")

    report = harness.next_tick()

    assert report.adopted == []
    assert harness.ledger.live_attempts() == []
    assert harness.attempt("ABC-1").state == ledger_mod.CANCELLED


def test_a_cancelled_run_whose_session_kept_working_is_supervised_again(harness):
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")
    harness.claude_state.set_session_state("ABC-1", "working")

    report = harness.next_tick()

    assert "ABC-1" in report.adopted
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_cancel_refuses_when_the_session_will_not_stop(harness):
    """An exit code is not proof: the listing has to show the session stopped."""
    launch(harness)
    harness.claude_state.set(stop_returncode=3)

    result = apply_request(harness, "cancel", "ABC-1")

    assert result["ok"] == 0
    assert "did not stop" in result["message"]
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_discard_refuses_to_pull_a_worktree_out_from_under_a_live_session(harness):
    launch(harness)
    harness.claude_state.set(stop_returncode=3)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "still in use" in result["message"]
    assert (harness.worktree_root / "ABC-1").is_dir()


def test_discard_force_overrides_a_session_that_will_not_stop(harness):
    launch(harness)
    harness.claude_state.set(stop_returncode=3)

    result = apply_request(harness, "discard", "ABC-1", force=True)

    assert result["ok"] == 1
    assert not (harness.worktree_root / "ABC-1").exists()


def test_discard_says_so_when_the_session_could_not_be_removed(harness):
    launch(harness)
    apply_request(harness, "cancel", "ABC-1")
    harness.claude_state.set(rm_returncode=4)

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 1
    assert "could not be removed" in result["message"]
    assert not (harness.worktree_root / "ABC-1").exists()


def test_retry_starts_a_new_session_instead_of_adopting_the_old_one(harness):
    """`stop` keeps the session, and it still carries this issue's name and worktree."""
    launch(harness)
    launches_before = len(harness.claude_state.launches)
    apply_request(harness, "cancel", "ABC-1")

    result = apply_request(harness, "retry", "ABC-1")

    assert result["ok"] == 1
    assert len(harness.claude_state.launches) == launches_before + 1
    attempt = harness.attempt("ABC-1")
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
    first = harness.attempt("ABC-1")
    apply_request(harness, "cancel", "ABC-1")
    # The old session is still listed, with this issue's name and working directory.
    assert any(s["id"] == first.session_id for s in harness.claude_state.sessions)

    apply_request(harness, "retry", "ABC-1")

    assert harness.attempt("ABC-1").session_id != first.session_id


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
    assert harness.attempt("ABC-1").state == ledger_mod.CANCELLED


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
    assert harness.attempt("ABC-1").state == ledger_mod.PAUSED


def test_an_unrecognised_session_state_is_not_taken_as_stopped(harness):
    """`unknown` means "go look" everywhere else; it must not authorise deleting a worktree."""
    launch(harness)
    # The stop reports success, but the listing still says something unrecognised.
    harness.claude_state.set(stop_state="reticulating-splines")

    result = apply_request(harness, "discard", "ABC-1")

    assert result["ok"] == 0
    assert "still in use" in result["message"]
    assert (harness.worktree_root / "ABC-1").is_dir()
