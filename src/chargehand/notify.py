"""Notifications: run a command the operator configures.

The payload is deliberately only the work item, the role, the state, and the item's
URL. A notification usually crosses a third-party relay, so no question text, no
titles, and no agent output go into it.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass

from chargehand import sanitize
from chargehand.config import NotifyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    item: str
    state: str
    url: str | None = None
    repository: str | None = None
    role: str | None = None
    detail: str | None = None

    def env(self) -> dict[str, str]:
        payload = {
            "CHARGEHAND_ITEM": self.item,
            "CHARGEHAND_ROLE": self.role or "",
            "CHARGEHAND_STATE": self.state,
            "CHARGEHAND_URL": self.url or "",
            "CHARGEHAND_REPOSITORY": self.repository or "",
            # Short, chargehand-generated reason (never agent or tracker text).
            "CHARGEHAND_DETAIL": self.detail or "",
            # The names hooks were written against before work items and repositories.
            "CHARGEHAND_ISSUE": self.item,
            "CHARGEHAND_ROUTE": self.repository or "",
        }
        # Historic names kept short for hand-written notify scripts.
        payload.update({"ISSUE": self.item, "STATE": self.state, "URL": self.url or ""})
        return payload


@dataclass(frozen=True)
class Outcome:
    """What a send did. *outcome* is chargehand's own phrase, for the log."""

    sent: bool
    failed: bool
    outcome: str


class Notifier:
    def __init__(self, config: NotifyConfig) -> None:
        self.config = config
        self.sent: list[Notification] = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.command)

    def send(self, notification: Notification) -> bool:
        return self.dispatch(notification).sent

    def dispatch(self, notification: Notification) -> Outcome:
        self.sent.append(notification)
        if not self.config.command:
            return Outcome(sent=False, failed=False, outcome="no command configured")
        try:
            argv = shlex.split(os.path.expanduser(self.config.command))
        except ValueError as exc:
            log.error("notify: cannot parse notify.command: %s", exc)
            return Outcome(sent=False, failed=True, outcome="notify.command does not parse")
        if not argv:
            return Outcome(sent=False, failed=True, outcome="notify.command is empty")
        argv[0] = os.path.expanduser(argv[0])
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_secs,
                check=False,
                env={**os.environ, **notification.env()},
            )
        except subprocess.TimeoutExpired:
            log.error("notify: %s timed out after %ss", argv[0], self.config.timeout_secs)
            return Outcome(sent=False, failed=True, outcome="timed out")
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("notify: %s failed: %s", argv[0], exc)
            return Outcome(sent=False, failed=True, outcome="could not run the command")
        if result.returncode != 0:
            log.error(
                "notify: %s exited %s: %s",
                argv[0], result.returncode, sanitize.one_line(result.stderr or "", limit=200),
            )
            return Outcome(sent=False, failed=True, outcome=f"exit {result.returncode}")
        return Outcome(sent=True, failed=False, outcome="sent")
