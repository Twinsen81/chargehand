"""The lease-pool interface.

The pool is an external tool, and optional: a machine without it runs with the no-op
pool. The runner needs only two calls. It reaps at the start of every tick, and it lists
the leases for `chargehand status`.

The runner never releases a lease itself. The pool records the agent process of every
lease, and a lease whose agent process has ended becomes void after the pool's grace
period. So when the runner stops a session, its leases end without the runner, and the
next reap takes their resources back. A release by the runner would need either the
worktree, which several agents can share, or the agent process, which has already ended
when the runner knows that the session stopped.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


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
        """Take back the resources of void leases. Returns one line for each lease it ended."""

    @abc.abstractmethod
    def status(self) -> list[Lease]:
        """Current leases, for `chargehand status`."""

    @property
    def enabled(self) -> bool:
        return True
