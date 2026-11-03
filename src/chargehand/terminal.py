"""Open a terminal window that runs one command: how the board attaches to a session.

This is the only module that knows `osascript` and the terminal applications. It
supports iTerm2 and Terminal.app, on macOS only. The command is never put into the
AppleScript source: the script reads it from its argument list, so no quoting in the
command can change the script. The terminal then runs the command in a shell, so only
a command of plain words is accepted: letters, digits and ``._/:=-``, separated by
single spaces.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable

from chargehand import sanitize

ITERM2 = "iterm2"
TERMINAL_APP = "terminal"
TERMINALS = (ITERM2, TERMINAL_APP)
DEFAULT_TERMINAL = ITERM2

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_SECS = 15.0

_COMMAND = re.compile(r"[A-Za-z0-9._/:=-]+(?: [A-Za-z0-9._/:=-]+)*")

# Each script takes the command as its one argument.
_SCRIPTS = {
    ITERM2: (
        "on run argv",
        'tell application "iTerm"',
        "activate",
        "set attached to (create window with default profile)",
        "tell current session of attached to write text (item 1 of argv)",
        "end tell",
        "end run",
    ),
    TERMINAL_APP: (
        "on run argv",
        'tell application "Terminal"',
        "activate",
        "do script (item 1 of argv)",
        "end tell",
        "end run",
    ),
}

Run = Callable[..., subprocess.CompletedProcess]


def is_plain_command(command: str) -> bool:
    return bool(_COMMAND.fullmatch(command))


def osascript_argv(terminal: str, command: str) -> list[str]:
    """The `osascript` command line that opens *terminal* with *command*. ValueError if unusable."""
    if terminal not in _SCRIPTS:
        raise ValueError(f"unknown terminal {terminal!r}; expected one of {', '.join(TERMINALS)}")
    if not is_plain_command(command):
        raise ValueError("the command is not a plain command line")
    argv = [OSASCRIPT]
    for line in _SCRIPTS[terminal]:
        argv += ["-e", line]
    return [*argv, command]


def open_window(
    terminal: str,
    command: str,
    *,
    run: Run = subprocess.run,
    platform: str = sys.platform,
) -> tuple[bool, str]:
    """Open a window of *terminal* that runs *command*. Whether it did, and what to say."""
    if platform != "darwin":
        return False, "attach opens a terminal window on macOS only"
    try:
        argv = osascript_argv(terminal, command)
    except ValueError as exc:
        return False, str(exc)
    try:
        result = run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECS, check=False)
    except subprocess.TimeoutExpired:
        return False, f"osascript did not answer within {TIMEOUT_SECS:.0f}s"
    except OSError as exc:
        return False, f"osascript could not be run: {exc}"
    if result.returncode != 0:
        # The usual cause is macOS asking whether this program may control the terminal:
        # System Settings, Privacy & Security, Automation.
        detail = sanitize.one_line(result.stderr or result.stdout or "", limit=200)
        return False, (
            f"osascript exited {result.returncode}: {detail}. If macOS asked for permission, "
            f"allow it under Privacy & Security, Automation, and try again"
        )
    return True, f"opened a {terminal} window running `{command}`"


__all__ = [
    "DEFAULT_TERMINAL",
    "ITERM2",
    "TERMINALS",
    "TERMINAL_APP",
    "is_plain_command",
    "open_window",
    "osascript_argv",
]
