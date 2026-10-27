"""Any system without a built-in source: an executable that prints events as JSON lines.

The operator configures commands, not code. chargehand never imports them: each one runs
as a subprocess with a timeout, without a shell, and its output is checked against the
closed shapes documented in ``docs/command-source.md``. Everything a command prints is
untrusted text, like any other source's. A line that does not fit is logged and skipped,
and it never stops the poll.

    [[source]]
    name = "jira"
    type = "command"
    poll = "jira-events"                         # reads the cursor on stdin
    mark = "jira-mark {ref} {status}"            # optional
    item_open = "jira-open {ref}"                # optional
    account = "me@example.com"                   # the actor name the workers post as
    repositories = ["app"]                       # where its start events may start workers
    timeout_secs = 30
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from chargehand import paths, sanitize
from chargehand.config import render_argv
from chargehand.errors import AmbiguousWrite, ConfigError, TrackerError
from chargehand.sources.base import Event, Source, WorkItem
from chargehand.trackers.base import Status

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECS = 30.0
# What one poll may print. A source with more to say returns the rest on the next poll,
# from the cursor it printed.
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
# Only for the error message of a failed command.
MAX_STDERR_BYTES = 4096

# The mark command's exit codes. Any other non-zero code is a failure.
EXIT_OK = 0
EXIT_AMBIGUOUS = 3

EVENT_REQUIRED = ("kind", "item", "actor", "occurred_at", "dedupe_id", "pointer")
EVENT_OPTIONAL = ("source", "ref", "url", "repository", "marked")
CURSOR_KEY = "cursor"
# The refs of the items that live workers follow, one per line, for the poll command.
REFS_ENV = "CHARGEHAND_REFS"

_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_TEXT = 256
_MAX_POINTER = 2048

_OPTIONS = ("poll", "mark", "item_open", "account", "repositories", "timeout_secs")
_POLL_VARS = ("source",)
_ITEM_VARS = ("source", "item", "ref")
_MARK_VARS = (*_ITEM_VARS, "status")


class Skip(ValueError):
    """One output line does not fit the contract."""


@dataclass(frozen=True)
class Output:
    returncode: int
    stdout: bytes
    stderr: str


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    # The command runs in its own process group, so a shell script's children go too
    # and nothing is left holding the pipes open.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run(
    argv: Sequence[str],
    *,
    stdin: bytes | None,
    timeout: float,
    cwd: Path | None,
    env: Mapping[str, str] | None = None,
) -> Output:
    """Run *argv* with *stdin* and *env* added to the environment, and read at most :data:`MAX_OUTPUT_BYTES` of its stdout.

    Raises :class:`TrackerError` when it cannot start, does not finish in *timeout*
    seconds, or prints more than the cap.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env={**os.environ, **env} if env else None,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise TrackerError(f"{argv[0]} not found") from exc
    except OSError as exc:
        raise TrackerError(f"could not run {argv[0]}: {exc}") from exc

    out = bytearray()
    err = bytearray()
    overflow = threading.Event()

    def read_out() -> None:
        assert proc.stdout is not None
        while chunk := proc.stdout.read1(65536):
            if len(out) + len(chunk) > MAX_OUTPUT_BYTES:
                overflow.set()
                _kill_group(proc)
                return
            out.extend(chunk)

    def read_err() -> None:
        assert proc.stderr is not None
        while chunk := proc.stderr.read1(65536):
            err.extend(chunk[: max(0, MAX_STDERR_BYTES - len(err))])

    def write_in() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin or b"")
            proc.stdin.close()
        except OSError:
            # The command did not read its stdin; that is its choice.
            pass

    threads = [threading.Thread(target=read_out, daemon=True),
               threading.Thread(target=read_err, daemon=True)]
    if stdin is not None:
        threads.append(threading.Thread(target=write_in, daemon=True))
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    # A child that the command left behind may still hold its stdout open.
    timed_out = timed_out or any(thread.is_alive() for thread in threads)
    if timed_out or overflow.is_set():
        _kill_group(proc)
        proc.wait()
        for thread in threads:
            thread.join(1.0)
    if timed_out and not overflow.is_set():
        raise TrackerError(f"{argv[0]} did not finish within {timeout:g}s")
    if overflow.is_set():
        raise TrackerError(f"{argv[0]} printed more than {MAX_OUTPUT_BYTES} bytes")
    return Output(proc.returncode, bytes(out), err.decode("utf-8", errors="replace"))


# ----- parsing -------------------------------------------------------------------


def _text(entry: Mapping[str, Any], key: str, *, limit: int = _MAX_TEXT, empty: bool = False) -> str:
    value = entry[key]
    if not isinstance(value, str):
        raise Skip(f"{key} is not a string")
    if not value and not empty:
        raise Skip(f"{key} is empty")
    if len(value) > limit:
        raise Skip(f"{key} is longer than {limit} characters")
    return value


def _timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise Skip("occurred_at is not a time")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise Skip("occurred_at is not an ISO 8601 time") from exc
        if parsed.tzinfo is None:
            raise Skip("occurred_at has no time zone")
        value = parsed.timestamp()
    if not isinstance(value, (int, float)):
        raise Skip("occurred_at is not a time")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise Skip("occurred_at is out of range") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise Skip("occurred_at is out of range")
    return seconds


def _decode(raw: bytes) -> object:
    if len(raw) > MAX_LINE_BYTES:
        raise Skip(f"the line is longer than {MAX_LINE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Skip("the line is not UTF-8") from exc
    except ValueError as exc:
        raise Skip("the line is not JSON") from exc
    except RecursionError as exc:
        raise Skip("the line is nested too deeply") from exc


def _is_cursor_line(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {CURSOR_KEY}


class CommandSource(Source):
    """``[[source]] type = "command"``: see ``docs/command-source.md`` for the contract."""

    type = "command"

    def __init__(self, name: str, options: Mapping[str, object]) -> None:
        super().__init__(name, options)
        where = f"source {name}"
        unknown = sorted(set(options) - set(_OPTIONS))
        if unknown:
            raise ConfigError(f"{where}: unknown option(s): {', '.join(unknown)}")
        if "poll" not in options:
            raise ConfigError(f"{where}: missing required option 'poll'")
        poll = self._template(options, "poll", _POLL_VARS, where)
        assert poll is not None
        self.poll_command: str = poll
        self.mark_command = self._template(options, "mark", _MARK_VARS, where)
        self.item_open_command = self._template(options, "item_open", _ITEM_VARS, where)
        account = options.get("account", "")
        if not isinstance(account, str):
            raise ConfigError(f"{where}.account: expected a string")
        self.account = account
        repositories = options.get("repositories", [])
        if not isinstance(repositories, list) or not all(
            isinstance(r, str) and r for r in repositories
        ):
            raise ConfigError(f"{where}.repositories: expected a list of repository names")
        self.repositories: tuple[str, ...] = tuple(repositories)
        timeout = options.get("timeout_secs", DEFAULT_TIMEOUT_SECS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"{where}.timeout_secs: expected a positive number")
        self.timeout = float(timeout)

    @staticmethod
    def _template(
        options: Mapping[str, object], key: str, names: Sequence[str], where: str
    ) -> str | None:
        if key not in options:
            return None
        template = options[key]
        if not isinstance(template, str) or not template.strip():
            raise ConfigError(f"{where}.{key}: expected a non-empty command")
        # Rendered once with placeholder values, so an unknown variable is a configuration
        # error now rather than a failed poll later.
        render_argv(template, {n: n for n in names}, where=f"{where}.{key}")
        return template

    def describe(self) -> str:
        return f"{self.name} (command {shlex.split(self.poll_command)[0]})"

    def whoami(self) -> str:
        return self.account

    @property
    def marks_items(self) -> bool:
        return self.mark_command is not None

    # ----- the three commands ---------------------------------------------------

    def poll(
        self, cursor: str | None, items: Sequence[WorkItem]
    ) -> tuple[list[Event], str | None]:
        # A system that is asked item by item needs the items that live workers follow.
        # They go in the environment, so a command that does not need them ignores them.
        refs = [
            item.ref for item in items if item.ref == sanitize.clean(item.ref, keep_newlines=False)
        ]
        output = self._run(
            "poll",
            self.poll_command,
            {},
            stdin=(cursor or "").encode("utf-8"),
            env={REFS_ENV: "\n".join(refs)},
        )
        if output.returncode != EXIT_OK:
            raise self._failure("poll", output)
        lines = [
            (number, raw.strip())
            for number, raw in enumerate(output.stdout.split(b"\n"), start=1)
            if raw.strip()
        ]
        next_cursor = cursor
        found, value = self._cursor_line(*lines[-1]) if lines else (False, None)
        if found:
            lines.pop()
            next_cursor = value
        else:
            log.warning("source %s: the poll printed no cursor line; keeping the cursor", self.name)
        events: list[Event] = []
        for number, raw in lines:
            try:
                events.append(self._event(_decode(raw)))
            except Skip as exc:
                log.warning("source %s: skipped output line %d: %s", self.name, number, exc)
            except Exception as exc:
                # A line the checks did not foresee is still one line: it must not fail
                # the poll, or the command prints it again on every tick.
                log.warning(
                    "source %s: skipped output line %d: %s", self.name, number, type(exc).__name__
                )
        return events, next_cursor

    def mark(self, item: WorkItem, status: Status) -> None:
        """Run the mark command. Exit 3 means the write may or may not have landed."""
        if self.mark_command is None:
            return None
        values = {"item": item.key, "ref": item.ref, "status": status.value}
        output = self._run("mark", self.mark_command, values, stdin=None)
        if output.returncode == EXIT_AMBIGUOUS:
            raise AmbiguousWrite(
                f"source {self.name}: mark {item.key} {status.value} may or may not have "
                f"landed: {sanitize.one_line(output.stderr, limit=300)}"
            )
        if output.returncode != EXIT_OK:
            raise self._failure("mark", output)
        return None

    def item_open(self, item: WorkItem) -> bool:
        """Ask the item_open command; an item is open when no command is configured."""
        if self.item_open_command is None:
            return True
        values = {"item": item.key, "ref": item.ref}
        output = self._run("item_open", self.item_open_command, values, stdin=None)
        if output.returncode != EXIT_OK:
            raise self._failure("item_open", output)
        lines = [line for line in output.stdout.split(b"\n") if line.strip()]
        try:
            answer = _decode(lines[-1].strip()) if lines else None
        except Skip:
            answer = None
        if isinstance(answer, dict) and set(answer) == {"open"} and isinstance(answer["open"], bool):
            return answer["open"]
        raise TrackerError(f'source {self.name}: item_open did not print {{"open": true|false}}')

    # ----- helpers --------------------------------------------------------------

    def _run(
        self,
        what: str,
        template: str,
        values: Mapping[str, str],
        *,
        stdin: bytes | None,
        env: Mapping[str, str] | None = None,
    ) -> Output:
        argv = render_argv(
            template, {"source": self.name, **values}, where=f"source {self.name}.{what}"
        )
        argv[0] = os.path.expanduser(argv[0])
        program = Path(argv[0])
        root = paths.config_dir()
        if not program.is_absolute():
            # As a repository's setup script resolves against its checkout, a relative
            # program resolves against the configuration's directory when it is there.
            candidate = root / program
            if candidate.exists():
                argv[0] = str(candidate)
        try:
            return run(
                argv,
                stdin=stdin,
                timeout=self.timeout,
                cwd=root if root.is_dir() else None,
                env=env,
            )
        except TrackerError as exc:
            raise TrackerError(f"source {self.name}: {what}: {exc}") from exc

    def _failure(self, what: str, output: Output) -> TrackerError:
        detail = sanitize.one_line(output.stderr, limit=300)
        return TrackerError(f"source {self.name}: {what} exited {output.returncode}: {detail}")

    def _cursor_line(self, number: int, raw: bytes) -> tuple[bool, str | None]:
        """Whether *raw* is a valid cursor line, and the cursor it carries."""
        try:
            value = _decode(raw)
        except Exception:
            return False, None
        if not _is_cursor_line(value):
            return False, None
        cursor = value[CURSOR_KEY]  # type: ignore[index]
        if cursor is not None and not isinstance(cursor, str):
            log.warning("source %s: line %d: the cursor is not a string", self.name, number)
            return False, None
        return True, cursor

    def _event(self, entry: object) -> Event:
        if _is_cursor_line(entry):
            raise Skip("a cursor line that is not the last line, or not a valid one")
        if not isinstance(entry, dict):
            raise Skip("the line is not a JSON object")
        unknown = sorted(set(entry) - set(EVENT_REQUIRED) - set(EVENT_OPTIONAL))
        if unknown:
            names = sanitize.one_line(", ".join(str(key) for key in unknown), limit=80)
            raise Skip(f"unknown field(s): {names}")
        missing = [key for key in EVENT_REQUIRED if key not in entry]
        if missing:
            raise Skip(f"missing field(s): {', '.join(missing)}")
        if "source" in entry and entry["source"] != self.name:
            raise Skip("the event names another source")
        kind = _text(entry, "kind")
        if not _KIND.fullmatch(kind):
            raise Skip("kind is not a bare lower-case name")
        key = _text(entry, "item")
        if not sanitize.is_safe_identifier(key):
            raise Skip("item is not usable as a path or branch name")
        ref = _text(entry, "ref") if "ref" in entry else key
        if ref != sanitize.clean(ref, keep_newlines=False):
            raise Skip("ref contains control characters")
        url = None
        if "url" in entry:
            url = sanitize.safe_url(entry["url"])
            if url is None:
                raise Skip("url is not a plain http(s) URL")
        hint = self._repository(entry)
        marked = entry.get("marked", False)
        if not isinstance(marked, bool):
            raise Skip("marked is not true or false")
        return Event(
            source=self.name,
            kind=kind,
            item=WorkItem(source=self.name, key=key, ref=ref, url=url, hint=hint),
            actor=_text(entry, "actor", empty=True),
            occurred_at=_timestamp(entry["occurred_at"]),
            dedupe_id=_text(entry, "dedupe_id"),
            pointer=_text(entry, "pointer", limit=_MAX_POINTER),
            marked=marked,
        )

    def _repository(self, entry: Mapping[str, Any]) -> str | None:
        """The bound repository a start event belongs to, as the work item's hint."""
        if "repository" not in entry:
            return self.repositories[0] if len(self.repositories) == 1 else None
        name = _text(entry, "repository")
        if name not in self.repositories:
            raise Skip("repository is not one of the source's repositories")
        return name


__all__ = ["CommandSource", "EXIT_AMBIGUOUS", "MAX_LINE_BYTES", "MAX_OUTPUT_BYTES", "REFS_ENV"]
