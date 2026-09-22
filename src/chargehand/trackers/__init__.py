"""Tracker adapter registry."""

from __future__ import annotations

from collections.abc import Mapping

from chargehand.config import Route
from chargehand.errors import ConfigError
from chargehand.trackers.base import Issue, Status, Tracker
from chargehand.trackers.linear import LinearTracker

_BUILTIN: dict[str, type[Tracker]] = {LinearTracker.type: LinearTracker}


def register(adapter: type[Tracker]) -> None:
    """Register an adapter. Used by the test suite's fake tracker."""
    if not adapter.type:
        raise ValueError("a tracker adapter needs a non-empty `type`")
    _BUILTIN[adapter.type] = adapter


def available() -> tuple[str, ...]:
    return tuple(sorted(_BUILTIN))


def build(route: Route) -> Tracker:
    try:
        adapter = _BUILTIN[route.tracker.type]
    except KeyError as exc:
        raise ConfigError(
            f"route '{route.name}': unknown tracker type '{route.tracker.type}'; "
            f"available: {', '.join(available())}"
        ) from exc
    return adapter(route.tracker.options, route.labels.as_dict())  # type: ignore[call-arg]


def build_from(tracker_type: str, options: Mapping[str, object], labels: Mapping[str, str]) -> Tracker:
    try:
        adapter = _BUILTIN[tracker_type]
    except KeyError as exc:
        raise ConfigError(f"unknown tracker type '{tracker_type}'") from exc
    return adapter(options, labels)  # type: ignore[call-arg]


__all__ = ["Issue", "Status", "Tracker", "build", "build_from", "register", "available"]
