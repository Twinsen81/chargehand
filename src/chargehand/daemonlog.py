"""The daemon's log: one format, a level from the configuration, rotation, quiet polls.

Every line goes through :class:`OneLineFormatter`, so a message that quotes a script, a
tracker or a command's stderr cannot break the line or reach the terminal that tails
the file. What a worker said or did in its session never reaches a log call at all;
the formatter is the second line of defence, not the first.

The daemon writes the file itself, through a size-limited rotating handler. The launchd
job's own output paths point to another file, which only holds what the interpreter
prints before logging is set up, such as a crash on import: a rotation renames the log
file, and launchd would go on writing into the renamed one.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from chargehand import sanitize

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}
DEFAULT_LEVEL = "info"

# Ten files of this size at most: the log and its backups.
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

# The longest message line. Longer ones are cut, as a report line is.
MESSAGE_LIMIT = 1000

# How long the polls of a source that found nothing are held back before one line
# reports them. Without a limit an idle daemon would write nothing for hours, and a
# daemon that stopped polling would look the same as one that polls and finds nothing.
QUIET_POLL_WINDOW_SECS = 15 * 60

# Marks the handler this module installed, so that a second setup replaces it.
_HANDLER_MARK = "_chargehand_handler"

poll_log = logging.getLogger("chargehand.poll")


class OneLineFormatter(logging.Formatter):
    """Each message on one line with no control sequences; a traceback keeps its lines."""

    def __init__(self) -> None:
        super().__init__(FORMAT)

    def formatMessage(self, record: logging.LogRecord) -> str:
        record.message = sanitize.one_line(record.message, limit=MESSAGE_LIMIT)
        return super().formatMessage(record)

    def formatException(self, ei) -> str:
        return sanitize.clean(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return sanitize.clean(super().formatStack(stack_info))


def level_for(verbosity: int, configured: str | None = None) -> int:
    """`-v` decides when given; otherwise the configured name, otherwise WARNING."""
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return logging.INFO
    if configured is not None:
        return LEVELS[configured]
    return logging.WARNING


def setup(level: int, *, log_file: Path | None = None, stream: TextIO | None = None) -> None:
    """Send every chargehand log line to *log_file*, rotated, or to *stream* (stderr).

    A handler that an earlier call installed is replaced; handlers that others added to
    the root logger, such as a test's, are left alone.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    elif root.handlers:
        # Someone else, such as pytest, already collects the records.
        return
    else:
        handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(OneLineFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)


def set_level(level: int) -> None:
    logging.getLogger().setLevel(level)


# ----- polls ---------------------------------------------------------------------


def _cursor(value: str | None) -> str:
    # A command source prints its own cursor; it is the operator's command, but capped.
    return sanitize.one_line(value, limit=80) if value else "none"


def _clock_time(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


@dataclass
class _Quiet:
    first_at: float
    last_at: float
    count: int
    cursor_before: str | None
    cursor_after: str | None
    longest: float


class PollLog:
    """One line per poll that found new events, and one line per source for the polls that did not.

    A source reads some minutes back from its previous poll, so a poll often returns events
    it returned before; only the new ones make a poll worth its own line. The daemon keeps
    one for its lifetime, across ticks. A *window_secs* of 0 writes every poll at once, as
    a tick run by hand does.
    """

    def __init__(
        self,
        window_secs: float = QUIET_POLL_WINDOW_SECS,
        *,
        clock: Callable[[], float] = time.time,
        log: logging.Logger = poll_log,
    ) -> None:
        self.window_secs = window_secs
        self._clock = clock
        self._log = log
        self._quiet: dict[str, _Quiet] = {}

    def polled(
        self,
        source: str,
        before: str | None,
        after: str | None,
        events: int,
        duration: float,
        *,
        new: int | None = None,
    ) -> None:
        """Record one poll that returned *events*, of which *new* were not seen before."""
        now = self._clock()
        new = events if new is None else new
        if new or self.window_secs <= 0:
            self.flush(source)
            self._log.info(
                "poll %s: %d event(s), %d new, cursor %s -> %s, %.1fs",
                source, events, new, _cursor(before), _cursor(after), duration,
            )
            return
        quiet = self._quiet.get(source)
        if quiet is None:
            self._quiet[source] = _Quiet(now, now, 1, before, after, duration)
        else:
            quiet.last_at, quiet.cursor_after = now, after
            quiet.count += 1
            quiet.longest = max(quiet.longest, duration)
        if now - self._quiet[source].first_at >= self.window_secs:
            self.flush(source)

    def failed(self, source: str) -> None:
        """A poll of *source* failed; its quiet polls are written first, so the order holds."""
        self.flush(source)

    def flush(self, source: str | None = None) -> None:
        """Write the held-back polls of *source*, or of every source."""
        names = [source] if source is not None else sorted(self._quiet)
        for name in names:
            quiet = self._quiet.pop(name, None)
            if quiet is None:
                continue
            self._log.info(
                "poll %s: %d poll(s) found nothing new from %s to %s, cursor %s -> %s, longest %.1fs",
                name, quiet.count, _clock_time(quiet.first_at), _clock_time(quiet.last_at),
                _cursor(quiet.cursor_before), _cursor(quiet.cursor_after), quiet.longest,
            )


__all__ = [
    "DEFAULT_LEVEL",
    "LEVELS",
    "OneLineFormatter",
    "PollLog",
    "level_for",
    "set_level",
    "setup",
]
