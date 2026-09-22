"""A pool that holds nothing, so the runner can be proven before the pool exists."""

from __future__ import annotations

from pathlib import Path

from chargehand.pool.base import Lease, Pool


class NoopPool(Pool):
    def reap(self) -> list[str]:
        return []

    def release_all(self, owner: Path) -> list[str]:
        return []

    def status(self) -> list[Lease]:
        return []

    @property
    def enabled(self) -> bool:
        return False
