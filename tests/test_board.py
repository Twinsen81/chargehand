"""The read-only board: the page, its headers, and the model it renders.

The daemon here serves on 127.0.0.1 on a port the kernel picks, runs no loop, and is
shut down in the test. Sessions are the fake `claude`'s; the tracker is the fake one.
"""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import time

import pytest

from chargehand import board, delivery, ledger as ledger_mod, serve, trackers, usage
from chargehand.claude import ClaudeRuntime
from chargehand.sources import Event, WorkItem
from support import daemon as support
from support import fake_tracker

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}
HOSTILE = "\x1b]0;owned\x07\x1b[2J<img src=x onerror=alert(1)>\u202eevil"


@pytest.fixture
def daemon(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def seed(harness, item, state=ledger_mod.RUNNING, **changes):
    worker = harness.ledger.create_worker(
        role="implementer", item=item, item_id=f"id-{item}", url=f"https://tracker.example/{item}",
        repository="test", repo=harness.repo, worktree=harness.worktree(item),
        branch=f"chargehand/{item}",
    )
    return harness.ledger.update(worker, state=state, **changes)


def model(harness, **kwargs):
    return board.build(harness.config, harness.ledger, health={}, **kwargs)


def cards_by_column(page):
    return {column["key"]: [card["item"] for card in column["cards"]] for column in page["columns"]}


def card_of(page, item):
    return next(card for column in page["columns"] for card in column["cards"] if card["item"] == item)


def get(daemon, path, method="GET"):
    connection = http.client.HTTPConnection(serve.HOST, daemon.port, timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


# ----- columns ----------------------------------------------------------------------


EXPECTED_COLUMN = {
    ledger_mod.BLOCKED: "needs_you",
    ledger_mod.LAUNCHING: "working",
    ledger_mod.RUNNING: "working",
    ledger_mod.QUEUED: "queued",
    ledger_mod.PARKED: "queued",
    ledger_mod.IDLE: "idle",
    ledger_mod.PAUSED: "idle",
    ledger_mod.DONE: "closed",
    ledger_mod.FAILED: "closed",
    ledger_mod.CANCELLED: "closed",
}


def test_every_worker_state_has_one_column():
    assert set(board.COLUMN_OF) == set(ledger_mod.WORKER_STATES)
    assert board.COLUMN_OF == EXPECTED_COLUMN
    assert [key for key, *_ in board.COLUMNS] == ["needs_you", "working", "queued", "idle", "closed"]


def test_each_worker_is_in_the_column_of_its_state(harness):
    for index, state in enumerate(ledger_mod.WORKER_STATES):
        seed(harness, f"ABC-{index}", state)

    page = model(harness)

    columns = cards_by_column(page)
    for index, state in enumerate(ledger_mod.WORKER_STATES):
        assert f"ABC-{index}" in columns[EXPECTED_COLUMN[state]], state
    assert sum(len(items) for items in columns.values()) == len(ledger_mod.WORKER_STATES)


def test_closed_shows_only_the_workers_that_finished_in_the_last_day(harness):
    seed(harness, "ABC-1", ledger_mod.DONE, finished_at=time.time() - 60)
    seed(harness, "ABC-2", ledger_mod.FAILED, finished_at=time.time() - 3 * 24 * 3600)

    assert cards_by_column(model(harness))["closed"] == ["ABC-1"]


def test_the_throttle_counts_working_and_open_workers_against_the_caps(harness):
    seed(harness, "ABC-1", ledger_mod.RUNNING)
    seed(harness, "ABC-2", ledger_mod.LAUNCHING)
    seed(harness, "ABC-3", ledger_mod.IDLE)
    harness.ledger.set_paused(True)

    throttle = model(harness)["throttle"]

    assert throttle == {
        "working": 2, "max_concurrent": harness.config.max_concurrent,
        "open": 3, "max_open_workers": harness.config.max_open_workers,
        "paused": True, "paused_repositories": [],
    }


# ----- card fields -------------------------------------------------------------------


def test_a_card_before_any_wake_shows_the_role_prompt_model_effort_and_attach(harness):
    roles = harness.repo / ".chargehand" / "roles"
    roles.mkdir(parents=True)
    (roles / "implementer.toml").write_text(
        'prompt = "work on {item} ({item_url})"\nmodel = "opus"\neffort = "high"\n'
        "[policy]\nmax_wakes_per_day = 7\n"
    )
    worker = seed(harness, "ABC-1", ledger_mod.RUNNING, session_id="sess-1")

    card = card_of(model(harness, attach=lambda w: f"attach {w.session_id}"), "ABC-1")

    assert card["role"] == "implementer"
    assert card["item_url"] == "https://tracker.example/ABC-1"
    assert card["branch"] == "chargehand/ABC-1"
    assert card["worktree"] == worker.worktree
    assert (card["model"], card["effort"]) == ("opus", "high")
    assert card["last_prompt"] == "work on ABC-1 (https://tracker.example/ABC-1)"
    assert (card["wakes_today"], card["wake_cap"]) == (0, 7)
    assert card["attach"] == "attach sess-1"
    assert card["tokens_estimate"] is None
    assert card["last_answer"] is None


def test_a_card_after_a_wake_shows_the_wake_message_of_that_wake(harness):
    worker = seed(harness, "ABC-1", ledger_mod.IDLE, session_id="sess-1")
    item = WorkItem("fake", "ABC-1", "id-ABC-1")
    for dedupe, kind, pointer in (
        ("old", "comment", "https://example.invalid/old"),
        ("c1", "comment", "https://example.invalid/c1"),
        ("r1", "review_requested", "https://example.invalid/pr/1"),
    ):
        harness.ledger.enqueue(worker.id, Event(
            source="fake", kind=kind, item=item, actor="someone", occurred_at=time.time(),
            dedupe_id=dedupe, pointer=pointer,
        ))
    old, *last = harness.ledger.inbox(worker)
    earlier, wake_at = time.time() - 7200, time.time() - 60
    harness.ledger.mark_delivered([old], now=earlier)
    harness.ledger.mark_delivered(last, now=wake_at)
    for at in (time.time() - 2 * 24 * 3600, earlier, wake_at):
        harness.ledger.record_wake(worker, session_id="sess-1", session_uuid=None, now=at)
    harness.ledger.update(worker, wakes=3, last_wake_at=wake_at)

    card = card_of(model(harness), "ABC-1")

    expected, _ = delivery.wake_message(last)
    assert card["last_prompt"] == expected
    assert "comment: https://example.invalid/c1" in card["last_prompt"]
    assert "review_requested: https://example.invalid/pr/1" in card["last_prompt"]
    assert "old" not in card["last_prompt"]
    # Two wakes in the window of the daily cap; the one from two days ago is outside it.
    assert card["wakes_today"] == 2


def test_hook_records_reach_the_card(daemon, harness, tmp_path):
    harness.board.add("ABC-1")
    harness.tick()
    worker = harness.worker("ABC-1")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"x" * 4000)

    status, _ = support.request(
        daemon.port, "POST", "/hooks/Stop",
        body=json.dumps({
            "session_id": worker.session_uuid, "transcript_path": str(transcript),
            "last_assistant_message": "Opened the pull request.",
        }).encode(),
        headers=AUTH,
    )
    assert status == 200

    status, raw = support.request(daemon.port, "GET", "/api/board")
    assert status == 200
    card = card_of(json.loads(raw), "ABC-1")
    assert card["last_answer"] == "Opened the pull request."
    assert card["tokens_estimate"] == 1000
    assert card["attach"] == f"claude attach {worker.session_id}"
    assert card["column"] == "idle"


def test_an_open_permission_prompt_is_on_the_card(harness):
    worker = seed(harness, "ABC-1", ledger_mod.BLOCKED)
    harness.ledger.record_permission_prompt(worker, tool_name="Bash", tool_input='{"command": "ls"}')

    card = card_of(model(harness), "ABC-1")

    assert card["column"] == "needs_you"
    assert card["permission_prompt"] == {"tool_name": "Bash", "tool_input": '{"command": "ls"}'}


def test_a_parked_worker_is_queued_with_its_reset_time(harness):
    reset = time.time() + 3600
    seed(harness, "ABC-1", ledger_mod.PARKED, reset_at=reset)

    card = card_of(model(harness), "ABC-1")

    assert card["column"] == "queued"
    assert card["reset_at"] == reset


def test_the_token_estimate_needs_a_regular_file(tmp_path):
    assert board.tokens_estimate(None) is None
    assert board.tokens_estimate("relative/transcript.jsonl") is None
    assert board.tokens_estimate(str(tmp_path)) is None
    assert board.tokens_estimate(str(tmp_path / "missing.jsonl")) is None
    (tmp_path / "t.jsonl").write_bytes(b"x" * 10)
    assert board.tokens_estimate(str(tmp_path / "t.jsonl")) == 2


# ----- untrusted text -----------------------------------------------------------------


def test_untrusted_text_never_reaches_the_output_unescaped_or_as_a_link(daemon, harness):
    worker = seed(
        harness, "ABC-1", ledger_mod.PAUSED,
        last_answer=HOSTILE, waiting_for=HOSTILE, last_error=HOSTILE,
        pr_url="javascript:alert(1)", branch=f"chargehand/{HOSTILE}",
    )
    harness.ledger.update(worker, url="javascript:alert(document.cookie)")
    harness.ledger.record_permission_prompt(worker, tool_name=HOSTILE, tool_input=HOSTILE)
    harness.ledger.enqueue(worker.id, Event(
        source="fake", kind="comment", item=WorkItem("fake", "ABC-1", "id-ABC-1"),
        actor="x", occurred_at=time.time(), dedupe_id="e1", pointer="javascript:alert(1)",
    ))

    status, headers, raw = get(daemon, "/api/board")

    assert status == 200
    text = raw.decode("utf-8")
    for forbidden in ("\x1b", "\x07", "\u202e", "<img", "<", ">"):
        assert forbidden not in text
    card = card_of(json.loads(text), "ABC-1")
    assert card["item_url"] is None
    assert card["pr_url"] is None
    assert card["last_answer"] == "<img src=x onerror=alert(1)>evil"
    # No string on the card kept a control sequence or a direction override.
    assert all("\x1b" not in s and "\u202e" not in s for s in _strings(card))
    links = [card["item_url"], card["pr_url"]]
    assert all(link is None or link.startswith(("http://", "https://")) for link in links)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_page_renders_text_only_and_links_only_plain_urls():
    script = (board.asset_dir() / "board.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "new Function", "setAttribute('style'", 'setAttribute("style"'):
        assert sink not in script, sink
    assert "rel = 'noopener noreferrer'" in script
    # The one place that sets an href checks the scheme first.
    assert script.count(".href =") == 1
    assert re.search(r"/\^https\?:\\/\\/", script)


# ----- the page and its headers --------------------------------------------------------


def test_the_page_is_sent_with_a_strict_content_security_policy(daemon):
    for method in ("GET", "HEAD"):
        status, headers, body = get(daemon, "/", method)

        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Content-Security-Policy"] == board.CSP
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert (body == b"") == (method == "HEAD")
    for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                      "connect-src 'self'", "frame-ancestors 'none'", "form-action 'none'"):
        assert directive in board.CSP


def test_the_assets_are_served_with_their_types(daemon):
    for path, kind in (("/board.js", "text/javascript"), ("/board.css", "text/css")):
        status, headers, body = get(daemon, path)
        assert status == 200
        assert headers["Content-Type"].startswith(kind)
        assert headers["Content-Security-Policy"] == board.CSP
        assert body
    assert get(daemon, "/board/../ledger.sqlite")[0] == 404
    assert get(daemon, "/index.html")[0] == 404


def test_the_page_loads_nothing_from_the_network_and_has_no_inline_code():
    for name in ("index.html", "board.js", "board.css"):
        text = (board.asset_dir() / name).read_text()
        assert "http://" not in text and "https://" not in text, name
        assert "@import" not in text and "url(" not in text, name
    page = (board.asset_dir() / "index.html").read_text()
    assert re.findall(r"<script[^>]*>", page) == ['<script src="/board.js" defer>']
    assert "<style" not in page and "style=" not in page
    assert re.search(r"\son[a-z]+=", page) is None


def test_the_board_is_read_only(daemon):
    board_routes = [
        (method, path) for method, path, _, handler in daemon.routes._routes
        if getattr(handler, "__module__", "") == board.__name__
    ]
    assert board_routes and all(method == "GET" for method, _ in board_routes)
    script = (board.asset_dir() / "board.js").read_text()
    assert re.findall(r"fetch\('([^']*)'", script) == ["/api/board"]
    assert "method" not in script
    assert "Authorization" not in script
    status, _ = support.request(daemon.port, "POST", "/", body=b"{}", headers=AUTH)
    assert status == 405


# ----- usage --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "used, level",
    [(0, "green"), (59.9, "green"), (60, "amber"), (79.9, "amber"), (80, "red"), (100, "red")],
)
def test_the_usage_gauges_take_their_level_from_the_usage_file(harness, used, level):
    now = time.time()
    usage.write_usage(harness.config.serve.usage_file, {
        "five_hour": {"used_percent": used, "resets_at": now + 3600},
        "seven_day": {"used_percent": 10, "resets_at": now + 86400},
    }, now=now)

    reading = model(harness)["usage"]

    assert reading["known"] is True
    assert reading["five_hour"]["level"] == level
    assert reading["five_hour"]["resets_at"] == pytest.approx(now + 3600)
    assert reading["seven_day"]["level"] == "green"


def test_the_usage_gauges_are_grey_without_a_reading_or_with_a_stale_one(harness):
    page = model(harness)
    assert page["usage"]["known"] is False
    assert {page["usage"][w]["level"] for w in usage.WINDOWS} == {"grey"}

    old = time.time() - delivery.USAGE_MAX_AGE_SECS - 60
    usage.write_usage(harness.config.serve.usage_file, {
        "five_hour": {"used_percent": 90, "resets_at": time.time() + 3600},
    }, now=old)
    page = model(harness)
    assert page["usage"]["known"] is False
    assert page["usage"]["five_hour"]["level"] == "grey"


# ----- no calls per request ----------------------------------------------------------


def test_a_board_request_calls_no_tracker_and_no_runtime(daemon, harness, monkeypatch):
    harness.board.add("ABC-1")
    harness.tick()

    def refuse(*args, **kwargs):
        raise AssertionError("the board made an external call")

    monkeypatch.setattr(trackers, "build", refuse)
    monkeypatch.setattr(fake_tracker.FakeTracker, "list", refuse)
    monkeypatch.setattr(ClaudeRuntime, "list_sessions", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    status, raw = support.request(daemon.port, "GET", "/api/board")

    assert status == 200
    assert card_of(json.loads(raw), "ABC-1")["column"] == "working"


def test_the_health_reports_the_last_tick(harness):
    page = board.build(harness.config, harness.ledger, health={
        "version": "1.2.3", "started_at": 100.0, "last_tick_started_at": 200.0,
        "last_tick_finished_at": 201.5, "last_tick_error": "boom\x1b[31m", "failed_ticks": 1,
    })

    assert page["health"]["last_tick_secs"] == 1.5
    assert page["health"]["last_tick_at"] == 201.5
    assert page["health"]["last_tick_error"] == "boom"
    assert page["health"]["version"] == "1.2.3"
