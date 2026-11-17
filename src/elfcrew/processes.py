"""The processes below a session's process, and the end of those that outlive its stop.

A background shell that a session's turn started is a child process of the session, in a
process group of its own. A stop of the session does not end it: the shell gets PID 1 as
its parent and runs on, and no session owns it. So a stop reads the processes below the
session before the stop, and ends the ones that still run after it.

Only this module reads the process table (``ps``) and signals processes that elfcrew
did not start. A process is known by its pid and its start time, so a pid that the system
gave to a new process in the meantime is never signalled.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

# How long an end waits for the processes to exit after SIGTERM, before SIGKILL.
END_WAIT_SECS = 3.0
END_POLL_SECS = 0.1
# How many pids a log line names.
PIDS_SHOWN = 10


@dataclass(frozen=True)
class Process:
    """A process as the table showed it: *started* tells it from a later one with its pid."""

    pid: int
    group: int
    started: str


@dataclass(frozen=True)
class Row:
    process: Process
    parent: int


def read_table() -> list[Row]:
    """Every process of this machine, or an empty list when ``ps`` fails."""
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid=,lstart="],
            capture_output=True, text=True, timeout=30, check=False,
            env={**os.environ, "LC_ALL": "C"}, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not read the process table: %s", exc)
        return []
    if completed.returncode != 0:
        log.warning("could not read the process table: ps exited %s", completed.returncode)
        return []
    return parse_table(completed.stdout)


def parse_table(text: str) -> list[Row]:
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 3)
        if len(fields) != 4:
            continue
        try:
            pid, parent, group = (int(value) for value in fields[:3])
        except ValueError:
            continue
        rows.append(Row(Process(pid, group, " ".join(fields[3].split())), parent))
    return rows


def send_signal(target: int, signum: int, *, group: bool) -> None:
    """Send *signum* to the process *target*, or with *group* to its process group."""
    try:
        if group:
            os.killpg(target, signum)
        else:
            os.kill(target, signum)
    except (ProcessLookupError, PermissionError):
        pass


def below(pid: int) -> tuple[Process, ...]:
    """The processes below *pid*, at every depth, without *pid* itself."""
    children: dict[int, list[Process]] = {}
    for row in read_table():
        children.setdefault(row.parent, []).append(row.process)
    found: list[Process] = []
    parents = [pid]
    seen = {pid}
    while parents:
        for child in children.get(parents.pop(), ()):
            if child.pid not in seen:
                seen.add(child.pid)
                found.append(child)
                parents.append(child.pid)
    return tuple(found)


def running(processes: Iterable[Process]) -> tuple[Process, ...]:
    """Those of *processes* that still run: the same pid with the same start time."""
    wanted = tuple(processes)
    if not wanted:
        return ()
    current = {row.process.pid: row.process.started for row in read_table()}
    return tuple(p for p in wanted if current.get(p.pid) == p.started)


def _signal(process: Process, signum: int) -> None:
    if process.pid <= 1 or process.pid == os.getpid():
        return
    if process.group == process.pid and process.group not in (1, os.getpgrp()):
        # A background shell leads a group of its own: the group also holds what it
        # started after the table was read.
        send_signal(process.group, signum, group=True)
    send_signal(process.pid, signum, group=False)


def end(processes: Sequence[Process]) -> tuple[tuple[Process, ...], tuple[Process, ...]]:
    """End those of *processes* that still run: SIGTERM, then SIGKILL after END_WAIT_SECS.

    Returns the processes that ran, and those that still run afterwards.
    """
    found = running(processes)
    if not found:
        return (), ()
    for process in found:
        _signal(process, signal.SIGTERM)
    left = running(found)
    deadline = time.monotonic() + END_WAIT_SECS
    while left and time.monotonic() < deadline:
        time.sleep(END_POLL_SECS)
        left = running(left)
    if left:
        for process in left:
            _signal(process, signal.SIGKILL)
        time.sleep(END_POLL_SECS)
        left = running(left)
    return found, left


def pid_list(processes: Sequence[Process]) -> str:
    shown = ", ".join(str(p.pid) for p in processes[:PIDS_SHOWN])
    more = len(processes) - PIDS_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def end_after_stop(processes: Sequence[Process], *, who: str, session: str) -> tuple[Process, ...]:
    """End the processes below *session* that outlived its confirmed stop, and log them.

    *who* names the worker and *session* the session, for the log; both are elfcrew's
    own text. Returns the processes that could not be ended.
    """
    found, left = end(processes)
    ended = tuple(p for p in found if p not in left)
    if ended:
        log.info("%s: ended %d process(es) that outlived the stop of session %s: %s",
                 who, len(ended), session, pid_list(ended))
    if left:
        log.warning("%s: %d process(es) below the stopped session %s did not end: %s",
                    who, len(left), session, pid_list(left))
    return left
