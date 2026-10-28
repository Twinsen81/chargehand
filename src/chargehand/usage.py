"""Usage readings: the subscription's five-hour and weekly windows.

A status line command gets a JSON payload on stdin, and
:func:`chargehand.claude.status_line_windows` finds the two windows in it. The wrapper
(``chargehand statusline -- '<your command>'``) writes those two windows to the usage
file and then runs the user's own status line command with the same stdin, so the
status line shows what it showed before.

The wrapper runs on every status line refresh, so it does little: one read of stdin,
one small atomic write, one subprocess. Nothing it does to the usage file can change
the status line: a payload without the windows leaves the file as it is, and a failed
write is ignored.

The daemon reads the file for each ``/api/state``. A reading older than
:data:`~chargehand.delivery.USAGE_MAX_AGE_SECS` is unknown, and so is a window whose
reset time has passed, because its percentage belongs to the window before.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from chargehand import claude, paths
from chargehand.config import ServeConfig
from chargehand.delivery import USAGE_MAX_AGE_SECS

# The windows the file holds, in the order the board shows them.
WINDOWS = ("five_hour", "seven_day")

GREEN = "green"
AMBER = "amber"
RED = "red"
GREY = "grey"
# A window is amber from this percentage used, and red from the next one.
AMBER_FROM = 60.0
RED_FROM = 80.0

# The file is a few hundred bytes. A larger one is not a reading.
MAX_FILE_BYTES = 64 * 1024


# ----- the wrapper -------------------------------------------------------------


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def percent(value: object) -> float | None:
    """A percentage clamped to 0..100, or None when *value* is not a number."""
    number = _number(value)
    return None if number is None else min(100.0, max(0.0, number))


def resets_at(value: object) -> float | None:
    """A reset time as epoch seconds, from epoch seconds or an ISO 8601 string.

    An ISO time without a zone is read as UTC.
    """
    number = _number(value)
    if number is not None:
        return number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return _number(float(text))
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def windows_from_payload(raw: bytes) -> dict[str, dict[str, float | None]] | None:
    """The windows of a status line payload with a percentage, or None when none has one."""
    windows: dict[str, dict[str, float | None]] = {}
    for name, window in (claude.status_line_windows(raw) or {}).items():
        used = percent(window.get("used_percent"))
        if name in WINDOWS and used is not None:
            windows[name] = {"used_percent": used, "resets_at": resets_at(window.get("resets_at"))}
    return windows or None


def write_usage(path: Path, windows: Mapping[str, object], *, now: float | None = None) -> None:
    """Write *windows* to *path* atomically, with mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps(
        {**windows, "written_at": time.time() if now is None else now}, sort_keys=True
    )
    # mkstemp creates the file with mode 0600, in the same directory so the rename is atomic.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(body)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def configured_usage_file(config_path: Path) -> Path:
    """The usage file of `[serve]` in *config_path*, or the default without a configuration.

    Only `[serve]` is read: the wrapper must not wait for, or fail on, the rest.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return paths.usage_file()
    return ServeConfig.parse(tomllib.loads(text).get("serve")).usage_file


def user_command(words: Sequence[str]) -> str | None:
    """The user's status line command as one shell string.

    One word is a shell command already, as the `statusLine` setting holds it. Several
    words were split by a shell before, so they are quoted back into one command.
    """
    words = list(words)
    if words and words[0] == "--":
        words = words[1:]
    if not words:
        return None
    return words[0] if len(words) == 1 else shlex.join(words)


def run_statusline(
    stdin: bytes, command: str | None, usage_path: "Path | None", *, now: float | None = None
) -> int:
    """Record the windows of *stdin*, then run *command* with the same stdin.

    The command's stdout and stderr are this process's own, so they reach the status
    line unchanged; its exit code is returned. *usage_path* is None when the
    configuration could not name a file.
    """
    if usage_path is not None:
        try:
            windows = windows_from_payload(stdin)
            if windows:
                write_usage(usage_path, windows, now=now)
        except Exception:
            # The status line is the user's; a usage file that cannot be written must not
            # change it.
            pass
    if command is None:
        return 0
    # The status line setting is a shell command, so it runs through a shell here too.
    code = subprocess.run(command, shell=True, input=stdin, check=False).returncode
    return code if code >= 0 else 128 - code


# ----- the reader --------------------------------------------------------------


def level(used_percent: float | None) -> str:
    if used_percent is None:
        return GREY
    if used_percent >= RED_FROM:
        return RED
    if used_percent >= AMBER_FROM:
        return AMBER
    return GREEN


def _unknown_window() -> dict[str, object]:
    return {"used_percent": None, "resets_at": None, "level": GREY}


def read_usage(path: Path, now: float | None = None) -> dict[str, object]:
    """The reading in *path*, for the board: each window's percent, reset time and level.

    ``known`` is False when the file is missing, unreadable, or older than
    :data:`USAGE_MAX_AGE_SECS`; then every window is grey.
    """
    current = time.time() if now is None else now
    result: dict[str, object] = {
        "known": False,
        "written_at": None,
        "age_secs": None,
        **{name: _unknown_window() for name in WINDOWS},
    }
    try:
        with path.open("rb") as reader:
            raw = reader.read(MAX_FILE_BYTES + 1)
        data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_FILE_BYTES else None
    except (OSError, UnicodeDecodeError, ValueError):
        return result
    if not isinstance(data, Mapping):
        return result
    written_at = _number(data.get("written_at"))
    if written_at is None:
        return result
    age = max(0.0, current - written_at)
    result["written_at"] = written_at
    result["age_secs"] = round(age, 1)
    if age > USAGE_MAX_AGE_SECS:
        return result
    result["known"] = True
    for name in WINDOWS:
        window = data.get(name)
        if not isinstance(window, Mapping):
            continue
        used = percent(window.get("used_percent"))
        reset = _number(window.get("resets_at"))
        if used is None or (reset is not None and reset <= current):
            continue
        result[name] = {"used_percent": used, "resets_at": reset, "level": level(used)}
    return result


__all__ = [
    "AMBER",
    "AMBER_FROM",
    "GREEN",
    "GREY",
    "RED",
    "RED_FROM",
    "WINDOWS",
    "configured_usage_file",
    "level",
    "percent",
    "read_usage",
    "resets_at",
    "run_statusline",
    "user_command",
    "windows_from_payload",
    "write_usage",
]
