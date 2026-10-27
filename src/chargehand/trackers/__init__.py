"""Tracker adapter registry."""

from __future__ import annotations

import importlib
from collections.abc import Mapping

from chargehand.config import Repository
from chargehand.errors import ConfigError
from chargehand.trackers.base import Issue, Status, Tracker
from chargehand.trackers.linear import LinearTracker

_BUILTIN: dict[str, type[Tracker]] = {LinearTracker.type: LinearTracker}

# Adapters that live in a source module, imported when first asked for: the sources
# package imports this one, so importing them here would be circular.
_DEFERRED = {"github": ("chargehand.sources.github", "GithubTracker")}


def _adapter(tracker_type: str) -> type[Tracker]:
    if tracker_type not in _BUILTIN and tracker_type in _DEFERRED:
        module, name = _DEFERRED[tracker_type]
        _BUILTIN[tracker_type] = getattr(importlib.import_module(module), name)
    return _BUILTIN[tracker_type]


def register(adapter: type[Tracker]) -> None:
    """Register an adapter. Used by the test suite's fake tracker."""
    if not adapter.type:
        raise ValueError("a tracker adapter needs a non-empty `type`")
    _BUILTIN[adapter.type] = adapter


def available() -> tuple[str, ...]:
    return tuple(sorted(set(_BUILTIN) | set(_DEFERRED)))


def build(repository: Repository) -> Tracker:
    try:
        adapter = _adapter(repository.tracker.type)
    except KeyError as exc:
        raise ConfigError(
            f"repository '{repository.name}': unknown tracker type '{repository.tracker.type}'; "
            f"available: {', '.join(available())}"
        ) from exc
    return adapter(repository.tracker.options, repository.labels.as_dict())  # type: ignore[call-arg]


def build_from(tracker_type: str, options: Mapping[str, object], labels: Mapping[str, str]) -> Tracker:
    try:
        adapter = _adapter(tracker_type)
    except KeyError as exc:
        raise ConfigError(f"unknown tracker type '{tracker_type}'") from exc
    return adapter(options, labels)  # type: ignore[call-arg]


__all__ = ["Issue", "Status", "Tracker", "build", "build_from", "register", "available"]
