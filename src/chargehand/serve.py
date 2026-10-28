"""The resident daemon: the tick on a timer, and a loopback HTTP API.

The main thread runs the loop. It builds a fresh :class:`~chargehand.tick.Runner`
for each tick with the configuration read again, as each run of a timer job did, and
holds the tick lock only while that tick runs, so a `chargehand tick` by hand still
serialises against it. Between ticks it looks for recorded control requests about
once a second and runs a tick at once when a new one waits. That keeps one writer of
side effects: the CLI records, the daemon applies.

A second thread serves HTTP on 127.0.0.1 only. Reads need nothing; every other method
must carry the token from a mode 0600 file. A request whose Host header names another
host is refused, which stops a web page that rebinds its own name to 127.0.0.1 from
reading the API, and no response carries a CORS header. Other modules add their
handlers to the route table, and their part of `/api/state`; each handler thread opens
its own ledger connection, because a SQLite connection belongs to the thread that opened
it. :mod:`chargehand.hooks` receives the posts of the worker sessions there, and
:mod:`chargehand.board` serves the read-only board.

SIGTERM and SIGINT end the loop between two tick phases: the phase in progress
finishes, the rest of that tick is skipped, the server shuts down, and the exit code
is 0.
"""

from __future__ import annotations

import hmac
import http.client
import importlib
import json
import logging
import os
import secrets
import signal
import socketserver
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlsplit

from chargehand import __version__, paths, sanitize
from chargehand.config import MachineConfig
from chargehand.errors import ChargehandError, TickBusy
from chargehand.ledger import Ledger
from chargehand.tick import Runner, TickReport, tick_lock

log = logging.getLogger(__name__)

# The only address the daemon binds. No option changes it.
HOST = "127.0.0.1"

# How often the loop looks for a recorded control request between ticks.
REQUEST_POLL_SECS = 1.0
# The largest request body a handler gets. A larger one is refused before it is read.
MAX_BODY_BYTES = 1024 * 1024
# How long a client waits for `/api/health`.
HEALTH_TIMEOUT_SECS = 2.0
# How long a connection may stay idle or slow before the server drops it. Without a limit,
# connections that send nothing would hold a thread and a file descriptor each until the
# daemon has none left for the tick's own subprocesses.
CONNECTION_TIMEOUT_SECS = 10.0

# The methods that change nothing and need no token.
SAFE_METHODS = ("GET", "HEAD")

# Modules that add routes, each through a `register_routes(routes)` function.
ROUTE_PROVIDERS: tuple[str, ...] = ("chargehand.hooks", "chargehand.board")

# What `/api/state` says about sessions before the first tick has listed them.
NO_SESSIONS_YET = "the daemon has not listed the sessions yet"

Sessions = tuple[list[dict[str, object]], "str | None"]


# ----- the token -------------------------------------------------------------


def token_problem(path: Path) -> str | None:
    """Why the token file must not be used, or None when it is a private regular file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return f"{path} does not exist; the daemon writes it when it starts"
    except OSError as exc:
        return f"{path}: {exc}"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if info.st_uid != os.getuid():
        return f"{path} belongs to another user"
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return f"{path} has mode {mode:o}, so other users can read it; run `chmod 600 {path}`"
    return None


def ensure_token(path: Path) -> str:
    """The daemon's token. Write a new one when the file is missing.

    O_EXCL makes the file exist with mode 0600 from its first byte, and a file that
    exists is never rewritten, so a client that read the token keeps a valid one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(secrets.token_urlsafe(32) + "\n")
    problem = token_problem(path)
    if problem:
        raise ChargehandError(problem)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ChargehandError(f"{path} is empty; remove it and the daemon writes a new one")
    return token


def read_token(path: Path) -> str | None:
    """The token for a client, or None when the file is missing or not private."""
    if token_problem(path):
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ----- requests, responses, routes -------------------------------------------


class HTTPError(Exception):
    """Ends a request with *status*. The message is chargehand's own text."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    body: bytes
    daemon: "Daemon"
    _ledger: Ledger | None = field(default=None, repr=False)

    def ledger(self) -> Ledger:
        """A ledger connection of this handler thread, closed when the response is sent."""
        if self._ledger is None:
            self._ledger = Ledger(self.daemon.ledger_path)
        return self._ledger

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPError(400, "the body is not JSON") from None

    def close(self) -> None:
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None


@dataclass(frozen=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    # Sent after the headers every response carries.
    headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def json(cls, payload: object, status: int = 200) -> "Response":
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return cls(status=status, body=body)

    @classmethod
    def error(cls, status: int, message: str) -> "Response":
        return cls.json({"error": message}, status=status)


Handler = Callable[[Request], Response]
# Adds a module's part to the `/api/state` payload, in place.
StateExtender = Callable[[Request, dict[str, object]], None]


class Routes:
    """Method and path to handler. A prefix route takes every path below it."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, str, bool, Handler]] = []
        self.state_extenders: list[StateExtender] = []

    def extend_state(self, extender: StateExtender) -> None:
        self.state_extenders.append(extender)

    def add(self, method: str, path: str, handler: Handler, *, prefix: bool = False) -> None:
        method = method.upper()
        for known_method, known_path, known_prefix, _ in self._routes:
            if (known_method, known_path, known_prefix) == (method, path, prefix):
                raise ValueError(f"a route for {method} {path} exists already")
        self._routes.append((method, path, prefix, handler))

    def match(self, method: str, path: str) -> tuple[Handler | None, bool]:
        """The handler for *method* on *path*, and whether any route has the path."""
        known = False
        best: tuple[int, Handler] | None = None
        for route_method, route_path, prefix, handler in self._routes:
            if prefix:
                hit = path == route_path or path.startswith(route_path.rstrip("/") + "/")
            else:
                hit = path == route_path
            if not hit:
                continue
            known = True
            # The longest path wins, so a module can take a part of another's prefix.
            if route_method == method and (best is None or len(route_path) > best[0]):
                best = (len(route_path), handler)
        return (best[1] if best else None), known


def build_routes(providers: Iterable[str] = ROUTE_PROVIDERS) -> Routes:
    routes = Routes()
    routes.add("GET", "/api/health", lambda request: Response.json(request.daemon.health()))
    routes.add("GET", "/api/state", lambda request: Response.json(request.daemon.state(request)))
    for name in providers:
        importlib.import_module(name).register_routes(routes)
    return routes


# ----- the daemon --------------------------------------------------------------


# The configuration, a ledger of the handler thread, the sessions of the last tick and the
# daemon's health, to the `status --json` payload.
StatusBuilder = Callable[
    [MachineConfig, Ledger, Sessions, dict[str, object]], dict[str, object]
]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The server closes each connection first, so its port stays in TIME_WAIT for about
    # half a minute after a stop. Without SO_REUSEADDR a daemon that launchd starts again
    # in that time cannot bind. A socket that listens on 127.0.0.1 at the port still makes
    # the bind fail; on macOS one that listens on the wildcard address (0.0.0.0) does not.
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], owner: "Daemon") -> None:
        self.owner = owner
        super().__init__(address, _HTTPHandler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks the address up with socket.getfqdn before the socket
        # listens. That reverse lookup can take tens of seconds on a machine whose resolver
        # is slow, and the daemon answers nothing meanwhile. The name is only used in CGI
        # variables, so the address itself is enough.
        socketserver.TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]


class _HTTPHandler(BaseHTTPRequestHandler):
    timeout = CONNECTION_TIMEOUT_SECS

    def version_string(self) -> str:
        return "chargehand"

    def _handle(self) -> None:
        owner: Daemon = self.server.owner  # type: ignore[attr-defined]
        try:
            response = owner.dispatch(self.command, self.path, self.headers, self.rfile)
        except Exception:
            log.exception("%s %s failed", self.command, urlsplit(self.path).path)
            response = Response.error(500, "internal error; see the daemon's log")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, format: str, *args: object) -> None:
        log.debug("http: " + format, *args)


@dataclass
class Health:
    started_at: float = field(default_factory=time.time)
    ticks: int = 0
    failed_ticks: int = 0
    ticking: bool = False
    last_tick_started_at: float | None = None
    last_tick_finished_at: float | None = None
    last_tick_error: str | None = None


class Daemon:
    """The loop and the server. Every collaborator is injectable for tests."""

    def __init__(
        self,
        config: MachineConfig,
        *,
        load_config: Callable[[], MachineConfig] | None = None,
        runner_factory: Callable[[MachineConfig, Ledger], Runner] | None = None,
        status: StatusBuilder | None = None,
        sessions: Callable[[Runner], Sessions] | None = None,
        report: Callable[[TickReport], None] | None = None,
        ledger_path: Path | None = None,
        lock_path: Path | None = None,
        port: int | None = None,
        token: str | None = None,
        interval_secs: float | None = None,
        poll_secs: float = REQUEST_POLL_SECS,
        providers: Iterable[str] = ROUTE_PROVIDERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._load_config = load_config or (lambda: self.config)
        self._runner_factory = runner_factory or (lambda cfg, ledger: Runner(cfg, ledger))
        self._status = status
        self._list_sessions = sessions
        self._report = report or (lambda report: None)
        # The tick and the hook threads print reports into the same log.
        self._report_lock = threading.Lock()
        self.ledger_path = ledger_path or paths.ledger_file()
        self._lock_path = lock_path
        self._port = config.serve.port if port is None else port
        self._token = token
        self._interval = interval_secs
        self._poll_secs = poll_secs
        self._clock = clock
        self.routes = build_routes(providers)
        self.stopping = threading.Event()
        # Set by the signal handler, which must not take the lock inside an Event: the
        # main thread may hold it at the moment the signal arrives.
        self._signalled = False
        self.health_data = Health()
        self._sessions: Sessions = ([], NO_SESSIONS_YET)
        self._seen_request = 0
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # ----- the server -----------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else self._port

    @property
    def interval_secs(self) -> float:
        return self._interval if self._interval is not None else self.config.serve.tick_interval_secs

    def start(self) -> None:
        """Read or write the token, bind, and serve on a thread of its own."""
        if self._token is None:
            self._token = ensure_token(self.config.serve.token_file)
        try:
            self._server = _Server((HOST, self._port), self)
        except OSError as exc:
            raise ChargehandError(f"cannot listen on {HOST}:{self._port}: {exc}") from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
            name="chargehand-http", daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def allowed_hosts(self) -> set[str]:
        return {f"{HOST}:{self.port}", f"localhost:{self.port}"}

    def dispatch(
        self, method: str, target: str, headers: Mapping[str, str], body: BinaryIO
    ) -> Response:
        try:
            return self._dispatch(method.upper(), target, headers, body)
        except HTTPError as exc:
            return Response.error(exc.status, str(exc))

    def _dispatch(
        self, method: str, target: str, headers: Mapping[str, str], body: BinaryIO
    ) -> Response:
        host = (headers.get("Host") or "").strip().lower()
        if host not in self.allowed_hosts():
            raise HTTPError(403, "unknown Host header")
        if method not in SAFE_METHODS and not self._authorized(headers):
            raise HTTPError(401, "a valid bearer token is required")
        payload = self._read_body(headers, body)
        parts = urlsplit(target)
        handler, known = self.routes.match("GET" if method == "HEAD" else method, parts.path)
        if handler is None:
            raise HTTPError(405 if known else 404, "method not allowed" if known else "not found")
        request = Request(
            method=method, path=parts.path, query=parse_qs(parts.query),
            headers=headers, body=payload, daemon=self,
        )
        try:
            return handler(request)
        finally:
            request.close()

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        scheme, _, given = (headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not given or not self._token:
            return False
        return hmac.compare_digest(given.strip().encode("utf-8"), self._token.encode("utf-8"))

    @staticmethod
    def _read_body(headers: Mapping[str, str], body: BinaryIO) -> bytes:
        if headers.get("Transfer-Encoding"):
            raise HTTPError(411, "send a Content-Length, not a chunked body")
        raw = headers.get("Content-Length")
        if not raw:
            return b""
        try:
            length = int(raw)
        except ValueError:
            raise HTTPError(400, "the Content-Length is not a number") from None
        if length < 0:
            raise HTTPError(400, "the Content-Length is negative")
        if length > MAX_BODY_BYTES:
            raise HTTPError(413, f"the body is larger than {MAX_BODY_BYTES} bytes")
        return body.read(length)

    # ----- what the routes answer -------------------------------------------

    def health(self) -> dict[str, object]:
        data = self.health_data
        return {
            "ok": True,
            "version": __version__,
            "pid": os.getpid(),
            "port": self.port,
            "ledger": os.path.realpath(self.ledger_path),
            "tick_interval_secs": self.interval_secs,
            "started_at": data.started_at,
            "ticks": data.ticks,
            "failed_ticks": data.failed_ticks,
            "ticking": data.ticking,
            "last_tick_started_at": data.last_tick_started_at,
            "last_tick_finished_at": data.last_tick_finished_at,
            "last_tick_error": data.last_tick_error,
        }

    def state(self, request: Request) -> dict[str, object]:
        """The `status --json` payload, from the ledger and the sessions of the last tick."""
        if self._status is None:
            raise HTTPError(404, "not found")
        payload = self._status(
            self.config, request.ledger(), self._sessions, {"answers": True, **self.health()}
        )
        for extend in self.routes.state_extenders:
            extend(request, payload)
        return payload

    def runner(self, ledger: Ledger) -> Runner:
        """A runner on a handler thread's *ledger*, built as the loop builds one for a tick."""
        return self._runner_factory(self.config, ledger)

    # ----- the loop -------------------------------------------------------

    def run(self) -> int:
        """Serve and tick until a stop is requested. Returns the exit code."""
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, self._on_signal)
        if self._server is None:
            self.start()
        ledger = Ledger(self.ledger_path)
        try:
            next_tick = self._clock()
            while not self.stop_requested():
                if self._clock() >= next_tick or self._request_waits(ledger):
                    if self.tick_once(ledger):
                        next_tick = self._clock() + self.interval_secs
                self.stopping.wait(self._poll_secs)
        finally:
            ledger.close()
            self.close()
        return 0

    def stop(self) -> None:
        self.stopping.set()

    def stop_requested(self) -> bool:
        return self._signalled or self.stopping.is_set()

    def report(self, report: TickReport) -> None:
        """Print *report* as a tick's is printed; a hook thread calls it for its own changes."""
        with self._report_lock:
            self._report(report)

    def _on_signal(self, signum: int, frame: object) -> None:
        self._signalled = True

    def _request_waits(self, ledger: Ledger) -> bool:
        """Whether a request was recorded since the last tick began.

        A request that tick left pending, because it could not list the sessions, waits
        for the next timed tick instead of making the loop tick every second.
        """
        return any(request["id"] > self._seen_request for request in ledger.pending_requests())

    def tick_once(self, ledger: Ledger) -> bool:
        """One tick under the lock. False when another tick holds the lock."""
        try:
            config = self._load_config()
        except ChargehandError as exc:
            # The request that woke the loop waits for a configuration that parses; asking
            # again every second would only repeat this error.
            self._mark_requests_seen(ledger)
            self._failed(f"configuration: {exc}")
            return True
        self.config = config
        try:
            with tick_lock(self._lock_path):
                self._mark_requests_seen(ledger)
                data = self.health_data
                data.ticking = True
                data.last_tick_started_at = time.time()
                try:
                    runner = self._runner_factory(config, ledger)
                    report = runner.tick(stop=self.stop_requested)
                    if self._list_sessions is not None:
                        self._sessions = self._list_sessions(runner)
                finally:
                    data.ticking = False
                    data.last_tick_finished_at = time.time()
        except TickBusy:
            log.debug("another tick holds the lock; trying again shortly")
            return False
        except Exception as exc:
            log.exception("the tick failed")
            self._failed(str(exc))
            return True
        self.health_data.ticks += 1
        self.health_data.last_tick_error = _error_line(report.errors[0]) if report.errors else None
        self.report(report)
        return True

    def _mark_requests_seen(self, ledger: Ledger) -> None:
        pending = ledger.pending_requests()
        if pending:
            self._seen_request = max(self._seen_request, *(r["id"] for r in pending))

    def _failed(self, message: str) -> None:
        log.error("tick failed: %s", message)
        self.health_data.failed_ticks += 1
        self.health_data.last_tick_error = _error_line(message)


def _error_line(message: str) -> str:
    # The health answers without a token, and an error can quote a source's output.
    return sanitize.one_line(message, limit=300)


# ----- clients ---------------------------------------------------------------


def probe(port: int, *, timeout: float = HEALTH_TIMEOUT_SECS) -> dict[str, object] | None:
    """The health of the daemon on 127.0.0.1:*port*, or None when no daemon answers there."""
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(MAX_BODY_BYTES).decode("utf-8"))
    except (OSError, http.client.HTTPException, UnicodeDecodeError, ValueError):
        return None
    finally:
        connection.close()
    return data if isinstance(data, dict) and data.get("ok") is True else None


def serves_this_ledger(health: Mapping[str, object] | None, ledger_path: Path | None = None) -> bool:
    """Whether *health* is from the daemon of this instance.

    A second instance, started with CHARGEHAND_STATE_DIR, can find the default
    instance's daemon on the same port; that daemon never reads this ledger.
    """
    if not health:
        return False
    return health.get("ledger") == os.path.realpath(ledger_path or paths.ledger_file())


def daemon_for(config: MachineConfig, *, timeout: float = HEALTH_TIMEOUT_SECS) -> dict[str, object] | None:
    """The health of this instance's daemon, or None when it does not answer."""
    health = probe(config.serve.port, timeout=timeout)
    return health if serves_this_ledger(health) else None


__all__ = [
    "Daemon",
    "HTTPError",
    "Request",
    "Response",
    "Routes",
    "build_routes",
    "daemon_for",
    "ensure_token",
    "probe",
    "read_token",
    "serves_this_ledger",
    "token_problem",
]
