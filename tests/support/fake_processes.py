"""A process table for the tests, in place of `ps` and real signals.

No test reads the machine's process table or signals a real process. A test adds the
processes that a session started, below the fake session's pid, and reads back which
signals went to them.
"""

from __future__ import annotations

import signal

from elfcrew.processes import Process, Row


class FakeProcesses:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.signals: list[tuple[int, int, bool]] = []
        # Pids that ignore SIGTERM, such as a shell that traps it.
        self.ignore_term: set[int] = set()
        # Pids that no signal ends.
        self.unkillable: set[int] = set()
        self._serial = 0

    def add(self, pid: int, parent: int, *, group: int | None = None) -> Process:
        self._serial += 1
        process = Process(pid, pid if group is None else group, f"Mon Nov 16 10:00:{self._serial:02d} 2026")
        self.rows.append(Row(process, parent))
        return process

    def reparent(self, pid: int, parent: int = 1) -> None:
        """What a stop of the session does to its background shell."""
        self.rows = [Row(r.process, parent) if r.process.pid == pid else r for r in self.rows]

    def replace(self, pid: int) -> None:
        """The process ended, and a new process got its pid."""
        self.rows = [
            Row(Process(r.process.pid, r.process.group, "Tue Nov 17 09:00:00 2026"), r.parent)
            if r.process.pid == pid else r
            for r in self.rows
        ]

    def pids(self) -> set[int]:
        return {row.process.pid for row in self.rows}

    def read_table(self) -> list[Row]:
        return list(self.rows)

    def send_signal(self, target: int, signum: int, *, group: bool) -> None:
        self.signals.append((target, signum, group))
        hit = {
            row.process.pid for row in self.rows
            if (row.process.group == target if group else row.process.pid == target)
        }
        if signum == signal.SIGTERM:
            hit -= self.ignore_term
        hit -= self.unkillable
        self.rows = [row for row in self.rows if row.process.pid not in hit]
