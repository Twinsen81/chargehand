"""The command source: an executable that prints events as JSON lines."""

from __future__ import annotations

import json
import logging
import shlex
import time
from pathlib import Path

import pytest

from conftest import Harness, make_config
from chargehand import ledger as ledger_mod
from chargehand import sources
from chargehand.config import SourceConfig
from chargehand.delivery import MESSAGE_HEADER
from chargehand.errors import AmbiguousWrite, ConfigError, TrackerError
from chargehand.gitutil import Git
from chargehand.ledger import Ledger
from chargehand.notify import Notifier
from chargehand.sources.base import WorkItem
from chargehand.sources.command import (
    MAX_LINE_BYTES,
    MAX_OUTPUT_BYTES,
    REFS_ENV,
    CommandSource,
)
from chargehand.tick import Runner
from chargehand.trackers.base import Status
from support.fake_command_source import SCRIPT as FAKE_SCRIPT
from support.fake_command_source import CommandBoard

EVENT = {
    "kind": "comment",
    "item": "DEMO-1",
    "actor": "someone@example.invalid",
    "occurred_at": 1_700_000_000,
    "dedupe_id": "comment:1",
    "pointer": "https://tracker.invalid/DEMO-1#1",
}


def script(tmp_path: Path, body: str, name: str = "source.sh") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def printing(tmp_path: Path, *lines: object, name: str = "source.sh") -> Path:
    """A poll script that prints *lines* (JSON-encoded unless they are str)."""
    text = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    data = tmp_path / f"{name}.out"
    data.write_text(text + "\n")
    return script(tmp_path, f"cat >/dev/null\ncat {shlex.quote(str(data))}\n", name)


def source(poll: Path | str, **options) -> CommandSource:
    return CommandSource("cmd", {"poll": shlex.quote(str(poll)), **options})


# ----- the poll contract ---------------------------------------------------------


def test_an_event_line_becomes_an_event_with_every_field(tmp_path):
    full = {
        **EVENT,
        "source": "cmd",
        "ref": "10042",
        "url": "https://tracker.invalid/DEMO-1",
        "repository": "app",
        "marked": True,
        "occurred_at": "2026-10-27T10:00:00Z",
    }
    poll = printing(tmp_path, full, {"cursor": "c2"})

    events, cursor = source(poll, repositories=["app", "web"]).poll(None, [])

    (event,) = events
    assert cursor == "c2"
    assert (event.source, event.kind, event.actor) == ("cmd", "comment", "someone@example.invalid")
    assert event.item == WorkItem(
        source="cmd", key="DEMO-1", ref="10042", url="https://tracker.invalid/DEMO-1", hint="app"
    )
    assert event.occurred_at == 1_793_095_200.0
    assert (event.dedupe_id, event.pointer, event.marked) == (
        "comment:1", "https://tracker.invalid/DEMO-1#1", True,
    )


def test_the_optional_fields_default(tmp_path):
    poll = printing(tmp_path, EVENT, {"cursor": None})

    (event,), cursor = source(poll, repositories=["app"]).poll("c1", [])

    assert cursor is None
    assert event.item == WorkItem(source="cmd", key="DEMO-1", ref="DEMO-1", url=None, hint="app")
    assert event.marked is False
    # With several repositories and none named, the event can be delivered but starts nothing.
    (event,), _ = source(poll, repositories=["app", "web"]).poll(None, [])
    assert event.item.hint is None


def test_lines_that_do_not_fit_are_logged_and_skipped(tmp_path, caplog):
    bad = [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({**EVENT, "body": "the text of the comment"}),
        json.dumps({k: v for k, v in EVENT.items() if k != "pointer"}),
        json.dumps({**EVENT, "kind": "Comment"}),
        json.dumps({**EVENT, "kind": "cmd.comment"}),
        json.dumps({**EVENT, "item": "../escape"}),
        json.dumps({**EVENT, "source": "other"}),
        json.dumps({**EVENT, "repository": "elsewhere"}),
        json.dumps({**EVENT, "url": "javascript:alert(1)"}),
        json.dumps({**EVENT, "occurred_at": "yesterday"}),
        json.dumps({**EVENT, "occurred_at": "2026-10-27T10:00:00"}),
        json.dumps({**EVENT, "occurred_at": 10**400}),
        json.dumps({**EVENT, "occurred_at": -1}),
        "[" * 30_000 + "]" * 30_000,
        json.dumps({**EVENT, "marked": "yes"}),
        json.dumps({**EVENT, "actor": None}),
        json.dumps({**EVENT, "dedupe_id": ""}),
        json.dumps({**EVENT, "ref": "a\x1b[31mb"}),
        json.dumps({"cursor": "too early"}),
        json.dumps({**EVENT, "pointer": "x" * MAX_LINE_BYTES}),
    ]
    good = {**EVENT, "dedupe_id": "comment:2"}
    poll = printing(tmp_path, *bad, good, {"cursor": "c9"})

    with caplog.at_level(logging.WARNING, logger="chargehand.sources.command"):
        events, cursor = source(poll, repositories=["app"]).poll("c1", [])

    assert [event.dedupe_id for event in events] == ["comment:2"]
    assert cursor == "c9"
    skipped = [r.getMessage() for r in caplog.records if "skipped output line" in r.getMessage()]
    assert len(skipped) == len(bad)
    assert any("unknown field(s): body" in message for message in skipped)
    assert any(f"longer than {MAX_LINE_BYTES} bytes" in message for message in skipped)
    assert any("occurred_at is out of range" in message for message in skipped)
    # The text of a skipped line is not logged.
    assert not any("the text of the comment" in r.getMessage() for r in caplog.records)


def test_output_over_the_cap_fails_the_poll(tmp_path):
    poll = script(tmp_path, f"head -c {MAX_OUTPUT_BYTES + 10} /dev/zero | tr '\\0' 'x'\n")

    with pytest.raises(TrackerError, match="printed more than"):
        source(poll).poll(None, [])


@pytest.mark.parametrize(
    "body",
    ["sleep 30\n", "(sleep 30) &\necho '{\"cursor\": \"c\"}'\n"],
    ids=["slow", "a child holds stdout"],
)
def test_a_poll_that_does_not_finish_in_time_fails(tmp_path, body):
    poll = script(tmp_path, body)
    started = time.monotonic()

    with pytest.raises(TrackerError, match="did not finish within 0.5s"):
        source(poll, timeout_secs=0.5).poll(None, [])

    assert time.monotonic() - started < 5


def test_a_poll_that_exits_non_zero_fails_with_its_stderr(tmp_path):
    poll = script(tmp_path, "echo 'jira: 401 \x1b[31mUnauthorized' >&2\nexit 1\n")

    with pytest.raises(TrackerError) as raised:
        source(poll).poll("c1", [])

    assert str(raised.value) == "source cmd: poll exited 1: jira: 401 Unauthorized"


def test_a_missing_program_fails_the_poll(tmp_path):
    with pytest.raises(TrackerError, match="not found"):
        source(tmp_path / "missing").poll(None, [])


def test_the_cursor_goes_in_on_stdin_and_comes_back_from_the_last_line(tmp_path):
    seen = tmp_path / "seen"
    poll = script(
        tmp_path,
        f'cursor=$(cat)\nprintf "%s|" "$cursor" >> {shlex.quote(str(seen))}\n'
        f'printf \'{{"cursor": "%s+"}}\\n\' "$cursor"\n',
    )
    cmd = source(poll)

    _, first = cmd.poll(None, [])
    _, second = cmd.poll(first, [])

    assert (first, second) == ("+", "++")
    assert seen.read_text() == "|+|"


def test_an_unforeseen_error_on_one_line_skips_only_that_line(tmp_path, monkeypatch, caplog):
    poll = printing(tmp_path, EVENT, {**EVENT, "dedupe_id": "comment:2"}, {"cursor": "c2"})
    cmd = source(poll)
    real = cmd._event

    def flaky(entry):
        if entry["dedupe_id"] == "comment:1":
            raise RuntimeError("unforeseen")
        return real(entry)

    monkeypatch.setattr(cmd, "_event", flaky)
    with caplog.at_level(logging.WARNING, logger="chargehand.sources.command"):
        events, cursor = cmd.poll("c1", [])

    assert [event.dedupe_id for event in events] == ["comment:2"]
    assert cursor == "c2"
    assert any("skipped output line 1: RuntimeError" in r.getMessage() for r in caplog.records)


def test_the_poll_gets_the_followed_refs_in_the_environment(tmp_path):
    seen = tmp_path / "seen"
    poll = script(
        tmp_path,
        f'printf "%s" "${REFS_ENV}" > {shlex.quote(str(seen))}\necho \'{{"cursor": null}}\'\n',
    )
    items = [
        WorkItem(source="cmd", key="DEMO-1", ref="10 042"),
        WorkItem(source="cmd", key="DEMO-2", ref="DEMO-2"),
        WorkItem(source="cmd", key="DEMO-3", ref="bad\nref"),
    ]

    source(poll).poll(None, items)
    assert seen.read_text() == "10 042\nDEMO-2"
    source(poll).poll(None, [])
    assert seen.read_text() == ""


def test_without_a_cursor_line_the_old_cursor_is_kept(tmp_path, caplog):
    poll = printing(tmp_path, EVENT)

    with caplog.at_level(logging.WARNING, logger="chargehand.sources.command"):
        events, cursor = source(poll).poll("c1", [])

    assert len(events) == 1 and cursor == "c1"
    assert any("no cursor line" in r.getMessage() for r in caplog.records)
    # A cursor that is not a string is no cursor either.
    poll = printing(tmp_path, {"cursor": 7}, name="numeric.sh")
    assert source(poll).poll("c1", []) == ([], "c1")


def test_the_command_runs_without_a_shell(tmp_path):
    args = tmp_path / "args"
    poll = script(tmp_path, f'printf "%s\\n" "$@" > {shlex.quote(str(args))}\necho \'{{"cursor": null}}\'\n')

    CommandSource("cmd", {"poll": f"{shlex.quote(str(poll))} --source {{source}} '$(id)'"}).poll(None, [])

    assert args.read_text().splitlines() == ["--source", "cmd", "$(id)"]


def test_a_relative_program_resolves_against_the_configuration_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    (config_dir / "bin").mkdir(parents=True)
    monkeypatch.setenv("CHARGEHAND_CONFIG", str(config_dir / "config.toml"))
    script(config_dir / "bin", 'echo \'{"cursor": "here"}\'\n', "events")

    assert CommandSource("cmd", {"poll": "bin/events"}).poll(None, []) == ([], "here")


# ----- mark and item_open ------------------------------------------------------------


ITEM = WorkItem(source="cmd", key="DEMO-1", ref="10 042")


@pytest.mark.parametrize(
    "code, error",
    [(0, None), (1, TrackerError), (2, TrackerError), (3, AmbiguousWrite)],
)
def test_the_mark_exit_code_says_whether_the_write_landed(tmp_path, code, error):
    args = tmp_path / "args"
    mark = script(
        tmp_path, f'printf "%s\\n" "$@" > {shlex.quote(str(args))}\necho nope >&2\nexit {code}\n', "mark.sh"
    )
    cmd = source(tmp_path / "unused", mark=f"{shlex.quote(str(mark))} {{item}} {{ref}} {{status}}")

    if error is None:
        cmd.mark(ITEM, Status.RUNNING)
    else:
        with pytest.raises(error) as raised:
            cmd.mark(ITEM, Status.RUNNING)
        assert (type(raised.value) is AmbiguousWrite) == (code == 3)

    assert args.read_text().splitlines() == ["DEMO-1", "10 042", "running"]


def test_without_a_mark_command_mark_does_nothing(tmp_path):
    source(tmp_path / "unused").mark(ITEM, Status.BLOCKED)


@pytest.mark.parametrize("answer, expected", [("true", True), ("false", False)])
def test_item_open_reads_the_answer(tmp_path, answer, expected):
    probe = script(tmp_path, f'echo \'{{"open": {answer}}}\'\n', "open.sh")

    assert source(tmp_path / "unused", item_open=f"{probe} {{ref}}").item_open(ITEM) is expected


@pytest.mark.parametrize(
    "body", ['echo closed\n', 'echo \'{"open": "no"}\'\n', 'echo \'{"open": false}\'\nexit 1\n']
)
def test_an_unclear_item_open_answer_is_an_error_not_a_closed_item(tmp_path, body):
    probe = script(tmp_path, body, "open.sh")

    with pytest.raises(TrackerError):
        source(tmp_path / "unused", item_open=str(probe)).item_open(ITEM)


def test_without_an_item_open_command_the_item_stays_open(tmp_path):
    assert source(tmp_path / "unused").item_open(ITEM) is True


# ----- configuration ---------------------------------------------------------------


def test_the_registry_builds_a_command_source(tmp_path):
    built = sources.build(
        SourceConfig(name="jira", type="command", options={"poll": "jira-events", "account": "me"})
    )

    assert isinstance(built, CommandSource)
    assert built.whoami() == "me"
    assert built.describe() == "jira (command jira-events)"


@pytest.mark.parametrize(
    "options, message",
    [
        ({}, "missing required option 'poll'"),
        ({"poll": "x", "list": "y"}, "unknown option"),
        ({"poll": "x {cursor}"}, "unknown template variable {cursor}"),
        ({"poll": "x", "mark": "m {item} {state}"}, "unknown template variable {state}"),
        ({"poll": "x", "item_open": "o {status}"}, "unknown template variable {status}"),
        ({"poll": "  "}, "expected a non-empty command"),
        ({"poll": "x", "timeout_secs": 0}, "timeout_secs"),
        ({"poll": "x", "repositories": "app"}, "repositories"),
        ({"poll": "x", "account": 1}, "account"),
    ],
)
def test_a_bad_option_is_a_configuration_error(options, message):
    with pytest.raises(ConfigError, match=message.replace("{", r"\{").replace("}", r"\}")):
        CommandSource("cmd", options)


# ----- the fake tracker as a command source --------------------------------------------


WATCHER = """\
prompt = "work on {item} ({item_url})"

[[start]]
on = "cmd.queued"
subscribe = ["comment"]
"""


def fake_options(board: CommandBoard) -> dict[str, object]:
    base = f"{shlex.quote(str(FAKE_SCRIPT))} {shlex.quote(str(board.path))}"
    return {
        "poll": f"{base} poll",
        "mark": f"{base} mark {{ref}} {{status}}",
        "item_open": f"{base} open {{ref}}",
        "account": "tester@example.invalid",
        "repositories": ["test"],
    }


@pytest.fixture
def fake_board(tmp_path) -> CommandBoard:
    return CommandBoard(tmp_path / "command-board.json")


def test_the_fake_tracker_contract_marks_and_answers_item_open(fake_board):
    cmd = CommandSource("cmd", fake_options(fake_board))
    fake_board.add("DEMO-1")
    (queued,), _ = cmd.poll(None, [])

    cmd.mark(queued.item, Status.RUNNING)
    assert fake_board.labels_of("DEMO-1") == ["chargehand-running"]
    fake_board.update(fail_next_mark="ambiguous")
    with pytest.raises(AmbiguousWrite):
        cmd.mark(queued.item, Status.BLOCKED)
    # Ambiguous means the write may have landed: here it did.
    assert fake_board.labels_of("DEMO-1") == ["chargehand-blocked"]

    assert cmd.item_open(queued.item) is True
    fake_board.close("DEMO-1")
    assert cmd.item_open(queued.item) is False
    assert cmd.item_open(WorkItem(source="cmd", key="GONE-1", ref="GONE-1")) is False


@pytest.fixture
def watchers() -> tuple[str, ...]:
    """The roles that start on the source's events, in the repository's order."""
    return ("watcher",)


@pytest.fixture
def with_mark() -> bool:
    """Whether the source's configuration has a mark command."""
    return True


@pytest.fixture
def wired(tmp_path, repo, claude_state, board, fake_board, watchers, with_mark):
    for name in watchers:
        target = repo / ".chargehand" / "roles" / f"{name}.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(WATCHER)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    options = fake_options(fake_board)
    if not with_mark:
        del options["mark"]
    config = make_config(
        repo,
        worktree_root,
        roles=("implementer", *watchers),
        sources=(SourceConfig(name="cmd", type="command", options=options),),
    )
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    harness = Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root, notifier)
    yield harness
    ledger.close()


def test_the_fake_tracker_as_a_command_source_starts_and_wakes_a_worker(wired, fake_board):
    fake_board.add("DEMO-1")

    report = wired.tick()

    assert report.errors == []
    watcher = wired.worker("DEMO-1", "watcher")
    assert watcher is not None and watcher.state == ledger_mod.RUNNING
    assert watcher.item_id == "DEMO-1"
    assert fake_board.cursors == [""]

    # The turn ends; a reply and the worker's own marked comment arrive.
    wired.claude_state.set_session_state("DEMO-1", "done", status="idle")
    fake_board.comment("DEMO-1")
    fake_board.comment("DEMO-1", author="tester@example.invalid", marked=True)

    report = wired.next_tick()

    assert report.errors == []
    assert report.woken == ["watcher/DEMO-1"]
    (resume,) = wired.claude_state.read()["resumes"]
    assert resume["target"] == watcher.session_uuid
    assert resume["prompt"].splitlines() == [MESSAGE_HEADER, "- https://tracker.invalid/DEMO-1#c1"]
    assert fake_board.cursors == ["", "0"]
    assert fake_board.followed == ["", "DEMO-1"]
    assert wired.ledger.get_cursor("cmd") == "2"
    # The queue event came again and started no second worker.
    assert [w.role for w in wired.ledger.live_workers()] == ["watcher"]

    # The issue closes: garbage collection asks item_open and ends the worker.
    wired.claude_state.set_session_state("DEMO-1", "done", status="idle")
    fake_board.close("DEMO-1")
    report = wired.next_tick()
    assert "DEMO-1: issue closed -> done" in report.transitions
    assert wired.ledger.get(watcher.id).state == ledger_mod.DONE


def test_a_garbage_line_does_not_stop_the_tick(wired, fake_board, caplog):
    fake_board.add("DEMO-1")
    fake_board.update(garbage=["<html>502 Bad Gateway</html>"])

    with caplog.at_level(logging.WARNING, logger="chargehand.sources.command"):
        report = wired.tick()

    assert report.errors == []
    assert wired.worker("DEMO-1", "watcher") is not None
    assert any("skipped output line 1" in r.getMessage() for r in caplog.records)


def mark_calls(fake_board: CommandBoard) -> list[list[str]]:
    return [call for call in fake_board.calls if call[0] == "mark"]


def test_the_worker_a_command_source_started_marks_its_item(wired, fake_board):
    fake_board.add("DEMO-1")

    report = wired.tick()

    assert report.errors == []
    watcher = wired.worker("DEMO-1", "watcher")
    assert watcher.owns_label == ledger_mod.LABEL_SOURCE
    assert watcher.label_state == "running"
    assert fake_board.labels_of("DEMO-1") == ["chargehand-running"]

    # The turn ends: the worker waits for events, and its item shows it as blocked.
    wired.claude_state.set_session_state("DEMO-1", "done", status="idle")
    wired.next_tick()
    assert wired.worker("DEMO-1", "watcher").state == ledger_mod.IDLE
    assert fake_board.labels_of("DEMO-1") == ["chargehand-blocked"]

    fake_board.close("DEMO-1")
    wired.next_tick()

    assert wired.worker("DEMO-1", "watcher").state == ledger_mod.DONE
    assert fake_board.labels_of("DEMO-1") == []
    assert mark_calls(fake_board) == [
        ["mark", "DEMO-1", "running"], ["mark", "DEMO-1", "blocked"], ["mark", "DEMO-1", "done"],
    ]


@pytest.mark.parametrize("with_mark", [False])
def test_without_a_mark_command_the_tick_marks_nothing(wired, fake_board):
    fake_board.add("DEMO-1")
    labels = fake_board.labels_of("DEMO-1")

    report = wired.tick()

    assert report.errors == []
    watcher = wired.worker("DEMO-1", "watcher")
    assert watcher.state == ledger_mod.RUNNING
    assert watcher.owns_label == ledger_mod.LABEL_NONE

    wired.claude_state.set_session_state("DEMO-1", "done", status="idle")
    wired.next_tick()
    assert wired.worker("DEMO-1", "watcher").state == ledger_mod.IDLE
    fake_board.close("DEMO-1")
    report = wired.next_tick()

    assert report.errors == []
    assert wired.ledger.get(watcher.id).state == ledger_mod.DONE
    assert wired.ledger.get(watcher.id).label_state is None
    assert mark_calls(fake_board) == []
    assert fake_board.labels_of("DEMO-1") == labels


@pytest.mark.parametrize(
    "failure, error",
    [
        ("ambiguous", "DEMO-1: source cmd: mark DEMO-1 running may or may not have landed"),
        ("failure", "DEMO-1: source cmd: mark exited 1: fake: the write failed"),
    ],
)
def test_a_mark_that_did_not_land_for_sure_is_tried_again_on_the_next_tick(
    wired, fake_board, failure, error
):
    fake_board.add("DEMO-1")
    # The tick syncs the item after the state diff and again in its label step.
    fake_board.update(fail_next_mark=[failure, failure])

    report = wired.tick()

    assert any(e.startswith(error) for e in report.errors), report.errors
    assert wired.worker("DEMO-1", "watcher").label_state is None

    report = wired.next_tick()

    assert report.errors == []
    assert wired.worker("DEMO-1", "watcher").label_state == "running"
    assert mark_calls(fake_board) == [["mark", "DEMO-1", "running"]] * 3


@pytest.mark.parametrize("watchers", [("watcher", "helper")])
def test_only_the_first_worker_on_an_item_marks_it(wired, fake_board):
    fake_board.add("DEMO-1")

    report = wired.tick()

    assert report.errors == []
    watcher, helper = wired.worker("DEMO-1", "watcher"), wired.worker("DEMO-1", "helper")
    assert watcher.state == helper.state == ledger_mod.RUNNING
    assert (watcher.owns_label, helper.owns_label) == (
        ledger_mod.LABEL_SOURCE, ledger_mod.LABEL_NONE,
    )
    assert mark_calls(fake_board) == [["mark", "DEMO-1", "running"]]


def test_retry_does_not_queue_a_command_source_item_again(wired, fake_board):
    fake_board.add("DEMO-1")
    wired.tick()
    wired.ledger.record_request("cancel", "DEMO-1", role="watcher")
    wired.next_tick()
    wired.ledger.record_request("retry", "DEMO-1", role="watcher", force=True)

    report = wired.next_tick()

    (outcome,) = [r for r in report.requests if r.startswith("retry")]
    assert "the next start event for the item starts a new worker" in outcome
    assert "queued" not in [call[2] for call in mark_calls(fake_board)]


def test_a_failing_poll_is_one_line_per_tick_without_a_traceback(
    tmp_path, repo, claude_state, board, caplog
):
    poll = script(tmp_path, "cat >/dev/null\necho 'upstream is down' >&2\nexit 2\n")
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    config = make_config(
        repo, worktree_root,
        sources=(SourceConfig(name="demo", type="command", options={"poll": str(poll)}),),
    )
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    harness = Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root, notifier)

    with caplog.at_level(logging.DEBUG, logger="chargehand"):
        reports = [harness.next_tick(), harness.next_tick()]

    expected = "source demo: poll exited 2: upstream is down"
    assert [report.errors for report in reports] == [[expected], [expected]]
    records = [r for r in caplog.records if "upstream is down" in r.getMessage()]
    assert [r.getMessage() for r in records] == [expected, expected]
    # The tick prints the report's error line; this record shows only at debug level.
    assert all(r.levelno < logging.INFO and r.exc_info is None for r in records)
    ledger.close()
