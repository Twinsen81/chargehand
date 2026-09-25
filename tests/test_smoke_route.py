"""Cover the parts of the smoke probe that can be checked without a real install.

The probe itself needs a real tracker and a real Claude Code, so it can never run here.
What can run here is everything it decides *with*: which GraphQL document a fault matches,
what the proxy does when it fires, what the sandbox writes, and how a verdict is reached.
Without this, a rename in the adapter would leave the probe injecting faults that never
match and reporting a clean run, which is the worst thing a probe can do.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from chargehand import ledger as ledger_mod
from chargehand.config import MachineConfig, RepoConfig
from chargehand.trackers import linear as linear_mod

import smoke_route
import smoke_support
from smoke_support import Fault, FaultProxy, Sandbox, operation_name


def document(query: str) -> bytes:
    return json.dumps({"query": query, "variables": {}}).encode()


# ----- the fault matcher against the adapter's real documents ---------------


@pytest.mark.parametrize(
    "query, expected",
    [
        (linear_mod._QUEUE_QUERY, "Queue"),
        (linear_mod._ISSUE_QUERY, "Issue"),
        (linear_mod._LABELS_QUERY, "Labels"),
        (linear_mod._ADD_LABEL, "Add"),
        (linear_mod._REMOVE_LABEL, "Remove"),
        (linear_mod._VIEWER_QUERY, "query"),
    ],
)
def test_operation_name_matches_every_document_the_adapter_sends(query, expected):
    assert operation_name(document(query)) == expected


def test_operation_name_survives_junk():
    assert operation_name(b"not json") == "?"
    assert operation_name(json.dumps({"variables": {}}).encode()) == "?"


# ----- the proxy ------------------------------------------------------------


class RecordingProxy(FaultProxy):
    """A proxy whose upstream is a recorder rather than a network."""

    def __init__(self, plan=None):
        super().__init__(upstream="http://upstream.invalid/graphql", plan=plan)
        self.forwarded: list[str] = []

    def _forward(self, body, authorization):
        self.forwarded.append(operation_name(body))
        return 200, json.dumps({"data": {"ok": True}}).encode()


def test_an_unmatched_call_is_forwarded_untouched():
    proxy = RecordingProxy()
    status, payload = proxy.handle(document(linear_mod._ISSUE_QUERY), "key")
    assert status == 200
    assert proxy.forwarded == ["Issue"]
    assert json.loads(payload)["data"] == {"ok": True}


def test_a_fault_that_applies_upstream_still_reports_failure():
    """The case the whole section exists for: it landed, and the caller was told it did not."""
    proxy = RecordingProxy([Fault(operation="Remove", status=502, apply_upstream=True)])
    status, payload = proxy.handle(document(linear_mod._REMOVE_LABEL), "key")
    assert status == 502
    assert proxy.forwarded == ["Remove"], "the mutation must still reach the tracker"
    assert json.loads(payload)["errors"]
    assert proxy.calls[-1]["action"].startswith("applied upstream")


def test_a_fault_that_does_not_apply_upstream_never_reaches_the_tracker():
    proxy = RecordingProxy([Fault(operation="Issue", status=502, apply_upstream=False)])
    status, _ = proxy.handle(document(linear_mod._ISSUE_QUERY), "key")
    assert status == 502
    assert proxy.forwarded == []


def test_a_fault_fires_only_as_many_times_as_it_was_armed_for():
    proxy = RecordingProxy([Fault(operation="Add", status=502, apply_upstream=False, times=1)])
    assert proxy.handle(document(linear_mod._ADD_LABEL), "key")[0] == 502
    assert proxy.handle(document(linear_mod._ADD_LABEL), "key")[0] == 200
    assert proxy.forwarded == ["Add"]


def test_a_fault_does_not_fire_on_a_different_operation():
    proxy = RecordingProxy([Fault(operation="Add", status=502)])
    assert proxy.handle(document(linear_mod._REMOVE_LABEL), "key")[0] == 200


def test_arming_replaces_the_previous_plan():
    proxy = RecordingProxy([Fault(operation="Add")])
    proxy.arm(Fault(operation="Remove", apply_upstream=False))
    assert proxy.handle(document(linear_mod._ADD_LABEL), "key")[0] == 200
    assert proxy.handle(document(linear_mod._REMOVE_LABEL), "key")[0] == 502


# ----- the sandbox ----------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path):
    box = Sandbox(tmp_path / "smoke", team="ABC", keychain_service="probe-key")
    box.build()
    return box


def test_the_sandbox_writes_configuration_the_package_accepts(sandbox):
    import tomllib

    machine = MachineConfig.parse(tomllib.loads(sandbox.config_file.read_text()))
    assert [route.name for route in machine.routes] == ["smoke"]
    assert machine.routes[0].labels.queued == smoke_support.SMOKE_LABELS["queued"]
    assert machine.routes[0].tracker.options["keychain_service"] == "probe-key"
    assert machine.worktree_root == sandbox.worktree_root

    repo = RepoConfig.parse(tomllib.loads((sandbox.repo / ".chargehand.toml").read_text()))
    assert repo.base == "origin/main"
    assert "{issue}" in repo.prompt


def test_the_smoke_labels_cannot_collide_with_the_defaults():
    from chargehand.config import DEFAULT_LABELS

    assert not set(smoke_support.SMOKE_LABELS.values()) & set(DEFAULT_LABELS.values())


def test_the_scratch_repository_has_a_resolvable_base(sandbox):
    head = sandbox._git("rev-parse", "origin/main", cwd=sandbox.repo)
    assert len(head) == 40
    assert sandbox._git("rev-list", "--count", "HEAD", "--not", "--remotes",
                        cwd=sandbox.repo) == "0"


def test_swapping_the_prompt_rewrites_the_repository_config(sandbox):
    sandbox.set_repo_prompt("a different prompt for {issue}")
    assert "a different prompt" in (sandbox.repo / ".chargehand.toml").read_text()
    sandbox.set_repo_prompt(None)
    assert smoke_support.SMOKE_PROMPT in (sandbox.repo / ".chargehand.toml").read_text()


def test_the_sandbox_points_every_path_override_inside_itself(sandbox):
    env = sandbox.env
    for key in ("CHARGEHAND_CONFIG", "CHARGEHAND_STATE_DIR", "CHARGEHAND_LOG_FILE"):
        assert env[key].startswith(str(sandbox.root)), key


def test_attempts_reads_rows_the_real_ledger_wrote(sandbox):
    ledger = ledger_mod.Ledger(sandbox.ledger_file)
    ledger.create_attempt(
        issue_id="uuid-1", identifier="ABC-1", url=None, route="smoke",
        repo=sandbox.repo, worktree=sandbox.worktree_root / "ABC-1", branch="chargehand/ABC-1",
    )
    ledger.close()
    rows = sandbox.attempts()
    assert [row["identifier"] for row in rows] == ["ABC-1"]
    assert sandbox.attempt("ABC-1")["state"] == ledger_mod.LAUNCHING
    assert sandbox.attempt("ABC-2") is None


def test_attempts_opens_the_ledger_read_only(sandbox):
    ledger_mod.Ledger(sandbox.ledger_file).close()
    sandbox.attempts()
    with pytest.raises(sqlite3.OperationalError):
        connection = sqlite3.connect(f"file:{sandbox.ledger_file}?mode=ro", uri=True)
        connection.execute("CREATE TABLE probe_should_not_write (x INTEGER)")


def test_notifications_are_read_back_from_the_hook(sandbox):
    sandbox.notify_log.write_text('{"issue":"ABC-1","state":"blocked"}\nnot json\n')
    assert sandbox.notifications() == [{"issue": "ABC-1", "state": "blocked"}]


def test_the_notification_hook_records_what_the_runner_sends(sandbox):
    import subprocess

    subprocess.run(
        [str(sandbox.root / "notify.sh")],
        env={"CHARGEHAND_ISSUE": "ABC-9", "CHARGEHAND_STATE": "blocked",
             "CHARGEHAND_URL": "", "CHARGEHAND_ROUTE": "smoke",
             "CHARGEHAND_DETAIL": "needs input", "PATH": "/usr/bin:/bin"},
        check=True,
    )
    assert sandbox.notifications() == [
        {"issue": "ABC-9", "state": "blocked", "url": "", "route": "smoke",
         "detail": "needs input"}
    ]


# ----- verdict logic --------------------------------------------------------


class StubAdmin:
    def __init__(self, running=()):
        self.running = list(running)
        self.deleted = []

    def issues_with_label(self, team_id, label):
        return [{"identifier": name} for name in self.running]

    def delete_issue(self, issue_id):
        self.deleted.append(issue_id)
        return True

    def queue(self, team_key, label, state):
        return []


class StubSandbox:
    labels = smoke_support.SMOKE_LABELS
    worktree_root = None

    def __init__(self, rows):
        self.rows = rows

    def attempts(self):
        return self.rows


def probe_with(rows, running=(), sessions=(), monkeypatch=None, mine=None):
    probe = smoke_route.Probe(StubSandbox(rows), StubAdmin(running), team="ABC", keep=True)
    probe.team_id = "team"
    probe.mine = set(running if mine is None else mine)
    if monkeypatch is not None:
        monkeypatch.setattr(smoke_route, "_sessions_under", lambda root: list(sessions))
    return probe


def verdicts(probe, name):
    return [check.verdict for check in probe.checks if check.name == name]


def test_a_running_label_with_no_live_row_is_a_stranded_label(monkeypatch):
    probe = probe_with([], running=["ABC-1"], monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no stranded running label") == [smoke_route.FAIL]


def test_a_running_label_backed_by_a_live_row_is_not_stranded(monkeypatch):
    rows = [{"identifier": "ABC-1", "state": "launching", "worktree": "/w/ABC-1"}]
    probe = probe_with(rows, running=["ABC-1"], monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no stranded running label") == [smoke_route.PASS]


def test_a_terminal_row_does_not_keep_a_running_label_honest(monkeypatch):
    rows = [{"identifier": "ABC-1", "state": "done", "worktree": "/w/ABC-1"}]
    probe = probe_with(rows, running=["ABC-1"], monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no stranded running label") == [smoke_route.FAIL]


def test_an_issue_this_run_never_created_is_not_its_stray(monkeypatch):
    """Every case shares one team. Someone else's label is not this probe's failure."""
    probe = probe_with([], running=["ABC-1"], mine=set(), monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no stranded running label") == [smoke_route.PASS]


def test_a_worktree_column_that_is_empty_is_not_the_current_directory():
    assert smoke_route._worktree_of({"worktree": ""}) is None
    assert smoke_route._worktree_of({}) is None
    assert smoke_route._worktree_of({"worktree": "/w/ABC-1"}) == pathlib.Path("/w/ABC-1")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://linear.app/acme/issue/ABC-1", True),
        ("https://linear.app/acme/issue/ABC-1/", True),
        ("https://linear.app/acme/issue/ABC-1/fix-billing-for-globex", False),
        ("https://linear.app/acme/issue/ABC-1?title=globex", False),
        ("https://linear.app/acme/issue/ABC-1#globex", False),
        ("https://linear.app/acme/issue/ABC-12", False),
        ("", False),
        (None, False),
    ],
)
def test_a_notification_url_passes_only_in_the_identifier_form(url, expected):
    assert smoke_route._is_short_issue_url(url, "ABC-1") is expected


def test_a_working_session_no_row_owns_is_unsupervised(monkeypatch):
    sessions = [{"id": "s1", "cwd": "/w/ABC-2", "state": "working"}]
    probe = probe_with([], sessions=sessions, monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no unsupervised session") == [smoke_route.FAIL]


def test_a_session_a_live_row_owns_is_supervised(monkeypatch):
    rows = [{"identifier": "ABC-2", "state": "running", "worktree": "/w/ABC-2"}]
    sessions = [{"id": "s1", "cwd": "/w/ABC-2", "state": "working"}]
    probe = probe_with(rows, sessions=sessions, monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no unsupervised session") == [smoke_route.PASS]


def test_a_finished_session_is_not_unsupervised(monkeypatch):
    sessions = [{"id": "s1", "cwd": "/w/ABC-2", "state": "done"}]
    probe = probe_with([], sessions=sessions, monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no unsupervised session") == [smoke_route.PASS]


def test_a_cancelled_run_keeps_its_worktree_without_owning_the_session(monkeypatch):
    """`cancel` stops the session and keeps the worktree, so its leftovers are expected."""
    rows = [{"identifier": "ABC-2", "state": "cancelled", "worktree": "/w/ABC-2"}]
    sessions = [{"id": "s1", "cwd": "/w/ABC-2", "state": "stopped"}]
    probe = probe_with(rows, sessions=sessions, monkeypatch=monkeypatch)
    probe.assert_no_strays("crash")
    assert verdicts(probe, "no unsupervised session") == [smoke_route.PASS]


def test_a_question_read_as_done_fails_the_vocabulary_check():
    probe = probe_with([])
    probe.sample_session("asked something", {"state": "done", "status": "idle"}, "blocked")
    probe.sample_session("finished", {"state": "done", "status": "idle"}, "done")
    probe._summarise_states("states")
    assert verdicts(probe, "every run that asked something reported blocked") == [smoke_route.FAIL]


def test_every_ending_read_correctly_passes_the_vocabulary_check():
    probe = probe_with([])
    probe.sample_session("asked something", {"state": "blocked", "status": "idle"}, "blocked")
    probe.sample_session("finished", {"state": "done", "status": "idle"}, "done")
    probe._summarise_states("states")
    assert verdicts(probe, "every run that asked something reported blocked") == [smoke_route.PASS]
    verdict = [c for c in probe.checks if c.name == "verdict on the session-state vocabulary"][0]
    assert "separated asking from finishing" in verdict.detail


def test_a_session_that_never_ended_a_turn_is_left_out_of_the_verdict():
    probe = probe_with([])
    probe.sample_session("never finished", None, "blocked")
    probe._summarise_states("states")
    assert verdicts(probe, "every run that asked something reported blocked") == [smoke_route.PASS]


def test_the_summary_fails_the_run_when_any_check_failed():
    probe = probe_with([])
    probe.record("happy", "a", smoke_route.PASS)
    probe.record("happy", "b", smoke_route.INFO)
    assert probe.summary()["ok"] is True
    probe.record("happy", "c", smoke_route.FAIL)
    assert probe.summary()["ok"] is False
    assert probe.summary()["counts"][smoke_route.FAIL] == 1


def test_the_rendered_table_lists_every_check_and_every_sample():
    probe = probe_with([])
    probe.record("happy", "the label swapped", smoke_route.PASS, "labels=['running']")
    probe.sample_session("runner happy path", {"state": "done", "status": "idle"}, "done")
    rendered = probe.render()
    assert "happy/the label swapped" in rendered
    assert "runner happy path" in rendered
    assert "1 passed, 0 failed" in rendered


# ----- argument handling ----------------------------------------------------


def test_an_unknown_section_is_refused_before_anything_is_created(capsys):
    assert smoke_route.main(["--team", "ABC", "--only", "happy,nonsense"]) == 2
    assert "nonsense" in capsys.readouterr().err


def test_a_missing_credential_is_refused_before_anything_is_created(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(smoke_route, "keychain_key", lambda *a, **k: None)
    assert smoke_route.main(["--team", "ABC", "--root", str(tmp_path / "unused")]) == 2
    assert "no API key" in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()



# ----- what the probe refuses to destroy ------------------------------------


def test_a_built_sandbox_is_recognisable_as_one(sandbox):
    assert sandbox.marker_file.is_file()
    assert smoke_support.is_sandbox(sandbox.root) is True


def test_a_directory_this_probe_did_not_build_is_not_a_sandbox(tmp_path):
    someones_work = tmp_path / "code"
    someones_work.mkdir()
    (someones_work / "main.py").write_text("print('hello')\n")

    assert smoke_support.is_sandbox(someones_work) is False


def test_an_existing_directory_without_the_marker_is_refused(monkeypatch, capsys, tmp_path):
    """`--root` is deleted and rebuilt, so a mistyped path would take the answer with it."""
    monkeypatch.setattr(smoke_route, "keychain_key", lambda *a, **k: "lin_api_test")
    someones_work = tmp_path / "code"
    someones_work.mkdir()
    (someones_work / "main.py").write_text("print('hello')\n")

    assert smoke_route.main(["--team", "ABC", "--root", str(someones_work)]) == 2

    assert "will not be deleted" in capsys.readouterr().err
    assert (someones_work / "main.py").exists()


# ----- --keep ---------------------------------------------------------------


class RecordingSandbox(StubSandbox):
    def __init__(self):
        super().__init__([{"identifier": "ABC-1", "state": "running", "worktree": "/w/ABC-1"}])
        self.commands = []

    def attempt(self, identifier):
        return self.rows[0]

    def cli(self, *args, **kwargs):
        self.commands.append(args)
        return None


def keeping_probe(keep):
    probe = smoke_route.Probe(RecordingSandbox(), StubAdmin(), team="ABC", keep=keep)
    probe.team_id = "team"
    return probe


def test_keep_leaves_the_worktree_and_its_session_alone():
    """The flag exists for picking over a failed run, and that is what there is to see."""
    probe = keeping_probe(True)

    probe.teardown_issue(smoke_support.IssueRef("uuid", "ABC-1", "url"))

    assert probe.sandbox.commands == []
    assert probe.cleanup() == {"sessions_removed": [], "issues_deleted": [], "kept": True}


def test_without_keep_the_worktree_is_discarded():
    probe = keeping_probe(False)

    probe.teardown_issue(smoke_support.IssueRef("uuid", "ABC-1", "url"))

    assert any("discard" in command for command in probe.sandbox.commands)
    assert probe.admin.deleted == ["uuid"]


def test_the_probe_runs_as_a_script_outside_pytest():
    """pytest puts `src` on the path for us; a probe run from a shell has to do it itself.

    Without this the import error only appears when someone actually runs the probe,
    which is exactly when they are trying to use it.
    """
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, str(pathlib.Path(smoke_route.__file__)), "--help"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )

    assert result.returncode == 0, result.stderr
    assert "--team" in result.stdout
