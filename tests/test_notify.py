"""Notifications carry an identifier, a state, and a URL — never issue text or output."""

from __future__ import annotations

import os
import stat

from chargehand.config import NotifyConfig
from chargehand.notify import Notification, Notifier


def test_the_payload_has_no_field_for_free_text():
    env = Notification(issue="ABC-1", state="blocked", url="https://x.invalid/ABC-1").env()

    assert env["CHARGEHAND_ISSUE"] == "ABC-1"
    assert env["CHARGEHAND_STATE"] == "blocked"
    assert env["CHARGEHAND_URL"] == "https://x.invalid/ABC-1"
    assert set(env) == {
        "CHARGEHAND_ISSUE", "CHARGEHAND_STATE", "CHARGEHAND_URL",
        "CHARGEHAND_ROUTE", "CHARGEHAND_DETAIL", "ISSUE", "STATE", "URL",
    }


def test_the_command_receives_the_payload_as_environment(tmp_path):
    script = tmp_path / "notify.sh"
    output = tmp_path / "out"
    script.write_text(
        f'#!/bin/sh\nprintf "%s %s %s" "$CHARGEHAND_ISSUE" "$CHARGEHAND_STATE" '
        f'"$CHARGEHAND_DETAIL" > "{output}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    notifier = Notifier(NotifyConfig(command=str(script)))

    assert notifier.send(Notification("ABC-1", "blocked", detail="needs input")) is True
    assert output.read_text() == "ABC-1 blocked needs input"


def test_a_failing_command_is_logged_rather_than_raised(tmp_path):
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/sh\nexit 4\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    assert Notifier(NotifyConfig(command=str(script))).send(Notification("ABC-1", "blocked")) is False


def test_a_missing_command_does_not_break_a_tick(tmp_path):
    notifier = Notifier(NotifyConfig(command=str(tmp_path / "gone.sh")))

    assert notifier.send(Notification("ABC-1", "blocked")) is False
    assert notifier.sent[-1].issue == "ABC-1"


def test_with_no_command_configured_nothing_is_sent_but_the_call_is_recorded():
    notifier = Notifier(NotifyConfig())

    assert notifier.enabled is False
    assert notifier.send(Notification("ABC-1", "done")) is False
    assert [n.state for n in notifier.sent] == ["done"]


def test_the_bundled_notify_template_runs(tmp_path):
    from chargehand import paths

    script = tmp_path / "notify.sh"
    script.write_text((paths.templates_dir() / "notify.sh").read_text())
    script.chmod(0o755)

    assert Notifier(NotifyConfig(command=str(script))).send(
        Notification("ABC-1", "blocked", url="https://x.invalid")
    ) is True
