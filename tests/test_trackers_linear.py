"""The Linear adapter, exercised through a stubbed transport. No network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chargehand.config import Labels, Repository, TrackerConfig
from chargehand.errors import AmbiguousWrite, ConfigError, TrackerAuthError, TrackerError
from chargehand.sources import AUTHORSHIP_MARKER, WorkItem
from chargehand.sources.tracker import TrackerSource
from chargehand.trackers.base import Issue, Status
from chargehand.trackers.linear import OVERLAP_SECS, LinearTracker

LABELS = {"queued": "chargehand", "running": "chargehand-running", "blocked": "chargehand-blocked"}

LABEL_IDS = {
    "chargehand": "lbl-q",
    "chargehand-running": "lbl-r",
    "chargehand-blocked": "lbl-b",
    "unrelated": "lbl-u",
}


def node(identifier: str, labels: tuple[str, ...], *, state: str = "Todo", state_type: str = "unstarted"):
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": "untrusted title",
        "url": f"https://linear.app/x/issue/{identifier}",
        "branchName": f"me/{identifier.lower()}",
        "labels": {"nodes": [{"id": LABEL_IDS[name], "name": name} for name in labels]},
        "state": {"name": state, "type": state_type},
    }


class StubTransport:
    """Records every GraphQL call and answers from an in-memory board."""

    def __init__(self, issues: dict[str, tuple[str, ...]]):
        self.issues = dict(issues)
        self.calls: list[str] = []
        self.fail_on: str | None = None
        self.failure: Exception | None = None
        self.mutation_lands = True
        # Name, id and owning team, because a workspace can hold several labels with one
        # name and the adapter has to pick the right one.
        self.labels = [{"id": v, "name": k, "team": {"id": "t-ABC", "key": "ABC"}}
                       for k, v in LABEL_IDS.items()]

    def __call__(self, query: str, variables=None):
        variables = variables or {}
        if "viewer" in query:
            self.calls.append("viewer")
            return {"viewer": {"email": "me@example.invalid"}}
        if "issueLabels" in query:
            label_filter = variables["filter"]
            wanted = label_filter["name"]["eqIgnoreCase"].lower()
            team = (label_filter.get("team") or {}).get("key", {}).get("eq")
            self.calls.append(f"labels:{wanted}" + (f"@{team}" if team else ""))
            nodes = [n for n in self.labels if n["name"].lower() == wanted]
            if team is not None:
                # The server narrows before it truncates, which is the whole point of
                # sending the team: one match comes back instead of a page of them.
                nodes = [n for n in nodes if (n.get("team") or {}).get("key") == team]
            return {"issueLabels": {"nodes": nodes[: variables["first"]]}}
        if query.strip().startswith("query Queue"):
            self.calls.append("queue")
            wanted = variables["filter"]["labels"]["name"]["eq"]
            return {
                "issues": {
                    "nodes": [
                        node(identifier, labels)
                        for identifier, labels in self.issues.items()
                        if wanted in labels
                    ]
                }
            }
        if query.strip().startswith("query Issue"):
            self.calls.append("issue")
            identifier = variables["id"].removeprefix("id-")
            if identifier not in self.issues:
                return {"issue": None}
            return {"issue": node(identifier, self.issues[identifier])}
        if "issueAddLabel" in query or "issueRemoveLabel" in query:
            adding = "issueAddLabel" in query
            self.calls.append("add" if adding else "remove")
            identifier = variables["id"].removeprefix("id-")
            name = next(n["name"] for n in self.labels if n["id"] == variables["labelId"])
            if self.mutation_lands:
                current = list(self.issues[identifier])
                if adding and name not in current:
                    current.append(name)
                if not adding and name in current:
                    current.remove(name)
                self.issues[identifier] = tuple(current)
            if self.fail_on == ("add" if adding else "remove"):
                self.fail_on = None
                raise self.failure or TrackerError("linear: HTTP 502")
            return {"issueAddLabel" if adding else "issueRemoveLabel": {"success": True}}
        raise AssertionError(f"unexpected query: {query[:60]}")


@pytest.fixture
def tracker_and_transport():
    def build(issues, **options):
        tracker = LinearTracker({"team": "ABC", **options}, LABELS)
        transport = StubTransport(issues)
        tracker._post = transport  # the one seam that would otherwise touch the network
        tracker._api_key = "test-key"
        return tracker, transport

    return build


def test_whoami_identifies_the_key_owner(tracker_and_transport):
    tracker, _ = tracker_and_transport({})

    assert tracker.whoami() == "me@example.invalid"


def test_the_queue_filters_by_assignee_label_team_and_state(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)}, state="Todo")
    captured = {}

    original = transport.__call__

    def capture(query, variables=None):
        if query.strip().startswith("query Queue"):
            captured.update(variables["filter"])
        return original(query, variables)

    tracker._post = capture

    issues = tracker.list(Status.QUEUED)

    assert [issue.identifier for issue in issues] == ["ABC-1"]
    assert captured["assignee"] == {"isMe": {"eq": True}}
    assert captured["team"] == {"key": {"eq": "ABC"}}
    assert captured["labels"] == {"name": {"eq": "chargehand"}}
    assert captured["state"] == {"name": {"eq": "Todo"}}


def test_the_state_filter_does_not_apply_when_looking_for_running_issues(tracker_and_transport):
    """A running issue has been moved on by whatever the prompt runs; it must still be found."""
    tracker, transport = tracker_and_transport({}, state="Todo")
    captured = {}
    original = transport.__call__

    def capture(query, variables=None):
        if query.strip().startswith("query Queue"):
            captured.update(variables["filter"])
        return original(query, variables)

    tracker._post = capture
    tracker.list(Status.RUNNING)

    assert "state" not in captured


def test_marking_running_swaps_the_labels_and_keeps_unrelated_ones(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand", "unrelated")})
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand", "unrelated"))

    updated = tracker.mark(issue, Status.RUNNING)

    assert set(updated.labels) == {"chargehand-running", "unrelated"}
    assert transport.calls.count("add") == 1
    assert transport.calls.count("remove") == 1
    # Every write is followed by a re-read.
    assert transport.calls[-1] == "issue"


def test_marking_done_clears_every_managed_label(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand-running", "unrelated")})
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand-running", "unrelated"))

    updated = tracker.mark(issue, Status.DONE)

    assert updated.labels == ("unrelated",)


def test_a_write_that_is_already_correct_does_nothing(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand-running",)})
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand-running",))

    tracker.mark(issue, Status.RUNNING)

    assert "add" not in transport.calls and "remove" not in transport.calls


def test_a_write_that_landed_behind_a_reported_failure_is_accepted(tracker_and_transport):
    """A 502 after the last mutation applied must not be treated as a failure."""
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.fail_on = "remove"
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    updated = tracker.mark(issue, Status.RUNNING)

    assert updated.labels == ("chargehand-running",)


def test_a_half_applied_label_swap_is_ambiguous(tracker_and_transport):
    """The add and the remove run serially, not atomically, so one can land alone."""
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.fail_on = "add"
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(AmbiguousWrite):
        tracker.mark(issue, Status.RUNNING)

    # Both labels are present: the caller must leave this for reconciliation.
    assert set(transport.issues["ABC-1"]) == {"chargehand", "chargehand-running"}


def test_a_write_that_reported_success_without_landing_is_ambiguous(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.mutation_lands = False
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(AmbiguousWrite, match="re-read"):
        tracker.mark(issue, Status.RUNNING)


def test_a_write_that_failed_and_did_not_land_is_ambiguous(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.mutation_lands = False
    transport.fail_on = "add"
    transport.failure = TrackerError("linear: HTTP 500")
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(AmbiguousWrite, match="did not land"):
        tracker.mark(issue, Status.RUNNING)


def test_an_issue_deleted_mid_write_is_ambiguous(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    def vanish(query, variables=None):
        if query.strip().startswith("query Issue"):
            return {"issue": None}
        return StubTransport.__call__(transport, query, variables)

    tracker._post = vanish

    with pytest.raises(AmbiguousWrite, match="disappeared"):
        tracker.mark(issue, Status.RUNNING)


def test_a_missing_label_names_the_fix(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.labels = [n for n in transport.labels if n["name"] != "chargehand-running"]
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(TrackerError, match="create it"):
        tracker.mark(issue, Status.RUNNING)


def test_a_label_is_looked_up_by_name_not_by_enumerating_the_workspace(tracker_and_transport):
    """A shared workspace holds more labels than any fixed page budget can walk.

    Enumerating stops short and reports the labels beyond the cut as non-existent - and
    because the listing comes back newest first, the one that disappears is whichever has
    been in use longest. Asking for the name cannot go stale that way.
    """
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    tracker.mark(issue, Status.RUNNING)

    looked_up = [call for call in transport.calls if call.startswith("labels:")]
    assert looked_up == ["labels:chargehand-running@ABC", "labels:chargehand@ABC"]


def test_a_resolved_label_is_not_looked_up_twice(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",), "ABC-2": ("chargehand",)})

    tracker.mark(Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",)), Status.RUNNING)
    before = len([call for call in transport.calls if call.startswith("labels:")])
    tracker.mark(Issue(id="id-ABC-2", identifier="ABC-2", labels=("chargehand",)), Status.RUNNING)

    assert before == len([call for call in transport.calls if call.startswith("labels:")])


def test_the_route_team_narrows_the_lookup_on_the_server(tracker_and_transport):
    """A shared workspace holds more labels with one name than any page can carry.

    Measured on a real one: 250 teams, and 114 label names used by more than 25 of them.
    Asking for the name alone and choosing from what came back would miss the right
    label for anyone whose queue label is an ordinary word.
    """
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.labels.insert(
        0, {"id": "lbl-other", "name": "chargehand-running",
            "team": {"id": "t-XYZ", "key": "XYZ"}}
    )
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    tracker.mark(issue, Status.RUNNING)

    assert tracker._label_ids["chargehand-running"] == "lbl-r"
    assert any(call.endswith("@ABC") for call in transport.calls)


def test_a_full_page_of_labels_is_reported_rather_than_chosen_from(tracker_and_transport):
    """Truncation is silent, and picking from a partial list is how the wrong one wins."""
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)}, team=None)
    from chargehand.trackers.linear import _LABEL_PAGE

    transport.labels = [
        {"id": f"lbl-{n}", "name": "chargehand-running", "team": {"id": f"t{n}", "key": f"T{n}"}}
        for n in range(_LABEL_PAGE + 5)
    ]
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(TrackerError, match="more than"):
        tracker.mark(issue, Status.RUNNING)


def test_the_route_team_wins_among_whatever_came_back(tracker_and_transport):
    """Belt and braces behind the server filter, tested where it is actually reachable.

    The filter should make this moot. It is kept because writing the wrong team's label
    moves an issue into a queue this route does not watch, which nothing downstream
    would notice.
    """
    tracker, _ = tracker_and_transport({})
    candidates = [
        {"id": "lbl-other", "name": "chargehand-running", "team": {"id": "t-XYZ", "key": "XYZ"}},
        {"id": "lbl-r", "name": "chargehand-running", "team": {"id": "t-ABC", "key": "ABC"}},
    ]

    assert tracker._pick_label("chargehand-running", candidates)["id"] == "lbl-r"


def test_a_workspace_label_is_used_when_no_team_owns_the_name(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)}, team=None)
    transport.labels = [
        {"id": "lbl-ws", "name": "chargehand-running", "team": None},
        *[n for n in transport.labels if n["name"] != "chargehand-running"],
    ]
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    tracker.mark(issue, Status.RUNNING)

    assert tracker._label_ids["chargehand-running"] == "lbl-ws"


def test_an_unresolvable_label_name_is_reported_rather_than_guessed(tracker_and_transport):
    """With no team on the route there is nothing to prefer, so the ambiguity is named."""
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)}, team=None)
    transport.labels.insert(
        0, {"id": "lbl-other", "name": "chargehand-running",
            "team": {"id": "t-XYZ", "key": "XYZ"}}
    )
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(TrackerError, match="several teams"):
        tracker.mark(issue, Status.RUNNING)


def test_a_route_pointed_at_a_team_without_the_label_says_so(tracker_and_transport):
    tracker, _ = tracker_and_transport({"ABC-1": ("chargehand",)}, team="NOPE")
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(TrackerError, match="create it"):
        tracker.mark(issue, Status.RUNNING)


def test_refresh_labels_drops_the_cache(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    tracker.mark(Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",)), Status.RUNNING)
    before = len([call for call in transport.calls if call.startswith("labels:")])

    tracker.refresh_labels()
    tracker.mark(Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand-running",)),
                 Status.BLOCKED)

    assert len([call for call in transport.calls if call.startswith("labels:")]) > before


def test_a_completed_issue_reads_as_closed(tracker_and_transport):
    tracker, transport = tracker_and_transport({})
    tracker._post = lambda q, v=None: {
        "issue": node("ABC-1", ("chargehand",), state="Done", state_type="completed")
    }

    assert tracker.get("id-ABC-1").closed is True


def test_the_runner_never_receives_an_issue_body(tracker_and_transport):
    tracker, _ = tracker_and_transport({"ABC-1": ("chargehand",)})

    issue = tracker.list(Status.QUEUED)[0]

    assert not hasattr(issue, "body")
    assert not hasattr(issue, "description")


def test_unknown_adapter_options_are_rejected():
    with pytest.raises(ConfigError, match="unknown option"):
        LinearTracker({"team": "ABC", "teem": "typo"}, LABELS)


def test_a_missing_api_key_explains_every_way_to_supply_one(monkeypatch):
    monkeypatch.delenv("CHARGEHAND_LINEAR_API_KEY", raising=False)
    monkeypatch.setattr("chargehand.trackers.linear._lookup_keychain", lambda *a: None)

    # A TrackerError, not a ConfigError: an unreadable credential is transient, and the
    # tick must not treat it like a broken configuration file.
    with pytest.raises(TrackerAuthError, match="keychain"):
        LinearTracker({"team": "ABC"}, LABELS).api_key()


def test_the_api_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHARGEHAND_LINEAR_API_KEY", "lin_api_test")

    assert LinearTracker({}, LABELS).api_key() == "lin_api_test"


def test_the_issue_url_drops_the_title_slug(tracker_and_transport):
    """A Linear URL can end in the title, hyphenated. That text must not travel with it."""
    tracker, transport = tracker_and_transport({})
    tracker._post = lambda q, v=None: {
        "issue": {
            "id": "id-ABC-1",
            "identifier": "ABC-1",
            "title": "Fix billing for customer globex",
            "url": "https://linear.app/acme/issue/ABC-1/fix-billing-for-customer-globex",
            "labels": {"nodes": []},
            "state": {"name": "Todo", "type": "unstarted"},
        }
    }

    issue = tracker.get("id-ABC-1")

    assert issue.url == "https://linear.app/acme/issue/ABC-1"
    assert "globex" not in issue.url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://linear.app/acme/issue/ABC-1/slug", "https://linear.app/acme/issue/ABC-1"),
        ("https://linear.app/acme/issue/ABC-1", "https://linear.app/acme/issue/ABC-1"),
        ("https://linear.app/acme/issue/ABC-1/a/b?c=d", "https://linear.app/acme/issue/ABC-1"),
        ("https://example.invalid/other", "https://example.invalid/other"),
        (None, None),
        ("", None),
    ],
)
def test_url_canonicalisation_cases(raw, expected):
    assert LinearTracker._canonical_url(raw) == expected


# ----- the poll -------------------------------------------------------------------

NOW = 1_800_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class PollTransport:
    """Answers the poll's queries and records their variables.

    Comments come in pages of 50 with a `pageInfo`, filtered by `createdAt`, oldest first
    or, with `newest_first`, newest first: the adapter must not depend on the order.
    """

    PAGE = 50

    def __init__(self):
        self.queue: list[dict] = []
        self.followed: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.newest_first = False

    def __call__(self, query: str, variables=None):
        variables = variables or {}
        name = query.strip().split("(")[0].removeprefix("query ").strip()
        self.calls.append((name, variables))
        if name == "Poll":
            return {"issues": {"nodes": self.queue, "pageInfo": {"hasNextPage": False}}}
        if name == "Followed":
            wanted = set(variables["filter"]["id"]["in"])
            nodes = [
                {**n, "comments": self._page(n, variables.get("comments"), None)}
                for n in self.followed if n["id"] in wanted
            ]
            return {"issues": {"nodes": nodes}}
        if name == "Comments":
            found = next(n for n in self.followed if n["id"] == variables["id"])
            return {"issue": {"comments": self._page(found, variables["comments"],
                                                     variables["after"])}}
        raise AssertionError(f"unexpected query: {query[:60]}")

    def _page(self, issue_node, comment_filter, after):
        entries = list(issue_node["comments"]["nodes"])
        if comment_filter:
            gt = datetime.fromisoformat(comment_filter["createdAt"]["gt"]).timestamp()
            entries = [e for e in entries
                       if datetime.fromisoformat(e["createdAt"].replace("Z", "+00:00")).timestamp() > gt]
        entries.sort(key=lambda e: e["createdAt"], reverse=self.newest_first)
        start = int(after) if after else 0
        end = start + self.PAGE
        return {
            "nodes": entries[start:end],
            "pageInfo": {"hasNextPage": end < len(entries), "endCursor": str(end)},
        }


def poll_node(identifier, *, updated, labels=("chargehand",), state_type="unstarted", comments=()):
    entry = node(identifier, labels, state_type=state_type)
    entry["updatedAt"] = iso(updated)
    entry["comments"] = {"nodes": list(comments)}
    return entry


def comment(cid, *, created, author="other@example.invalid", body="please also fix X"):
    return {"id": cid, "createdAt": iso(created), "body": body, "user": {"email": author}}


@pytest.fixture
def polling():
    tracker = LinearTracker({"team": "ABC"}, LABELS)
    transport = PollTransport()
    tracker._post = transport
    tracker._api_key = "test-key"
    tracker.clock = lambda: NOW
    return tracker, transport


def test_the_poll_reads_the_queue_with_its_last_change_time(polling):
    tracker, transport = polling
    transport.queue = [poll_node("ABC-1", updated=NOW - 50), poll_node("ABC-2", updated=NOW - 10)]

    result = tracker.poll(None, [])

    assert [issue.identifier for issue in result.queued] == ["ABC-1", "ABC-2"]
    assert result.queued[1].updated_at == NOW - 10
    ((name, variables),) = transport.calls
    assert name == "Poll"
    assert variables["filter"]["labels"] == {"name": {"eq": "chargehand"}}
    assert variables["filter"]["assignee"] == {"isMe": {"eq": True}}
    assert variables["filter"]["team"] == {"key": {"eq": "ABC"}}


def test_every_poll_reads_the_whole_queue(polling):
    """A queue start that waited longer than the router keeps it must come back, and an
    unchanged issue would never come back from a query for what changed."""
    tracker, transport = polling
    transport.queue = [poll_node("ABC-1", updated=NOW - 10)]
    cursor = tracker.poll(None, []).cursor
    transport.calls.clear()

    result = tracker.poll(cursor, [])

    ((name, variables),) = transport.calls
    assert name == "Poll" and "updatedAt" not in variables["filter"]
    assert [issue.identifier for issue in result.queued] == ["ABC-1"]


def test_the_queue_is_read_page_by_page(polling):
    tracker, _ = polling
    pages = iter([
        {"issues": {"nodes": [poll_node("ABC-1", updated=NOW)],
                    "pageInfo": {"hasNextPage": True, "endCursor": "p2"}}},
        {"issues": {"nodes": [poll_node("ABC-2", updated=NOW)],
                    "pageInfo": {"hasNextPage": False}}},
    ])
    afters = []

    def paged(query, variables=None):
        afters.append(variables["after"])
        return next(pages)

    tracker._post = paged

    assert [i.identifier for i in tracker.poll(None, []).queued] == ["ABC-1", "ABC-2"]
    assert afters == [None, "p2"]


def test_followed_issues_give_their_new_comments_and_their_state(polling):
    tracker, transport = polling
    first = tracker.poll(None, []).cursor
    transport.followed = [
        poll_node(
            "ABC-1", updated=NOW, labels=("chargehand-running",),
            comments=[
                comment("c-old", created=NOW - 3 * OVERLAP_SECS),
                comment("c-new", created=NOW - 5),
                comment("c-own", created=NOW - 4, author="me@example.invalid",
                        body=f"done.\n\n{AUTHORSHIP_MARKER}"),
            ],
        ),
        poll_node("ABC-2", updated=NOW - 1, state_type="completed"),
    ]
    tracker.clock = lambda: NOW + 60

    result = tracker.poll(first, ["id-ABC-1", "id-ABC-2"])

    # Read from the previous poll on, less the overlap; an older comment is not news.
    assert [(c.id, c.issue.identifier, c.author, c.marked) for c in result.comments] == [
        ("c-new", "ABC-1", "other@example.invalid", False),
        ("c-own", "ABC-1", "me@example.invalid", True),
    ]
    assert not any(hasattr(c, "body") for c in result.comments)
    assert [(i.identifier, i.closed) for i in result.followed] == [
        ("ABC-1", False), ("ABC-2", True)
    ]
    name, variables = transport.calls[-1]
    assert name == "Followed"
    assert variables["filter"] == {"id": {"in": ["id-ABC-1", "id-ABC-2"]}}
    since = datetime.fromisoformat(variables["comments"]["createdAt"]["gt"]).timestamp()
    assert since == NOW - OVERLAP_SECS
    assert json.loads(result.cursor)["items"] == {"id-ABC-1": NOW + 60, "id-ABC-2": NOW + 60}


def test_a_followed_issue_is_read_from_its_own_last_poll(polling):
    tracker, transport = polling
    transport.followed = [
        poll_node("ABC-1", updated=NOW, comments=[
            comment("before", created=NOW - 1000), comment("c1", created=NOW + 500),
        ])
    ]
    cursor = tracker.poll(None, []).cursor
    # Followed from the second poll on: read from the first poll, not from the beginning.
    tracker.clock = lambda: NOW + 1000
    second = tracker.poll(cursor, ["id-ABC-1"])
    tracker.clock = lambda: NOW + 3000
    third = tracker.poll(second.cursor, ["id-ABC-1"])

    assert [c.id for c in second.comments] == ["c1"]
    assert third.comments == []


def test_no_followed_issue_means_no_second_query(polling):
    tracker, transport = polling

    tracker.poll(None, [])

    assert [name for name, _ in transport.calls] == ["Poll"]


def test_a_cursor_that_does_not_parse_starts_over(polling):
    tracker, transport = polling
    transport.followed = [poll_node("ABC-1", updated=NOW)]

    result = tracker.poll("not json", ["id-ABC-1"])

    assert json.loads(result.cursor) == {"polled_at": NOW, "items": {"id-ABC-1": NOW}}


@pytest.mark.parametrize("newest_first", [False, True])
def test_every_page_of_comments_is_read_whatever_their_order(polling, newest_first):
    tracker, transport = polling
    transport.newest_first = newest_first
    cursor = tracker.poll(None, []).cursor
    transport.followed = [
        poll_node("ABC-1", updated=NOW, comments=[
            comment(f"c{n}", created=NOW + n) for n in range(1, 121)
        ]),
        poll_node("ABC-2", updated=NOW, comments=[comment("d1", created=NOW + 1)]),
    ]
    tracker.clock = lambda: NOW + 600

    result = tracker.poll(cursor, ["id-ABC-1", "id-ABC-2"])

    assert sorted(c.id for c in result.comments if c.issue.identifier == "ABC-1") == sorted(
        f"c{n}" for n in range(1, 121)
    )
    assert [after for name, v in transport.calls if name == "Comments"
            for after in [v["after"]]] == ["50", "100"]
    assert json.loads(result.cursor)["items"] == {"id-ABC-1": NOW + 600, "id-ABC-2": NOW + 600}


@pytest.mark.parametrize("newest_first", [False, True])
def test_a_second_poll_reads_comment_51_and_later(polling, newest_first):
    tracker, transport = polling
    transport.newest_first = newest_first
    cursor = tracker.poll(None, []).cursor
    busy = poll_node("ABC-1", updated=NOW, comments=[
        comment(f"c{n}", created=NOW + n) for n in range(1, 51)
    ])
    transport.followed = [busy]
    tracker.clock = lambda: NOW + 100
    first = tracker.poll(cursor, ["id-ABC-1"])
    busy["comments"]["nodes"] += [comment(f"c{n}", created=NOW + 100 + n) for n in range(51, 71)]
    tracker.clock = lambda: NOW + 1000

    second = tracker.poll(first.cursor, ["id-ABC-1"])

    assert len(first.comments) == 50
    assert {f"c{n}" for n in range(51, 71)} <= {c.id for c in second.comments}


@pytest.mark.parametrize("newest_first", [False, True])
def test_an_issue_with_more_comments_than_one_poll_reads_goes_on_from_its_last_page(
    polling, newest_first
):
    tracker, transport = polling
    transport.newest_first = newest_first
    cursor = tracker.poll(None, []).cursor
    busy = poll_node("ABC-1", updated=NOW, comments=[
        comment(f"c{n}", created=NOW + n) for n in range(1, 50 * 20 + 31)
    ])
    transport.followed = [busy]
    tracker.clock = lambda: NOW + 3000

    first = tracker.poll(cursor, ["id-ABC-1"])

    assert len(first.comments) == 50 * 20
    state = json.loads(first.cursor)
    assert state["items"] == {"id-ABC-1": NOW}
    assert state["pages"]["id-ABC-1"]["after"] == "1000"
    assert state["pages"]["id-ABC-1"]["started"] == NOW + 3000

    # A comment arrives while the pages are read.
    busy["comments"]["nodes"].append(comment("late", created=NOW + 3500))
    transport.calls.clear()
    tracker.clock = lambda: NOW + 4000
    second = tracker.poll(first.cursor, ["id-ABC-1"])

    seen = {c.id for c in first.comments} | {c.id for c in second.comments}
    assert {f"c{n}" for n in range(1, 50 * 20 + 31)} <= seen
    (going_on,) = [v for name, v in transport.calls if name == "Comments"]
    assert going_on["after"] == "1000"
    assert going_on["comments"] == {"createdAt": {"gt": state["pages"]["id-ABC-1"]["gt"]}}
    # The pages are done: the issue is read again from when they started.
    state = json.loads(second.cursor)
    assert state["items"] == {"id-ABC-1": NOW + 3000} and "pages" not in state

    tracker.clock = lambda: NOW + 5000
    third = tracker.poll(second.cursor, ["id-ABC-1"])
    assert "late" in seen | {c.id for c in third.comments}
    assert json.loads(third.cursor)["items"] == {"id-ABC-1": NOW + 5000}


def test_a_page_cursor_that_linear_refuses_starts_the_issue_over(polling):
    tracker, transport = polling
    cursor = json.dumps({
        "polled_at": NOW, "items": {"id-ABC-1": NOW},
        "pages": {"id-ABC-1": {"after": "stale", "gt": iso(NOW - 300), "started": NOW}},
    })
    transport.followed = [poll_node("ABC-1", updated=NOW, comments=[comment("c1", created=NOW + 1)])]
    answer = transport.__call__

    def refusing(query, variables=None):
        if (variables or {}).get("after") == "stale":
            raise TrackerError("linear: invalid cursor")
        return answer(query, variables)

    tracker._post = refusing
    tracker.clock = lambda: NOW + 600

    result = tracker.poll(cursor, ["id-ABC-1"])

    # The issue is read from its own time again, from its first page.
    assert [c.id for c in result.comments] == ["c1"]
    assert "pages" not in json.loads(result.cursor)


def test_the_linear_tracker_as_a_source_gives_labeled_comment_and_done_events(polling):
    tracker, transport = polling
    repository = Repository(
        name="app", path=Path("/repo"), tracker=TrackerConfig(type="linear", options={}),
        labels=Labels(**LABELS),
    )
    source = TrackerSource(tracker, repository)
    transport.queue = [poll_node("ABC-3", updated=NOW - 1)]
    transport.followed = [
        poll_node("ABC-1", updated=NOW, comments=[comment("c1", created=NOW + 1)]),
        poll_node("ABC-2", updated=NOW - 2, state_type="canceled"),
    ]
    items = [WorkItem("linear", "ABC-1", "id-ABC-1"), WorkItem("linear", "ABC-2", "id-ABC-2")]

    events, cursor = source.poll(None, items)

    assert [(e.source, e.kind, e.item.key, e.item.ref, e.item.hint) for e in events] == [
        ("linear", "labeled", "ABC-3", "id-ABC-3", "app"),
        ("linear", "done", "ABC-2", "id-ABC-2", "app"),
        ("linear", "comment", "ABC-1", "id-ABC-1", "app"),
    ]
    assert all(TrackerSource.produced(e) for e in events)
    assert events[2].actor == "other@example.invalid"
    assert events[2].pointer == "https://linear.app/x/issue/ABC-1"
    assert cursor is not None
