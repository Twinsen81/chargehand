"""Archives: a zip of what a finished worker leaves, written by the daemon's job thread.

The tick records the job and removes the worktree and the session once the zip is in
place; the job runner writes the zip. These tests drive both by hand, so each step can be
interrupted, except the last ones, which run a daemon with its loop and its job thread.
Sessions are the fake `claude`'s and the tracker is the fake one.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import zipfile
from pathlib import Path

import pytest

from chargehand import archive, board, claude as claude_mod, ledger as ledger_mod, paths
from conftest import run_git
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}


def launch(harness, identifier="ABC-1"):
    harness.board.add(identifier)
    harness.tick()
    return harness.worker(identifier)


def apply_request(harness, kind, target=None, **args):
    request_id = harness.ledger.record_request(kind, target, **args)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


def cancelled(harness, identifier="ABC-1"):
    worker = launch(harness, identifier)
    assert apply_request(harness, "cancel", worker.target)["ok"] == 1
    return harness.ledger.get(worker.id)


def jobs(harness, worker):
    return harness.ledger.jobs_of(ledger_mod.JOB_ARCHIVE, str(worker.id))


def job_of(harness, worker):
    (job,) = jobs(harness, worker)
    return job


def runner(harness, **kwargs):
    return archive.JobRunner(harness.ledger.path, **kwargs)


def transcript_file(tmp_path) -> Path:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "w" / "session.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {"type": "user", "timestamp": "2026-11-03T10:00:00Z",
         "message": {"role": "user", "content": "fix the bug"}},
        {"type": "assistant", "timestamp": "2026-11-03T10:00:05Z", "message": {
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "Looking at it."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "README.md"}]}]}},
        {"type": "summary", "summary": "not a message"},
        "not json",
    ]
    path.write_text("\n".join(json.dumps(line) if isinstance(line, dict) else line for line in lines) + "\n")
    return path


# ----- the zip -------------------------------------------------------------------------------


def test_an_archive_holds_the_tree_the_rows_and_the_transcript(harness, tmp_path):
    worker = cancelled(harness)
    worktree = worker.worktree_path
    (worktree / "notes").mkdir()
    (worktree / "notes" / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))
    (worktree / "empty").mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("not the worker's\n")
    (worktree / "link").symlink_to(secret)
    path = transcript_file(tmp_path)
    worker = harness.ledger.update(worker, transcript_path=str(path))

    # The new files are not committed, so only a forced archive takes them.
    assert apply_request(harness, "archive", worker.target, force=True)["ok"] == 1
    runtime = harness.runner.runtime(worker.runtime)
    runner(harness, transcript=lambda ledger, w: runtime.transcript(w.transcript_path, w.session_id)
           ).run_queued(harness.ledger)

    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_WRITTEN
    dest = Path(job.result_path)
    assert dest.parent == paths.state_dir() / "archive" / "test"
    assert dest.name.startswith("implementer-ABC-1-") and dest.name.endswith(f"-{worker.id}.zip")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
        assert {"worker.json", "sessions.json", "transcript.txt", "worktree/README.md",
                "worktree/notes/big.bin", "worktree/empty/", "worktree/link"} <= names
        assert zf.read("worktree/notes/big.bin") == (worktree / "notes" / "big.bin").read_bytes()
        # A link is kept as a link, so the file it points at stays out of the archive.
        link = zf.getinfo("worktree/link")
        assert stat.S_ISLNK(link.external_attr >> 16)
        assert zf.read("worktree/link").decode() == str(secret)
        row = json.loads(zf.read("worker.json"))
        assert (row["id"], row["state"], row["worktree"]) == (worker.id, "cancelled", worker.worktree)
        sessions = json.loads(zf.read("sessions.json"))
        assert sessions and sessions[0]["worker_id"] == worker.id
        text = zf.read("transcript.txt").decode()
        assert "user:\nfix the bug" in text and "[tool Bash]" in text and "README.md" in text
        assert "hidden" not in text and "not a message" not in text
        assert "handoff.md" not in names
    assert not archive.part_path(dest).exists()
    assert worktree.is_dir()


def test_a_handoff_note_is_a_member_only_when_there_is_one(harness):
    worker = cancelled(harness)
    with_note = archive.records(worker, [], transcript=None, handoff="next: the tests")
    assert with_note["handoff.md"] == "next: the tests"
    assert set(archive.records(worker, [], transcript=None)) == {"worker.json", "sessions.json"}


def test_the_file_names_of_two_archives_of_one_day_differ(harness):
    worker = cancelled(harness)
    other = harness.ledger.create_worker(
        role="implementer", item=None, item_id=None, url=None, repository="test",
        repo=harness.repo, worktree=harness.worktree_root / "x", branch="b",
        origin=ledger_mod.ORIGIN_BOARD,
    )
    first, second = archive.archive_path(worker, 0), archive.archive_path(other, 0)
    assert first.parent == second.parent and first != second
    # A worker without an item has no item part.
    assert second.name.startswith("implementer-19") and second.name.endswith(f"-{other.id}.zip")


def test_a_transcript_is_read_only_from_the_session_files_of_claude_code(tmp_path):
    inside = transcript_file(tmp_path)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(inside.read_text())
    link = inside.parent / "link.jsonl"
    link.symlink_to(outside)

    assert claude_mod.transcript_file(str(inside)) == inside.resolve()
    for value in (str(outside), str(link), "relative.jsonl", str(inside.with_suffix(".txt")), None):
        assert claude_mod.transcript_file(value) is None


def test_two_archives_of_one_worker_on_one_day_get_two_files(harness):
    worker = cancelled(harness)
    assert archive.archive_path(worker, 1000.0) != archive.archive_path(worker, 1001.0)


def test_an_unreadable_transcript_is_logged_without_control_characters(
    harness, tmp_path, caplog, monkeypatch
):
    path = transcript_file(tmp_path)

    def unreadable(path):
        # The error text can carry the path from the hook payload.
        raise OSError(f"cannot read {path}\x1b]0;title\x07\x1b[31m")

    monkeypatch.setattr(claude_mod, "transcript_text", unreadable)
    runtime = harness.runner.runtime("claude")
    with caplog.at_level("WARNING", logger="chargehand.claude"):
        runtime.transcript(str(path), None)

    (record,) = [r for r in caplog.records if "transcript" in r.getMessage()]
    assert "\x1b" not in record.getMessage() and "\x07" not in record.getMessage()
    assert "cannot read" in record.getMessage()


def test_an_unexpected_error_fails_the_job_instead_of_leaving_it_running(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)

    def broken(ledger, worker):
        raise KeyError("transcript")

    jobs_runner = runner(harness, transcript=broken)
    assert jobs_runner.run_queued(harness.ledger) == 1

    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_FAILED and "KeyError" in job.error
    assert not Path(job.result_path).exists()
    assert not archive.part_path(Path(job.result_path)).exists()
    assert jobs_runner.run_queued(harness.ledger) == 0


# ----- interruptions -------------------------------------------------------------------------


def test_a_stop_during_the_zip_leaves_no_archive_and_the_job_runs_again(harness, monkeypatch):
    worker = cancelled(harness)
    (worker.worktree_path / "data.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    apply_request(harness, "archive", worker.target, force=True)
    jobs_runner = runner(harness)
    original = archive._copy_file

    def stop_after_one_file(*args):
        original(*args)
        jobs_runner.stopping.set()

    monkeypatch.setattr(archive, "_copy_file", stop_after_one_file)
    jobs_runner.run_job(harness.ledger, job_of(harness, worker))

    job = job_of(harness, worker)
    dest = Path(job.result_path)
    assert job.state == ledger_mod.JOB_QUEUED and "starts again" in job.progress
    assert not dest.exists() and not archive.part_path(dest).exists()
    harness.next_tick()
    assert worker.worktree_path.is_dir() and harness.ledger.get(worker.id).archived_at is None

    monkeypatch.setattr(archive, "_copy_file", original)
    runner(harness).run_queued(harness.ledger)
    assert job_of(harness, worker).state == ledger_mod.JOB_WRITTEN
    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None and "worktree/data.bin" in zf.namelist()


def test_a_crash_during_the_zip_is_recovered_at_the_next_start(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)
    job = job_of(harness, worker)
    dest = Path(job.result_path)
    # What a kill -9 during the zip leaves: a running job and half a file.
    harness.ledger.update_job(job, state=ledger_mod.JOB_RUNNING)
    dest.parent.mkdir(parents=True)
    archive.part_path(dest).write_bytes(b"PK\x03\x04 half")
    harness.next_tick()
    assert worker.worktree_path.is_dir() and not dest.exists()

    restarted = runner(harness)
    restarted.recover()
    assert not archive.part_path(dest).exists()
    assert job_of(harness, worker).state == ledger_mod.JOB_QUEUED
    restarted.run_queued(harness.ledger)
    assert job_of(harness, worker).state == ledger_mod.JOB_WRITTEN
    assert dest.is_file()


def test_a_crash_after_the_rename_writes_the_archive_again(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)
    runner(harness).run_queued(harness.ledger)
    job = job_of(harness, worker)
    # The rename landed, the row did not.
    harness.ledger.update_job(job, state=ledger_mod.JOB_RUNNING)

    restarted = runner(harness)
    restarted.recover()
    restarted.run_queued(harness.ledger)

    assert job_of(harness, worker).state == ledger_mod.JOB_WRITTEN
    with zipfile.ZipFile(job.result_path) as zf:
        assert zf.testzip() is None


def test_the_tick_removes_the_worktree_and_the_session_once_the_archive_is_in_place(harness):
    worker = cancelled(harness)
    session_id = worker.session_id
    apply_request(harness, "archive", worker.target)
    harness.next_tick()
    assert worker.worktree_path.is_dir(), "nothing is removed before the zip"

    runner(harness).run_queued(harness.ledger)
    report = harness.next_tick()

    done = harness.ledger.get(worker.id)
    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_DONE and job.finished_at
    assert not worker.worktree_path.exists()
    assert not harness.runner.git.branch_exists(harness.repo, worker.branch)
    assert ["rm", session_id] in harness.claude_state.commands
    assert (done.gc_done, done.session_id, done.archived_at is not None) == (1, None, True)
    assert worker.handle in report.collected
    assert Path(job.result_path).is_file()
    assert apply_request(harness, "archive", worker.target)["ok"] == 0


def test_an_archive_file_that_is_gone_before_the_removal_is_written_again(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)
    runner(harness).run_queued(harness.ledger)
    Path(job_of(harness, worker).result_path).unlink()

    harness.next_tick()

    assert job_of(harness, worker).state == ledger_mod.JOB_QUEUED
    assert worker.worktree_path.is_dir()


# ----- the guard -----------------------------------------------------------------------------


def commit(worktree: Path) -> None:
    (worktree / "work.txt").write_text("hours of it\n")
    run_git("add", "-A", cwd=worktree)
    run_git("commit", "-m", "wip", cwd=worktree)


def test_an_archive_refuses_unpushed_commits_and_a_dirty_tree_unless_forced(harness):
    worker = cancelled(harness)
    commit(worker.worktree_path)

    refused = apply_request(harness, "archive", worker.target)
    assert refused["ok"] == 0 and "unpushed commit" in refused["message"]
    assert "force" in refused["message"]

    run_git("push", "-u", "origin", "HEAD", cwd=worker.worktree_path)
    (worker.worktree_path / "scratch.txt").write_text("uncommitted\n")
    refused = apply_request(harness, "archive", worker.target)
    assert refused["ok"] == 0 and "uncommitted" in refused["message"]
    assert jobs(harness, worker) == []

    assert apply_request(harness, "archive", worker.target, force=True)["ok"] == 1
    runner(harness).run_queued(harness.ledger)
    harness.next_tick()
    assert job_of(harness, worker).state == ledger_mod.JOB_DONE
    assert not worker.worktree_path.exists()
    with zipfile.ZipFile(job_of(harness, worker).result_path) as zf:
        assert "worktree/scratch.txt" in zf.namelist()


def test_work_that_appears_during_the_zip_keeps_the_worktree(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)
    runner(harness).run_queued(harness.ledger)
    commit(worker.worktree_path)

    report = harness.next_tick()

    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_FAILED
    assert "unpushed" in job.error and job.result_path in job.error
    assert worker.worktree_path.is_dir() and Path(job.result_path).is_file()
    assert harness.ledger.get(worker.id).archived_at is None
    assert any("archived, but not removed" in line for line in report.warnings)


def test_discard_retry_and_garbage_collection_wait_for_an_archive(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)

    for kind in ("discard", "retry"):
        refused = apply_request(harness, kind, worker.target, force=True)
        assert refused["ok"] == 0 and "archive job" in refused["message"], kind
    harness.board.close("ABC-1")
    harness.next_tick()
    assert worker.worktree_path.is_dir()
    assert harness.ledger.get(worker.id).gc_done == 0
    assert board.actions(harness.ledger.get(worker.id), job_of(harness, worker)) == []


# ----- a live worker -------------------------------------------------------------------------


def test_a_live_worker_is_cancelled_first_through_a_cancel_request(harness):
    worker = launch(harness)
    request_id = harness.ledger.record_request(
        "archive", worker.target, origin=ledger_mod.REQUEST_BOARD
    )
    harness.next_tick()

    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 1 and "cancel requested" in outcome["message"]
    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_WAITING
    cancel = harness.ledger.get_request(job.args["cancel_request"])
    assert (cancel["kind"], cancel["target"], cancel["origin"]) == (
        "cancel", worker.id_handle, "board"
    )
    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING

    harness.next_tick()

    assert harness.ledger.get(worker.id).state == ledger_mod.CANCELLED
    assert job_of(harness, worker).state == ledger_mod.JOB_QUEUED
    assert ["stop", worker.session_id] in harness.claude_state.commands


def test_an_archive_fails_when_the_cancel_is_refused(harness):
    worker = launch(harness)
    harness.claude_state.set(stop_state="working")
    apply_request(harness, "archive", worker.target)

    harness.next_tick()

    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_FAILED and "was not cancelled" in job.error
    assert worker.worktree_path.is_dir()


def test_work_committed_before_the_cancel_applies_fails_the_archive_before_the_zip(harness):
    worker = launch(harness)
    apply_request(harness, "archive", worker.target)
    assert job_of(harness, worker).state == ledger_mod.JOB_WAITING
    commit(worker.worktree_path)

    report = harness.next_tick()

    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_FAILED and "unpushed" in job.error
    assert not Path(job.result_path).exists()
    assert worker.worktree_path.is_dir()
    assert f"{worker.handle}: not archived: its worktree holds 1 unpushed commit(s)" in report.warnings


def test_a_forced_archive_of_a_live_worker_does_not_check_again(harness):
    worker = launch(harness)
    apply_request(harness, "archive", worker.target, force=True)
    commit(worker.worktree_path)

    harness.next_tick()

    assert job_of(harness, worker).state == ledger_mod.JOB_QUEUED


# ----- a repository --------------------------------------------------------------------------


def test_a_repository_archive_refuses_live_workers_unless_forced_and_hides_it(harness):
    finished = cancelled(harness, "ABC-1")
    live = launch(harness, "ABC-2")

    refused = apply_request(harness, "archive_repository", "test")
    assert refused["ok"] == 0 and live.id_handle in refused["message"]
    assert harness.ledger.board_state() == [] and jobs(harness, finished) == []

    outcome = apply_request(harness, "archive_repository", "test", force=True)
    assert outcome["ok"] == 1 and "2 archive job(s)" in outcome["message"]
    hidden = board.BoardState(harness.ledger.board_state()).shown("repository", "test")["hidden"]
    assert hidden is True
    assert job_of(harness, finished).state == ledger_mod.JOB_QUEUED
    assert job_of(harness, live).state == ledger_mod.JOB_WAITING

    harness.next_tick()
    runner(harness).run_queued(harness.ledger)
    harness.next_tick()
    for worker in (finished, live):
        assert job_of(harness, worker).state == ledger_mod.JOB_DONE
        assert harness.ledger.get(worker.id).archived_at is not None
    zips = sorted(Path(job_of(harness, w).result_path) for w in (finished, live))

    shown = apply_request(harness, "unarchive_repository", "test")
    assert shown["ok"] == 1
    assert harness.ledger.board_state() == []
    assert all(path.is_file() for path in zips)


def test_a_repository_archive_refuses_work_that_is_not_pushed(harness):
    worker = cancelled(harness)
    commit(worker.worktree_path)

    refused = apply_request(harness, "archive_repository", "test")

    assert refused["ok"] == 0 and "unpushed" in refused["message"]
    assert harness.ledger.board_state() == []
    assert apply_request(harness, "archive_repository", "nope")["ok"] == 0


# ----- the board -----------------------------------------------------------------------------


@pytest.fixture
def daemon(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def post(daemon, path, body=None, headers=AUTH):
    raw = json.dumps(body if body is not None else {}).encode()
    status, payload = support.request(daemon.port, "POST", path, body=raw, headers=headers)
    return status, json.loads(payload or b"{}")


def test_the_archive_actions_record_their_requests(daemon, harness):
    worker = cancelled(harness)
    for path, body, row in (
        (f"/api/workers/{worker.id}/archive", {"force": True},
         ("archive", worker.target, {"force": True})),
        ("/api/repositories/test/archive", {}, ("archive_repository", "test", {"force": False})),
        ("/api/repositories/test/unarchive", {}, ("unarchive_repository", "test", {})),
    ):
        assert post(daemon, path, body, headers={"Content-Type": "application/json"})[0] == 401
        status, answer = post(daemon, path, body)
        assert status == 202, path
        recorded = harness.ledger.get_request(answer["id"])
        assert (recorded["kind"], recorded["target"], recorded["args"], recorded["origin"]) == (
            *row, "board"
        )
    assert post(daemon, f"/api/workers/{worker.id}/archive", {"force": "yes"})[0] == 400


def test_the_board_shows_a_job_on_the_card_and_in_the_top_bar(harness):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)

    page = board.build(harness.config, harness.ledger, health={})
    (card,) = [c for column in page["columns"] for c in column["cards"] if c["id"] == worker.id]
    (top,) = page["jobs"]
    assert top["handle"] == worker.target and top["state"] == "queued" and top["active"]
    assert card["job"]["id"] == top["id"] and card["actions"] == []
    assert top["result_path"].endswith(".zip")

    runner(harness).run_queued(harness.ledger)
    harness.next_tick()
    page = board.build(harness.config, harness.ledger, health={})
    (card,) = [c for column in page["columns"] for c in column["cards"] if c["id"] == worker.id]
    assert card["archived_at"] is not None and card["job"]["state"] == "done"
    assert page["jobs"][0]["state"] == "done" and not page["jobs"][0]["active"]


def test_the_top_bar_shows_running_and_failed_jobs_and_counts_the_others(harness):
    worker = cancelled(harness)
    make = harness.ledger.create_job
    running = harness.ledger.update_job(make(ledger_mod.JOB_ARCHIVE, str(worker.id)),
                                        state=ledger_mod.JOB_RUNNING, progress="zipping: 40%")
    failed = harness.ledger.update_job(make(ledger_mod.JOB_ARCHIVE, str(worker.id)),
                                       state=ledger_mod.JOB_FAILED, error="disk full")
    for state in (ledger_mod.JOB_QUEUED, ledger_mod.JOB_QUEUED, ledger_mod.JOB_WAITING):
        make(ledger_mod.JOB_ARCHIVE, str(worker.id), state=state)
    for _ in range(3):
        harness.ledger.update_job(make(ledger_mod.JOB_ARCHIVE, str(worker.id)),
                                  state=ledger_mod.JOB_DONE)

    summary = board.build(harness.config, harness.ledger, health={})["job_summary"]

    assert [(entry["id"], entry["state"]) for entry in summary["shown"]] == [
        (running.id, "running"), (failed.id, "failed")
    ]
    assert summary["counts"] == {"waiting": 1, "queued": 2, "written": 0, "done": 3}


def test_the_page_offers_archive_and_shows_the_jobs():
    script = (board.asset_dir() / "board.js").read_text()
    page = (board.asset_dir() / "index.html").read_text()
    assert "FORCED = new Set(['continue', 'retry', 'discard', 'archive'])" in script
    assert "renderJobs(model.job_summary)" in script and 'id="jobs"' in page
    assert "'/unarchive'" in script or "'unarchive'" in script


# ----- the daemon ----------------------------------------------------------------------------


def test_the_daemon_archives_a_worker_on_its_job_thread_and_the_board_stays_responsive(
    harness, tmp_path, monkeypatch
):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    worker = cancelled(harness)
    (worker.worktree_path / "data.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    # The copy of the file waits until the board has answered, so the board is asked while
    # the job runs.
    copying, answered = threading.Event(), threading.Event()
    original = archive._copy_file

    def held_copy(*args):
        copying.set()
        answered.wait(10)
        original(*args)

    monkeypatch.setattr(archive, "_copy_file", held_copy)
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600, poll_secs=0.05)
    with support.running(built):
        status, _ = post(built, f"/api/workers/{worker.id}/archive", {"force": True})
        assert status in (200, 202)
        assert copying.wait(10)
        started = time.monotonic()
        status, raw = support.request(built.port, "GET", "/api/board")
        took = time.monotonic() - started
        answered.set()
        assert status == 200 and took < 1.0
        assert json.loads(raw)["jobs"][0]["state"] == ledger_mod.JOB_RUNNING
        done = support.wait_for(
            lambda: (jobs(harness, worker) or [None])[0] is not None
            and job_of(harness, worker).state == ledger_mod.JOB_DONE,
            timeout=30,
        )
    assert done, job_of(harness, worker)
    assert not worker.worktree_path.exists()
    assert harness.ledger.get(worker.id).archived_at is not None


def test_a_stop_of_the_daemon_puts_the_running_job_back_in_the_queue(harness, tmp_path, monkeypatch):
    worker = cancelled(harness)
    apply_request(harness, "archive", worker.target)
    jobs_runner = runner(harness)
    started = []

    def slow_copy(zf, entry, check, advance):
        started.append(entry.name)
        jobs_runner.stopping.wait(10)
        check()

    monkeypatch.setattr(archive, "_copy_file", slow_copy)
    jobs_runner.start()
    assert support.wait_for(lambda: started)
    jobs_runner.close()

    job = job_of(harness, worker)
    assert job.state == ledger_mod.JOB_QUEUED
    assert not Path(job.result_path).exists()
    assert not archive.part_path(Path(job.result_path)).exists()
    assert worker.worktree_path.is_dir()
