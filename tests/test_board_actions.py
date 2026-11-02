"""Board actions: every one records the request the command line records, with its origin.

The daemon serves on 127.0.0.1 on a port the kernel picks and runs no loop; the tests
tick the harness themselves. Sessions are the fake `claude`'s, the tracker is the fake one,
and attach runs a fake `osascript`.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import subprocess
import time

import pytest

from chargehand import board, ledger as ledger_mod, terminal
from chargehand.cli import EXIT_OK, main
from chargehand.config import NotifyConfig
from conftest import run_git
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}


@pytest.fixture
def daemon(harness, tmp_path, monkeypatch):
    # The loop does not run, so an action answers at once with its request pending.
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def post(daemon, path, body=None, *, headers=AUTH, host=None):
    raw = json.dumps(body if body is not None else {}).encode()
    status, payload = support.request(daemon.port, "POST", path, body=raw, headers=headers, host=host)
    return status, json.loads(payload or b"{}")


def run(argv, config_file):
    return main(["--config", str(config_file), *argv])


def board_worker(harness, item="ABC-1", **kinds):
    """Start a worker from the board on *item*, through a request and a tick."""
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer", item=item, **kinds
    )
    harness.next_tick()
    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 1, outcome["message"]
    return harness.ledger.get(int(outcome["message"].rsplit("#", 1)[1]))


def unqueued(harness, item="ABC-1"):
    """An issue the tracker knows that is not in the queue, so no start rule takes it."""
    return harness.board.add(item, labels=())


def row(harness, request_id):
    request = harness.ledger.get_request(request_id)
    return request["kind"], request["target"], request["args"], request["origin"]


# ----- the token and the Host check ------------------------------------------------------


ENDPOINTS = (
    ("/api/workers", {"repository": "test", "role": "implementer"}),
    ("/api/workers/1/pause", {}),
    ("/api/workers/1/cancel", {}),
    ("/api/workers/1/continue", {"force": False}),
    ("/api/workers/1/retry", {"force": False}),
    ("/api/workers/1/discard", {"force": False}),
    ("/api/workers/1/reply", {"text": "hello"}),
    ("/api/workers/1/attach", {"terminal": "iterm2"}),
    ("/api/repositories/test/pause", {}),
    ("/api/repositories/test/resume", {}),
    ("/api/pause", {}),
    ("/api/resume", {}),
)


@pytest.mark.parametrize("path, body", ENDPOINTS)
def test_every_action_needs_the_token_and_a_loopback_host(daemon, harness, path, body):
    assert post(daemon, path, body, headers={"Content-Type": "application/json"})[0] == 401
    wrong = {"Authorization": "Bearer not-the-token", "Content-Type": "application/json"}
    assert post(daemon, path, body, headers=wrong)[0] == 401
    assert post(daemon, path, body, host="evil.example:80")[0] == 403
    assert harness.ledger.pending_requests() == []


def test_the_page_sends_the_token_only_in_a_header(daemon):
    script = (board.asset_dir() / "board.js").read_text()
    assert "'Authorization': 'Bearer ' + token()" in script
    assert "localStorage" in script
    assert "?token" not in script and "token=" not in script
    page = (board.asset_dir() / "index.html").read_text()
    assert 'type="password"' in page
    # No chat: the only text a session gets from the board is a one-line reply.
    assert "<textarea" not in page and "textarea" not in script
    assert "maxLength = REPLY_MAX" in script


# ----- the same rows as the command line ------------------------------------------------


@pytest.mark.parametrize("cli_argv, path, body", [
    (["stop", "{handle}"], "/api/workers/{id}/pause", {}),
    (["cancel", "{handle}"], "/api/workers/{id}/cancel", {}),
    (["continue", "{handle}"], "/api/workers/{id}/continue", {}),
    (["continue", "{handle}", "--force"], "/api/workers/{id}/continue", {"force": True}),
    (["retry", "{handle}"], "/api/workers/{id}/retry", {"force": False}),
    (["retry", "{handle}", "--force"], "/api/workers/{id}/retry", {"force": True}),
    (["discard", "{handle}", "--yes"], "/api/workers/{id}/discard", {}),
    (["discard", "{handle}", "--yes", "--force"], "/api/workers/{id}/discard", {"force": True}),
    (["pause", "--repository", "test"], "/api/repositories/test/pause", {}),
    (["resume", "--repository", "test"], "/api/repositories/test/resume", {}),
    (["pause"], "/api/pause", {}),
    (["resume"], "/api/resume", {}),
])
def test_a_board_action_records_the_row_of_its_command(daemon, harness, config_file, cli_argv, path, body):
    worker = harness.ledger.create_worker(
        role="implementer", item="ABC-1", item_id="id-1", url=None, repository="test",
        repo=harness.repo, worktree=harness.worktree_root / "implementer" / "ABC-1-1",
        branch="chargehand/implementer/ABC-1-1", origin=ledger_mod.ORIGIN_BOARD,
    )
    values = {"handle": worker.handle, "id": worker.id}

    assert run([*(part.format(**values) for part in cli_argv), "--no-wait"], config_file) == EXIT_OK
    status, answer = post(daemon, path.format(**values), body)

    assert status == 202 and answer["pending"] is True
    cli_row, board_row = harness.ledger.pending_requests()
    assert board_row["id"] == answer["id"]
    assert (board_row["kind"], board_row["target"], board_row["args"]) == (
        cli_row["kind"], cli_row["target"], cli_row["args"]
    )
    assert (cli_row["origin"], board_row["origin"]) == ("cli", "board")


def test_a_source_workers_card_names_it_by_role_and_item(daemon, harness):
    harness.board.add("ABC-1")
    harness.tick()
    worker = harness.worker("ABC-1")

    status, answer = post(daemon, f"/api/workers/{worker.id}/pause")

    assert status == 202
    assert answer["target"] == "implementer/ABC-1"
    harness.next_tick()
    assert harness.worker("ABC-1").state == ledger_mod.PAUSED


def test_an_older_worker_of_the_same_role_and_item_is_named_by_its_id(daemon, harness):
    first = harness.ledger.create_worker(
        role="implementer", item="ABC-1", item_id="id-1", url=None, repository="test",
        repo=harness.repo, worktree=harness.worktree("ABC-1"), branch="chargehand/implementer/ABC-1",
    )
    first = harness.ledger.update(first, state=ledger_mod.CANCELLED)
    harness.ledger.create_worker(
        role="implementer", item="ABC-1", item_id="id-1", url=None, repository="test",
        repo=harness.repo, worktree=harness.worktree("ABC-1"), branch="chargehand/implementer/ABC-1",
    )

    assert board.target_of(first) == f"implementer/ABC-1#{first.id}"


def test_the_outcome_of_a_request_is_readable_without_the_token(daemon, harness):
    _, answer = post(daemon, "/api/pause")
    harness.next_tick()

    status, raw = support.request(daemon.port, "GET", f"/api/requests/{answer['id']}")

    outcome = json.loads(raw)
    assert status == 200
    assert (outcome["pending"], outcome["ok"], outcome["origin"]) == (False, True, "board")
    assert outcome["message"] == "all repositories paused"
    assert support.request(daemon.port, "GET", "/api/requests/999")[0] == 404


def test_an_action_answers_with_the_outcome_when_the_tick_applies_it_in_time(
    harness, tmp_path, monkeypatch
):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 20.0)
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600, poll_secs=0.05)
    with support.running(built):
        status, answer = post(built, "/api/pause")

    assert status == 200
    assert (answer["pending"], answer["ok"], answer["kind"]) == (False, True, "pause")
    assert harness.ledger.is_paused()


def test_a_bad_action_is_refused_before_it_is_recorded(daemon, harness):
    worker = unqueued(harness) and board_worker(harness)

    for path, body in (
        ("/api/workers/999/pause", {}),
        (f"/api/workers/{worker.id}/explode", {}),
        (f"/api/workers/{worker.id}/continue", {"force": "yes"}),
        (f"/api/workers/{worker.id}/reply", {}),
        (f"/api/workers/{worker.id}/reply", {"text": "   "}),
        (f"/api/workers/{worker.id}/reply", {"text": "x" * 5000}),
        (f"/api/workers/{worker.id}/attach", {"terminal": "xterm"}),
        ("/api/repositories/nope/pause", {}),
        ("/api/repositories/test/explode", {}),
        ("/api/workers", {"repository": "nope", "role": "implementer"}),
        ("/api/workers", {"repository": "test", "role": "nope"}),
        ("/api/workers", {"repository": "test", "role": "implementer", "item": "two words"}),
        ("/api/workers", {"repository": "test", "role": "implementer", "subscribe": ["comment"]}),
        ("/api/workers", {"repository": "test", "role": "implementer", "item": "A-1",
                          "subscribe": ["Not A Kind"]}),
    ):
        status, _ = post(daemon, path, body)
        assert status in (400, 404), (path, body, status)
    assert harness.ledger.pending_requests() == []


# ----- new worker ------------------------------------------------------------------------


def test_new_worker_starts_board_workers_on_one_item_within_the_caps(daemon, harness):
    unqueued(harness)
    harness.config = dataclasses.replace(harness.config, max_concurrent=3, max_open_workers=2)

    first = board_worker(harness, subscribe=["comment"])
    second = board_worker(harness)

    assert (first.origin, second.origin) == (ledger_mod.ORIGIN_BOARD, ledger_mod.ORIGIN_BOARD)
    assert (first.state, second.state) == (ledger_mod.RUNNING, ledger_mod.RUNNING)
    assert first.handle == f"implementer/ABC-1#{first.id}"
    assert first.worktree != second.worktree and first.branch != second.branch
    assert first.worktree.endswith(f"implementer/ABC-1-{first.id}")
    assert first.branch == f"chargehand/implementer/ABC-1-{first.id}"
    assert first.start_rule is None and first.owns_label == ledger_mod.LABEL_NONE
    # No label write: a board worker never drives the queue label.
    assert harness.labels("ABC-1") == ()
    assert [(s.item, s.kinds) for s in harness.ledger.subscriptions_of(first)] == [("ABC-1", ("comment",))]
    names = {s["name"] for s in harness.claude_state.sessions}
    assert {first.handle, second.handle} <= names

    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer", item="ABC-1"
    )
    harness.next_tick()
    refused = harness.ledger.get_request(request_id)
    assert refused["ok"] == 0
    assert "2/2 open workers" in refused["message"]


def test_new_worker_needs_no_item_and_refuses_one_the_tracker_does_not_know(daemon, harness):
    status, answer = post(daemon, "/api/workers", {"repository": "test", "role": "implementer"})
    assert status == 202
    assert row(harness, answer["id"]) == ("new_worker", "test", {"role": "implementer"}, "board")
    harness.next_tick()
    started = harness.ledger.get_request(answer["id"])
    assert started["ok"] == 1, started["message"]
    worker = harness.ledger.live_workers()[0]
    assert (worker.item, worker.handle) == (None, f"implementer/#{worker.id}")
    assert worker.worktree.endswith(f"implementer/worker-{worker.id}")

    _, missing = post(daemon, "/api/workers", {"repository": "test", "role": "implementer", "item": "NOPE-9"})
    harness.next_tick()
    assert harness.ledger.get_request(missing["id"])["ok"] == 0
    assert "has no item NOPE-9" in harness.ledger.get_request(missing["id"])["message"]


def test_new_worker_is_held_by_a_paused_repository(daemon, harness):
    unqueued(harness)
    harness.ledger.set_paused(True, "test")

    _, answer = post(daemon, "/api/workers", {"repository": "test", "role": "implementer", "item": "ABC-1"})
    harness.next_tick()

    assert harness.ledger.get_request(answer["id"])["message"] == "repository test is paused"
    assert harness.ledger.live_workers() == []


def test_a_comment_on_its_item_wakes_a_board_worker_that_follows_it(harness):
    unqueued(harness)
    worker = board_worker(harness, subscribe=["comment"])
    harness.claude_state.set_session_state(worker.session_id, "done")
    harness.next_tick()
    assert harness.ledger.get(worker.id).state == ledger_mod.IDLE

    harness.board.comment("ABC-1")
    harness.next_tick()

    assert harness.ledger.get(worker.id).wakes == 1


def test_a_source_start_is_not_held_by_a_board_workers_worktree(harness):
    unqueued(harness)
    worker = board_worker(harness)
    harness.ledger.update(worker, state=ledger_mod.CANCELLED)
    issue = harness.board.by_identifier("ABC-1")
    harness.board.issues[issue.id] = dataclasses.replace(issue, labels=("chargehand",))

    harness.next_tick()

    started = harness.ledger.live_worker_for("ABC-1")
    assert started is not None and started.origin == ledger_mod.ORIGIN_SOURCE


# ----- reply -----------------------------------------------------------------------------


def test_a_reply_enters_the_inbox_as_an_event_from_the_board(daemon, harness):
    unqueued(harness)
    worker = board_worker(harness)

    status, answer = post(daemon, f"/api/workers/{worker.id}/reply", {"text": "say \x1b[2JPONG\n now"})

    assert status == 202
    assert row(harness, answer["id"]) == ("reply", worker.handle, {"text": "say PONG now"}, "board")
    harness.next_tick()
    (entry,) = harness.ledger.inbox(worker)
    assert (entry.source, entry.kind, entry.pointer) == ("board", "reply", "say PONG now")
    outcome = harness.ledger.get_request(answer["id"])
    assert outcome["ok"] == 1
    assert "delivered when the current turn ends" in outcome["message"]


def test_a_reply_wakes_an_idle_worker_with_the_line_and_shows_on_its_card(daemon, harness):
    unqueued(harness)
    worker = board_worker(harness)
    harness.claude_state.set_session_state(worker.session_id, "done")
    harness.next_tick()
    assert harness.ledger.get(worker.id).state == ledger_mod.IDLE

    _, answer = post(daemon, f"/api/workers/{worker.id}/reply", {"text": "say PONG"})
    harness.next_tick()

    woken = harness.ledger.get(worker.id)
    assert (woken.state, woken.wakes) == (ledger_mod.RUNNING, 1)
    assert "delivery wakes the worker" in harness.ledger.get_request(answer["id"])["message"]
    (resume,) = harness.claude_state.read().get("resumes", [])
    assert "reply from the operator: say PONG" in resume["prompt"].splitlines()
    card = next(c for column in board.build(harness.config, harness.ledger, health={})["columns"]
                for c in column["cards"] if c["id"] == worker.id)
    assert "reply from the operator: say PONG" in card["last_prompt"]


@pytest.mark.parametrize("state", [ledger_mod.BLOCKED, ledger_mod.PAUSED, ledger_mod.PARKED])
def test_a_reply_to_a_worker_that_delivery_does_not_wake_waits_and_says_so(daemon, harness, state):
    unqueued(harness)
    worker = harness.ledger.update(board_worker(harness), state=state)

    _, answer = post(daemon, f"/api/workers/{worker.id}/reply", {"text": "are you there"})
    harness.next_tick()

    outcome = harness.ledger.get_request(answer["id"])
    assert outcome["ok"] == 1
    assert f"is {state}" in outcome["message"] and "kept in the inbox" in outcome["message"]
    assert [e.pointer for e in harness.ledger.inbox(worker)] == ["are you there"]
    assert harness.ledger.get(worker.id).wakes == 0


def test_a_source_named_like_the_board_replies_is_refused(harness):
    from chargehand.config import SourceConfig

    harness.config = dataclasses.replace(
        harness.config, sources=(SourceConfig(name="board", type="fake"),)
    )
    report = harness.next_tick()

    assert any("reserved for replies from the board" in error for error in report.errors)


# ----- discard keeps its guard ------------------------------------------------------------


def test_discard_from_the_board_is_refused_for_unpushed_commits_and_a_dirty_tree(daemon, harness):
    unqueued(harness)
    worker = board_worker(harness)
    worktree = harness.ledger.get(worker.id).worktree_path
    (worktree / "notes.txt").write_text("work\n")

    _, dirty = post(daemon, f"/api/workers/{worker.id}/discard")
    harness.next_tick()
    assert "uncommitted changes" in harness.ledger.get_request(dirty["id"])["message"]

    run_git("add", "notes.txt", cwd=worktree)
    run_git("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-m", "work", cwd=worktree)
    _, unpushed = post(daemon, f"/api/workers/{worker.id}/discard")
    harness.next_tick()
    refused = harness.ledger.get_request(unpushed["id"])
    assert refused["ok"] == 0 and "unpushed commit" in refused["message"]
    assert worktree.exists()


# ----- the handle on the command line -------------------------------------------------------


def test_the_cli_accepts_a_board_workers_handle(capsys, config_file, harness):
    unqueued(harness)
    first = board_worker(harness)
    board_worker(harness)
    harness.claude_state.set(logs="the first worker's output\n")

    assert run(["logs", first.handle], config_file) == EXIT_OK
    assert "the first worker's output" in capsys.readouterr().out

    assert run(["stop", first.handle], config_file) == EXIT_OK
    assert harness.ledger.get(first.id).state == ledger_mod.PAUSED
    assert run(["continue", first.handle], config_file) == EXIT_OK
    assert harness.ledger.get(first.id).state == ledger_mod.RUNNING
    assert run(["cancel", first.handle], config_file) == EXIT_OK
    assert harness.ledger.get(first.id).state == ledger_mod.CANCELLED
    assert run(["retry", first.handle, "--force"], config_file) == EXIT_OK
    assert run(["discard", f"#{first.id}", "--yes", "--force"], config_file) == EXIT_OK
    assert not harness.ledger.get(first.id).worktree_path.exists()
    # Every request names the first worker; the second one is untouched, and the retry
    # started a third on the same item, from the board again.
    second, third = [w for w in harness.ledger.live_workers() if w.id != first.id]
    assert second.state == ledger_mod.RUNNING and second.wakes == 0
    assert ["stop", second.session_id] not in harness.claude_state.commands
    assert (third.origin, third.item, third.state) == (ledger_mod.ORIGIN_BOARD, "ABC-1", ledger_mod.RUNNING)


def test_the_cli_tells_a_start_rule_worker_and_a_board_worker_on_one_item_apart(
    capsys, config_file, harness
):
    harness.board.add("ABC-1")
    harness.tick()
    source = harness.worker("ABC-1")
    board_one = board_worker(harness)
    harness.claude_state.set(logs="some output\n")

    run(["status", "--json", "--no-queue"], config_file)
    handles = {w["id"]: w["handle"] for w in json.loads(capsys.readouterr().out)["workers"]}
    assert handles == {source.id: "implementer/ABC-1", board_one.id: board_one.handle}

    assert run(["logs", "implementer/ABC-1"], config_file) == EXIT_OK
    assert run(["stop", "implementer/ABC-1"], config_file) == EXIT_OK
    assert harness.ledger.get(source.id).state == ledger_mod.PAUSED
    assert harness.ledger.get(board_one.id).state == ledger_mod.RUNNING
    assert run(["continue", "ABC-1"], config_file) == EXIT_OK
    assert harness.ledger.get(source.id).state == ledger_mod.RUNNING
    assert run(["stop", board_one.handle], config_file) == EXIT_OK
    assert harness.ledger.get(board_one.id).state == ledger_mod.PAUSED
    assert harness.ledger.get(source.id).state == ledger_mod.RUNNING


def test_discard_with_the_handle_status_shows_for_a_finished_worker_leaves_a_live_twin_alone(
    capsys, config_file, harness
):
    harness.board.add("ABC-1")
    harness.tick()
    assert run(["cancel", "ABC-1"], config_file) == EXIT_OK
    source = harness.worker("ABC-1")
    assert source.state == ledger_mod.CANCELLED
    live = board_worker(harness)
    capsys.readouterr()

    run(["status", "--json", "--no-queue"], config_file)
    status = json.loads(capsys.readouterr().out)
    (shown,) = [w["handle"] for w in status["recent"] if w["id"] == source.id]
    assert shown == source.id_handle

    for name in (shown, "implementer/ABC-1"):
        assert harness.ledger.find_worker(name)[0].id == source.id
    assert run(["discard", shown, "--yes"], config_file) == EXIT_OK

    assert harness.ledger.get(source.id).gc_done == 1
    assert not harness.ledger.get(source.id).worktree_path.exists()
    after = harness.ledger.get(live.id)
    assert (after.state, after.gc_done) == (ledger_mod.RUNNING, 0)
    assert after.worktree_path.exists()
    assert ["stop", live.session_id] not in harness.claude_state.commands


def test_a_bare_item_with_several_board_workers_asks_for_a_handle(capsys, config_file, harness):
    unqueued(harness)
    first, second = board_worker(harness), board_worker(harness)

    run(["stop", "ABC-1"], config_file)

    err = capsys.readouterr().err
    assert first.handle in err and second.handle in err


def test_status_json_shows_the_id_origin_and_handle_and_where_requests_came_from(
    capsys, config_file, harness
):
    unqueued(harness)
    worker = board_worker(harness)
    harness.ledger.record_request("stop", worker.handle, origin=ledger_mod.REQUEST_BOARD)

    run(["status", "--json", "--no-queue"], config_file)

    status = json.loads(capsys.readouterr().out)
    (shown,) = status["workers"]
    assert (shown["id"], shown["origin"], shown["handle"]) == (worker.id, "board", worker.handle)
    assert shown["branch"] == worker.branch
    (pending,) = status["runner"]["pending_requests"]
    assert (pending["kind"], pending["target"], pending["origin"]) == ("stop", worker.handle, "board")


def test_the_tick_report_says_where_a_request_came_from(harness):
    harness.ledger.record_request("pause", origin=ledger_mod.REQUEST_BOARD)

    report = harness.next_tick()

    assert report.requests == ["pause from board: ok — all repositories paused"]


# ----- attach ---------------------------------------------------------------------------------


class FakeOsascript:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


@pytest.fixture
def osascript(monkeypatch):
    fake = FakeOsascript()
    monkeypatch.setattr(
        terminal, "open_window",
        functools.partial(terminal.open_window, run=fake, platform="darwin"),
    )
    return fake


@pytest.mark.parametrize("name, application, line", [
    ("iterm2", 'tell application "iTerm"', "tell current session of attached to write text (item 1 of argv)"),
    ("terminal", 'tell application "Terminal"', "do script (item 1 of argv)"),
])
def test_attach_opens_the_chosen_terminal_on_the_cards_session(daemon, harness, osascript, name, application, line):
    unqueued(harness)
    worker = board_worker(harness)

    status, answer = post(daemon, f"/api/workers/{worker.id}/attach", {"terminal": name})

    assert status == 200
    assert (answer["ok"], answer["pending"], answer["kind"], answer["origin"]) == (True, False, "attach", "board")
    (argv,) = osascript.calls
    assert argv[0] == terminal.OSASCRIPT
    assert argv[-1] == f"claude attach {worker.session_id}"
    script = [argv[i + 1] for i in range(1, len(argv) - 1, 2)]
    assert application in script and line in script
    assert all(argv[i] == "-e" for i in range(1, len(argv) - 1, 2))
    # Recorded for the audit trail, already applied: no tick acts on it.
    assert harness.ledger.pending_requests() == []
    assert row(harness, answer["id"]) == ("attach", worker.handle, {"terminal": name}, "board")


def test_attach_is_refused_without_a_live_session_and_off_macos(daemon, harness, osascript, monkeypatch):
    unqueued(harness)
    worker = board_worker(harness)
    ended = harness.ledger.update(board_worker(harness), state=ledger_mod.CANCELLED)

    assert post(daemon, f"/api/workers/{ended.id}/attach")[0] == 409
    monkeypatch.setattr(
        terminal, "open_window",
        functools.partial(terminal.open_window.func, run=osascript, platform="linux"),
    )
    _, answer = post(daemon, f"/api/workers/{worker.id}/attach")

    assert answer["ok"] is False and "macOS only" in answer["message"]
    assert osascript.calls == []


def test_osascript_never_carries_the_command_in_its_script():
    argv = terminal.osascript_argv("iterm2", "claude attach abc-123")
    assert argv[-1] == "claude attach abc-123"
    assert not any("abc-123" in part for part in argv[:-1])
    for bad in ('claude attach x"; do shell script "rm', "claude attach $(id)", "claude  attach x", ""):
        with pytest.raises(ValueError):
            terminal.osascript_argv("iterm2", bad)
    with pytest.raises(ValueError):
        terminal.osascript_argv("xterm", "claude attach abc")


def test_a_failed_osascript_says_how_to_allow_it():
    fake = FakeOsascript(returncode=1, stderr="Not authorized to send Apple events to iTerm.\x1b[2J")

    ok, message = terminal.open_window("iterm2", "claude attach abc", run=fake, platform="darwin")

    assert ok is False
    assert "Not authorized" in message and "Automation" in message and "\x1b" not in message


# ----- the card ----------------------------------------------------------------------------


def test_a_bare_item_does_not_reach_a_board_worker_after_its_start_rule_worker_ended(
    capsys, config_file, harness
):
    harness.board.add("ABC-1")
    harness.tick()
    assert run(["cancel", "ABC-1"], config_file) == EXIT_OK
    assert "cancelled implementer/ABC-1#" in capsys.readouterr().out
    source = harness.worker("ABC-1")
    live = board_worker(harness)

    run(["discard", "ABC-1", "--yes"], config_file)
    err = capsys.readouterr().err

    assert source.id_handle in err and live.id_handle in err
    assert harness.ledger.get(live.id).worktree_path.exists()
    assert harness.ledger.get(source.id).gc_done == 0

    assert run(["discard", source.id_handle, "--yes"], config_file) == EXIT_OK
    assert f"discarded {source.id_handle}: session removed" in capsys.readouterr().out
    assert harness.ledger.get(live.id).state == ledger_mod.RUNNING


def test_a_refusal_about_a_finished_worker_lists_the_live_workers_on_its_item(
    capsys, config_file, harness
):
    harness.board.add("ABC-1")
    harness.tick()
    run(["cancel", "ABC-1"], config_file)
    live = board_worker(harness)
    capsys.readouterr()

    run(["stop", "ABC-1", "--role", "implementer"], config_file)

    err = capsys.readouterr().err
    assert "is already cancelled" in err and live.id_handle in err


def test_an_unknown_item_is_shown_as_typed(daemon, harness):
    _, answer = post(daemon, "/api/workers",
                     {"repository": "test", "role": "implementer", "item": "local/scratch#999"})
    harness.next_tick()

    message = harness.ledger.get_request(answer["id"])["message"]
    assert message.endswith("has no item local/scratch#999")
    assert "�" not in message


@pytest.mark.parametrize("length, status", [(500, 202), (501, 400), (2000, 400), (5000, 400)])
def test_a_reply_over_the_limit_is_refused_not_cut(daemon, harness, length, status):
    unqueued(harness)
    worker = board_worker(harness)

    code, answer = post(daemon, f"/api/workers/{worker.id}/reply", {"text": "x" * length})

    assert code == status
    if status == 400:
        assert answer["error"] == "a reply is one line of at most 500 characters"
        assert harness.ledger.pending_requests() == []


def test_twin_cards_on_one_item_show_their_handles(harness):
    unqueued(harness)
    first, second = board_worker(harness), board_worker(harness)

    page = board.build(harness.config, harness.ledger, health={})

    shown = {c["id"]: c["handle"] for column in page["columns"] for c in column["cards"]}
    assert shown == {first.id: first.target, second.id: second.target}
    assert first.target != second.target
    # The page renders the handle in the card's title, as text.
    script = (board.asset_dir() / "board.js").read_text()
    assert "title.append(el('span', 'handle', c.handle))" in script


def test_a_card_offers_the_actions_its_state_can_take(harness):
    unqueued(harness)
    worker = board_worker(harness)

    def card_actions(state, **changes):
        updated = harness.ledger.update(worker, state=state, **changes)
        return board.actions(updated)

    assert card_actions(ledger_mod.RUNNING) == ["pause", "cancel", "reply", "attach", "discard"]
    assert card_actions(ledger_mod.PAUSED) == ["continue", "cancel", "reply", "attach", "discard"]
    assert card_actions(ledger_mod.BLOCKED)[0] == "continue"
    assert card_actions(ledger_mod.CANCELLED) == ["retry", "discard"]
    assert card_actions(ledger_mod.CANCELLED, gc_done=1) == ["retry"]

    page = board.build(harness.config, harness.ledger, health={})
    assert page["repositories"] == [{
        "name": "test", "roles": ["implementer"], "paused": False, "display_name": None,
        "icon": None, "icon_glyph": None, "pinned": False, "collapsed": False, "hidden": False,
    }]
    assert page["terminals"] == ["iterm2", "terminal"]
    card = next(c for column in page["columns"] for c in column["cards"])
    assert (card["handle"], card["origin"]) == (worker.handle, "board")


# ----- supervision of workers started by hand ------------------------------------------------


def item_less_worker(harness):
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer"
    )
    harness.next_tick()
    assert harness.ledger.get_request(request_id)["ok"] == 1
    (worker,) = harness.ledger.live_workers()
    return worker


def test_an_item_less_worker_is_notified_through_a_notify_command(harness, tmp_path):
    seen = tmp_path / "notified.txt"
    hook = tmp_path / "notify.sh"
    hook.write_text(f'#!/bin/sh\necho "$CHARGEHAND_ITEM $CHARGEHAND_STATE" >> "{seen}"\n')
    hook.chmod(0o755)
    harness.notifier.config = NotifyConfig(command=str(hook))
    worker = item_less_worker(harness)

    harness.ledger.update(worker, working_since=time.time() - 7 * 3600)
    long_run = harness.next_tick()
    harness.claude_state.set_session_state(worker.session_id, "blocked")
    blocked = harness.next_tick()
    harness.ledger.update(harness.ledger.get(worker.id), state=ledger_mod.LAUNCHING, step=0,
                          launch_attempts=harness.config.max_launch_attempts, session_id=None)
    harness.claude_state.drop_sessions()
    failed = harness.next_tick()

    for report in (long_run, blocked, failed):
        assert not [e for e in report.errors if "phase" in e], report.errors
    assert seen.read_text().splitlines() == [
        f"{worker.handle} long-running", f"{worker.handle} blocked", f"{worker.handle} failed",
    ]
    assert harness.ledger.get(worker.id).state == ledger_mod.FAILED


def test_a_finished_worker_is_named_by_its_id_even_as_the_newest_of_its_item(harness):
    harness.board.add("ABC-1")
    harness.tick()
    worker = harness.ledger.update(harness.worker("ABC-1"), state=ledger_mod.CANCELLED)

    assert board.target_of(worker) == f"implementer/ABC-1#{worker.id}"


def test_a_working_session_of_a_finished_board_worker_is_reported_not_adopted(harness):
    unqueued(harness)
    worker = board_worker(harness)
    harness.ledger.update(worker, state=ledger_mod.CANCELLED)

    first = harness.next_tick()
    second = harness.next_tick()

    assert harness.ledger.live_workers() == []
    assert any(f"of {worker.handle}, which is cancelled, is still working" in w for w in first.warnings)
    assert not any(worker.handle in w for w in second.warnings)


def test_a_start_event_of_a_kind_a_board_worker_did_not_choose_starts_a_worker(harness):
    from chargehand import router
    from chargehand.roles import StartRule
    from chargehand.sources import Event, WorkItem

    unqueued(harness)
    worker = board_worker(harness, subscribe=["comment"])
    item = WorkItem("fake", "ABC-1", "id-ABC-1")
    rule = StartRule(on="review_requested")
    review = Event(source="fake", kind="review_requested", item=item, actor="a",
                   occurred_at=time.time() + 60, dedupe_id="r1", pointer="ABC-1")

    assert not router.suitable_worker_exists(harness.ledger, rule, "implementer", item, "review_requested")
    assert router.suitable_worker_exists(harness.ledger, rule, "implementer", item, "comment")
    assert router.deliver_to_holders(harness.ledger, "implementer", review) == (0, 0)
    assert harness.ledger.inbox(worker) == []


def test_holds_for_counts_a_board_worker_only_for_its_kinds(harness):
    from chargehand import router

    unqueued(harness)
    worker = board_worker(harness, subscribe=["comment"])
    (subscription,) = harness.ledger.subscriptions_of(worker)

    assert router.holds_for(subscription, worker, "comment")
    assert not router.holds_for(subscription, worker, "review_requested")
    assert router.holds_for(subscription, worker, None)


def test_an_item_less_worker_of_a_role_that_checks_out_its_item_is_refused(harness):
    roles = harness.repo / ".chargehand" / "roles"
    roles.mkdir(parents=True)
    (roles / "implementer.toml").write_text(
        'prompt = "review {item}"\ncheckout = "item"\npermission_mode = "auto"\n'
    )
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer"
    )
    harness.next_tick()

    refused = harness.ledger.get_request(request_id)
    assert refused["ok"] == 0 and "needs a work item" in refused["message"]
    assert harness.ledger.live_workers() == []


def test_a_launch_that_stops_part_way_answers_created_and_resumes(harness, monkeypatch):
    from chargehand.errors import GitError
    from chargehand.gitutil import Git

    calls = []
    real_fetch = Git.fetch

    def fetch_once_failing(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise GitError("fetch failed")
        return real_fetch(self, *args, **kwargs)

    monkeypatch.setattr(Git, "fetch", fetch_once_failing)
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer"
    )
    harness.next_tick()

    answer = harness.ledger.get_request(request_id)
    (worker,) = harness.ledger.live_workers()
    assert answer["ok"] == 1
    assert answer["message"].startswith(f"created {worker.handle}; its launch did not complete")
    harness.next_tick()
    assert harness.ledger.get(worker.id).state == ledger_mod.RUNNING
