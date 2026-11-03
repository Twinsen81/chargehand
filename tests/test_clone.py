"""Clone: a new worker made from an existing one, from the board and from the command line.

A clone works in a worktree of its own, branched from its original's current branch. In
fresh mode its session starts new; in full mode it starts as a copy of the original's
conversation. Sessions are the fake `claude`'s or the fake runtime's, the tracker is the
fake one, and the daemon serves on a port the kernel picks without running its loop.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from pathlib import Path

import pytest

from chargehand import board, claude as claude_mod, ledger as ledger_mod
from chargehand import usage as usage_mod
from chargehand.cli import EXIT_ERROR, EXIT_OK, build_parser, build_status, main
from chargehand.config import ServeConfig
from chargehand.errors import LaunchAborted
from chargehand.runtime import Capabilities
from chargehand.tick import Runner
from conftest import run_git
from support import daemon as support
from support import fake_runtime

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}
ROLE = (
    'prompt = "work on {item} in {worktree}"\npermission_mode = "manual"\n'
    'model = "haiku"\neffort = "low"\n'
)
FAKE_ROLE = 'runtime = "fake"\nprompt = "work on {item} in {worktree}"\nsandbox = true\n'


@pytest.fixture
def daemon(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(board, "REQUEST_WAIT_SECS", 0.0)
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def write_role(harness, text=ROLE):
    path = harness.repo / ".chargehand" / "roles" / "implementer.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def post(daemon, path, body=None, *, headers=AUTH):
    raw = json.dumps(body if body is not None else {}).encode()
    status, payload = support.request(daemon.port, "POST", path, body=raw, headers=headers)
    return status, json.loads(payload or b"{}")


def get(daemon, path):
    status, payload = support.request(daemon.port, "GET", path)
    return status, json.loads(payload or b"{}")


def run(argv, config_file):
    return main(["--config", str(config_file), *argv])


def idle(harness, worker):
    """Let *worker*'s turn end, on the fake binary or the fake runtime."""
    if worker.runtime == "fake":
        fake_runtime.world().set_session_state(worker.session_id, "done")
    else:
        harness.claude_state.set_session_state(worker.session_id, "done")
    harness.next_tick()
    worker = harness.ledger.get(worker.id)
    assert worker.state == ledger_mod.IDLE
    return worker


def original_worker(harness, *, subscribe=("comment",), role=ROLE, item="ABC-1"):
    """An idle worker started from the board on *item*, with one commit of its own."""
    write_role(harness, role)
    harness.board.add(item, labels=())
    args = {"subscribe": list(subscribe)} if subscribe else {}
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer", item=item, **args
    )
    harness.next_tick()
    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 1, outcome["message"]
    worker = harness.ledger.get(int(outcome["message"].rsplit("#", 1)[1]))
    (worker.worktree_path / "work.txt").write_text("the original's work\n")
    run_git("add", "work.txt", cwd=worker.worktree_path)
    run_git("commit", "-m", "work", cwd=worker.worktree_path)
    return idle(harness, worker)


def request_clone(harness, original, *, origin=ledger_mod.REQUEST_BOARD, **args):
    args.setdefault("mode", "fresh")
    request_id = harness.ledger.record_request("clone", original.target, origin=origin, **args)
    harness.next_tick()
    return harness.ledger.get_request(request_id)


def started(harness, outcome):
    assert outcome["ok"] == 1, outcome["message"]
    return harness.ledger.get(int(re.search(r"#(\d+)", outcome["message"]).group(1)))


def clone_of(harness, original, **args):
    return started(harness, request_clone(harness, original, **args))


def fork_calls(harness, runtime="claude"):
    if runtime == "fake":
        return fake_runtime.world().forks
    return harness.claude_state.read().get("forks", [])


# ----- fresh and full, from the board and from the command line ---------------------------


@pytest.mark.parametrize("mode", ["fresh", "full"])
def test_the_board_and_the_command_line_record_the_same_clone_row(
    daemon, harness, config_file, mode
):
    original = original_worker(harness)
    body = {"mode": mode, "prompt": "check {branch}\n\x1b[2Jtwice", "model": "sonnet",
            "effort": "high", "subscribe": ["comment", "comment"]}

    status, answer = post(daemon, f"/api/workers/{original.id}/clone", body)
    assert run([
        "clone", original.target, "--mode", mode, "--prompt", "check {branch}\n\x1b[2Jtwice",
        "--model", "sonnet", "--effort", "high", "--subscribe", "comment", "--subscribe",
        "comment", "--no-wait",
    ], config_file) == EXIT_OK

    assert status == 202 and answer["pending"] is True
    board_row, cli_row = harness.ledger.pending_requests()
    assert (board_row["kind"], board_row["target"]) == ("clone", original.target)
    assert board_row["args"] == {
        "mode": mode, "prompt": "check {branch}\ntwice", "model": "sonnet", "effort": "high",
        "subscribe": ["comment"],
    }
    assert (cli_row["kind"], cli_row["target"], cli_row["args"]) == (
        board_row["kind"], board_row["target"], board_row["args"]
    )
    assert (board_row["origin"], cli_row["origin"]) == ("board", "cli")


@pytest.mark.parametrize("origin", [ledger_mod.REQUEST_BOARD, ledger_mod.REQUEST_CLI])
@pytest.mark.parametrize("runtime", ["claude", "fake"])
def test_a_fresh_clone_launches_a_new_session_in_its_own_worktree(harness, origin, runtime):
    original = original_worker(harness, role=ROLE if runtime == "claude" else FAKE_ROLE)

    clone = clone_of(harness, original, origin=origin, mode="fresh")

    assert (clone.origin, clone.parent_id, clone.clone_mode) == ("clone", original.id, "fresh")
    assert (clone.role, clone.item, clone.state) == ("implementer", "ABC-1", ledger_mod.RUNNING)
    assert clone.handle == f"implementer/ABC-1#{clone.id}"
    assert clone.worktree.endswith(f"implementer/ABC-1-{clone.id}")
    assert clone.branch == f"chargehand/implementer/ABC-1-{clone.id}"
    assert fork_calls(harness, runtime) == []
    if runtime == "claude":
        launch = harness.claude_state.launches[-1]
        assert launch["cwd"] == os.path.realpath(clone.worktree)
        assert launch["argv"][-1] == f"work on ABC-1 in {clone.worktree}"
    else:
        launch = fake_runtime.world().launches[-1]
        assert launch["prompt"] == f"work on ABC-1 in {clone.worktree}"
    assert [s.kind for s in harness.ledger.sessions_of(clone)] == ["initial"]


@pytest.mark.parametrize("runtime", ["claude", "fake"])
def test_a_full_clone_forks_the_original_session_into_its_own_worktree(harness, runtime):
    original = original_worker(harness, role=ROLE if runtime == "claude" else FAKE_ROLE)

    clone = clone_of(harness, original, mode="full", prompt="go on in {worktree}", model="sonnet")

    (fork,) = fork_calls(harness, runtime)
    note = (
        f"This session continues a copy of another worker's conversation. From now on you work "
        f"in {clone.worktree} on branch {clone.branch}. The worktree {original.worktree} belongs "
        f"to the other worker: do not change anything there.\n\ngo on in {clone.worktree}"
    )
    if runtime == "claude":
        assert fork["target"] == original.session_uuid
        assert fork["cwd"] == os.path.realpath(clone.worktree)
        assert fork["prompt"] == note
        session = next(s for s in harness.claude_state.sessions if s["id"] == clone.session_id)
        assert (session["name"], session["cwd"]) == (clone.handle, os.path.realpath(clone.worktree))
    else:
        assert (fork["session_id"], fork["name"], fork["prompt"]) == (original.session_id, clone.handle, note)
        assert fork["worktree"] == clone.worktree and fork["model"] == "sonnet"
    assert clone.session_id not in (None, original.session_id)
    assert [s.kind for s in harness.ledger.sessions_of(clone)] == ["fork"]
    # The original keeps its own session, untouched.
    assert harness.ledger.get(original.id).session_id == original.session_id


def test_the_full_fork_argv_names_the_clone_and_passes_the_role_options_again(harness, tmp_path):
    token_file = tmp_path / "hooks.token"
    token_file.write_text("the-daemon-token\n")
    token_file.chmod(0o600)
    harness.config = dataclasses.replace(harness.config, serve=ServeConfig(port=4567, token_file=token_file))
    original = original_worker(harness)

    clone = clone_of(harness, original, mode="full", model="sonnet")

    (fork,) = fork_calls(harness)
    settings = claude_mod.settings_file_for(clone.worktree_path)
    argv = fork["argv"]
    assert argv[:12] == [
        "--resume", original.session_uuid, "--fork-session", "--bg", "-n", clone.handle,
        "--permission-mode", "manual", "--model", "sonnet", "--effort", "low",
    ]
    assert argv[12:14] == ["--settings", str(settings)]
    assert settings != claude_mod.settings_file_for(original.worktree_path)
    written = json.loads(settings.read_text())
    assert "Stop" in written["hooks"] and "the-daemon-token" not in " ".join(argv)
    # The file tools are denied in the original's worktree, in every permission mode.
    deny = written["permissions"]["deny"]
    for path in {str(original.worktree_path), os.path.realpath(original.worktree)}:
        assert f"Edit(/{path}/**)" in deny and f"Write(/{path}/**)" in deny
    assert not any(clone.worktree in rule for rule in deny)
    # A resume reads piped text into the prompt; the copy still runs its SessionStart hook.
    assert fork["stdin"] == "devnull"
    assert fork["cwd"] == os.path.realpath(clone.worktree)


def test_a_clone_starts_from_the_originals_current_branch_with_its_unpushed_commit(harness):
    original = original_worker(harness)
    run_git("branch", "-m", "feature/renamed-by-the-session", cwd=original.worktree_path)
    (original.worktree_path / "draft.txt").write_text("not committed\n")

    outcome = request_clone(harness, original, mode="fresh")

    clone = started(harness, outcome)
    assert clone.branch != "feature/renamed-by-the-session"
    assert (clone.worktree_path / "work.txt").read_text() == "the original's work\n"
    assert not (clone.worktree_path / "draft.txt").exists()
    head = run_git("rev-parse", "HEAD", cwd=clone.worktree_path).strip()
    assert head == run_git("rev-parse", "feature/renamed-by-the-session", cwd=harness.repo).strip()
    assert run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone.worktree_path).strip() == clone.branch
    assert "uncommitted changes in the worktree of" in outcome["message"]
    assert "are not in the clone" in outcome["message"]


# ----- refusals -------------------------------------------------------------------------------


def no_fork(monkeypatch, harness):
    monkeypatch.setattr(
        claude_mod.ClaudeRuntime, "capabilities",
        dataclasses.replace(claude_mod.ClaudeRuntime.capabilities, fork=False),
    )


def no_session(monkeypatch, harness):
    harness.ledger.update(harness.ledger.live_workers()[0], session_uuid=None)


def running(monkeypatch, harness):
    harness.ledger.update(harness.ledger.live_workers()[0], state=ledger_mod.RUNNING)


def blocked(monkeypatch, harness):
    harness.ledger.update(harness.ledger.live_workers()[0], state=ledger_mod.BLOCKED)


def branch_gone(monkeypatch, harness):
    worker = harness.ledger.live_workers()[0]
    run_git("worktree", "remove", "--force", worker.worktree, cwd=harness.repo)
    run_git("branch", "-D", worker.branch, cwd=harness.repo)


def paused(monkeypatch, harness):
    harness.ledger.set_paused(True, "test")


def at_the_cap(monkeypatch, harness):
    harness.config = dataclasses.replace(harness.config, max_open_workers=1)


def over_the_reserve(monkeypatch, harness):
    now = time.time()
    usage_mod.write_usage(
        harness.config.serve.usage_file,
        {"seven_day": {"used_percent": 75, "resets_at": now + 3600}}, now=now,
    )


@pytest.mark.parametrize("mode, setup, message", [
    ("full", no_fork, "runtime claude cannot copy a session"),
    ("full", no_session, "has no session to copy"),
    ("full", running, "is running; a copy would take a turn in progress"),
    ("full", blocked, "is blocked; a copy would take a turn in progress or an open prompt"),
    ("fresh", branch_gone, "its branch is gone"),
    ("fresh", paused, "repository test is paused"),
    ("fresh", at_the_cap, "1/1 open workers"),
    ("fresh", over_the_reserve, "held by the usage reserve (claude seven_day at 75%, above 70%)"),
])
def test_a_refused_clone_says_why_and_leaves_no_worker_and_no_worktree(
    harness, monkeypatch, mode, setup, message
):
    original = original_worker(harness)
    setup(monkeypatch, harness)
    before = sorted(p.name for p in (harness.worktree_root / "implementer").iterdir())

    outcome = request_clone(harness, harness.ledger.get(original.id), mode=mode)

    assert outcome["ok"] == 0
    assert message in outcome["message"]
    assert [w.id for w in harness.ledger.recent_workers(10)] == [original.id]
    assert sorted(p.name for p in (harness.worktree_root / "implementer").iterdir()) == before
    assert fork_calls(harness) == []


def test_a_clone_of_a_worker_without_an_item_follows_nothing(harness):
    write_role(harness)
    request_id = harness.ledger.record_request(
        "new_worker", "test", origin=ledger_mod.REQUEST_BOARD, role="implementer"
    )
    harness.next_tick()
    (original,) = harness.ledger.live_workers()

    refused = request_clone(harness, original, subscribe=["comment"])
    clone = clone_of(harness, original)

    assert refused["ok"] == 0 and "has no item" in refused["message"]
    assert harness.ledger.get_request(request_id)["ok"] == 1
    assert (clone.item, clone.handle) == (None, f"implementer/#{clone.id}")
    assert clone.worktree.endswith(f"implementer/worker-{clone.id}")


# ----- subscriptions -------------------------------------------------------------------------


def test_a_clone_follows_nothing_by_default_and_a_reply_reaches_only_it(daemon, harness):
    original = original_worker(harness)
    clone = idle(harness, clone_of(harness, original, mode="full"))

    assert [(s.item, s.kinds) for s in harness.ledger.subscriptions_of(clone)] == [("ABC-1", ())]
    harness.board.comment("ABC-1")
    harness.next_tick()
    assert (harness.ledger.get(original.id).wakes, harness.ledger.get(clone.id).wakes) == (1, 0)
    assert harness.ledger.inbox(clone) == []

    harness.claude_state.set_session_state(original.session_id, "done")
    harness.next_tick()
    _, answer = post(daemon, f"/api/workers/{clone.id}/reply", {"text": "say PONG"})
    harness.next_tick()

    assert harness.ledger.get_request(answer["id"])["ok"] == 1
    assert (harness.ledger.get(original.id).wakes, harness.ledger.get(clone.id).wakes) == (1, 1)
    resumed = [r["target"] for r in harness.claude_state.read()["resumes"]]
    assert resumed[-1] == clone.session_uuid


def test_a_clone_follows_the_kinds_it_was_given(harness):
    original = original_worker(harness)
    clone = idle(harness, clone_of(harness, original, subscribe=["comment"]))

    harness.board.comment("ABC-1")
    harness.next_tick()

    assert (harness.ledger.get(original.id).wakes, harness.ledger.get(clone.id).wakes) == (1, 1)


# ----- an interrupted launch, and retry -------------------------------------------------------


@pytest.mark.parametrize("step", [0, 1, 2, 3])
def test_an_interrupted_clone_launch_resumes_with_its_own_values_and_forks_once(
    harness, monkeypatch, step
):
    original = original_worker(harness)
    crashed = []

    def crash_once(at, crash_after_step):
        if at == step and not crashed:
            crashed.append(at)
            raise LaunchAborted(at)

    monkeypatch.setattr(Runner, "_check_crash", staticmethod(crash_once))
    outcome = request_clone(harness, original, mode="full", prompt="go on", model="sonnet", effort="high")
    assert outcome["ok"] == 0 and f"launch aborted after step {step}" in outcome["message"]
    (clone,) = [w for w in harness.ledger.live_workers() if w.id != original.id]
    assert (clone.state, clone.step) == (ledger_mod.LAUNCHING, step)

    harness.next_tick()

    clone = harness.ledger.get(clone.id)
    assert clone.state == ledger_mod.RUNNING
    (fork,) = fork_calls(harness)
    assert fork["argv"][fork["argv"].index("--model") + 1] == "sonnet"
    assert fork["argv"][fork["argv"].index("--effort") + 1] == "high"
    assert fork["prompt"].endswith("\n\ngo on")


def test_a_fork_whose_ids_were_not_recorded_is_adopted_not_forked_again(harness, monkeypatch):
    original = original_worker(harness)
    real_fork = claude_mod.ClaudeRuntime.fork

    def fork_then_die(self, spec, **kwargs):
        real_fork(self, spec, **kwargs)
        raise LaunchAborted(4)

    monkeypatch.setattr(claude_mod.ClaudeRuntime, "fork", fork_then_die)
    request_clone(harness, original, mode="full")
    monkeypatch.setattr(claude_mod.ClaudeRuntime, "fork", real_fork)

    harness.next_tick()

    (clone,) = [w for w in harness.ledger.live_workers() if w.id != original.id]
    assert clone.state == ledger_mod.RUNNING and clone.session_id
    assert len(fork_calls(harness)) == 1


def test_a_retry_of_a_clone_starts_a_new_clone_of_the_same_original(harness):
    original = original_worker(harness, subscribe=())
    clone = clone_of(harness, original, mode="full", prompt="again", model="sonnet",
                     subscribe=["comment"])
    harness.ledger.record_request("cancel", clone.target)
    harness.next_tick()
    clone = harness.ledger.get(clone.id)
    assert clone.state == ledger_mod.CANCELLED

    request_id = harness.ledger.record_request("retry", clone.target, force=True)
    harness.next_tick()

    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 1, outcome["message"]
    assert outcome["message"].startswith("cleared; started implementer/ABC-1#")
    again = started(harness, {"ok": 1, "message": outcome["message"]})
    assert again.id != clone.id
    assert (again.parent_id, again.clone_mode, again.prompt, again.model) == (
        original.id, "full", "again", "sonnet"
    )
    assert [s.kinds for s in harness.ledger.subscriptions_of(again)] == [("comment",)]
    assert len(fork_calls(harness)) == 2
    assert harness.ledger.get(clone.id).gc_done == 1


def test_a_retry_of_a_clone_whose_original_is_gone_says_so(harness):
    original = original_worker(harness)
    clone = clone_of(harness, original)
    harness.ledger.record_request("cancel", clone.target)
    harness.next_tick()
    harness.ledger.record_request("discard", original.target, force=True)
    harness.next_tick()

    request_id = harness.ledger.record_request("retry", f"#{clone.id}", force=True)
    harness.next_tick()

    outcome = harness.ledger.get_request(request_id)
    assert outcome["ok"] == 0
    assert "cleared, but no new clone started" in outcome["message"]
    assert "worktree and branch are gone" in outcome["message"]


# ----- the board ------------------------------------------------------------------------------


@pytest.mark.parametrize("body, error", [
    ({}, "mode must be one of fresh, full"),
    ({"mode": "summary"}, "mode must be one of fresh, full"),
    ({"mode": "fresh", "prompt": 5}, "prompt must be text"),
    ({"mode": "fresh", "prompt": "x" * (16 * 1024 + 1)}, "longer than 16 KiB"),
    ({"mode": "fresh", "prompt": "about {title}"}, "unknown template variable {title}"),
    ({"mode": "fresh", "model": "--dangerously-skip-permissions"}, "model must be"),
    ({"mode": "fresh", "model": "a b"}, "model must be"),
    ({"mode": "fresh", "effort": "huge"}, "effort must be one of"),
    ({"mode": "fresh", "subscribe": "comment"}, "subscribe must be a list"),
    ({"mode": "fresh", "subscribe": ["Not A Kind"]}, "subscribe must be a list"),
])
def test_a_bad_clone_is_refused_before_it_is_recorded(daemon, harness, body, error):
    original = original_worker(harness)

    status, answer = post(daemon, f"/api/workers/{original.id}/clone", body)

    assert status == 400
    assert error in answer["error"]
    assert harness.ledger.pending_requests() == []


def test_a_clone_needs_the_token(daemon, harness):
    original = original_worker(harness)

    status, _ = post(daemon, f"/api/workers/{original.id}/clone", {"mode": "fresh"},
                     headers={"Content-Type": "application/json"})

    assert status == 401
    assert harness.ledger.pending_requests() == []
    assert post(daemon, "/api/workers/999/clone", {"mode": "fresh"})[0] == 404


def test_the_clone_form_starts_with_the_role_values_and_says_when_full_is_not_possible(daemon, harness):
    original = original_worker(harness)

    status, form = get(daemon, f"/api/workers/{original.id}/clone")

    assert status == 200
    assert form["prompt"] == "work on {item} in {worktree}"
    assert (form["model"], form["effort"], form["item"]) == ("haiku", "low", "ABC-1")
    assert form["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert (form["modes"], form["full_refused"], form["subscribe"]) == (["fresh", "full"], None, [])
    assert form["prompt_limit"] == 16 * 1024
    assert form["role_values"] == {
        "prompt": "work on {item} in {worktree}", "model": "haiku", "effort": "low",
    }

    harness.ledger.update(original, state=ledger_mod.RUNNING)
    _, busy = get(daemon, f"/api/workers/{original.id}/clone")
    assert busy["modes"] == ["fresh"]
    assert "is running" in busy["full_refused"]

    harness.ledger.update(original, state=ledger_mod.CANCELLED, gc_done=1)
    assert get(daemon, f"/api/workers/{original.id}/clone")[0] == 409
    assert get(daemon, "/api/workers/999/clone")[0] == 404
    assert get(daemon, f"/api/workers/{original.id}/other")[0] == 404


def test_the_clone_form_of_a_clone_starts_with_its_own_values(daemon, harness):
    original = original_worker(harness)
    clone = clone_of(harness, original, prompt="mine: {branch}", model="opus", effort="max")

    _, form = get(daemon, f"/api/workers/{clone.id}/clone")

    assert (form["prompt"], form["model"], form["effort"]) == ("mine: {branch}", "opus", "max")
    assert form["role_values"]["model"] == "haiku"


def cards(harness):
    page = board.build(harness.config, harness.ledger, health={})
    return {c["id"]: c for column in page["columns"] for c in column["cards"]}, page


def test_a_clone_card_names_its_original_and_shows_its_own_model_effort_and_prompt(harness):
    original = original_worker(harness)
    clone = clone_of(harness, original, mode="full", prompt="go on in {worktree}", model="sonnet")

    by_id, _ = cards(harness)

    card = by_id[clone.id]
    assert card["parent"] == {"id": original.id, "handle": original.target}
    assert (card["origin"], card["clone_mode"]) == ("clone", "full")
    assert (card["model"], card["effort"]) == ("sonnet", "low")
    assert card["last_prompt"].endswith(f"go on in {clone.worktree}")
    assert card["last_prompt"].startswith("This session continues a copy")
    assert "clone" in card["actions"]
    assert by_id[original.id]["parent"] is None
    assert (by_id[original.id]["model"], by_id[original.id]["effort"]) == ("haiku", "low")


def test_a_clone_card_follows_its_original_in_one_column(harness):
    original = original_worker(harness)
    other = original_worker(harness, item="ABC-2")
    clone = idle(harness, clone_of(harness, original))
    grandchild = idle(harness, clone_of(harness, clone))
    # The newest activity sorts first; the clones still follow their original.
    harness.ledger.update(other, state=ledger_mod.IDLE)

    _, page = cards(harness)

    (idle_column,) = [c for c in page["columns"] if c["key"] == "idle"]
    order = [c["id"] for c in idle_column["cards"]]
    assert order.index(clone.id) == order.index(original.id) + 1
    assert order.index(grandchild.id) == order.index(clone.id) + 1
    assert set(order) == {original.id, other.id, clone.id, grandchild.id}


@pytest.mark.parametrize("runtime", ["claude", "fake"])
def test_a_clone_of_either_mode_is_launched_with_the_originals_worktree_protected(harness, runtime):
    original = original_worker(harness, role=ROLE if runtime == "claude" else FAKE_ROLE)

    fresh = clone_of(harness, original, mode="fresh")

    if runtime == "claude":
        argv = harness.claude_state.launches[-1]["argv"]
        deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
        assert f"Edit(/{original.worktree}/**)" in deny and f"Write(/{original.worktree}/**)" in deny
    else:
        assert fake_runtime.world().launches[-1]["protected_paths"] == [original.worktree]
    assert fresh.parent_id == original.id


def test_a_pinned_clone_keeps_its_place_among_the_pinned_cards():
    # In page order: pinned first. The pinned clone 8 of the unpinned card 7 stays with
    # the pinned cards, and the unpinned clone 9 of the pinned card 3 stays with the others.
    group = [
        {"id": 3, "pinned": True, "parent": None},
        {"id": 8, "pinned": True, "parent": {"id": 7, "handle": "a"}},
        {"id": 7, "pinned": False, "parent": None},
        {"id": 9, "pinned": False, "parent": {"id": 3, "handle": "b"}},
        {"id": 5, "pinned": False, "parent": None},
        {"id": 10, "pinned": False, "parent": {"id": 7, "handle": "a"}},
    ]

    order = [c["id"] for c in board.under_originals(group)]

    assert order == [3, 8, 7, 10, 9, 5]
    pins = [next(c["pinned"] for c in group if c["id"] == i) for i in order]
    assert pins == sorted(pins, reverse=True)


def test_under_originals_keeps_the_order_of_cards_whose_original_is_elsewhere():
    group = [
        {"id": 5, "parent": {"id": 1, "handle": "a"}},
        {"id": 2, "parent": None},
        {"id": 7, "parent": {"id": 2, "handle": "b"}},
        {"id": 3, "parent": None},
        {"id": 9, "parent": {"id": 7, "handle": "c"}},
        {"id": 8, "parent": {"id": 2, "handle": "b"}},
    ]

    assert [c["id"] for c in board.under_originals(group)] == [5, 2, 7, 9, 8, 3]


def test_the_clone_panel_is_built_with_elements_and_sends_the_form(daemon):
    script = (board.asset_dir() / "board.js").read_text()
    panel = script[script.index("async function openClone("):script.index("function openNewWorker(")]

    assert "innerHTML" not in script
    assert "fetch('/api/workers/' + encodeURIComponent(c.id) + '/clone'" in panel
    assert "credentials: 'omit'" in panel
    assert "el('textarea', 'prompt')" in panel and "radio.type = 'radio'" in panel
    assert "radio.disabled = !form.modes.includes(mode)" in panel
    assert "form.full_refused" in panel
    assert "kinds.value = ''" in panel and "if (form.item) fields.push(field('wakes on', kinds))" in panel
    assert "send('/api/workers/' + encodeURIComponent(c.id) + '/clone', body" in panel
    # A value left as the role has it is not sent, so the row equals the command line's.
    assert "model.value.trim() !== role.model" in panel and "effort.value !== role.effort" in panel
    assert "prompt.value.trim() !== (role.prompt || '').trim()" in panel
    assert "clone: 'Clone'" in script
    assert "'clone of ' + c.parent.handle" in script


# ----- the command line -----------------------------------------------------------------------


def test_the_clone_command_parses_its_options():
    args = build_parser().parse_args([
        "clone", "implementer/ABC-1#3", "--mode", "full", "--prompt-file", "p.txt",
        "--model", "sonnet", "--effort", "high", "--subscribe", "comment", "--subscribe", "review",
        "--role", "implementer",
    ])

    assert (args.item, args.mode, args.prompt_file, args.prompt) == ("implementer/ABC-1#3", "full", "p.txt", None)
    assert (args.model, args.effort, args.subscribe, args.role) == ("sonnet", "high", ["comment", "review"], "implementer")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clone", "ABC-1"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clone", "ABC-1", "--mode", "summary"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clone", "ABC-1", "--mode", "fresh", "--prompt", "a", "--prompt-file", "b"])


def test_the_clone_command_reads_the_prompt_file_and_refuses_a_bad_model(
    harness, config_file, tmp_path, capsys
):
    original = original_worker(harness)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("from a file: {item}\n")

    assert run(["clone", original.target, "--mode", "fresh", "--model=-x", "--no-wait"],
               config_file) == EXIT_ERROR
    assert "model must be" in capsys.readouterr().err
    assert run(["clone", original.target, "--mode", "fresh", "--prompt-file", str(prompt), "--no-wait"],
               config_file) == EXIT_OK

    (row,) = harness.ledger.pending_requests()
    assert row["args"] == {"mode": "fresh", "prompt": "from a file: {item}"}


def test_status_json_shows_the_parent_of_a_clone(harness):
    original = original_worker(harness)
    clone = clone_of(harness, original)

    status = build_status(harness.config, harness.ledger, include_queue=False, sessions=([], None))

    parents = {w["id"]: w["parent"] for w in status["workers"]}
    assert parents == {original.id: None, clone.id: original.id}


def test_the_fake_runtime_forks_in_the_given_worktree_under_the_given_name(tmp_path):
    world = fake_runtime.world()
    original = world.add_session(name="a", cwd="/elsewhere", state="done")
    spec = fake_runtime.LaunchSpec(worktree=tmp_path, name="implementer/#2", prompt="go")

    fake_runtime.FakeRuntime().fork(spec, session_id=original["id"])

    copy = world.sessions[-1]
    assert (copy["name"], copy["cwd"]) == ("implementer/#2", os.path.realpath(tmp_path))
    assert world.forks[-1]["worktree"] == str(tmp_path)
    assert Capabilities(fork=True) == fake_runtime.FakeRuntime.capabilities


def test_a_clone_of_an_archived_worker_is_refused(harness):
    original = original_worker(harness)
    harness.ledger.update(original, state=ledger_mod.CANCELLED, archived_at=time.time())

    outcome = request_clone(harness, harness.ledger.get(original.id))

    assert outcome["ok"] == 0 and "worktree and branch are gone" in outcome["message"]
    assert "clone" not in board.actions(harness.ledger.get(original.id))


def test_the_board_offers_clone_while_the_worktree_or_branch_exists(harness):
    original = original_worker(harness)
    cancelled = harness.ledger.update(original, state=ledger_mod.CANCELLED)

    assert "clone" in board.actions(cancelled)
    assert "clone" not in board.actions(harness.ledger.update(cancelled, gc_done=1))


def test_the_clone_prompt_is_cleaned_and_kept_out_of_the_log(harness, caplog):
    original = original_worker(harness)

    with caplog.at_level("INFO"):
        clone = clone_of(harness, original, prompt="SECRET-PLAN {item}")

    assert clone.prompt == "SECRET-PLAN {item}"
    assert "SECRET-PLAN" not in caplog.text
    assert "prompt of 18 characters" in caplog.text


def test_a_clone_is_not_held_by_the_one_worker_per_role_and_item_rule(harness):
    write_role(harness, ROLE + '\n[[start]]\non = "labeled"\n')
    harness.board.add("ABC-1")
    harness.tick()
    original = idle(harness, harness.worker("ABC-1"))
    assert original.origin == ledger_mod.ORIGIN_SOURCE

    first = clone_of(harness, original)
    second = clone_of(harness, original)

    assert {first.origin, second.origin} == {"clone"}
    assert len({original.worktree, first.worktree, second.worktree}) == 3
    assert Path(first.worktree).name == f"ABC-1-{first.id}"
