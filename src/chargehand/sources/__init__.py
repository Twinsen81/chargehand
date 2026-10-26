"""Event source registry."""

from __future__ import annotations

from chargehand.config import SourceConfig
from chargehand.errors import ConfigError
from chargehand.sources.base import AUTHORSHIP_MARKER, Event, Source, WorkItem

_BUILTIN: dict[str, type[Source]] = {}


def register(adapter: type[Source]) -> None:
    """Register a source type. Used by the built-in sources and the test suite's fakes."""
    if not adapter.type:
        raise ValueError("a source needs a non-empty `type`")
    _BUILTIN[adapter.type] = adapter


def available() -> tuple[str, ...]:
    return tuple(sorted(_BUILTIN))


def build(config: SourceConfig) -> Source:
    try:
        adapter = _BUILTIN[config.type]
    except KeyError as exc:
        raise ConfigError(
            f"source '{config.name}': unknown source type '{config.type}'; "
            f"available: {', '.join(available()) or 'none'}"
        ) from exc
    return adapter(config.name, config.options)


__all__ = [
    "AUTHORSHIP_MARKER",
    "Event",
    "Source",
    "WorkItem",
    "available",
    "build",
    "register",
]
