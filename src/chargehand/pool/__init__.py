"""Lease pool: the interface, the banksman pool, and the no-op pool."""

from __future__ import annotations

import os
import shutil

from chargehand.pool.banksman import BanksmanPool
from chargehand.pool.base import Lease, Pool
from chargehand.pool.noop import NoopPool

__all__ = ["BanksmanPool", "Lease", "Pool", "NoopPool", "select"]


def select(binary: str) -> Pool:
    """The banksman pool when *binary* resolves on PATH, and the no-op pool otherwise.

    A tick runs with the PATH of the scheduled job, so this is the job's view, which
    `doctor` checks as well.
    """
    where = shutil.which(os.path.expanduser(binary))
    return BanksmanPool(where) if where else NoopPool()
