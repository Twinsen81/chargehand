"""The board's own state: names, icons, pins, collapsed and hidden repositories.

The daemon serves on 127.0.0.1 on a port the kernel picks and runs no loop. Sessions are
the fake `claude`'s, the tracker is the fake one.
"""

from __future__ import annotations

import json
import re

import pytest

from chargehand import board, ledger as ledger_mod
from chargehand.cli import EXIT_OK, main
from chargehand.ledger import Ledger
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}", "Content-Type": "application/json"}
REPOSITORY = ledger_mod.BOARD_REPOSITORY
WORKER = ledger_mod.BOARD_WORKER


@pytest.fixture
def daemon(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    built = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(built, loop=False):
        yield built


def post(daemon, path, body, *, headers=AUTH, host=None):
    raw = json.dumps(body).encode()
    status, payload = support.request(daemon.port, "POST", path, body=raw, headers=headers, host=host)
    return status, json.loads(payload or b"{}")


def page(daemon, query=""):
    status, raw = support.request(daemon.port, "GET", "/api/board" + query)
    assert status == 200
    return json.loads(raw)


def seed(harness, item, *, repository="test", updated_at=None, state=ledger_mod.RUNNING):
    worker = harness.ledger.create_worker(
        role="implementer", item=item, item_id=f"id-{item}", url=None, repository=repository,
        repo=harness.repo, worktree=harness.worktree(item), branch=f"chargehand/{item}",
    )
    worker = harness.ledger.update(worker, state=state)
    if updated_at is not None:
        # update() stamps the time itself; the sort tests need a known order.
        harness.ledger._conn.execute(
            "UPDATE workers SET updated_at = ? WHERE id = ?", (updated_at, worker.id)
        )
    return harness.ledger.get(worker.id)


def model(harness, **kwargs):
    return board.build(harness.config, harness.ledger, health={}, **kwargs)


def column(page_model, key):
    return next(entry for entry in page_model["columns"] if entry["key"] == key)


def items(page_model, key="working"):
    return [card["item"] for card in column(page_model, key)["cards"]]


def run(argv, config_file):
    return main(["--config", str(config_file), *argv])


# ----- the ledger ------------------------------------------------------------------------


def test_board_state_rows_are_set_listed_and_deleted(harness):
    ledger = harness.ledger
    ledger.set_board_state(REPOSITORY, "test", "name", "One", now=10.0)
    ledger.set_board_state(REPOSITORY, "test", "name", "Two", now=11.0)
    ledger.set_board_state(WORKER, "7", "pinned", "1", now=12.0)

    assert ledger.board_state() == [
        {"kind": "repository", "key": "test", "field": "name", "value": "Two", "updated_at": 11.0},
        {"kind": "worker", "key": "7", "field": "pinned", "value": "1", "updated_at": 12.0},
    ]
    ledger.delete_board_state(REPOSITORY, "test", "name")
    assert [row["kind"] for row in ledger.board_state()] == ["worker"]


@pytest.mark.parametrize("kind, field", [
    ("elsewhere", "name"), (REPOSITORY, "colour"), (WORKER, "hidden"), (WORKER, "collapsed"),
])
def test_the_ledger_refuses_an_unknown_kind_or_field(harness, kind, field):
    with pytest.raises(ValueError):
        harness.ledger.set_board_state(kind, "x", field, "1")
    with pytest.raises(ValueError):
        harness.ledger.delete_board_state(kind, "x", field)


# ----- the endpoints ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/repositories/test/board", "/api/workers/{id}/board"])
def test_a_board_setting_needs_the_token_and_a_loopback_host(daemon, harness, path):
    worker = seed(harness, "ABC-1")
    path = path.format(id=worker.id)

    assert post(daemon, path, {"pinned": True}, headers={"Content-Type": "application/json"})[0] == 401
    wrong = {"Authorization": "Bearer not-the-token", "Content-Type": "application/json"}
    assert post(daemon, path, {"pinned": True}, headers=wrong)[0] == 401
    assert post(daemon, path, {"pinned": True}, host="evil.example:80")[0] == 403
    assert harness.ledger.board_state() == []


def test_every_repository_and_worker_setting_is_written_and_shown(daemon, harness):
    worker = seed(harness, "ABC-1")

    status, answer = post(daemon, "/api/repositories/test/board", {
        "name": "Scratch One", "icon": "rocket", "pinned": True, "collapsed": True, "hidden": True,
    })
    assert status == 200
    assert answer == {"kind": "repository", "key": "test", "settings": {
        "name": "Scratch One", "icon": "rocket", "pinned": "1", "collapsed": "1", "hidden": "1",
    }}
    status, answer = post(daemon, f"/api/workers/{worker.id}/board", {
        "name": "The login fix", "icon": "\U0001f41b", "pinned": True,
    })
    assert status == 200
    assert answer["settings"] == {"name": "The login fix", "icon": "\U0001f41b", "pinned": "1"}

    shown = page(daemon, "?show_hidden=1")
    (repository,) = shown["repositories"]
    assert {key: repository[key] for key in (
        "name", "display_name", "icon", "icon_glyph", "pinned", "collapsed", "hidden"
    )} == {
        "name": "test", "display_name": "Scratch One", "icon": "rocket",
        "icon_glyph": board.ICONS["rocket"], "pinned": True, "collapsed": True, "hidden": True,
    }
    (card,) = column(shown, "working")["cards"]
    assert (card["display_name"], card["icon"], card["icon_glyph"], card["pinned"]) == (
        "The login fix", "\U0001f41b", "\U0001f41b", True,
    )
    assert (card["repository"], card["repository_label"], card["hidden"]) == ("test", "Scratch One", True)
    # A display name changes nothing that names the worker or the repository elsewhere.
    assert (card["handle"], card["item"]) == (worker.target, "ABC-1")
    after = harness.ledger.get(worker.id)
    assert (after.repository, after.handle, after.target) == ("test", worker.handle, worker.target)
    # No request row: nothing for the tick to apply.
    assert harness.ledger.pending_requests() == []


def test_false_null_and_empty_drop_a_setting(daemon, harness):
    worker = seed(harness, "ABC-1")
    path = f"/api/workers/{worker.id}/board"
    post(daemon, path, {"name": "Fix", "icon": "star", "pinned": True})

    status, answer = post(daemon, path, {"name": "  ", "icon": None, "pinned": False})

    assert (status, answer["settings"]) == (200, {})
    assert harness.ledger.board_state() == []


def test_one_field_changes_and_the_others_stay(daemon, harness):
    post(daemon, "/api/repositories/test/board", {"name": "One", "hidden": True})
    status, answer = post(daemon, "/api/repositories/test/board", {"hidden": False})
    assert (status, answer["settings"]) == (200, {"name": "One"})


@pytest.mark.parametrize("path, body, message", [
    ("/api/repositories/test/board", {}, "send one or more of"),
    ("/api/repositories/test/board", {"colour": "red"}, "no board setting colour"),
    ("/api/workers/{id}/board", {"hidden": True}, "no board setting hidden"),
    ("/api/workers/{id}/board", {"collapsed": True}, "no board setting collapsed"),
    ("/api/repositories/test/board", {"pinned": "yes"}, "pinned must be true or false"),
    ("/api/repositories/test/board", {"hidden": 1}, "hidden must be true or false"),
    ("/api/repositories/test/board", {"name": 7}, "name must be text"),
    ("/api/repositories/test/board", {"name": "x" * 61}, "at most 60 characters"),
    ("/api/repositories/test/board", {"name": "x" * 1000}, "at most 60 characters"),
    ("/api/workers/{id}/board", {"icon": "<img src=x onerror=alert(1)>"}, "icon must be one emoji"),
    ("/api/workers/{id}/board", {"icon": "<img>"}, "icon must be one emoji"),
    ("/api/workers/{id}/board", {"icon": "https://x.invalid/a.png"}, "icon must be one emoji"),
    ("/api/workers/{id}/board", {"icon": "x\U0001f525"}, "icon must be one emoji"),
    ("/api/workers/{id}/board", {"icon": "\U0001f525" * 11}, "icon must be one emoji"),
    ("/api/workers/{id}/board", {"icon": "nonsense"}, "icon must be one emoji"),
    ("/api/workers/{id}/board", {"icon": ["rocket"]}, "icon must be text"),
    # A bad field leaves the good ones in the same body unwritten.
    ("/api/repositories/test/board", {"name": "Fine", "icon": "<b>"}, "icon must be one emoji"),
])
def test_a_bad_field_or_icon_is_refused_and_nothing_is_written(daemon, harness, path, body, message):
    worker = seed(harness, "ABC-1")

    status, answer = post(daemon, path.format(id=worker.id), body)

    assert status == 400 and message in answer["error"], answer
    assert harness.ledger.board_state() == []


def test_a_body_that_is_not_an_object_is_refused(daemon, harness):
    status, answer = post(daemon, "/api/repositories/test/board", ["pinned"])
    assert status == 400 and "JSON object" in answer["error"]


@pytest.mark.parametrize("path", [
    "/api/repositories/elsewhere/board", "/api/workers/999/board", "/api/things/test/board",
])
def test_an_unknown_kind_repository_or_worker_is_not_found(daemon, harness, path):
    assert post(daemon, path, {"pinned": True})[0] == 404
    assert harness.ledger.board_state() == []


@pytest.mark.parametrize("icon", [
    "\U0001f680",
    "⭐",
    "\U0001f44d\U0001f3fd",
    "\U0001f468‍\U0001f469‍\U0001f467",
    "\U0001f1fa\U0001f1e6",
    "1️⃣",
    *board.ICONS,
])
def test_an_emoji_or_a_bundled_name_is_an_icon(daemon, harness, icon):
    status, answer = post(daemon, "/api/repositories/test/board", {"icon": icon})
    assert (status, answer["settings"]["icon"]) == (200, icon)
    assert board.icon_glyph(icon) == board.ICONS.get(icon, icon)


def test_every_bundled_glyph_is_an_emoji():
    assert all(board.is_emoji(glyph) for glyph in board.ICONS.values())
    assert not any(board.is_emoji(text) for text in ("", "a", "12", "<", "&amp;", "\U0001f525 "))


def test_a_name_loses_its_control_sequences(daemon, harness):
    status, answer = post(daemon, "/api/repositories/test/board", {
        "name": "\x1b[31mRed\x1b[0m‮ one\nline",
    })
    assert (status, answer["settings"]["name"]) == (200, "Red one line")


def test_a_stored_icon_that_is_not_an_icon_is_not_shown(harness):
    # An import writes rows too; the model shows only what the endpoint would accept.
    harness.ledger.set_board_state(REPOSITORY, "test", "icon", "<img src=x>")
    harness.ledger.set_board_state(REPOSITORY, "test", "name", "\x1b]0;x\x07" + "n" * 100)
    (repository,) = model(harness)["repositories"]
    assert (repository["icon"], repository["icon_glyph"]) == (None, None)
    assert len(repository["display_name"]) == board.NAME_LIMIT
    assert "\x1b" not in repository["display_name"]


# ----- order, collapse and hide ----------------------------------------------------------


def test_pinned_cards_come_first_then_the_most_recent(harness):
    old = seed(harness, "OLD-1", updated_at=100.0)
    seed(harness, "MID-1", updated_at=200.0)
    seed(harness, "NEW-1", updated_at=300.0)

    assert items(model(harness)) == ["NEW-1", "MID-1", "OLD-1"]
    harness.ledger.set_board_state(WORKER, str(old.id), "pinned", "1")
    assert items(model(harness)) == ["OLD-1", "NEW-1", "MID-1"]


def test_cards_are_grouped_by_repository_and_a_pinned_repository_comes_first(harness):
    seed(harness, "A-1", repository="test", updated_at=300.0)
    seed(harness, "B-1", repository="other", updated_at=200.0)
    seed(harness, "A-2", repository="test", updated_at=100.0)

    working = column(model(harness), "working")
    assert [group["repository"] for group in working["groups"]] == ["test", "other"]
    assert [card["item"] for card in working["cards"]] == ["A-1", "A-2", "B-1"]

    harness.ledger.set_board_state(REPOSITORY, "other", "pinned", "1")
    working = column(model(harness), "working")
    assert [group["repository"] for group in working["groups"]] == ["other", "test"]
    assert [card["item"] for card in working["cards"]] == ["B-1", "A-1", "A-2"]
    assert [group["count"] for group in working["groups"]] == [1, 2]


def test_a_collapsed_repository_shows_only_its_pinned_cards(harness):
    seed(harness, "A-1", updated_at=300.0)
    pinned = seed(harness, "A-2", updated_at=200.0)
    seed(harness, "A-3", updated_at=100.0, state=ledger_mod.IDLE)
    harness.ledger.set_board_state(WORKER, str(pinned.id), "pinned", "1")
    harness.ledger.set_board_state(REPOSITORY, "test", "collapsed", "1")

    shown = model(harness)

    working = column(shown, "working")
    assert [card["item"] for card in working["cards"]] == ["A-2"]
    (group,) = working["groups"]
    assert (group["collapsed"], group["count"], group["folded"]) == (True, 2, 1)
    assert working["count"] == 2
    idle = column(shown, "idle")
    assert (idle["cards"], idle["groups"][0]["folded"]) == ([], 1)


def test_a_hidden_repository_leaves_the_board_until_the_filter_shows_it(daemon, harness):
    seed(harness, "A-1")
    seed(harness, "B-1", repository="other")
    seed(harness, "B-2", repository="other", state=ledger_mod.BLOCKED)
    harness.ledger.set_board_state(REPOSITORY, "other", "hidden", "1")

    default = page(daemon)
    assert items(default) == ["A-1"] and column(default, "needs_you")["cards"] == []
    assert [group["repository"] for group in column(default, "working")["groups"]] == ["test"]
    assert column(default, "needs_you")["count"] == 0
    assert default["hidden"] == {"repositories": ["other"], "count": 1, "cards": 2, "shown": False}

    shown = page(daemon, "?show_hidden=1")
    assert sorted(items(shown)) == ["A-1", "B-1"]
    assert [card["item"] for card in column(shown, "needs_you")["cards"]] == ["B-2"]
    assert all(card["hidden"] for card in column(shown, "needs_you")["cards"])
    assert shown["hidden"]["shown"] is True


# ----- what does not read it -------------------------------------------------------------


def test_a_blocked_worker_of_a_hidden_repository_is_notified_from_a_hook(daemon, harness):
    harness.board.add("ABC-1")
    harness.tick()
    worker = harness.worker("ABC-1")
    assert post(daemon, "/api/repositories/test/board", {"hidden": True})[0] == 200

    status, raw = support.request(
        daemon.port, "POST", "/hooks/Notification", headers=AUTH,
        body=json.dumps({"session_id": worker.session_uuid, "notification_type": "permission_prompt"}).encode(),
    )

    assert status == 200
    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    assert harness.notifications().count(("ABC-1", "blocked")) == 1
    assert page(daemon)["hidden"]["cards"] == 1


def test_a_hidden_repository_still_launches_and_supervises(harness):
    harness.ledger.set_board_state(REPOSITORY, "test", "hidden", "1")
    harness.ledger.set_board_state(REPOSITORY, "test", "collapsed", "1")
    harness.board.add("ABC-1")
    harness.tick()
    worker = harness.worker("ABC-1")
    assert worker.state == ledger_mod.RUNNING

    harness.claude_state.set_session_state(worker.session_id, "blocked")
    harness.next_tick()

    assert harness.worker("ABC-1").state == ledger_mod.BLOCKED
    assert ("ABC-1", "blocked") in harness.notifications()


def test_status_json_does_not_change_with_the_board_state(capsys, config_file, harness):
    worker = seed(harness, "ABC-1")
    run(["status", "--json", "--no-queue"], config_file)
    before = json.loads(capsys.readouterr().out)

    for kind, key, field, value in (
        (REPOSITORY, "test", "name", "Renamed"), (REPOSITORY, "test", "hidden", "1"),
        (REPOSITORY, "test", "collapsed", "1"), (REPOSITORY, "test", "pinned", "1"),
        (REPOSITORY, "test", "icon", "star"), (WORKER, str(worker.id), "name", "Other"),
        (WORKER, str(worker.id), "pinned", "1"), (WORKER, str(worker.id), "icon", "bug"),
    ):
        harness.ledger.set_board_state(kind, key, field, value)
    assert run(["status", "--json", "--no-queue"], config_file) == EXIT_OK
    after = json.loads(capsys.readouterr().out)

    assert after["workers"] == before["workers"]
    assert {key: value for key, value in after.items() if key != "generated_at"} == {
        key: value for key, value in before.items() if key != "generated_at"
    }


# ----- persistence -----------------------------------------------------------------------


def test_every_setting_survives_a_new_ledger_and_a_new_daemon(harness, tmp_path):
    worker = seed(harness, "ABC-1")
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    with support.running(support.build(config, interval_secs=3600), loop=False) as first:
        post(first, "/api/repositories/test/board", {
            "name": "Scratch One", "icon": "star", "pinned": True, "collapsed": True, "hidden": True,
        })
        post(first, f"/api/workers/{worker.id}/board", {"name": "Fix", "icon": "bug", "pinned": True})
        before = page(first, "?show_hidden=1")

    with Ledger(harness.ledger.path) as reopened:
        assert len(reopened.board_state()) == 8
    with support.running(support.build(config, interval_secs=3600), loop=False) as second:
        after = page(second, "?show_hidden=1")
        assert page(second)["hidden"]["count"] == 1

    assert after["repositories"] == before["repositories"]
    assert column(after, "working") == column(before, "working")


# ----- the page --------------------------------------------------------------------------


def test_the_page_tells_the_board_pin_apart_and_writes_through_the_board_routes():
    script = (board.asset_dir() / "board.js").read_text()
    assert "'board pin'" in script
    assert "It does not keep the session running" in script
    assert "+ '/board'" in script
    assert "?show_hidden=1" in script
    assert "maxLength = NAME_MAX" in script and "const NAME_MAX = 60;" in script
    assert f"const NAME_MAX = {board.NAME_LIMIT};" in script
    # The icon and the names reach the page as text only.
    assert re.search(r"innerHTML|outerHTML|insertAdjacentHTML", script) is None
    page_html = (board.asset_dir() / "index.html").read_text()
    assert 'id="show-hidden"' in page_html and 'id="hidden-count"' in page_html
