"""A second runtime for the tests, with fewer capabilities than Claude Code.

It keeps the interface honest: a tick test that passes on both this and the claude
runtime does not depend on anything only Claude Code offers. Its sessions live in
memory, as a process chargehand hosts would, and a test drives them through
:func:`world`, with the same calls ``FakeClaudeState`` offers for the fake binary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from chargehand.errors import AgentRuntimeError, ConfigError
from chargehand.runtime import (
    AT_REST,
    STOPPED,
    WORKING,
    Capabilities,
    CommandResult,
    LaunchResult,
    LaunchSpec,
    Runtime,
    Session,
    WakeResult,
    register,
)

NAME = "fake"


class World:
    """Every session the fake runtime hosts, and every command it was given."""

    def __init__(self) -> None:
        self.state: dict = {}

    def reset(self) -> None:
        self.state = {}

    def read(self) -> dict:
        return self.state

    def set(self, **values) -> None:
        self.state.update(values)

    @property
    def sessions(self) -> list[dict]:
        return self.state.setdefault("sessions", [])

    @property
    def launches(self) -> list[dict]:
        return self.state.setdefault("launches", [])

    @property
    def commands(self) -> list[list[str]]:
        return self.state.setdefault("commands", [])

    @property
    def wakes(self) -> list[dict]:
        return self.state.setdefault("wakes", [])

    def set_session_state(self, name: str, session_state: str, **extra) -> None:
        """Match a session by id, by its full name, or by the item part of `role/item`."""
        for session in self.sessions:
            full = session.get("name") or ""
            if full == name or full.endswith(f"/{name}") or session.get("id") == name:
                session["state"] = session_state
                session.update(extra)

    def drop_sessions(self) -> None:
        self.state["sessions"] = []

    def find(self, session_id: str) -> dict | None:
        return next((s for s in self.sessions if s.get("id") == session_id), None)

    def add_session(self, *, name: str | None, cwd: str, state: str) -> dict:
        serial = self.state.get("serial", 0) + 1
        self.state["serial"] = serial
        session = {
            "id": f"f{serial}",
            "uuid": f"fake-uuid-{serial}",
            "name": name,
            "cwd": cwd,
            "state": state,
        }
        self.sessions.append(session)
        return session


_WORLD = World()


def world() -> World:
    return _WORLD


def reset() -> None:
    _WORLD.reset()


def _result(*argv: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult((NAME, *argv), returncode, stdout, stderr)


class FakeRuntime(Runtime):
    name = NAME
    # No live attach, no in-place wake, no board permissions, no usage readings, and
    # chargehand hosts the process.
    capabilities = Capabilities(fork=True)
    role_keys = ("sandbox",)

    @classmethod
    def from_config(cls, config) -> "FakeRuntime":
        return cls()

    @classmethod
    def parse_role_options(cls, table: Mapping[str, object], where: str) -> dict[str, object]:
        sandbox = table.get("sandbox", True)
        if not isinstance(sandbox, bool):
            raise ConfigError(f"{where}.sandbox: expected true or false")
        return {"sandbox": sandbox}

    def list_sessions(self) -> list[Session]:
        if _WORLD.state.get("list_fails"):
            raise AgentRuntimeError("fake runtime: listing unavailable")
        return [
            Session(
                id=s["id"],
                uuid=s.get("uuid"),
                name=s.get("name"),
                cwd=s.get("cwd"),
                state=s.get("state", WORKING),
                raw_state=s.get("state", ""),
                waiting_for=s.get("waitingFor"),
            )
            for s in _WORLD.sessions
        ]

    def launch(self, spec: LaunchSpec, *, warn=None) -> LaunchResult:
        _WORLD.launches.append(
            {
                "worktree": str(spec.worktree),
                "name": spec.name,
                "prompt": spec.prompt,
                "options": dict(spec.options),
                "model": spec.model,
                "effort": spec.effort,
            }
        )
        if _WORLD.state.get("launch_fails"):
            raise AgentRuntimeError("fake runtime: launch refused")
        if not _WORLD.state.get("launch_invisible"):
            _WORLD.add_session(
                name=spec.name,
                cwd=os.path.realpath(spec.worktree),
                state=_WORLD.state.get("launch_state", WORKING),
            )
        return LaunchResult(output="started")

    def wake(self, session_id: str, message: str, *, session_uuid: str | None = None) -> WakeResult:
        session = _WORLD.find(session_id)
        _WORLD.wakes.append({"id": session_id, "message": message})
        if session is None:
            return WakeResult(False, f"session {session_id} is not hosted")
        if session.get("state") not in AT_REST:
            return WakeResult(False, f"session {session_id} is still running a turn")
        session["state"] = WORKING
        return WakeResult(True, "new turn")

    def _command(self, action: str, session_id: str) -> CommandResult:
        _WORLD.commands.append([action, session_id])
        failed = int(_WORLD.state.get(f"{action}_returncode", 0))
        session = _WORLD.find(session_id)
        if not failed and session is not None:
            if action == "stop":
                session["state"] = _WORLD.state.get("stop_state", STOPPED)
            elif action == "respawn":
                session["state"] = _WORLD.state.get("respawn_state", WORKING)
            elif action == "rm":
                _WORLD.state["sessions"] = [s for s in _WORLD.sessions if s is not session]
        return _result(action, session_id, returncode=failed)

    def stop(self, session_id: str) -> CommandResult:
        return self._command("stop", session_id)

    def respawn(self, session_id: str) -> CommandResult:
        return self._command("respawn", session_id)

    def remove(self, session_id: str) -> CommandResult:
        return self._command("rm", session_id)

    def logs(self, session_id: str, *, lines: int = 200) -> str:
        _WORLD.commands.append(["logs", session_id])
        return "\n".join(_WORLD.state.get("logs", "fake runtime log line").splitlines()[-lines:])

    def attach_command(self, session_id: str) -> str:
        return f"fake-agent resume {session_id}"

    def fork(
        self,
        session_id: str,
        prompt: str,
        *,
        session_uuid: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CommandResult:
        original = _WORLD.find(session_id)
        if original is None:
            return _result("fork", session_id, returncode=1, stderr="no such session")
        copy = _WORLD.add_session(
            name=original.get("name"), cwd=str(Path(original.get("cwd") or ".")), state=WORKING
        )
        _WORLD.commands.append(["fork", session_id, copy["id"]])
        return _result("fork", session_id, stdout=copy["id"])


register(FakeRuntime)


__all__ = ["FakeRuntime", "NAME", "World", "reset", "world"]
