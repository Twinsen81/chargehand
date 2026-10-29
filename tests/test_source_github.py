"""The github source and the github tracker, against the fake gh."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import Harness, make_config
from chargehand import sanitize
from chargehand import sources as sources_mod
from chargehand import trackers as trackers_mod
from chargehand.config import Labels, MachineConfig, Repository, SourceConfig, TrackerConfig
from chargehand.errors import AmbiguousWrite, ConfigError, TrackerAuthError, TrackerError
from chargehand.gitutil import Git
from chargehand.ledger import Ledger
from chargehand.notify import Notifier
from chargehand.sources import AUTHORSHIP_MARKER, WorkItem
from chargehand.sources.github import GithubSource, GithubTracker, item_key, make_ref, parse_ref
from chargehand.tick import Runner
from chargehand.trackers import Status
from support.fake_gh import GhState

FAKE_GH = Path(__file__).parent / "support" / "fake_gh.py"
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc).timestamp()
PR = "octo/app#7"
PR_KEY = "octo_app_7"
LABELS = {"queued": "chargehand", "running": "chargehand-running", "blocked": "chargehand-blocked"}


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def gh(tmp_path, monkeypatch) -> GhState:
    path = tmp_path / "gh-state.json"
    monkeypatch.setenv("FAKE_GH_STATE", str(path))
    return GhState(path)


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def source(gh, clock) -> GithubSource:
    built = sources_mod.build(SourceConfig("github", "github", {"gh_bin": str(FAKE_GH)}))
    assert isinstance(built, GithubSource)
    built.clock = clock
    gh.now = clock.now
    return built


def followed(ref: str = PR) -> list[WorkItem]:
    parsed = parse_ref(ref)
    assert parsed is not None
    return [WorkItem("github", item_key(parsed), ref)]


def api_paths(gh: GhState) -> list[str]:
    return [
        next(arg for arg in call[1:] if not arg.startswith("-") and arg != "GET")
        for call in gh.calls
        if call[0] == "api"
    ]


def summary(events):
    return [(e.kind, e.item.key, e.dedupe_id, e.actor, e.marked) for e in events]


# ----- names ---------------------------------------------------------------------


def test_item_keys_are_path_safe_unique_and_case_insensitive():
    keys = {
        item_key(make_ref(owner, repo, 7))  # type: ignore[arg-type]
        for owner, repo in [("a-b", "c"), ("a", "b-c"), ("a", "b_c"), ("a_b", "c"), ("a", "b.c")]
    }

    assert len(keys) == 5
    assert all(sanitize.is_safe_identifier(key) for key in keys)
    assert item_key(make_ref("Octo", "App", 7)) == item_key(make_ref("octo", "app", 7)) == PR_KEY  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "owner, repo",
    [("o" * 39, "r" * 100), ("octo", "dots..dots"), ("octo", "x.lock"), ("user_emu", "app")],
)
def test_names_that_do_not_fit_the_plain_form_get_a_digest(owner, repo):
    key = item_key(make_ref(owner, repo, 123456))  # type: ignore[arg-type]

    assert sanitize.is_safe_identifier(key) and len(key) <= 64
    assert ".." not in key and not key.endswith(".lock")
    assert key != item_key(make_ref(owner, repo, 123457))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "owner, repo, number",
    [("../evil", "app", 1), ("octo", "a b", 1), ("octo", "..", 1), ("-x", "app", 1),
     ("octo", "app;rm", 1), ("octo", "app", 0), ("octo", "app", True), ("octo", "a/b", 1)],
)
def test_hostile_names_are_refused(owner, repo, number):
    assert make_ref(owner, repo, number) is None


# ----- notifications -------------------------------------------------------------


def test_notifications_become_events(source, gh):
    gh.add_pull(PR)
    gh.notify("review_requested", PR)
    gh.notify("mention", "octo/app#9", kind="Issue")
    gh.notify("ci_activity", PR)
    gh.notify("subscribed", "octo/app#10")
    gh.update(lambda s: s["notifications"].append(
        {"id": "99", "reason": "ci_activity", "updated_at": "2026-10-26T10:00:00Z",
         "subject": {"type": "CheckSuite", "url": None},
         "repository": {"full_name": "octo/app"}}
    ))

    events, cursor = source.poll(None, [])

    assert [(e.kind, e.item.key, e.item.ref, e.item.hint, e.item.url, e.actor) for e in events] == [
        ("review_requested", PR_KEY, PR, "octo/app", "https://github.com/octo/app/pull/7", ""),
        ("mention", "octo_app_9", "octo/app#9", "octo/app", "https://github.com/octo/app/issues/9", ""),
        ("ci_status", PR_KEY, PR, "octo/app", "https://github.com/octo/app/pull/7", ""),
    ]
    assert events[0].dedupe_id == "notification:1:2026-10-26T10:00:00Z"
    assert events[0].pointer == "https://github.com/octo/app/pull/7"
    assert json.loads(cursor)["last_modified"] == gh.read()["last_modified"]


def test_hostile_repository_names_in_notifications_are_skipped(source, gh):
    gh.add_pull(PR)
    gh.notify("review_requested", PR)
    gh.update(lambda s: s["notifications"].append(
        {"id": "66", "reason": "review_requested", "updated_at": "2026-10-26T10:00:00Z",
         "subject": {"type": "PullRequest",
                     "url": "https://api.github.com/repos/..%2F/x y/pulls/1"},
         "repository": {"full_name": "../x y"}}
    ))

    events, _ = source.poll(None, [])

    assert [e.item.key for e in events] == [PR_KEY]


def test_an_unchanged_list_is_a_304_and_no_events(source, gh, clock):
    gh.notify("review_requested", PR)
    _, cursor = source.poll(None, [])
    clock.now += 61

    events, cursor = source.poll(cursor, [])

    assert events == []
    last = gh.calls[-1]
    assert last[:2] == ["api", "-i"] and f"If-Modified-Since: {gh.read()['last_modified']}" in last
    assert json.loads(cursor)["next_poll_at"] == clock.now + 60


def test_the_poll_interval_is_obeyed(source, gh, clock):
    gh.set(poll_interval=120)
    gh.add_pull(PR)
    gh.notify("review_requested", PR)
    _, cursor = source.poll(None, [])
    gh.clear_calls()

    clock.now += 119
    events, early = source.poll(cursor, [])
    assert events == [] and gh.calls == []
    assert json.loads(early)["next_poll_at"] == json.loads(cursor)["next_poll_at"] == NOW + 120

    clock.now += 1
    gh.notify("mention", "octo/app#9", kind="Issue")
    events, _ = source.poll(cursor, [])
    # The overlap repeats the earlier notification with its old dedupe id; the router
    # drops it as seen.
    assert [(e.kind, e.dedupe_id) for e in events] == [
        ("review_requested", "notification:1:2026-10-26T10:00:00Z"),
        ("mention", "notification:2:2026-10-26T10:00:00Z"),
    ]
    # The second poll asks only for what changed since the list was last modified.
    assert [p for p in api_paths(gh) if p.startswith("notifications")][-1].count("since=") == 1


def test_a_followed_item_gets_only_review_requests_from_notifications(source, gh):
    gh.add_pull(PR)
    gh.add_pull("octo/app#8")
    gh.notify("comment", PR, thread="1")
    gh.notify("review_requested", "octo/app#8", thread="2")
    gh.notify("review_requested", PR, thread="3")

    events, _ = source.poll(None, followed())

    assert [(e.kind, e.item.key) for e in events] == [
        ("review_requested", "octo_app_8"),
        ("review_requested", PR_KEY),
    ]


def test_a_review_request_counts_only_while_it_is_still_asked_of_me(source, gh):
    gh.set(teams=[100])
    gh.add_pull("octo/app#1")
    gh.add_pull("octo/app#2", reviewers=("someone-else",))
    gh.add_pull("octo/app#3", reviewers=(), teams=(100,))
    gh.add_pull("octo/app#4", state="closed")
    gh.add_pull("octo/app#6", reviewers=(), teams=(200,))
    for number in range(1, 7):
        gh.notify("review_requested", f"octo/app#{number}")

    events, _ = source.poll(None, [])

    assert [e.item.ref for e in events] == ["octo/app#1", "octo/app#3"]
    # The account's teams are listed once per source, not once per notification.
    assert api_paths(gh).count("user/teams?per_page=100") == 1


def test_any_requested_team_counts_when_the_teams_cannot_be_listed(source, gh):
    gh.add_pull("octo/app#6", reviewers=(), teams=(200,))
    gh.notify("review_requested", "octo/app#6")

    events, _ = source.poll(None, [])

    assert [e.item.ref for e in events] == ["octo/app#6"]


def test_a_thread_read_before_the_poll_is_not_lost(source, gh, clock):
    gh.add_pull(PR)
    gh.notify("mention", "octo/app#9", kind="Issue")
    gh.notify("review_requested", "octo/app#5", unread=False)
    _, cursor = source.poll(None, [])
    # The first poll reads only the unread threads, so it does not replay the history.
    assert "all=true" not in api_paths(gh)[0]

    # The operator opens the review request in a browser before the next poll.
    gh.notify("review_requested", PR, updated_at="2026-10-26T10:05:00Z", unread=False)
    clock.now += 61
    events, _ = source.poll(cursor, [])

    assert ("review_requested", PR_KEY) in [(e.kind, e.item.key) for e in events]
    assert "all=true" in [p for p in api_paths(gh) if p.startswith("notifications")][-1]


def notification_calls(gh: GhState) -> list[list[str]]:
    return [
        call for call in gh.calls
        if call[:2] == ["api", "-i"] and any(arg.startswith("notifications") for arg in call)
    ]


def test_an_empty_list_is_asked_with_its_etag_and_ends_the_first_poll(source, gh, clock):
    gh.set(date="Mon, 26 Oct 2026 10:00:30 GMT")
    events, cursor = source.poll(None, [])

    # An empty list has an ETag but no Last-Modified.
    state = json.loads(cursor)
    assert events == [] and state["last_modified"] is None and state["etag"]
    # The first poll is over all the same: the next one starts from the server's Date.
    assert state["listed_at"] == datetime(2026, 10, 26, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    # A 200 would take this Date; the 304 keeps the window of the first poll.
    gh.set(date="Mon, 26 Oct 2026 10:01:30 GMT")
    clock.now += 61
    events, cursor = source.poll(cursor, [])

    assert json.loads(cursor)["listed_at"] == state["listed_at"]
    second = notification_calls(gh)[-1]
    assert f"If-None-Match: {state['etag']}" in second
    assert not any(arg.startswith("If-Modified-Since") for arg in second)
    assert "all=true" in api_paths(gh)[-1] and "since=2026-10-26T09:55:30Z" in api_paths(gh)[-1]
    assert events == [] and json.loads(cursor)["etag"] == state["etag"]
    assert json.loads(cursor)["next_poll_at"] == clock.now + 60

    # A thread that the operator read before this poll still arrives.
    gh.add_pull(PR)
    gh.notify("review_requested", PR, updated_at="2026-10-26T10:01:00Z", unread=False)
    clock.now += 61
    events, cursor = source.poll(cursor, [])

    assert [(e.kind, e.item.key) for e in events] == [("review_requested", PR_KEY)]
    assert json.loads(cursor)["last_modified"] == gh.read()["last_modified"]


def test_a_cursor_from_before_the_etag_keeps_its_window(source, gh, clock):
    old = json.dumps({
        "last_modified": "Mon, 26 Oct 2026 10:00:00 GMT", "poll_interval": 60,
        "next_poll_at": 0, "polled_at": NOW - 60, "items": {},
    })
    gh.notify("review_requested", PR)

    source.poll(old, [])

    assert "since=2026-10-26T09:55:00Z" in api_paths(gh)[0]
    assert "If-Modified-Since: Mon, 26 Oct 2026 10:00:00 GMT" in notification_calls(gh)[0]


# ----- review requests without a notification thread --------------------------------


OLD_REQUEST = "2026-10-26T09:00:00Z"


def review_warnings(source, gh, cursor=None):
    _, cursor = source.poll(cursor, [])
    return source.take_warnings(), cursor


def test_a_review_request_without_a_thread_warns_once_a_day(source, gh, clock):
    gh.add_pull(PR, updated_at=OLD_REQUEST, created_at="2026-10-20T08:00:00Z")

    warnings, cursor = review_warnings(source, gh)

    assert len(warnings) == 1
    assert PR_KEY in warnings[0] and '"On GitHub" setting' in warnings[0]
    search = next(call for call in gh.calls if any("search/issues" in arg for arg in call))
    assert "--paginate" in search
    # The threads are read back to the pull request's creation, read ones included.
    listed = [p for p in api_paths(gh) if p.startswith("notifications") and "since=2026-10-20T07:55" in p]
    assert listed and "all=true" in listed[0]

    gh.clear_calls()
    clock.now += 23 * 3600
    warnings, cursor = review_warnings(source, gh, cursor)
    assert warnings == [] and not any("search/issues" in p for p in api_paths(gh))

    clock.now += 3600
    warnings, _ = review_warnings(source, gh, cursor)
    assert len(warnings) == 1


def test_the_printed_warning_keeps_the_hint_with_long_keys(source, gh, capsys):
    from chargehand import cli
    from chargehand.tick import TickReport

    for number in (101, 102, 103):
        gh.add_pull(f"octo/{'long-repository-name-' * 4}{number}#{number}", updated_at=OLD_REQUEST)
    warnings, _ = review_warnings(source, gh)
    assert len(warnings) == 1 and len(warnings[0]) > 200

    cli._print_report(TickReport(warnings=warnings))

    line = capsys.readouterr().out.strip()
    assert line.endswith("…")
    assert 'check that GitHub\'s "On GitHub" setting for participating notifications is on' in line


def test_a_review_request_with_a_thread_does_not_warn(source, gh):
    gh.add_pull(PR, updated_at=OLD_REQUEST)
    gh.notify("review_requested", PR, updated_at=OLD_REQUEST, unread=False)

    warnings, _ = review_warnings(source, gh)

    assert warnings == []


def test_a_young_review_request_does_not_warn(source, gh, clock):
    gh.add_pull(PR, updated_at=iso(NOW - 5 * 60))

    warnings, _ = review_warnings(source, gh)

    assert warnings == []
    # Nothing to compare, so the threads are not read for the check.
    assert not any("all=true" in p for p in api_paths(gh) if p.startswith("notifications"))


def test_a_long_thread_list_is_read_in_a_few_pages_and_old_requests_are_not_guessed(
    source, gh, monkeypatch
):
    from chargehand.sources import github as github_mod

    monkeypatch.setattr(github_mod, "REVIEW_CHECK_PAGE_SIZE", 2)
    monkeypatch.setattr(github_mod, "REVIEW_CHECK_MAX_PAGES", 1)
    # Requested before the oldest thread that one page holds: not known either way.
    gh.add_pull("octo/app#1", created_at="2026-10-26T08:00:00Z", updated_at=OLD_REQUEST)
    # Requested after it: its thread would be on the page, so it has none.
    gh.add_pull("octo/app#2", created_at="2026-10-26T09:20:00Z", updated_at="2026-10-26T09:30:00Z")
    for number, at in ((10, "2026-10-26T08:30:00Z"), (11, "2026-10-26T09:10:00Z"),
                       (12, "2026-10-26T09:40:00Z")):
        gh.notify("mention", f"octo/app#{number}", kind="Issue", updated_at=at, unread=False)

    warnings, _ = review_warnings(source, gh)

    assert len(warnings) == 1 and "octo_app_2" in warnings[0] and "octo_app_1" not in warnings[0]
    pages = [p for p in api_paths(gh) if p.startswith("notifications") and "&page=" in p]
    assert len(pages) == 1 and "per_page=2&page=1" in pages[0]


def test_a_thread_on_a_later_page_counts(source, gh, monkeypatch):
    from chargehand.sources import github as github_mod

    monkeypatch.setattr(github_mod, "REVIEW_CHECK_PAGE_SIZE", 2)
    monkeypatch.setattr(github_mod, "REVIEW_CHECK_MAX_PAGES", 3)
    gh.add_pull(PR, created_at="2026-10-26T08:00:00Z", updated_at=OLD_REQUEST)
    # Newest first: the two mentions fill page 1, the review request is on page 2.
    gh.notify("review_requested", PR, updated_at="2026-10-26T08:10:00Z", unread=False)
    for number, at in ((10, "2026-10-26T09:10:00Z"), (11, "2026-10-26T09:40:00Z")):
        gh.notify("mention", f"octo/app#{number}", kind="Issue", updated_at=at, unread=False)

    warnings, _ = review_warnings(source, gh)

    assert warnings == []
    pages = [p for p in api_paths(gh) if p.startswith("notifications") and "&page=" in p]
    assert [p.split("&page=")[1].split("&")[0] for p in pages] == ["1", "2"]


def test_a_failed_review_request_check_does_not_fail_the_poll(source, gh):
    gh.add_pull(PR, updated_at=OLD_REQUEST)
    gh.notify("review_requested", PR)
    gh.set(fail=[{"match": "search/issues", "stderr": "HTTP 403: rate limit"}])

    events, _ = source.poll(None, [])

    assert [e.kind for e in events] == ["review_requested"]
    assert source.take_warnings() == []


# ----- per pull request ------------------------------------------------------------


def seed_pull(gh, *, at: float, state="open", sha="abc1234"):
    gh.add_pull(PR, sha=sha, state=state, closed_at=iso(at) if state == "closed" else None,
                merged_by={"login": "maintainer"} if state == "closed" else None)
    gh.add("issue_comments", PR, {"id": 11, "user": {"login": "alice"}, "body": "looks odd",
                                  "created_at": iso(at), "updated_at": iso(at),
                                  "html_url": "https://github.com/octo/app/pull/7#issuecomment-11"})
    gh.add("review_comments", PR, {"id": 12, "user": {"login": "operator"},
                                   "body": f"fixed {AUTHORSHIP_MARKER}",
                                   "created_at": iso(at), "updated_at": iso(at)})
    gh.add("reviews", PR, {"id": 13, "user": {"login": "bob"}, "body": "", "state": "APPROVED",
                           "submitted_at": iso(at)})
    gh.add("reviews", PR, {"id": 14, "user": {"login": "operator"}, "state": "PENDING"})
    gh.add("statuses", sha, {"id": 15, "state": "failure", "context": "ci/build",
                             "created_at": iso(at), "updated_at": iso(at),
                             "creator": {"login": "ci-bot"}})
    gh.add("statuses", sha, {"id": 16, "state": "pending", "created_at": iso(at)})
    gh.add("check_runs", sha, {"id": 17, "status": "completed", "conclusion": "success",
                               "completed_at": iso(at), "app": {"slug": "actions"},
                               "html_url": "https://github.com/octo/app/runs/17"})
    gh.add("check_runs", sha, {"id": 18, "status": "in_progress", "completed_at": None})


def test_a_followed_pull_request_gives_every_kind(source, gh, clock):
    _, cursor = source.poll(None, [])
    seed_pull(gh, at=NOW + 30, state="closed")
    clock.now += 60

    events, _ = source.poll(cursor, followed())

    assert sorted(summary(events)) == sorted([
        ("comment", PR_KEY, "review_comment:12", "operator", True),
        ("comment", PR_KEY, "issue_comment:11", "alice", False),
        ("review", PR_KEY, "review:13", "bob", False),
        ("ci_status", PR_KEY, "status:15", "ci-bot", False),
        ("ci_status", PR_KEY, f"check_run:17:{iso(NOW + 30)}", "actions", False),
        ("pr_closed", PR_KEY, f"pr_closed:{PR_KEY}:{iso(NOW + 30)}", "maintainer", False),
    ])
    by_id = {e.dedupe_id: e for e in events}
    assert by_id["issue_comment:11"].pointer.endswith("#issuecomment-11")
    assert by_id[f"check_run:17:{iso(NOW + 30)}"].pointer == "https://github.com/octo/app/runs/17"
    assert all(e.item.ref == PR and e.item.hint == "octo/app" for e in events)
    # The head commit's statuses and check runs are read by its sha.
    assert "repos/octo/app/commits/abc1234/check-runs?per_page=100" in api_paths(gh)


def test_dedupe_ids_are_stable_across_polls(source, gh, clock):
    _, cursor = source.poll(None, [])
    seed_pull(gh, at=NOW + 30)
    clock.now += 60
    first, cursor = source.poll(cursor, followed())
    clock.now += 60

    second, _ = source.poll(cursor, followed())

    assert {e.dedupe_id for e in first} == {e.dedupe_id for e in second} != set()


def test_what_came_before_a_new_follower_is_not_news(source, gh, clock):
    _, cursor = source.poll(None, [])
    seed_pull(gh, at=NOW - 3600)
    clock.now += 60

    events, cursor = source.poll(cursor, followed())
    assert events == []

    gh.add("issue_comments", PR, {"id": 21, "user": {"login": "alice"}, "body": "and now?",
                                  "created_at": iso(clock.now + 10)})
    clock.now += 60
    events, cursor = source.poll(cursor, followed())
    assert summary(events) == [("comment", PR_KEY, "issue_comment:21", "alice", False)]
    # Comments are asked for from the item's previous read on, less the clock margin.
    assert any("issues/7/comments" in p and "since=" in p for p in api_paths(gh))
    # An item no longer followed is dropped from the cursor.
    _, cursor = source.poll(cursor, [])
    assert json.loads(cursor)["items"] == {}


def test_every_page_is_read(source, gh, clock):
    _, cursor = source.poll(None, [])
    gh.set(page_size=2)
    gh.add_pull(PR)
    for comment_id in range(5):
        gh.add("issue_comments", PR, {"id": comment_id, "user": {"login": "a"},
                                      "created_at": iso(NOW + 10)})
    clock.now += 60

    events, _ = source.poll(cursor, followed())

    assert sorted(e.dedupe_id for e in events) == [f"issue_comment:{i}" for i in range(5)]


def test_a_followed_issue_gives_its_comments(source, gh, clock):
    _, cursor = source.poll(None, [])
    gh.add_issue("octo/app#9")
    gh.add("issue_comments", "octo/app#9", {"id": 31, "user": {"login": "a"},
                                            "created_at": iso(NOW + 10)})
    clock.now += 60

    events, _ = source.poll(cursor, followed("octo/app#9"))

    assert [(e.kind, e.item.url) for e in events] == [
        ("comment", "https://github.com/octo/app/issues/9")
    ]


def test_a_followed_item_that_is_gone_gives_nothing(source, gh):
    assert source.poll(None, followed())[0] == []


def test_a_review_without_a_body_is_marked_by_its_marked_inline_comments(source, gh, clock):
    _, cursor = source.poll(None, [])
    gh.add_pull(PR)
    gh.add("reviews", PR, {"id": 51, "user": {"login": "operator"}, "body": "",
                           "state": "COMMENTED", "submitted_at": iso(NOW + 10)})
    gh.add("review_comments", PR, {"id": 52, "pull_request_review_id": 51,
                                   "user": {"login": "operator"},
                                   "body": f"nit {AUTHORSHIP_MARKER}", "created_at": iso(NOW + 10)})
    gh.add("reviews", PR, {"id": 53, "user": {"login": "operator"}, "body": "",
                           "state": "APPROVED", "submitted_at": iso(NOW + 10)})
    clock.now += 60

    events, _ = source.poll(cursor, followed())

    marked = {e.dedupe_id: e.marked for e in events}
    assert marked == {"review:51": True, "review_comment:52": True, "review:53": False}


def test_one_unreadable_item_does_not_hold_back_the_others(source, gh, clock):
    _, cursor = source.poll(None, [])
    gh.add_pull(PR)
    gh.add_pull("octo/app#8")
    gh.add("issue_comments", PR, {"id": 61, "user": {"login": "a"}, "created_at": iso(NOW + 10)})
    gh.add("issue_comments", "octo/app#8", {"id": 62, "user": {"login": "a"},
                                            "created_at": iso(NOW + 10)})
    gh.set(fail=[{"match": "pulls/8", "stderr": "Resource protected by SAML (HTTP 403)"}])
    gh.notify("mention", "octo/app#9", kind="Issue")
    clock.now += 61

    events, cursor = source.poll(cursor, followed() + followed("octo/app#8"))

    assert sorted(e.dedupe_id for e in events) == [
        "issue_comment:61", "notification:1:2026-10-26T10:00:00Z",
    ]
    # The failed item keeps its old time and is read from there once it recovers.
    times = json.loads(cursor)["items"]
    assert times[PR_KEY] == clock.now and times["octo_app_8"] == NOW
    gh.set(fail=[])
    clock.now += 60
    events, _ = source.poll(cursor, followed() + followed("octo/app#8"))
    assert "issue_comment:62" in {e.dedupe_id for e in events}


def test_one_unreadable_notification_thread_does_not_hold_back_the_others(source, gh, clock):
    _, cursor = source.poll(None, [])
    before = json.loads(cursor)["last_modified"]
    gh.add_pull(PR)
    gh.add_pull("octo/app#8")
    gh.add("issue_comments", PR, {"id": 61, "user": {"login": "a"}, "created_at": iso(NOW + 10)})
    gh.set(fail=[{"match": "pulls/8", "stderr": "HTTP 502: Bad Gateway"}])
    gh.notify("review_requested", PR, thread="1")
    gh.notify("review_requested", "octo/app#8", thread="2")
    gh.notify("mention", "octo/app#9", kind="Issue", thread="3")
    clock.now += 61

    events, cursor = source.poll(cursor, followed())

    assert sorted(e.dedupe_id for e in events) == [
        "issue_comment:61",
        "notification:1:2026-10-26T10:00:00Z",
        "notification:3:2026-10-26T10:00:00Z",
    ]
    # The list keeps its old Last-Modified, so the next poll asks for the same window.
    assert json.loads(cursor)["last_modified"] == before
    gh.set(fail=[])
    clock.now += 61
    events, cursor = source.poll(cursor, followed())
    assert "notification:2:2026-10-26T10:00:00Z" in {e.dedupe_id for e in events}
    assert json.loads(cursor)["last_modified"] == gh.read()["last_modified"]


def test_a_review_request_on_an_unconfigured_repository_costs_no_call(source, gh):
    config = MachineConfig.parse(repository_table(github="octo/app"))
    source.use_repositories(config.repositories)
    gh.add_pull(PR)
    gh.add_pull("elsewhere/other#1")
    gh.notify("review_requested", PR)
    gh.notify("review_requested", "elsewhere/other#1")

    events, _ = source.poll(None, [])

    # It is passed on unchecked: it starts nothing, and the tick reports it once.
    assert [e.item.ref for e in events] == [PR, "elsewhere/other#1"]
    assert "repos/octo/app/pulls/7" in api_paths(gh)
    assert not any(path.startswith("repos/elsewhere") for path in api_paths(gh))


def test_an_auth_failure_on_an_item_still_fails_the_poll(source, gh):
    gh.add_pull(PR)
    gh.set(fail=[{"match": "pulls/7", "exit": 4, "stderr": "gh auth login"}])

    with pytest.raises(TrackerAuthError):
        source.poll(None, followed())


def test_item_open(source, gh):
    gh.add_pull(PR)
    gh.add_pull("octo/app#8", state="closed")
    item = followed()[0]

    assert source.item_open(item)
    assert not source.item_open(followed("octo/app#8")[0])
    assert not source.item_open(followed("octo/app#99")[0])


def test_the_open_pull_request_of_a_branch(source, gh, tmp_path):
    gh.add_pull("octo/app#8", state="closed", branch="feature/x")
    gh.add_pull("octo/app#9", branch="feature/x")
    gh.add_pull("octo/app#10", branch="other")
    repository = Repository(
        name="test", path=tmp_path, tracker=TrackerConfig(type="fake"),
        labels=Labels(**LABELS), github="octo/app",
    )

    item = source.pull_request_for(repository, "feature/x")

    assert (item.source, item.key, item.ref, item.url) == (
        "github", "octo_app_9", "octo/app#9", "https://github.com/octo/app/pull/9",
    )
    assert source.pull_request_for(repository, "nothing-here") is None
    # No call for a repository without a GitHub name, or for a branch name that is not plain.
    gh.clear_calls()
    assert source.pull_request_for(dataclasses.replace(repository, github=None), "feature/x") is None
    assert source.pull_request_for(repository, "-x&state=closed") is None
    assert gh.calls == []


# ----- failures and configuration -----------------------------------------------------


def test_a_gh_failure_is_a_tracker_error(source, gh):
    gh.set(fail=[{"match": "notifications", "stderr": "HTTP 502: Bad Gateway"}])

    with pytest.raises(TrackerError, match="502"):
        source.poll(None, [])


def test_gh_not_signed_in_is_an_auth_error(source, gh):
    gh.set(fail=[{"match": "user", "exit": 4, "stderr": "To get started, run: gh auth login"}])

    with pytest.raises(TrackerAuthError):
        source.whoami()


def test_a_missing_gh_is_a_tracker_error(tmp_path):
    missing = sources_mod.build(
        SourceConfig("github", "github", {"gh_bin": str(tmp_path / "no-gh")})
    )

    with pytest.raises(TrackerError, match="not found"):
        missing.whoami()


def test_whoami_is_the_gh_login(source, gh):
    assert source.whoami() == "operator"


def test_unknown_source_options_are_refused():
    with pytest.raises(ConfigError, match="unknown option"):
        sources_mod.build(SourceConfig("github", "github", {"token": "x"}))


def repository_table(**extra):
    return {
        "repository": [
            {"name": "app", "path": "/tmp/app", "tracker": {"type": "fake"}, **extra}
        ]
    }


def test_a_repository_names_its_github_repository():
    config = MachineConfig.parse(repository_table(github="octo/app"))

    assert config.repositories[0].github == "octo/app"
    assert MachineConfig.parse(repository_table()).repositories[0].github is None


@pytest.mark.parametrize("value", ["octo", "octo/app/x", "../x/y", "octo/..", 3])
def test_a_github_key_that_is_not_owner_repo_is_refused(value):
    with pytest.raises(ConfigError, match="github"):
        MachineConfig.parse(repository_table(github=value))


def test_start_events_map_to_the_repository_by_its_github_key(source, tmp_path):
    config = MachineConfig.parse(
        {"repository": [
            {"name": "one", "path": "/tmp/one", "tracker": {"type": "fake"}},
            {"name": "two", "path": "/tmp/two", "tracker": {"type": "fake"}, "github": "Octo/App"},
        ]}
    )
    item = WorkItem("github", PR_KEY, PR, hint="octo/app")

    assert source.repository_for(item, config.repositories).name == "two"
    assert source.repository_for(WorkItem("github", PR_KEY, PR), config.repositories).name == "two"
    assert source.repository_for(
        WorkItem("github", "x_y_1", "x/y#1", hint="x/y"), config.repositories
    ) is None


# ----- GitHub Issues as the queue ------------------------------------------------------


@pytest.fixture
def tracker(gh) -> GithubTracker:
    built = trackers_mod.build_from("github", {"repo": "octo/app", "gh_bin": str(FAKE_GH)}, LABELS)
    assert isinstance(built, GithubTracker)
    return built


def test_the_github_tracker_is_registered():
    assert "github" in trackers_mod.available()


def test_the_queue_is_open_labeled_issues_assigned_to_me(tracker, gh):
    gh.add_issue("octo/app#1", labels=["chargehand"])
    gh.add_issue("octo/app#2", labels=["chargehand"], assignees=["someone"])
    gh.add_issue("octo/app#3", labels=["chargehand"], state="closed")
    gh.add_issue("octo/app#4", labels=["other"])
    gh.set(extra_issue_list_entries=[{"number": 5, "pull_request": {}, "labels": []}])

    queued = tracker.list(Status.QUEUED)

    assert [(i.id, i.identifier, i.url, i.labels) for i in queued] == [
        ("octo/app#1", "octo_app_1", "https://github.com/octo/app/issues/1", ("chargehand",))
    ]
    assert tracker.whoami() == "operator"


def test_the_label_lifecycle(tracker, gh):
    gh.add_issue("octo/app#1", labels=["chargehand", "bug"])
    issue = tracker.list(Status.QUEUED)[0]

    running = tracker.mark(issue, Status.RUNNING)
    assert gh.labels_of("octo/app#1") == ["bug", "chargehand-running"]
    blocked = tracker.mark(running, Status.BLOCKED)
    assert gh.labels_of("octo/app#1") == ["bug", "chargehand-blocked"]
    done = tracker.mark(blocked, Status.DONE)
    assert gh.labels_of("octo/app#1") == ["bug"] and done.labels == ("bug",)
    assert tracker.mark(done, Status.DONE) is done
    edits = [call for call in gh.calls if call[:2] == ["issue", "edit"]]
    assert edits[0] == ["issue", "edit", "1", "--repo", "octo/app", "--add-label",
                        "chargehand-running", "--remove-label", "chargehand"]


def test_a_failed_write_that_landed_is_accepted(tracker, gh):
    gh.add_issue("octo/app#1", labels=["chargehand"])
    gh.set(edit_fails=True)

    fresh = tracker.mark(tracker.get("octo/app#1"), Status.RUNNING)

    assert fresh.labels == ("chargehand-running",)


@pytest.mark.parametrize("fails", [False, True])
def test_a_write_the_re_read_does_not_show_is_ambiguous(tracker, gh, fails):
    gh.add_issue("octo/app#1", labels=["chargehand"])
    gh.set(edit_lands=False, edit_fails=fails)

    with pytest.raises(AmbiguousWrite):
        tracker.mark(tracker.get("octo/app#1"), Status.RUNNING)


def test_a_write_that_cannot_be_verified_is_ambiguous(tracker, gh):
    gh.add_issue("octo/app#1", labels=["chargehand"])
    issue = tracker.get("octo/app#1")
    gh.set(fail=[{"match": "repos/octo/app/issues/1", "stderr": "HTTP 502: Bad Gateway"}])

    with pytest.raises(AmbiguousWrite, match="verify"):
        tracker.mark(issue, Status.RUNNING)


def test_get_of_an_issue_that_is_gone_is_none(tracker, gh):
    assert tracker.get("octo/app#404") is None
    assert tracker.get("not a ref") is None


def test_get_by_item_key_for_an_adopted_session(tracker, gh):
    """A session adopted with no ledger row behind it knows only its item key."""
    gh.add_issue("octo/app#1", labels=["chargehand-running"])

    issue = tracker.get("octo_app_1")

    assert issue is not None and (issue.id, issue.identifier) == ("octo/app#1", "octo_app_1")
    # A key of another repository, or one that only ends like a key, is not this one.
    assert tracker.get("other_app_1") is None
    assert tracker.get("octo_app_1-deadbeef") is None


def test_the_github_tracker_needs_owner_repo(gh):
    with pytest.raises(ConfigError, match="owner/repo"):
        trackers_mod.build_from("github", {"repo": "app"}, LABELS)
    with pytest.raises(ConfigError, match="unknown option"):
        trackers_mod.build_from("github", {"repo": "octo/app", "team": "x"}, LABELS)


# ----- in the tick -----------------------------------------------------------------------

REVIEWER = """\
prompt = "/review {item_url}"

[[start]]
on = "github.review_requested"
subscribe = ["comment", "review", "ci_status", "pr_closed"]
"""


@pytest.fixture
def ticking(tmp_path, repo, claude_state, board, gh):
    target = repo / ".chargehand" / "roles" / "pr-reviewer.toml"
    target.parent.mkdir(parents=True)
    target.write_text(REVIEWER)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    config = make_config(
        repo, worktree_root, roles=("implementer", "pr-reviewer"),
        sources=(SourceConfig("github", "github", {"gh_bin": str(FAKE_GH)}),),
    )
    config = dataclasses.replace(
        config, repositories=(dataclasses.replace(config.repositories[0], github="octo/app"),)
    )
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite")
    notifier = Notifier(config.notify)
    runner = Runner(config, ledger, git=Git(), notifier=notifier)
    harness = Harness(config, ledger, runner, claude_state, board, repo, repo, worktree_root, notifier)
    yield harness
    ledger.close()


def inbox_rows(harness):
    return [
        tuple(row)
        for row in harness.ledger._conn.execute(
            "SELECT worker_id, kind, dedupe_id FROM inbox ORDER BY worker_id, id"
        )
    ]


def test_a_review_request_starts_a_reviewer_and_a_comment_reaches_its_inbox(ticking, gh):
    gh.add_pull(PR)
    gh.notify("review_requested", PR)
    gh.notify("review_requested", "elsewhere/other#1")

    report = ticking.tick()

    assert report.errors == []
    assert report.launched == [f"pr-reviewer/{PR_KEY}"]
    reviewer = ticking.worker(PR_KEY, "pr-reviewer")
    assert (reviewer.item_id, reviewer.url, reviewer.repository) == (
        PR, "https://github.com/octo/app/pull/7", "test",
    )
    assert ticking.claude_state.launches[0]["argv"][-1] == "/review https://github.com/octo/app/pull/7"
    # A review request on a repository the machine does not know starts nothing, costs
    # no call, and is reported once.
    assert ticking.ledger.live_worker_for("elsewhere_other_1") is None
    assert not any(path.startswith("repos/elsewhere") for path in api_paths(gh))
    assert any("elsewhere/other" in warning for warning in report.warnings)

    now = time.time()
    gh.add("issue_comments", PR, {"id": 41, "user": {"login": "alice"}, "body": "why?",
                                  "created_at": iso(now)})
    gh.add("issue_comments", PR, {"id": 42, "user": {"login": "operator"},
                                  "body": f"because {AUTHORSHIP_MARKER}", "created_at": iso(now)})
    report = ticking.next_tick()

    assert report.errors == []
    assert inbox_rows(ticking) == [(reviewer.id, "comment", "issue_comment:41")]


def test_a_comment_from_before_the_reviewer_started_does_not_wake_it(ticking, gh):
    """The first read of a followed pull request reaches five minutes back; the router does not."""
    gh.add_pull(PR)
    gh.notify("review_requested", PR)
    assert ticking.tick().launched == [f"pr-reviewer/{PR_KEY}"]
    reviewer = ticking.worker(PR_KEY, "pr-reviewer")
    for comment_id, offset in ((51, -240), (52, 10)):
        gh.add("issue_comments", PR, {"id": comment_id, "user": {"login": "alice"}, "body": "hm",
                                      "created_at": iso(reviewer.created_at + offset)})
    ticking.claude_state.set_session_state(PR_KEY, "done")

    report = ticking.next_tick()

    assert report.errors == []
    assert inbox_rows(ticking) == [(reviewer.id, "comment", "issue_comment:52")]
    assert any("1 inbox row(s), 1 from before the worker's start" in line
               for line in report.routed), report.routed
    assert report.woken == [f"pr-reviewer/{PR_KEY}"]
    assert len(ticking.claude_state.read()["resumes"]) == 1


def test_an_unchanged_notification_list_is_no_error_in_the_tick(ticking, gh):
    gh.notify("mention", "octo/app#9", kind="Issue")
    ticking.tick()
    # Let the poll interval pass, so the next tick asks again.
    cursor = json.loads(ticking.ledger.get_cursor("github"))
    cursor["next_poll_at"] = 0
    ticking.ledger.set_cursor("github", json.dumps(cursor))

    report = ticking.next_tick()

    assert report.errors == []
    assert any(call[:2] == ["api", "-i"] for call in gh.calls[-2:])


def test_a_review_request_without_a_thread_is_one_tick_warning_a_day(ticking, gh, caplog):
    gh.add_pull(PR, updated_at=iso(time.time() - 3600))

    with caplog.at_level("DEBUG"):
        report = ticking.tick()

    assert report.errors == []
    warnings = [w for w in report.warnings if "no notification thread" in w]
    assert len(warnings) == 1 and PR_KEY in warnings[0]
    # `tick` and `serve` print the report's warnings; a log line would print it twice.
    assert not any("no notification thread" in r.getMessage() for r in caplog.records)
    assert ticking.ledger.live_worker_for(PR_KEY) is None

    # The check runs once a day, so the next tick does not warn again.
    report = ticking.next_tick()
    assert not any("no notification thread" in w for w in report.warnings)


@pytest.mark.parametrize("cursor", ["not json", "[]", '{"poll_interval": "soon"}'])
def test_an_unreadable_cursor_starts_again(source, gh, cursor):
    gh.add_pull(PR)
    gh.notify("review_requested", PR)

    events, fresh = source.poll(cursor, [])

    assert [e.kind for e in events] == ["review_requested"]
    assert json.loads(fresh)["next_poll_at"] == NOW + 60
