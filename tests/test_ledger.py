"""The ledger's guarantees: one live attempt per issue, and durable control requests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chargehand import ledger as ledger_mod
from chargehand.ledger import Ledger


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    with Ledger(tmp_path / "ledger.sqlite") as handle:
        yield handle


def make(ledger: Ledger, identifier: str = "ABC-1", worktree: str = "/w/ABC-1"):
    return ledger.create_attempt(
        issue_id=f"id-{identifier}",
        identifier=identifier,
        url=None,
        route="test",
        repo=Path("/repo"),
        worktree=Path(worktree),
        branch=f"chargehand/{identifier}",
    )


def test_a_new_attempt_starts_at_step_zero(ledger):
    attempt = make(ledger)

    assert attempt.state == ledger_mod.LAUNCHING
    assert attempt.step == 0
    assert attempt.launch_attempts == 1
    assert attempt.label_state is None


def test_the_database_itself_prevents_a_duplicate_live_attempt(ledger):
    make(ledger)

    with pytest.raises(sqlite3.IntegrityError):
        make(ledger, worktree="/w/other")


def test_a_new_attempt_is_allowed_once_the_previous_one_ended(ledger):
    first = make(ledger)
    ledger.update(first, state=ledger_mod.FAILED)

    second = make(ledger)

    assert second.id != first.id
    assert ledger.live_attempt_for("ABC-1").id == second.id
    assert ledger.latest_attempt_for("ABC-1").id == second.id


def test_reaching_a_terminal_state_stamps_the_finish_time(ledger):
    attempt = ledger.update(make(ledger), state=ledger_mod.DONE)

    assert attempt.is_terminal
    assert attempt.finished_at is not None


def test_an_unknown_column_is_rejected_rather_than_silently_dropped(ledger):
    with pytest.raises(ValueError, match="unknown attempt column"):
        ledger.update(make(ledger), stat="running")


def test_an_unknown_state_is_rejected(ledger):
    with pytest.raises(ValueError, match="unknown attempt state"):
        ledger.update(make(ledger), state="vibing")


def test_an_attempt_can_be_found_by_its_session_id(ledger):
    attempt = ledger.update(make(ledger), session_id="s1")

    assert ledger.attempt_by_session_id("s1").id == attempt.id
    assert ledger.attempt_by_session_id("nope") is None


def test_requests_are_pending_until_a_tick_completes_them(ledger):
    request_id = ledger.record_request("cancel", "ABC-1", force=True)

    assert [r["kind"] for r in ledger.pending_requests()] == ["cancel"]
    assert ledger.get_request(request_id)["args"] == {"force": True}

    ledger.complete_request(request_id, ok=False, message="refused")

    assert ledger.pending_requests() == []
    assert ledger.get_request(request_id)["ok"] == 0


def test_pausing_is_global_or_per_route(ledger):
    assert not ledger.is_paused("test")

    ledger.set_paused(True, "test")
    assert ledger.is_paused("test")
    assert not ledger.is_paused("other")
    assert ledger.paused_routes() == ["test"]

    ledger.set_paused(True)
    assert ledger.is_paused("other")

    ledger.set_paused(False)
    ledger.set_paused(False, "test")
    assert ledger.paused_routes() == []


def test_the_ledger_survives_being_reopened(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as first:
        make(first)
    with Ledger(path) as second:
        assert second.live_attempt_for("ABC-1") is not None


def test_a_newer_schema_is_refused_rather_than_corrupted(tmp_path):
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={ledger_mod.SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(RuntimeError, match="upgrade chargehand"):
        Ledger(path)


def test_applied_requests_are_pruned_but_pending_ones_are_kept(ledger):
    old = ledger.record_request("tick")
    ledger.complete_request(old, ok=True, message="")
    pending = ledger.record_request("cancel", "ABC-1")

    assert ledger.prune_requests(older_than_secs=-1) == 1
    assert [r["id"] for r in ledger.pending_requests()] == [pending]
