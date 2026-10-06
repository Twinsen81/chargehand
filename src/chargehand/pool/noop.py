"""A pool that holds nothing, for a machine without the lease pool."""

from __future__ import annotations

from chargehand.pool.base import Lease, Pool


class NoopPool(Pool):
    def reap(self) -> list[str]:
        return []

    def status(self) -> list[Lease]:
        return []

    @property
    def enabled(self) -> bool:
        return False
