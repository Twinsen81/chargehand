"""Lease pool: interface plus the no-op implementation the first releases ship."""

from chargehand.pool.base import Lease, Pool
from chargehand.pool.noop import NoopPool

__all__ = ["Lease", "Pool", "NoopPool"]
