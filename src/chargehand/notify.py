"""Notifications: run a command the operator configures.

The payload is deliberately only the issue identifier, the state, and the issue URL.
A notification usually crosses a third-party relay, so no question text, no titles,
and no agent output go into it.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from chargehand.config import NotifyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    issue: str
    state: str
    url: str | None = None
    route: str | None = None
    detail: str | None = None

    def env(self) -> dict[str, str]:
        payload = {
            "CHARGEHAND_ISSUE": self.issue,
            "CHARGEHAND_STATE": self.state,
            "CHARGEHAND_URL": self.url or "",
            "CHARGEHAND_ROUTE": self.route or "",
            # Short, chargehand-generated reason (never agent or tracker text).
            "CHARGEHAND_DETAIL": self.detail or "",
        }
        # Historic names kept short for hand-written notify scripts.
        payload.update({"ISSUE": self.issue, "STATE": self.state, "URL": self.url or ""})
        return payload


class Notifier:
    def __init__(self, config: NotifyConfig) -> None:
        self.config = config
        self.sent: list[Notification] = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.command)

    def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        if not self.config.command:
            log.info("notify (no command configured): %s -> %s",
                     notification.issue, notification.state)
            return False
        try:
            argv = shlex.split(os.path.expanduser(self.config.command))
        except ValueError as exc:
            log.error("notify: cannot parse notify.command: %s", exc)
            return False
        if not argv:
            return False
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
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("notify: %s failed: %s", argv[0], exc)
            return False
        if result.returncode != 0:
            log.error(
                "notify: %s exited %s: %s",
                argv[0], result.returncode, (result.stderr or "").strip()[:200],
            )
            return False
        return True
