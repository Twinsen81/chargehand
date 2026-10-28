"""The daemon: the tick on a timer, the loopback API, the token, and a clean stop.

Every server here binds 127.0.0.1 on a port the kernel picks and is shut down in the
test. The ticks run against the fake `claude` and the fake tracker.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from chargehand import cli, serve
from chargehand.config import ServeConfig, load_machine_config
from chargehand.errors import ChargehandError
from chargehand.ledger import Ledger
from conftest import write_machine_config
from support import daemon as support

AUTH = {"Authorization": f"Bearer {support.TOKEN}"}


@pytest.fixture
def config(harness, tmp_path):
    return support.daemon_config(harness.config, tmp_path / "board.token")


def _echo(request: serve.Request) -> serve.Response:
    return serve.Response.json({"got": request.json()})


# ----- the server -------------------------------------------------------------


def test_it_binds_the_loopback_address_only(config):
    daemon = support.build(config)
    with support.running(daemon, loop=False):
        server = daemon._server
        assert server.address_family == socket.AF_INET
        assert server.server_address[0] == "127.0.0.1"
        assert daemon.port > 0
        assert serve.probe(daemon.port)["ok"] is True


def test_the_configuration_has_no_address_to_bind():
    with pytest.raises(ChargehandError, match="unknown"):
        ServeConfig.parse({"host": "0.0.0.0"})
    parsed = ServeConfig.parse({"port": 9000, "tick_interval_secs": 30})
    assert (parsed.port, parsed.tick_interval_secs) == (9000, 30)
    assert ServeConfig.parse(None).port == 7878
    assert ServeConfig.parse(None).tick_interval_secs == 60


def test_the_health_names_the_ledger_it_serves(config):
    daemon = support.build(config)
    with support.running(daemon, loop=False):
        health = serve.probe(daemon.port)

    assert serve.serves_this_ledger(health)
    assert not serve.serves_this_ledger(health, Path("/elsewhere/ledger.sqlite"))
    assert not serve.serves_this_ledger(None)


def test_a_mutating_request_needs_the_token(config):
    daemon = support.build(config)
    daemon.routes.add("POST", "/api/echo", _echo)
    body = b'{"a": 1}'
    with support.running(daemon, loop=False):
        port = daemon.port
        assert support.request(port, "POST", "/api/echo", body=body)[0] == 401
        wrong = {"Authorization": "Bearer not-the-token"}
        assert support.request(port, "POST", "/api/echo", body=body, headers=wrong)[0] == 401
        basic = {"Authorization": f"Basic {support.TOKEN}"}
        assert support.request(port, "POST", "/api/echo", body=body, headers=basic)[0] == 401

        status, payload = support.request(port, "POST", "/api/echo", body=body, headers=AUTH)

    assert status == 200
    assert json.loads(payload) == {"got": {"a": 1}}


def test_a_request_for_another_host_is_refused(config):
    daemon = support.build(config)
    daemon.routes.add("POST", "/api/echo", _echo)
    with support.running(daemon, loop=False):
        port = daemon.port
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1:1", ""):
            assert support.request(port, "GET", "/api/health", host=host)[0] == 403, host
        status, _ = support.request(
            port, "POST", "/api/echo", body=b"{}", headers=AUTH, host="evil.example"
        )
        assert status == 403
        assert support.request(port, "GET", "/api/health", host=f"localhost:{port}")[0] == 200


def test_no_response_carries_a_cors_header(config):
    daemon = support.build(config)
    with support.running(daemon, loop=False):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
        connection.request("GET", "/api/health", headers={"Origin": "https://evil.example"})
        response = connection.getresponse()
        response.read()
        connection.close()

    assert not [name for name, _ in response.getheaders() if name.lower().startswith("access-control")]


def test_a_large_or_chunked_body_is_refused(config):
    daemon = support.build(config)
    daemon.routes.add("POST", "/api/echo", _echo)
    with support.running(daemon, loop=False):
        port = daemon.port
        too_large = {**AUTH, "Content-Length": str(serve.MAX_BODY_BYTES + 1)}
        assert support.request(port, "POST", "/api/echo", body=b"", headers=too_large)[0] == 413
        chunked = {**AUTH, "Transfer-Encoding": "chunked"}
        assert support.request(port, "POST", "/api/echo", body=b"0\r\n\r\n", headers=chunked)[0] == 411


def test_unknown_paths_and_methods(config):
    daemon = support.build(config)
    with support.running(daemon, loop=False):
        port = daemon.port
        assert support.request(port, "GET", "/api/nothing")[0] == 404
        assert support.request(port, "POST", "/api/health", body=b"", headers=AUTH)[0] == 405


def test_a_module_adds_routes_through_the_table():
    routes = serve.Routes()
    routes.add("POST", "/hooks", lambda request: serve.Response(status=201), prefix=True)
    routes.add("POST", "/hooks/Stop", lambda request: serve.Response(status=202))

    handler, known = routes.match("POST", "/hooks/Stop")
    assert known and handler(None).status == 202
    handler, known = routes.match("POST", "/hooks/Notification")
    assert known and handler(None).status == 201
    assert routes.match("GET", "/hooks/Stop") == (None, True)
    assert routes.match("POST", "/hooksX") == (None, False)
    with pytest.raises(ValueError):
        routes.add("POST", "/hooks/Stop", lambda request: serve.Response())


# ----- the state ----------------------------------------------------------------


def test_the_state_is_the_status_payload_without_tracker_or_session_calls(
    capsys, config, config_file, harness
):
    harness.board.add("ABC-1")
    daemon = support.build(config, notifier=harness.notifier, interval_secs=3600)
    with support.running(daemon):
        assert support.wait_for(lambda: daemon.health_data.ticks >= 1)
        assert harness.ledger.live_worker_for("ABC-1") is not None
        tracker_calls = len(harness.board.calls)
        # A listing would fail from here on, so a state that lists sessions shows it.
        harness.claude_state.set(agents_fail=True)

        status, body = support.request(daemon.port, "GET", "/api/state")

        assert status == 200
        assert len(harness.board.calls) == tracker_calls
    state = json.loads(body)

    harness.claude_state.set(agents_fail=False)
    capsys.readouterr()
    cli.main(["--config", str(config_file), "status", "--json", "--no-queue"])
    real = json.loads(capsys.readouterr().out)

    assert set(state) == set(real)
    assert set(state["machine"]) == set(real["machine"])
    assert set(state["runner"]) == set(real["runner"])
    assert [w["item"] for w in state["workers"]] == ["ABC-1"]
    assert state["session_error"] is None
    assert [s["id"] for s in state["sessions"]] == [s["id"] for s in real["sessions"]]
    assert state["daemon"]["answers"] is True
    assert state["daemon"]["ticks"] >= 1
    # The CLI's own status sees no daemon of its instance on the default port.
    assert real["daemon"]["answers"] is False


def test_the_state_before_the_first_tick_says_so(config):
    daemon = support.build(config)
    with support.running(daemon, loop=False):
        status, body = support.request(daemon.port, "GET", "/api/state")

    assert status == 200
    state = json.loads(body)
    assert state["sessions"] == []
    assert state["session_error"] == serve.NO_SESSIONS_YET


# ----- the loop -----------------------------------------------------------------


def test_the_loop_ticks_at_the_interval(config):
    daemon = support.build(config, interval_secs=0.2, poll_secs=0.02)
    with support.running(daemon):
        assert support.wait_for(lambda: daemon.health_data.ticks >= 3, timeout=10)
    health = daemon.health()
    assert health["failed_ticks"] == 0
    assert health["last_tick_finished_at"] >= health["last_tick_started_at"]


def test_the_loop_waits_the_interval_between_ticks(config):
    daemon = support.build(config, interval_secs=3600, poll_secs=0.02)
    with support.running(daemon):
        assert support.wait_for(lambda: daemon.health_data.ticks >= 1)
        support.wait_for(lambda: False, timeout=0.3)
        assert daemon.health_data.ticks == 1


def test_a_recorded_request_is_applied_by_the_daemon(config, harness):
    daemon = support.build(config, interval_secs=3600, poll_secs=0.05)
    with support.running(daemon):
        assert support.wait_for(lambda: daemon.health_data.ticks >= 1)
        request_id = harness.ledger.record_request("pause")

        applied = support.wait_for(
            lambda: (harness.ledger.get_request(request_id) or {}).get("applied_at"), timeout=5
        )

    assert applied
    assert harness.ledger.get_request(request_id)["ok"]
    assert harness.ledger.is_paused()
    assert daemon.health_data.ticks == 2


def test_a_failing_tick_is_counted_and_the_loop_goes_on(config):
    calls = []

    def broken(cfg, ledger):
        calls.append(1)
        raise RuntimeError("boom")

    daemon = support.build(config, runner_factory=broken, interval_secs=0.05, poll_secs=0.02)
    with support.running(daemon):
        assert support.wait_for(lambda: len(calls) >= 3)
    health = daemon.health()
    assert health["failed_ticks"] >= 3
    assert health["last_tick_error"] == "boom"


def test_a_configuration_that_stops_parsing_keeps_the_daemon_running(config):
    loads = []

    def load():
        loads.append(1)
        raise ChargehandError("config: broken")

    daemon = support.build(config, load_config=load, interval_secs=0.05, poll_secs=0.02)
    with support.running(daemon):
        assert support.wait_for(lambda: len(loads) >= 2)
        assert serve.probe(daemon.port)["ok"] is True
    assert "config: broken" in daemon.health()["last_tick_error"]


class _BlockingRunner:
    """Holds the tick lock inside a tick until the test lets it go."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, cfg, ledger):
        return self

    def tick(self, *, stop=None):
        from chargehand.tick import TickReport

        self.entered.set()
        assert self.release.wait(30)
        return TickReport()

    def runtime_names(self):
        return []


def test_a_hand_tick_reports_the_lock_while_the_daemon_ticks(capsys, config, config_file):
    blocking = _BlockingRunner()
    daemon = support.build(config, runner_factory=blocking, interval_secs=3600)
    with support.running(daemon):
        assert blocking.entered.wait(10)
        try:
            assert cli.main(["--config", str(config_file), "tick"]) == cli.EXIT_ERROR
        finally:
            blocking.release.set()

    assert "another chargehand tick is running" in capsys.readouterr().err


def test_the_daemon_waits_for_a_hand_tick_and_then_runs_its_own(config):
    from chargehand.tick import tick_lock

    daemon = support.build(config, interval_secs=3600, poll_secs=0.02)
    with support.running(daemon):
        hand_tick = tick_lock()
        hand_tick.__enter__()
        try:
            support.wait_for(lambda: False, timeout=0.3)
            assert daemon.health_data.ticks == 0
            assert daemon.health_data.failed_ticks == 0
        finally:
            hand_tick.__exit__(None, None, None)
        assert support.wait_for(lambda: daemon.health_data.ticks >= 1)


def test_a_stop_during_a_phase_finishes_it_and_skips_the_rest(harness, monkeypatch):
    ran = []
    stopping = threading.Event()

    def watchdog(report):
        ran.append("watchdog")
        stopping.set()

    monkeypatch.setattr(harness.runner, "_watchdog", watchdog)
    monkeypatch.setattr(harness.runner, "_follow_pull_requests", lambda report: ran.append("follow"))
    monkeypatch.setattr(harness.runner, "_collect_garbage", lambda report: ran.append("collect"))

    report = harness.tick(stop=stopping.is_set)

    assert ran == ["watchdog"]
    assert report.stopped_before == "follow"
    assert report.as_dict()["stopped_before"] == "follow"


def test_a_stop_ends_the_loop_after_the_tick_in_progress(config):
    blocking = _BlockingRunner()
    daemon = support.build(config, runner_factory=blocking, interval_secs=3600)
    daemon.start()
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        assert blocking.entered.wait(10)
        daemon.stop()
        assert thread.is_alive(), "the loop must let the tick in progress finish"
        blocking.release.set()
        thread.join(10)
        assert not thread.is_alive()
    finally:
        blocking.release.set()
        daemon.stop()
        daemon.close()
    assert daemon.health_data.ticks == 1
    assert serve.probe(daemon.port, timeout=0.5) is None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_sigterm_ends_the_process_with_exit_code_zero(tmp_path, repo, harness):
    port = _free_port()
    config_file = write_machine_config(tmp_path / "serve-config.toml", repo, harness.worktree_root)
    with config_file.open("a") as handle:
        handle.write(f"\n[serve]\nport = {port}\ntoken_file = \"{tmp_path / 'serve.token'}\"\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    process = subprocess.Popen(
        [sys.executable, "-m", "chargehand", "--config", str(config_file), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        health = support.wait_for(lambda: serve.probe(port, timeout=0.5), timeout=20)
        assert health, "the daemon never answered"
        assert support.wait_for(lambda: (serve.probe(port) or {}).get("ticks", 0) >= 1, timeout=20)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, output
    assert f"serving on http://127.0.0.1:{port}" in output
    assert output.rstrip().endswith("stopped")
    assert stat.S_IMODE((tmp_path / "serve.token").stat().st_mode) == 0o600


# ----- the token ----------------------------------------------------------------


def test_the_token_file_is_written_once_with_mode_0600(tmp_path):
    path = tmp_path / "nested" / "board.token"

    first = serve.ensure_token(path)

    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert serve.ensure_token(path) == first
    assert serve.read_token(path) == first
    assert serve.token_problem(path) is None


def test_a_readable_token_file_is_refused(tmp_path):
    path = tmp_path / "board.token"
    serve.ensure_token(path)
    path.chmod(0o644)

    assert "chmod 600" in serve.token_problem(path)
    assert serve.read_token(path) is None
    with pytest.raises(ChargehandError, match="mode 644"):
        serve.ensure_token(path)


def test_a_symlinked_token_file_is_refused(tmp_path):
    target = tmp_path / "real.token"
    serve.ensure_token(target)
    link = tmp_path / "board.token"
    link.symlink_to(target)

    assert "not a regular file" in serve.token_problem(link)


def test_the_daemon_writes_its_token_when_it_starts(harness, tmp_path):
    config = support.daemon_config(harness.config, tmp_path / "board.token")
    daemon = support.build(config, token=None)
    daemon.routes.add("POST", "/api/echo", _echo)
    with support.running(daemon, loop=False):
        token = (tmp_path / "board.token").read_text().strip()
        status, _ = support.request(
            daemon.port, "POST", "/api/echo", body=b"{}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert status == 200


def test_a_port_in_use_fails_the_start(config):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        daemon = support.build(config, port=port)
        with pytest.raises(ChargehandError, match="cannot listen"):
            daemon.start()


def test_the_default_token_file_sits_next_to_the_configuration(tmp_path, monkeypatch):
    from chargehand import paths

    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path / "elsewhere" / "config.toml"))

    assert ServeConfig().token_file == tmp_path / "elsewhere" / "board.token"
    assert ServeConfig.parse({"token_file": "~/t"}).token_file == Path(os.path.expanduser("~/t"))


def test_the_serve_table_is_read_from_the_machine_configuration(tmp_path, repo, harness):
    path = write_machine_config(tmp_path / "c.toml", repo, harness.worktree_root)
    with path.open("a") as handle:
        handle.write("\n[serve]\nport = 7979\ntick_interval_secs = 15\n")

    config = load_machine_config(path)

    assert (config.serve.port, config.serve.tick_interval_secs) == (7979, 15)
    assert "Bash(*127.0.0.1:7979*)" in config.deny_rules
