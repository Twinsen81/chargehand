"""The lease-pool interface.

The pool itself is deferred: parallel runs sharing scarce local resources is a real
problem, but the runner has to be proven on its own first. The interface exists from
the start so the watchdog, cancel, discard, and garbage-collection paths already call
it, and turning the pool on later changes one line of wiring rather than five call
sites.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Lease:
    resource: str
    kind: str
    state: str
    owner: str
    issue: str | None = None
    expires_in_secs: float | None = None


class Pool(abc.ABC):
    """Leases, not locks: a lease held by a dead run expires instead of persisting."""

    @abc.abstractmethod
    def reap(self) -> list[str]:
        """Void expired leases and terminate their registered users. Returns what was reaped."""

    @abc.abstractmethod
    def release_all(self, owner: Path) -> list[str]:
        """Release every lease held by *owner* (a worktree). Returns what was released."""

    @abc.abstractmethod
    def status(self) -> list[Lease]:
        """Current leases, for `chargehand status`."""

    @property
    def enabled(self) -> bool:
        return True
