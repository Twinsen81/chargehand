"""A daemon for tests: bound to 127.0.0.1 on a free port, its loop on a thread.

Nothing here installs a job or reaches a real port the operator uses. Every daemon is
stopped when its context ends.
"""

from __future__ import annotations

import dataclasses
import http.client
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from chargehand import cli, serve
from chargehand.config import MachineConfig, ServeConfig
from chargehand.gitutil import Git
from chargehand.notify import Notifier
from chargehand.tick import Runner

TOKEN = "test-token-0123456789"


def daemon_config(config: MachineConfig, token_file: Path, *, port: int = 0, interval: int = 60) -> MachineConfig:
    return dataclasses.replace(
        config, serve=ServeConfig(port=port, token_file=token_file, tick_interval_secs=interval)
    )


def build(
    config: MachineConfig,
    *,
    notifier: Notifier | None = None,
    runner_factory=None,
    token: str | None = TOKEN,
    **kwargs,
) -> serve.Daemon:
    """A daemon wired the way `chargehand serve` wires it, with the test's collaborators."""

    def runner(cfg: MachineConfig, ledger) -> Runner:
        return Runner(cfg, ledger, git=Git(cfg.git_bin), notifier=notifier or Notifier(cfg.notify))

    return serve.Daemon(
        config,
        runner_factory=runner_factory or runner,
        status=lambda cfg, ledger, sessions, health: cli.build_status(
            cfg, ledger, include_queue=False, sessions=sessions, daemon=health
        ),
        sessions=cli.list_sessions,
        token=token,
        **kwargs,
    )


@contextmanager
def running(daemon: serve.Daemon, *, loop: bool = True) -> Iterator[serve.Daemon]:
    """Serve, and run the loop on a thread when *loop*; stop both on exit."""
    daemon.start()
    thread = threading.Thread(target=daemon.run, name="test-daemon", daemon=True) if loop else None
    if thread:
        thread.start()
    try:
        yield daemon
    finally:
        daemon.stop()
        if thread:
            thread.join(timeout=30)
            assert not thread.is_alive(), "the daemon loop did not stop"
        daemon.close()


def wait_for(condition, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    host: str | None = None,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(serve.HOST, port, timeout=10)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{serve.HOST}:{port}" if host is None else host)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None and "Content-Length" not in (headers or {}):
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()
