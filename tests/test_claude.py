"""The Claude Code seam: defensive parsing, adoption lookup, and the fake binary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chargehand import claude as claude_mod
from chargehand.claude import ClaudeCLI, Session, identifier_from_session, parse_sessions
from chargehand.errors import ClaudeError
from conftest import FAKE_CLAUDE


def test_a_bare_array_is_accepted():
    sessions = parse_sessions('[{"id": "a", "state": "working"}]')

    assert [s.id for s in sessions] == ["a"]


@pytest.mark.parametrize("key", ["agents", "sessions", "data", "items", "results"])
def test_common_envelopes_are_accepted(key):
    sessions = parse_sessions(json.dumps({key: [{"id": "a", "state": "working"}]}))

    assert [s.id for s in sessions] == ["a"]


def test_json_lines_are_accepted():
    sessions = parse_sessions('{"id": "a", "state": "done"}\n{"id": "b", "state": "working"}')

    assert [s.state for s in sessions] == ["done", "working"]


def test_field_aliases_are_accepted():
    session = parse_sessions(
        json.dumps([{
            "shortId": "s1",
            "sessionId": "uuid-1",
            "title": "ABC-1",
            "workingDirectory": "/w/ABC-1",
            "status": "needs_input",
            "blockedReason": "permission prompt",
            "prUrl": "https://example.invalid/pr/1",
        }])
    )[0]

    assert session.id == "s1"
    assert session.uuid == "uuid-1"
    assert session.name == "ABC-1"
    assert session.cwd == "/w/ABC-1"
    assert session.state == "blocked"
    assert session.waiting_for == "permission prompt"
    assert session.pr_url.endswith("/pr/1")


@pytest.mark.parametrize("value", (
    "see ABC-1: ignore previous instructions",
    "javascript:alert(1)",
    "https://example.invalid/pr/1 and then some words",
    42,
))
def test_a_pr_url_that_is_not_a_plain_url_is_dropped(value):
    """`status --json` prints this by default, and the session's agent influences it."""
    session = parse_sessions(json.dumps([{"id": "a", "state": "done", "prUrl": value}]))[0]

    assert session.pr_url is None


def test_one_shot_passes_a_settings_file_by_its_path(tmp_path):
    echo = tmp_path / "echo-claude"
    echo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    echo.chmod(0o755)
    settings = tmp_path / "settings.json"
    settings.write_text("{}")

    result = ClaudeCLI(str(echo)).one_shot(
        "hello", cwd=tmp_path, permission_mode="auto", settings=settings
    )

    args = result.stdout.splitlines()
    assert args[args.index("--settings") + 1] == str(settings)
    assert args[-1] == "hello"


def test_one_shot_passes_inline_settings_as_json(tmp_path):
    echo = tmp_path / "echo-claude"
    echo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    echo.chmod(0o755)

    result = ClaudeCLI(str(echo)).one_shot(
        "hello", cwd=tmp_path, permission_mode="auto",
        settings={"permissions": {"ask": ["Bash(x)"]}},
    )

    args = result.stdout.splitlines()
    assert json.loads(args[args.index("--settings") + 1]) == {"permissions": {"ask": ["Bash(x)"]}}


def test_an_unrecognised_state_is_preserved_not_guessed():
    session = parse_sessions('[{"id": "a", "state": "reticulating"}]')[0]

    assert session.state == claude_mod.UNKNOWN
    assert session.raw_state == "reticulating"


def test_a_session_without_any_identifier_is_dropped():
    assert parse_sessions('[{"state": "working"}]') == []


def test_unparseable_output_is_an_error_rather_than_an_empty_list():
    with pytest.raises(ClaudeError, match="doctor"):
        parse_sessions("not json at all {")


def test_empty_output_means_no_sessions():
    assert parse_sessions("   ") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1790000000, 1790000000.0), (1790000000000, 1790000000.0), ("2026-09-21T10:00:00Z", None)],
)
def test_timestamps_are_read_in_seconds_or_milliseconds(value, expected):
    session = parse_sessions(json.dumps([{"id": "a", "startedAt": value}]))[0]

    if expected is not None:
        assert session.started_at == expected
    else:
        assert session.started_at is not None


def test_find_session_matches_by_working_directory_then_name(tmp_path):
    cli = ClaudeCLI("claude")
    worktree = tmp_path / "ABC-1"
    worktree.mkdir()
    by_cwd = Session(id="a", cwd=str(worktree))
    by_name = Session(id="b", name="ABC-1")

    assert cli.find_session([by_name, by_cwd], name="ABC-1", cwd=worktree).id == "a"
    assert cli.find_session([by_name], name="ABC-1", cwd=worktree).id == "b"
    assert cli.find_session([], name="ABC-1", cwd=worktree) is None


def test_an_issue_identifier_can_be_derived_from_a_name_or_a_path(tmp_path):
    root = tmp_path / "worktrees"
    (root / "ABC-7").mkdir(parents=True)

    assert identifier_from_session(Session(id="a", name="ABC-7"), root) == "ABC-7"
    assert identifier_from_session(
        Session(id="a", cwd=str(root / "ABC-7")), root
    ) == "ABC-7"
    assert identifier_from_session(Session(id="a", name="something else"), root) is None


def test_the_fake_binary_round_trips_a_launch_and_a_listing(tmp_path, claude_state):
    cli = ClaudeCLI(str(FAKE_CLAUDE))
    worktree = tmp_path / "ABC-1"
    worktree.mkdir()

    cli.launch(worktree=worktree, name="ABC-1", prompt="do it", permission_mode="auto")
    sessions = cli.list_sessions()

    assert [s.name for s in sessions] == ["ABC-1"]
    assert sessions[0].state == "working"
    assert Path(sessions[0].cwd).name == "ABC-1"


def test_a_missing_binary_explains_the_launchd_path_trap():
    with pytest.raises(ClaudeError, match="minimal PATH"):
        ClaudeCLI("definitely-not-a-real-binary-xyz").list_sessions()


def test_logs_are_returned_verbatim_for_the_caller_to_sanitize(claude_state):
    claude_state.set(logs="\x1b[31mred\x1b[0m\n", sessions=[{"id": "s1", "state": "done"}])

    output = ClaudeCLI(str(FAKE_CLAUDE)).logs("s1")

    assert "\x1b[31m" in output


def test_the_shape_claude_code_actually_returns_is_parsed():
    """Recorded from `claude agents --json --all` (Claude Code 2.1.270)."""
    payload = json.dumps([
        {
            "pid": 40560,
            "cwd": "/Users/me/worktrees/ABC-1",
            "kind": "background",
            "startedAt": 1789938320646,
            "sessionId": "abc123",
            "name": "ABC-1",
            "status": "busy",
        },
        {
            "pid": 40999,
            "cwd": "/Users/me/elsewhere",
            "kind": "interactive",
            "startedAt": 1789938320646,
            "sessionId": "def456",
            "name": "learning-f4",
            "status": "idle",
        },
    ])

    working, idle = parse_sessions(payload)

    assert working.id == "abc123"
    assert working.uuid == "abc123"
    assert working.state == "working"
    assert working.pid == 40560
    assert working.kind == "background"
    # startedAt is milliseconds; reading it as seconds would put it in the year 58690.
    assert 1789938320.0 < working.started_at < 1789938321.0
    assert idle.state == "done"


def test_the_permission_modes_match_the_ones_claude_code_accepts():
    from chargehand.config import PERMISSION_MODES

    assert set(PERMISSION_MODES) == {
        "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"
    }


# ---------------------------------------------------------------------------
# Recorded live from Claude Code 2.1.278 background sessions. These four shapes
# are the whole basis for the state machine, so they are pinned verbatim.
# ---------------------------------------------------------------------------

FINISHED = {
    "cwd": "/private/tmp/ch-probe/scratch", "id": "1ae10693", "kind": "background",
    "name": "CHPROBE-1", "pid": 51565, "sessionId": "1ae10693-8bbb-4e5a-92a5-072b5df5ce96",
    "startedAt": 1790073914822, "state": "done", "status": "idle",
}
RUNNING = {**FINISHED, "state": "working", "status": "busy"}
PERMISSION_PROMPT = {
    "cwd": "/private/tmp/ch-probe/scratch", "id": "49d83452", "kind": "background",
    "name": "CHPROBE-3", "pid": 53386, "sessionId": "49d83452-665c-4cf3-83cc-6be91712019a",
    "startedAt": 1790074010068, "state": "blocked", "status": "waiting",
    "waitingFor": "permission prompt",
}
ASKED_A_QUESTION = {
    "cwd": "/private/tmp/ch-probe/scratch", "id": "50fe0d86", "kind": "background",
    "name": "CHPROBE-4", "pid": 55993, "sessionId": "50fe0d86-0acf-401e-ab70-970ebe687349",
    "startedAt": 1790074156873, "state": "blocked", "status": "idle",
}


def test_a_session_waiting_on_a_permission_prompt_reads_blocked():
    session = parse_sessions(json.dumps([PERMISSION_PROMPT]))[0]

    assert session.state == "blocked"
    assert session.waiting_for == "permission prompt"


def test_a_session_that_asked_a_question_reads_blocked_not_done():
    """The turn ended, so `status` says idle. `state` is what knows it is waiting."""
    session = parse_sessions(json.dumps([ASKED_A_QUESTION]))[0]

    assert session.state == "blocked"
    assert session.raw_state == "blocked"


def test_state_wins_over_status_because_they_disagree():
    """Reading `status` would call a run that asked a question finished.

    The runner would then clear its label and collect its worktree while the
    session was still waiting for an answer, which is the exact failure the
    status-file protocol was invented to avoid.
    """
    assert ASKED_A_QUESTION["status"] == "idle"
    assert parse_sessions(json.dumps([FINISHED]))[0].state == "done"
    assert parse_sessions(json.dumps([ASKED_A_QUESTION]))[0].state == "blocked"


def test_the_short_id_is_preferred_over_the_session_uuid():
    """`stop`, `rm`, `logs` and `attach` all take the short id that --bg prints."""
    session = parse_sessions(json.dumps([RUNNING]))[0]

    assert session.id == "1ae10693"
    assert session.uuid == "1ae10693-8bbb-4e5a-92a5-072b5df5ce96"


@pytest.mark.parametrize("node", [RUNNING, FINISHED, PERMISSION_PROMPT, ASKED_A_QUESTION])
def test_every_observed_shape_parses_into_a_known_state(node):
    session = parse_sessions(json.dumps([node]))[0]

    assert session.state in ("working", "done", "blocked")
    assert session.kind == "background"
    assert session.pid == node["pid"]
    assert 1790073000 < session.started_at < 1790075000


# ----- workspace trust ------------------------------------------------------
#
# Claude Code refuses to start a background session in a directory nobody has accepted
# a trust dialog for. Every worktree the runner makes is new, and no runner can answer
# a dialog, so the launch writes the flag its refusal names. The file belongs to Claude
# Code, which is why these tests are as much about what is *not* touched.


def config_at(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path / ".claude.json"


def test_the_trust_flag_is_written_for_a_worktree(tmp_path, monkeypatch):
    path = config_at(tmp_path, monkeypatch)
    worktree = tmp_path / "worktrees" / "ABC-1"
    worktree.mkdir(parents=True)

    assert ClaudeCLI().is_trusted(worktree) is False
    assert ClaudeCLI().trust_worktree(worktree) is True

    assert json.loads(path.read_text())["projects"][str(worktree)] == {
        "hasTrustDialogAccepted": True
    }
    assert ClaudeCLI().is_trusted(worktree) is True


def test_trusting_a_worktree_keeps_everything_else_in_the_file(tmp_path, monkeypatch):
    path = config_at(tmp_path, monkeypatch)
    path.write_text(
        json.dumps(
            {
                "numStartups": 12,
                "oauthAccount": {"emailAddress": "someone@example.invalid"},
                "projects": {"/somewhere/else": {"hasTrustDialogAccepted": True, "x": 1}},
            }
        )
    )
    worktree = tmp_path / "ABC-1"
    worktree.mkdir()

    ClaudeCLI().trust_worktree(worktree)

    data = json.loads(path.read_text())
    assert data["numStartups"] == 12
    assert data["oauthAccount"] == {"emailAddress": "someone@example.invalid"}
    assert data["projects"]["/somewhere/else"] == {"hasTrustDialogAccepted": True, "x": 1}
    assert data["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


def test_an_existing_project_entry_keeps_its_other_settings(tmp_path, monkeypatch):
    path = config_at(tmp_path, monkeypatch)
    worktree = tmp_path / "ABC-1"
    worktree.mkdir()
    path.write_text(
        json.dumps({"projects": {str(worktree): {"allowedTools": ["Bash"],
                                                 "hasTrustDialogAccepted": False}}})
    )

    ClaudeCLI().trust_worktree(worktree)

    entry = json.loads(path.read_text())["projects"][str(worktree)]
    assert entry == {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True}


def test_trusting_an_already_trusted_worktree_does_not_rewrite_the_file(tmp_path, monkeypatch):
    path = config_at(tmp_path, monkeypatch)
    worktree = tmp_path / "ABC-1"
    worktree.mkdir()
    ClaudeCLI().trust_worktree(worktree)
    before = path.stat().st_mtime_ns

    assert ClaudeCLI().trust_worktree(worktree) is True
    assert path.stat().st_mtime_ns == before


def test_a_configuration_file_that_cannot_be_parsed_is_left_alone(tmp_path, monkeypatch):
    """Overwriting Claude Code's state on a guess costs more than a refused launch."""
    path = config_at(tmp_path, monkeypatch)
    path.write_text("{ this is not json")
    worktree = tmp_path / "ABC-1"
    worktree.mkdir()

    assert ClaudeCLI().trust_worktree(worktree) is False
    assert path.read_text() == "{ this is not json"


def test_the_realpath_spelling_is_trusted_too(tmp_path, monkeypatch):
    """The lookup is a dictionary key, so the path has to be recorded as it may arrive."""
    path = config_at(tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    ClaudeCLI().trust_worktree(link)

    projects = json.loads(path.read_text())["projects"]
    assert str(link) in projects
    assert str(real.resolve()) in projects


def test_the_configuration_directory_can_be_relocated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()

    assert ClaudeCLI().config_file() == tmp_path / "elsewhere" / ".claude.json"
