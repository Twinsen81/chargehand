"""Open a terminal window that runs one command: how the board attaches to a session.

This is the only module that knows the terminal applications and how to open them. It
supports Terminal.app and iTerm2, on macOS only, and accepts only a command of plain
words (letters, digits and ``._/:=-``, separated by single spaces) whose last word, the
session id, is a plain identifier.

Terminal.app is opened through LaunchServices (``/usr/bin/open``) on a script file that
the daemon writes into a private directory, so it needs no Automation permission. The
script deletes itself and then execs the command; a script that never ran is removed
when the next one is written.

iTerm2 asks "OK to run" for every file it is asked to open, so it is still driven
through ``osascript`` and needs the Automation permission once. The command is never put
into the AppleScript source: the script reads it from its argument list, so no quoting in
the command can change the script.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from chargehand import paths, sanitize

ITERM2 = "iterm2"
TERMINAL_APP = "terminal"
# The first is the default and comes first on the board: it needs no macOS permission.
TERMINALS = (TERMINAL_APP, ITERM2)
DEFAULT_TERMINAL = TERMINAL_APP

OPEN = "/usr/bin/open"
OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_SECS = 15.0
SCRIPT_SUFFIX = ".command"
# Long enough for a slow Terminal.app start to reach the script, short enough that a
# script that never ran does not stay for long.
STALE_SECS = 300

_COMMAND = re.compile(r"[A-Za-z0-9._/:=-]+(?: [A-Za-z0-9._/:=-]+)*")

# The command is the script's one argument.
_ITERM2_SCRIPT = (
    "on run argv",
    'tell application "iTerm"',
    "activate",
    "set attached to (create window with default profile)",
    "tell current session of attached to write text (item 1 of argv)",
    "end tell",
    "end run",
)

Run = Callable[..., subprocess.CompletedProcess]


def is_plain_command(command: str) -> bool:
    return bool(_COMMAND.fullmatch(command))


def _check(terminal: str, command: str) -> None:
    if terminal not in TERMINALS:
        raise ValueError(f"unknown terminal {terminal!r}; expected one of {', '.join(TERMINALS)}")
    if not is_plain_command(command):
        raise ValueError("the command is not a plain command line")
    if not sanitize.is_safe_identifier(command.rsplit(" ", 1)[-1]):
        raise ValueError("the session id is not a plain identifier")


def osascript_argv(command: str) -> list[str]:
    """The `osascript` command line that opens iTerm2 with *command*. ValueError if unusable."""
    _check(ITERM2, command)
    argv = [OSASCRIPT]
    for line in _ITERM2_SCRIPT:
        argv += ["-e", line]
    return [*argv, command]


def open_argv(script: Path) -> list[str]:
    return [OPEN, "-a", "Terminal", str(script)]


def script_text(command: str) -> str:
    """The attach script: it removes itself first, so it runs at most once."""
    return f'#!/bin/sh\nrm -f -- "$0"\nexec {command}\n'


def script_dir() -> Path:
    return paths.state_dir() / "attach"


def write_script(command: str, directory: Path, *, now: float | None = None) -> Path:
    """Write the attach script for *command* into *directory*, private to this user."""
    _check(TERMINAL_APP, command)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")
    os.chmod(directory, 0o700)
    remove_stale(directory, now=time.time() if now is None else now)
    path = directory / f"attach-{secrets.token_hex(8)}{SCRIPT_SUFFIX}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    try:
        os.fchmod(fd, 0o700)
        os.write(fd, script_text(command).encode("ascii"))
    finally:
        os.close(fd)
    return path


def remove_stale(directory: Path, *, now: float) -> None:
    for path in directory.glob(f"attach-*{SCRIPT_SUFFIX}"):
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime > STALE_SECS:
                path.unlink()
        except OSError:
            continue


def _run(argv: list[str], run: Run) -> tuple[bool, str]:
    program = Path(argv[0]).name
    try:
        result = run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECS, check=False)
    except subprocess.TimeoutExpired:
        return False, f"{program} did not answer within {TIMEOUT_SECS:.0f}s"
    except OSError as exc:
        return False, f"{program} could not be run: {exc}"
    if result.returncode != 0:
        detail = sanitize.one_line(result.stderr or result.stdout or "", limit=200)
        return False, f"{program} exited {result.returncode}: {detail}"
    return True, ""


def open_window(
    terminal: str,
    command: str,
    *,
    run: Run = subprocess.run,
    platform: str = sys.platform,
    directory: Path | None = None,
) -> tuple[bool, str]:
    """Open a window of *terminal* that runs *command*. Whether it did, and what to say."""
    if platform != "darwin":
        return False, "attach opens a terminal window on macOS only"
    try:
        _check(terminal, command)
    except ValueError as exc:
        return False, str(exc)
    if terminal == ITERM2:
        ok, message = _run(osascript_argv(command), run)
        if not ok:
            # The usual cause is that macOS has not allowed this program to control
            # iTerm2 yet: System Settings, Privacy & Security, Automation.
            return False, (
                f"{message}. iTerm2 needs Automation permission: allow it under Privacy & "
                f"Security, Automation, or choose Terminal, which needs none"
            )
    else:
        try:
            script = write_script(command, script_dir() if directory is None else directory)
        except (OSError, ValueError) as exc:
            return False, f"could not write the attach script: {exc}"
        ok, message = _run(open_argv(script), run)
        if not ok:
            script.unlink(missing_ok=True)
            return False, message
    return True, f"opened a {terminal} window running `{command}`"


__all__ = [
    "DEFAULT_TERMINAL",
    "ITERM2",
    "TERMINALS",
    "TERMINAL_APP",
    "is_plain_command",
    "open_argv",
    "open_window",
    "osascript_argv",
    "script_text",
    "write_script",
]
