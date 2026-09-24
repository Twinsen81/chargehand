"""Where chargehand keeps its configuration, state, and logs.

Every location is overridable through an environment variable so the test suite
never touches the real ones, and so a second instance can be run side by side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_ENV = "CHARGEHAND_CONFIG"
STATE_DIR_ENV = "CHARGEHAND_STATE_DIR"
LOG_FILE_ENV = "CHARGEHAND_LOG_FILE"

LAUNCHD_LABEL = "dev.chargehand.tick"
REPO_CONFIG_NAME = ".chargehand.toml"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def config_file() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return _home() / ".config" / "chargehand" / "config.toml"


def config_dir() -> Path:
    return config_file().parent


def state_dir() -> Path:
    """Durable state. Deliberately not /tmp: the ledger must survive a reboot."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "chargehand"
    return Path(os.environ.get("XDG_STATE_HOME", _home() / ".local" / "state")) / "chargehand"


def ledger_file() -> Path:
    return state_dir() / "ledger.sqlite"


def tick_lock_file() -> Path:
    return state_dir() / "tick.lock"


def log_file() -> Path:
    override = os.environ.get(LOG_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return _home() / "Library" / "Logs" / "chargehand.log"
    return state_dir() / "chargehand.log"


def is_default_instance() -> bool:
    """Whether this process is the instance the scheduled job would run.

    The LaunchAgent carries no path overrides, so it always runs the default
    configuration and the default state directory. A process started with either
    override is a different instance that happens to share the binary, and anything
    it asks launchd to do would land on the other one's ledger.
    """
    return not (os.environ.get(CONFIG_ENV) or os.environ.get(STATE_DIR_ENV))


def launch_agent_plist() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def ensure_state_dir() -> Path:
    path = state_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
