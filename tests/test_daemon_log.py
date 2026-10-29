"""The daemon's log: enough to trace a missed event, hook, notification or wake, and no session text.

Every test runs against the fakes. A daemon here serves on 127.0.0.1 on a port the
kernel picks and runs no loop.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys

import pytest

from conftest import make_config
from chargehand import cli, daemonlog, install, ledger as ledger_mod, paths
from chargehand.config import NotifyConfig, ServeConfig, SourceConfig
from chargehand.errors import ConfigError
from chargehand.gitutil import Git
from chargehand.ledger import Ledger
from chargehand.notify import Notifier
from chargehand.router import Router, StartResult, event_name
from chargehand.sources import Event, WorkItem
from chargehand.tick import Runner, TickReport
from support import daemon as support
from support import fake_source

FAKE_SOURCE = SourceConfig(name="fake", type="fake", options={"board": "default", "repository": "test"})
AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}
MARKER = "zz-session-text-marker-zz"

REVIEWER = """\
prompt = "/review {item_url}"
permission_mode = "auto"

[[start]]
on = "fake.review_requested"
subscribe = ["comment"]
"""


def messages(caplog, name: str | None = None, level: int = logging.DEBUG) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.levelno >= level and (name is None or record.name == name)
    ]


@pytest.fixture
def wired(tmp_path, repo, claude_state, board):
    """A harness with the fake `[[source]]` and a reviewer role that it starts."""
    from conftest import Harness

    target = repo / ".chargehand" / "roles" / "reviewer.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(REVIEWER)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir(exist_ok=True)
    config = make_config(repo, worktree_root, roles=("implementer", "reviewer"), sources=(FAKE_SOURCE,))
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    harness = Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root, notifier)
    yield harness
    ledger.close()


def start_reviewer(harness, item="PR-7"):
    harness.board.add(item, labels=())
    fake_source.feed().emit("review_requested", item)
    harness.tick()
    reviewer = harness.worker(item, "reviewer")
    assert reviewer is not None and reviewer.state == ledger_mod.RUNNING
    return reviewer


# ----- format, level, file -------------------------------------------------------


def test_the_formatter_keeps_each_message_on_one_line_without_control_sequences():
    formatter = daemonlog.OneLineFormatter()
    record = logging.LogRecord("chargehand.x", logging.ERROR, __file__, 1,
                               "setup said: %s", ("line one\nline two \x1b]0;title\x07 end",), None)

    line = formatter.format(record)

    assert "\n" not in line
    assert "\x1b" not in line and "\x07" not in line
    assert line.endswith("chargehand.x: setup said: line one line two end")


def test_the_formatter_keeps_the_lines_of_a_traceback():
    formatter = daemonlog.OneLineFormatter()
    try:
        raise RuntimeError("boom \x1b[31mred")
    except RuntimeError:
        record = logging.LogRecord("chargehand.x", logging.ERROR, __file__, 1, "failed", (),
                                   sys.exc_info())

    text = formatter.format(record)

    assert "Traceback" in text and text.count("\n") >= 2
    assert "\x1b" not in text


def test_the_level_comes_from_the_configuration_unless_v_is_given():
    assert daemonlog.level_for(0, "debug") == logging.DEBUG
    assert daemonlog.level_for(0, "warning") == logging.WARNING
    assert daemonlog.level_for(1, "warning") == logging.INFO
    assert daemonlog.level_for(2, "warning") == logging.DEBUG
    assert daemonlog.level_for(0) == logging.WARNING


def test_log_level_is_read_from_serve_and_checked():
    assert ServeConfig.parse({"log_level": "DEBUG"}).log_level == "debug"
    assert ServeConfig.parse({}).log_level == "info"

    with pytest.raises(ConfigError, match="log_level"):
        ServeConfig.parse({"log_level": "verbose"})


def test_the_daemon_writes_its_log_file_through_a_rotating_handler(tmp_path):
    root = logging.getLogger()
    level = root.level
    target = tmp_path / "logs" / "chargehand.log"
    try:
        daemonlog.setup(logging.INFO, log_file=target)
        daemonlog.setup(logging.INFO, log_file=target)
        ours = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(ours) == 1
        assert ours[0].maxBytes == daemonlog.MAX_BYTES and ours[0].backupCount == daemonlog.BACKUP_COUNT
        logging.getLogger("chargehand.test").info("hello\nworld")
        ours[0].flush()
        [line] = target.read_text().splitlines()
        assert line.endswith("INFO    chargehand.test: hello world")
    finally:
        for handler in [h for h in root.handlers if getattr(h, "_chargehand_handler", False)]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level)


# ----- polls ---------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_a_poll_that_found_events_is_one_line_at_once(caplog):
    caplog.set_level(logging.INFO)
    polls = daemonlog.PollLog(clock=Clock())

    polls.polled("github", "c1", "c2", 3, 0.25)

    assert messages(caplog, "chargehand.poll") == ["poll github: 3 event(s), 3 new, cursor c1 -> c2, 0.2s"]


def test_polls_that_found_nothing_become_one_line(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    polls = daemonlog.PollLog(window_secs=900, clock=clock)

    for cursor in range(3):
        polls.polled("github", f"c{cursor}", f"c{cursor + 1}", 0, 0.1 * (cursor + 1))
        clock.now += 60
    assert messages(caplog, "chargehand.poll") == []

    polls.polled("github", "c3", "c4", 1, 0.1)

    summary, found = messages(caplog, "chargehand.poll")
    assert summary.startswith("poll github: 3 poll(s) found nothing new from ")
    assert summary.endswith("cursor c0 -> c3, longest 0.3s")
    assert found == "poll github: 1 event(s), 1 new, cursor c3 -> c4, 0.1s"


def test_quiet_polls_are_written_once_the_window_has_passed(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    polls = daemonlog.PollLog(window_secs=900, clock=clock)

    for _ in range(16):
        polls.polled("linear", "c", "c", 0, 0.1)
        clock.now += 60

    [line] = messages(caplog, "chargehand.poll")
    assert "16 poll(s) found nothing new" in line


def test_a_failed_poll_writes_the_quiet_polls_before_its_error(caplog):
    caplog.set_level(logging.INFO)
    polls = daemonlog.PollLog(window_secs=900, clock=Clock())
    polls.polled("github", "a", "a", 0, 0.1)
    polls.polled("tracker:test", "b", "b", 0, 0.1)

    polls.failed("github")

    [line] = messages(caplog, "chargehand.poll")
    assert line.startswith("poll github: 1 poll(s) found nothing new")
    polls.flush()
    assert messages(caplog, "chargehand.poll")[-1].startswith("poll tracker:test: 1 poll(s)")


def test_a_poll_that_returned_only_events_seen_before_is_quiet(caplog):
    caplog.set_level(logging.INFO)
    polls = daemonlog.PollLog(window_secs=900, clock=Clock())

    polls.polled("github", "a", "b", 5, 0.1, new=0)

    assert messages(caplog, "chargehand.poll") == []
    polls.flush()
    assert messages(caplog, "chargehand.poll")[0].startswith("poll github: 1 poll(s) found nothing new")


def test_events_seen_before_add_no_routed_line_and_no_event_line(wired, caplog):
    fake_source.feed().emit("comment", "PR-9", dedupe_id="seen-once")
    wired.tick()
    caplog.set_level(logging.INFO)
    feed = fake_source.feed()
    feed.entries.append(dict(feed.entries[-1]))

    report = wired.next_tick()

    assert report.routed == []
    assert [m for m in messages(caplog, "chargehand.router") if "seen-once" in m] == []
    assert "poll fake: 1 event(s), 0 new, cursor 1 -> 2" in messages(caplog, "chargehand.poll")[0]


def test_a_tick_by_hand_logs_every_poll_with_its_cursor(wired, caplog):
    caplog.set_level(logging.INFO)

    wired.tick()

    polls = messages(caplog, "chargehand.poll")
    assert "poll fake: 0 event(s), 0 new, cursor none -> 0" in polls[0]
    assert any(line.startswith("poll tracker:test: ") for line in polls)


# ----- events ---------------------------------------------------------------------


def test_an_event_that_starts_a_worker_names_the_decision_and_the_worker(wired, caplog):
    caplog.set_level(logging.INFO)

    start_reviewer(wired)

    [line] = [m for m in messages(caplog, "chargehand.router") if "review_requested" in m]
    assert line.startswith("event fake review_requested PR-7 [evt-")
    assert "launched reviewer/PR-7" in line
    assert "https://tracker.invalid/PR-7#evt-" in line
    assert fake_source.OTHER_ACTOR not in caplog.text


def test_an_event_delivered_to_a_subscriber_names_the_worker(wired, caplog):
    start_reviewer(wired)
    wired.claude_state.set_session_state("PR-7", "done", status="idle")
    caplog.set_level(logging.INFO)

    fake_source.feed().emit("comment", "PR-7", pointer="not a url, the agent wrote this")
    wired.tick()

    [line] = [m for m in messages(caplog, "chargehand.router") if " comment " in m]
    assert line.endswith(": delivered to reviewer/PR-7")
    assert "agent wrote" not in line


def test_an_event_nobody_follows_says_so():
    ledger = _Ledger()
    router = Router(ledger, start=lambda event: False)
    event = Event("github", "comment", WorkItem("github", "o-r-1", "o/r#1"), MARKER, 0.0,
                  "comment:123456789012345", "https://github.invalid/o/r/pull/1")
    records: list[str] = []
    handler = _Collect(records)
    logging.getLogger("chargehand.router").addHandler(handler)
    logging.getLogger("chargehand.router").setLevel(logging.INFO)
    try:
        router.route([event], signed_in="me")
    finally:
        logging.getLogger("chargehand.router").removeHandler(handler)
        logging.getLogger("chargehand.router").setLevel(logging.NOTSET)

    assert records == [
        "event github comment o-r-1 [comment:123456789012345]: no subscriber and no start; "
        "https://github.invalid/o/r/pull/1"
    ]


def test_a_deferred_start_says_why():
    event = Event("fake", "labeled", WorkItem("fake", "ABC-1", "1"), "x", 0.0, "d", "p")
    line = cli_free_decision(StartResult(True, "start of implementer/ABC-1 waits, launch throttle (2/2)"), event)
    assert line == "start of implementer/ABC-1 waits, launch throttle (2/2)"
    assert cli_free_decision(True, event) == "start deferred"
    # A start step with nothing to say, as for a tracker comment that no rule starts on.
    assert cli_free_decision(StartResult(True, ""), event) == "no subscriber and no start"
    assert event_name(event) == "event fake labeled ABC-1 [d]"


def cli_free_decision(started, event):
    from chargehand.router import decision

    return decision([], [], 0, started, event)


class _Collect(logging.Handler):
    def __init__(self, into: list[str]) -> None:
        super().__init__()
        self.into = into

    def emit(self, record: logging.LogRecord) -> None:
        self.into.append(record.getMessage())


class _Ledger:
    """Just enough of a ledger for the router, with nothing seen and nobody subscribed."""

    def event_seen(self, source, dedupe_id):
        return False

    def record_event(self, event, start_pending=False):
        pass

    def live_subscriptions(self, source, key):
        return []


# ----- wakes, labels, notifications --------------------------------------------------


def test_a_wake_names_the_kinds_and_pointers_it_carried(wired, caplog):
    start_reviewer(wired)
    wired.claude_state.set_session_state("PR-7", "done", status="idle")
    wired.tick()
    caplog.set_level(logging.INFO)

    fake_source.feed().emit("comment", "PR-7", pointer="https://tracker.invalid/PR-7#c1")
    wired.tick()

    [line] = [m for m in messages(caplog, "chargehand.delivery") if "woke" in m]
    assert line.startswith("reviewer/PR-7: woke session ")
    assert line.endswith("with comment: https://tracker.invalid/PR-7#c1")


def test_a_label_write_is_logged_with_the_status_before_and_after(harness, caplog):
    caplog.set_level(logging.INFO)
    harness.board.add("ABC-1")

    harness.tick()

    marks = [m for m in messages(caplog, "chargehand.tick") if " marked " in m]
    assert marks == ["implementer/ABC-1: marked ABC-1 on fake: none -> running"]


def test_what_the_listing_says_is_info_when_it_changes_and_debug_when_it_does_not(harness, caplog):
    harness.board.add("ABC-1")
    harness.tick()
    harness.claude_state.set_session_state("ABC-1", "blocked", waitingFor=f"run {MARKER}")
    caplog.set_level(logging.DEBUG)

    harness.next_tick()
    harness.next_tick()

    listed = [(r.levelno, r.getMessage()) for r in caplog.records if "the listing shows" in r.getMessage()]
    assert [level for level, _ in listed] == [logging.INFO, logging.DEBUG]
    assert listed[0][1].startswith("implementer/ABC-1: the listing shows session ")
    assert listed[0][1].endswith(" as blocked (recorded working, worker running)")
    assert MARKER not in caplog.text


def test_the_report_names_each_notification_with_its_detail_and_outcome(harness):
    harness.board.add("ABC-1")
    harness.tick()
    harness.claude_state.set_session_state("ABC-1", "blocked", waitingFor="a question")

    report = harness.tick()

    assert report.notified == ["implementer/ABC-1: blocked (needs input), no command configured"]
    assert any(line == (logging.INFO, "notified: implementer/ABC-1: blocked (needs input), "
                                      "no command configured")
               for line in cli.report_lines(report))


def test_a_failed_notification_names_the_exit_code(harness, tmp_path):
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/sh\nexit 3\n")
    script.chmod(0o755)
    harness.notifier = Notifier(NotifyConfig(command=str(script)))
    harness.fresh_runner()
    harness.board.add("ABC-1")
    harness.tick()
    harness.claude_state.set_session_state("ABC-1", "blocked", waitingFor="a question")

    report = harness.next_tick()

    assert report.notified == ["implementer/ABC-1: blocked (needs input), exit 3"]


# ----- hooks ------------------------------------------------------------------------


@pytest.fixture
def daemon(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def post(daemon, event, payload, headers=AUTH):
    status, _ = support.request(daemon.port, "POST", f"/hooks/{event}",
                                body=json.dumps(payload).encode(), headers=headers)
    return status


def launch(harness):
    harness.board.add("ABC-1")
    harness.tick()
    return harness.worker("ABC-1")


def test_a_hook_is_logged_with_its_worker_and_session(harness, daemon, caplog):
    worker = launch(harness)
    caplog.set_level(logging.INFO)

    post(daemon, "Stop", {"session_id": worker.session_uuid})

    assert f"implementer/ABC-1: hook Stop from session {worker.session_uuid[:8]}" in messages(
        caplog, "chargehand.hooks"
    )


def test_a_hook_for_no_live_worker_says_why_with_its_cwd(harness, daemon, caplog):
    caplog.set_level(logging.INFO)

    post(daemon, "Stop", {"session_id": "0123456789abcdef", "cwd": "/nowhere/\x1b[2Jhere"})

    assert messages(caplog, "chargehand.hooks") == [
        "hook Stop from session 01234567: no live worker has this session (cwd /nowhere/here)"
    ]


def test_a_refused_hook_is_logged(harness, daemon, caplog):
    caplog.set_level(logging.INFO)

    assert post(daemon, "Stop", {"session_id": "x"}, headers={"Authorization": "Bearer stale"}) == 401

    assert messages(caplog, "chargehand.serve") == [
        "POST /hooks/Stop refused: 401 a valid bearer token is required"
    ]


# ----- the daemon's report and heartbeat ------------------------------------------------


def test_the_daemon_logs_its_report_through_the_tick_logger_with_levels(caplog):
    caplog.set_level(logging.DEBUG)
    report = TickReport(launched=["implementer/ABC-1"], warnings=["w"], errors=["e"],
                        routed=["fake: 1 event(s)"])

    cli._log_report(report)

    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("chargehand.tick", logging.INFO, "launched: implementer/ABC-1"),
        ("chargehand.tick", logging.INFO, "routed: fake: 1 event(s)"),
        ("chargehand.tick", logging.WARNING, "warning: w"),
        ("chargehand.tick", logging.ERROR, "error: e"),
    ]


def test_an_idle_daemon_at_info_writes_no_line(harness, daemon, caplog):
    harness.tick()
    caplog.clear()
    caplog.set_level(logging.INFO)
    ledger = Ledger(daemon.ledger_path)
    try:
        assert daemon.tick_once(ledger)
        assert daemon.tick_once(ledger)
    finally:
        ledger.close()

    assert messages(caplog, level=logging.INFO) == []
    daemon.polls.flush()
    assert any("poll(s) found nothing new" in m for m in messages(caplog, "chargehand.poll"))


def test_the_heartbeat_is_debug_and_warns_for_a_slow_or_late_tick(harness, daemon, caplog):
    caplog.set_level(logging.DEBUG, logger="chargehand.serve")
    data = daemon.health_data
    data.ticks = 7
    data.last_tick_started_at, data.last_tick_finished_at = 10_000.0, 10_002.0

    daemon._heartbeat(TickReport(launched=["implementer/ABC-1"]), previous_end=9_999.0)
    data.last_tick_started_at, data.last_tick_finished_at = 20_000.0, 24_000.0
    daemon._heartbeat(TickReport(), previous_end=10_002.0)

    quiet, loud = [r for r in caplog.records if r.name == "chargehand.serve"]
    assert (quiet.levelno, quiet.getMessage()) == (logging.DEBUG, "tick 7 took 2.0s: 1 launched")
    assert loud.levelno == logging.WARNING
    assert loud.getMessage() == (
        "tick 7 took 4000.0s: nothing to do; longer than the 3600s interval; "
        "the previous tick ended 9998s before it"
    )


# ----- install ------------------------------------------------------------------------


def test_the_job_runs_the_daemon_with_its_own_log_and_launchd_output_elsewhere():
    data = install.build_plist()

    argv = data["ProgramArguments"]
    assert argv[argv.index("serve") + 1:] == ["--log-file", str(paths.log_file())]
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(paths.launchd_output_file())
    assert paths.launchd_output_file() != paths.log_file()
    assert not install.is_timer_job(data)
    assert install.writes_own_log(data)


def test_a_job_from_before_the_log_file_is_not_a_timer_but_writes_no_log_of_its_own():
    job = {"ProgramArguments": ["/bin/chargehand", "serve"]}

    assert not install.is_timer_job(job)
    assert not install.writes_own_log(job)


def test_serve_takes_a_log_file():
    args = cli.build_parser().parse_args(["serve", "--log-file", "/tmp/x.log"])
    assert args.log_file == "/tmp/x.log"


# ----- what never reaches the log -----------------------------------------------------


def test_session_text_and_actors_never_reach_either_channel(wired, tmp_path, caplog, capsys):
    """A last answer, a waiting text, a tool input and a comment actor stay out of the log."""
    caplog.set_level(logging.DEBUG)
    wired.board.add("PR-7", labels=())
    fake_source.feed().emit("review_requested", "PR-7", actor=f"{MARKER}@example.invalid")
    wired.tick()
    reviewer = wired.worker("PR-7", "reviewer")
    wired.claude_state.set_session_state("PR-7", "blocked", waitingFor=f"run {MARKER}")
    wired.tick()

    config = support.daemon_config(wired.config, tmp_path / "board.token")
    built = support.build(config, notifier=wired.notifier, report=cli._log_report,
                          interval_secs=3600)
    with support.running(built, loop=False):
        post(built, "PermissionRequest", {
            "session_id": reviewer.session_uuid, "tool_name": "Bash",
            "tool_input": {"command": f"echo {MARKER}"},
        })
        post(built, "Stop", {
            "session_id": reviewer.session_uuid, "last_assistant_message": f"done {MARKER}",
        })
        fake_source.feed().emit("comment", "PR-7", actor=f"{MARKER}@example.invalid")
        ledger = Ledger(built.ledger_path)
        try:
            built.tick_once(ledger)
        finally:
            ledger.close()
        built.polls.flush()

    worker = wired.worker("PR-7", "reviewer")
    assert MARKER in (worker.last_answer or "")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    out = capsys.readouterr()
    for channel in (logged, out.out, out.err):
        assert MARKER not in channel
