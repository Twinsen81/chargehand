"""The Linear adapter, exercised through a stubbed transport. No network."""

from __future__ import annotations

import pytest

from chargehand.errors import AmbiguousWrite, ConfigError, TrackerAuthError, TrackerError
from chargehand.trackers.base import Issue, Status
from chargehand.trackers.linear import LinearTracker

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
            self.calls.append(f"labels:{variables['name']}")
            wanted = variables["name"].lower()
            return {
                "issueLabels": {
                    "nodes": [n for n in self.labels if n["name"].lower() == wanted],
                }
            }
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
    assert looked_up == ["labels:chargehand-running", "labels:chargehand"]


def test_a_resolved_label_is_not_looked_up_twice(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",), "ABC-2": ("chargehand",)})

    tracker.mark(Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",)), Status.RUNNING)
    before = len([call for call in transport.calls if call.startswith("labels:")])
    tracker.mark(Issue(id="id-ABC-2", identifier="ABC-2", labels=("chargehand",)), Status.RUNNING)

    assert before == len([call for call in transport.calls if call.startswith("labels:")])


def test_the_route_team_wins_when_two_teams_share_a_label_name(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.labels.insert(
        0, {"id": "lbl-other", "name": "chargehand-running",
            "team": {"id": "t-XYZ", "key": "XYZ"}}
    )
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    tracker.mark(issue, Status.RUNNING)

    assert tracker._label_ids["chargehand-running"] == "lbl-r"


def test_a_workspace_label_is_used_when_no_team_owns_the_name(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)})
    transport.labels = [
        {"id": "lbl-ws", "name": "chargehand-running", "team": None},
        *[n for n in transport.labels if n["name"] != "chargehand-running"],
    ]
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    tracker.mark(issue, Status.RUNNING)

    assert tracker._label_ids["chargehand-running"] == "lbl-ws"


def test_an_unresolvable_label_name_is_reported_rather_than_guessed(tracker_and_transport):
    tracker, transport = tracker_and_transport({"ABC-1": ("chargehand",)}, team="NOPE")
    transport.labels.insert(
        0, {"id": "lbl-other", "name": "chargehand-running",
            "team": {"id": "t-XYZ", "key": "XYZ"}}
    )
    issue = Issue(id="id-ABC-1", identifier="ABC-1", labels=("chargehand",))

    with pytest.raises(TrackerError, match="several teams"):
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
    """Linear's url ends in the title, hyphenated. That text must not travel with it."""
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
