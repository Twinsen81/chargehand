"""Fault injection: interrupt a launch after every step and assert the next tick
adopts, resumes, or fails loudly. Never a stranded label, never an unsupervised session.
"""

from __future__ import annotations

import pytest

from chargehand import ledger as ledger_mod
from chargehand.errors import LaunchAborted
from chargehand.ledger import LAST_STEP


@pytest.mark.parametrize("step", [0, 1, 2, 3])
def test_a_launch_interrupted_after_any_step_completes_on_the_next_tick(harness, step):
    harness.board.add("ABC-1")

    with pytest.raises(LaunchAborted) as aborted:
        harness.tick(crash_after_step=step)
    assert aborted.value.step == step

    interrupted = harness.attempt("ABC-1")
    assert interrupted.state == ledger_mod.LAUNCHING
    assert interrupted.step == step

    report = harness.next_tick()

    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.RUNNING, report.errors
    assert attempt.step == LAST_STEP
    assert attempt.session_id
    assert harness.labels("ABC-1") == ("chargehand-running",)
    assert len(harness.claude_state.launches) == 1


def test_an_interrupted_launch_never_leaves_the_issue_stranded_as_queued(harness):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=1)

    # The label moved on at step 1; reconciliation must never put it back.
    assert harness.labels("ABC-1") == ("chargehand-running",)
    harness.next_tick()
    assert harness.labels("ABC-1") == ("chargehand-running",)


def test_a_session_started_before_the_crash_is_adopted_not_duplicated(harness):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=3)

    # The tick died between starting the session and recording its ids.
    worktree = harness.worktree_root / "ABC-1"
    state = harness.claude_state.read()
    state["sessions"] = [
        {"id": "pre-existing", "uuid": "u1", "name": "ABC-1", "cwd": str(worktree),
         "state": "working"}
    ]
    harness.claude_state.write(state)

    report = harness.next_tick()

    assert "ABC-1" in report.adopted
    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.RUNNING
    assert attempt.session_id == "pre-existing"
    assert harness.claude_state.launches == []


def test_a_launch_that_never_completes_fails_loudly_after_the_attempt_limit(harness):
    harness.board.add("ABC-1")
    harness.claude_state.set(launch_fails=True)

    for _ in range(harness.config.max_launch_attempts + 1):
        harness.next_tick()

    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.FAILED
    assert "did not complete after" in attempt.last_error
    assert harness.labels("ABC-1") == ("chargehand-blocked",)
    assert ("ABC-1", "failed") in harness.notifications()


def test_a_tracker_write_that_landed_but_reported_failure_is_reconciled(harness):
    issue = harness.board.add("ABC-1")
    harness.board.land_then_report_failure = True

    first = harness.tick()

    # The write landed; the tick was told it failed, so the row stayed at step 0.
    assert any("502" in error for error in first.errors)
    assert harness.attempt("ABC-1").step == 0
    assert harness.labels("ABC-1") == ("chargehand-running",)

    harness.next_tick()

    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.RUNNING
    assert attempt.step == LAST_STEP
    assert harness.labels("ABC-1") == ("chargehand-running",)
    assert issue.identifier == "ABC-1"


def test_a_write_that_reported_success_without_landing_is_caught_by_the_re_read(harness):
    harness.board.add("ABC-1")
    harness.board.silently_drop_next_write = True

    report = harness.tick()

    assert any("did not land" in error for error in report.errors)
    assert harness.attempt("ABC-1").step == 0
    assert harness.labels("ABC-1") == ("chargehand",)

    harness.next_tick()

    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING
    assert harness.labels("ABC-1") == ("chargehand-running",)


def test_a_running_label_with_no_live_attempt_is_marked_blocked(harness):
    harness.board.add("ABC-9", labels=("chargehand-running",))

    report = harness.next_tick()

    assert harness.labels("ABC-9") == ("chargehand-blocked",)
    assert ("ABC-9", "blocked") in harness.notifications()
    assert any("no live attempt" in warning for warning in report.warnings)


def test_a_session_under_the_worktree_root_with_no_ledger_row_is_adopted(harness):
    issue = harness.board.add("ABC-5", labels=("chargehand-running",))
    worktree = harness.worktree_root / "ABC-5"
    harness.runner.git.add_worktree(harness.repo, worktree, "chargehand/ABC-5", "origin/main")
    harness.claude_state.set(
        sessions=[{"id": "orphan", "uuid": "u9", "name": "ABC-5", "cwd": str(worktree),
                   "state": "working"}]
    )

    report = harness.next_tick()

    attempt = harness.attempt("ABC-5")
    assert attempt is not None
    assert attempt.state == ledger_mod.RUNNING
    assert attempt.session_id == "orphan"
    assert attempt.issue_id == issue.id
    assert any("unsupervised" in warning for warning in report.warnings)


def test_a_session_with_no_derivable_issue_is_reported_not_ignored(harness):
    stray = harness.worktree_root / "not-an-issue"
    stray.mkdir()
    harness.claude_state.set(
        sessions=[{"id": "mystery", "cwd": str(stray), "state": "working"}]
    )

    report = harness.next_tick()

    assert any("no derivable issue" in warning for warning in report.warnings)
    assert ("?", "orphan-session") in harness.notifications()


def test_a_session_outside_the_worktree_root_is_left_alone(harness):
    harness.claude_state.set(
        sessions=[{"id": "mine", "cwd": "/somewhere/else", "state": "working"}]
    )

    report = harness.next_tick()

    assert report.warnings == []


def test_the_tick_gives_up_cleanly_when_the_session_listing_is_unreadable(harness):
    harness.board.add("ABC-1")
    harness.claude_state.set(agents_fail=True)

    report = harness.next_tick()

    assert report.errors and "claude" in report.errors[0]
    assert report.launched == []
    assert harness.attempt("ABC-1") is None


def test_a_tracker_outage_during_admission_does_not_touch_other_phases(harness):
    harness.board.add("ABC-1")
    harness.tick()
    harness.board.fail_next_list = "tracker is down"

    report = harness.next_tick()

    assert any("tracker is down" in error for error in report.errors)
    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_a_tracker_outage_during_a_resume_does_not_burn_a_launch_retry(harness, monkeypatch):
    """Three unlucky ticks must not fail a launch that never actually went wrong."""
    from chargehand.errors import TrackerError
    from support import fake_tracker

    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=0)
    assert harness.attempt("ABC-1").launch_attempts == 1

    # monkeypatch.undo() would also undo the fixtures' own patches, so the outage is a
    # flag the stand-in reads instead.
    down = {"value": True}
    healthy = fake_tracker.FakeTracker.get

    def flaky(self, issue_id):
        if down["value"]:
            raise TrackerError("tracker is down")
        return healthy(self, issue_id)

    monkeypatch.setattr(fake_tracker.FakeTracker, "get", flaky)
    for _ in range(4):
        harness.next_tick()

    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.LAUNCHING
    assert attempt.launch_attempts == 1

    down["value"] = False
    harness.next_tick()

    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_an_unusable_credential_still_lets_the_labels_phase_report(harness, monkeypatch):
    from chargehand.errors import TrackerAuthError
    from support import fake_tracker

    launch_report = harness.tick()
    harness.board.add("ABC-1")
    harness.next_tick()
    monkeypatch.setattr(
        fake_tracker.FakeTracker,
        "get",
        lambda self, issue_id: (_ for _ in ()).throw(TrackerAuthError("linear: no API key")),
    )
    harness.claude_state.set_session_state("ABC-1", "blocked")

    report = harness.next_tick()

    assert any("no API key" in error for error in report.errors)
    assert not any("phase" in error for error in report.errors)
    assert harness.attempt("ABC-1").state == ledger_mod.BLOCKED


def test_reconciliation_never_adopts_a_previous_attempts_session(harness):
    """A stopped session keeps this issue's name and worktree; it is not this attempt's."""
    harness.board.add("ABC-1")
    harness.tick()
    first = harness.attempt("ABC-1")
    harness.ledger.record_request("cancel", "ABC-1")
    harness.next_tick()
    # `cancel` keeps the worktree and the session. The operator tidies the worktree up
    # by hand, so admission lets the re-armed issue through while the session remains.
    harness.runner.git.remove_worktree(harness.repo, harness.worktree_root / "ABC-1", force=True)
    assert harness.attempt("ABC-1").session_id == first.session_id

    issue = harness.board.by_identifier("ABC-1")
    harness.board.issues[issue.id] = issue.__class__(
        **{**issue.__dict__, "labels": ("chargehand",)}
    )
    harness.fresh_runner()
    with pytest.raises(LaunchAborted):
        harness.runner.tick(crash_after_step=2)

    harness.next_tick()

    second = harness.attempt("ABC-1")
    assert second.id != first.id
    assert second.session_id != first.session_id
    assert second.state == ledger_mod.RUNNING


# ----- failures inside a step on the resume path -----------------------------
# Crashing *between* steps only proves the ledger is resumable. These inject a real
# failure inside a step while resuming, which is where a bad config or a flaky command
# actually shows up.


def test_a_step_that_keeps_failing_on_resume_fails_the_attempt_loudly(harness, monkeypatch):
    from chargehand.errors import GitError
    from chargehand.gitutil import Git

    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=1)
    monkeypatch.setattr(
        Git, "fetch",
        lambda self, repo, remote="origin": (_ for _ in ()).throw(GitError("no route to host")),
    )

    for _ in range(harness.config.max_launch_attempts + 1):
        harness.next_tick()

    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.FAILED
    assert harness.labels("ABC-1") == ("chargehand-blocked",)
    assert ("ABC-1", "failed") in harness.notifications()


def test_a_step_that_recovers_on_resume_completes_the_launch(harness, monkeypatch):
    from chargehand.errors import GitError
    from chargehand.gitutil import Git

    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=1)
    down = {"value": True}
    healthy = Git.fetch

    def flaky(self, repo, remote="origin"):
        if down["value"]:
            raise GitError("no route to host")
        return healthy(self, repo, remote)

    monkeypatch.setattr(Git, "fetch", flaky)
    harness.next_tick()
    assert harness.attempt("ABC-1").state == ledger_mod.LAUNCHING

    down["value"] = False
    harness.next_tick()

    assert harness.attempt("ABC-1").state == ledger_mod.RUNNING


def test_a_setup_failure_on_the_resume_path_is_terminal_not_retried(harness, repo):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=2)
    (repo / "setup.sh").write_text("#!/bin/sh\nexit 9\n")
    (repo / "setup.sh").chmod(0o755)
    (repo / ".chargehand.toml").write_text(
        'base = "origin/main"\nprompt = "{issue}"\nsetup = "setup.sh {worktree}"\n'
    )

    harness.next_tick()

    attempt = harness.attempt("ABC-1")
    assert attempt.state == ledger_mod.FAILED
    assert "exited 9" in attempt.last_error
    assert attempt.launch_attempts == 2


def test_a_launch_that_keeps_being_refused_on_resume_fails_loudly(harness):
    harness.board.add("ABC-1")
    with pytest.raises(LaunchAborted):
        harness.tick(crash_after_step=3)
    harness.claude_state.set(launch_fails=True)

    for _ in range(harness.config.max_launch_attempts + 1):
        harness.next_tick()

    assert harness.attempt("ABC-1").state == ledger_mod.FAILED


def test_an_unadoptable_orphan_is_reported_once_not_every_tick(harness):
    """A three-minute poll would otherwise relay this notification ~480 times a day."""
    stray = harness.worktree_root / "not-an-issue"
    stray.mkdir()
    harness.claude_state.set(sessions=[{"id": "mystery", "cwd": str(stray), "state": "working"}])

    for _ in range(4):
        report = harness.next_tick()

    assert harness.notifications().count(("?", "orphan-session")) == 1
    # It stays visible in the tick's own output every time.
    assert any("no derivable issue" in warning for warning in report.warnings)
