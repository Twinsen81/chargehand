#!/usr/bin/env python3
"""A stand-in for the `claude` binary.

State lives in the JSON file named by $FAKE_CLAUDE_STATE so a test can decide what a
session does next, and can read back exactly which argv a launch produced. The tests
never need a real Claude Code install.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HELP = """Usage: claude [options] [prompt]

Options:
  --bg                     start a background session
  -n, --name <name>        name the session
  --permission-mode <mode> auto | default | acceptEdits | dontAsk | plan | bypassPermissions
  --settings <json>        per-session settings
  --version

Commands:
  agents [--json] [--all]  list sessions
  stop <id>
  respawn <id>
  rm <id>
  logs <id>
"""


def state_path() -> Path:
    return Path(os.environ["FAKE_CLAUDE_STATE"])


def load() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


def save(state: dict) -> None:
    state_path().write_text(json.dumps(state, indent=2))


def record(state: dict, key: str, value) -> None:
    state.setdefault(key, []).append(value)


def main(argv: list[str]) -> int:
    state = load()
    if state.get("fail_every_call"):
        sys.stderr.write("fake claude: forced failure\n")
        return 9

    if not argv or argv[0] in ("--help", "-h"):
        sys.stdout.write(HELP)
        return 0
    if argv[0] == "--version":
        sys.stdout.write(state.get("version", "claude 2.0.0-fake") + "\n")
        return 0

    if argv[0] == "agents":
        if state.get("agents_output") is not None:
            sys.stdout.write(state["agents_output"])
            return int(state.get("agents_returncode", 0))
        if state.get("agents_fail"):
            sys.stderr.write("fake claude: agents unavailable\n")
            return 1
        sys.stdout.write(json.dumps(state.get("sessions", [])))
        return 0

    if argv[0] in ("stop", "respawn", "rm") and len(argv) >= 2:
        action, session_id = argv[0], argv[1]
        record(state, "commands", [action, session_id])
        sessions = state.get("sessions", [])
        failed = int(state.get(f"{action}_returncode", 0)) != 0
        for session in sessions:
            if session.get("id") != session_id:
                continue
            if action == "stop" and not failed:
                # stop_state lets a test model a stop that reports success while the
                # listing goes on saying something the runner does not recognise.
                session["state"] = state.get("stop_state", "stopped")
            elif action == "respawn" and not failed:
                session["state"] = state.get("respawn_state", "working")
        if action == "rm" and not failed:
            state["sessions"] = [s for s in sessions if s.get("id") != session_id]
        save(state)
        return int(state.get(f"{action}_returncode", 0))

    if argv[0] == "logs" and len(argv) >= 2:
        record(state, "commands", ["logs", argv[1]])
        save(state)
        sys.stdout.write(state.get("logs", "plain log line\n"))
        return 0

    if "--bg" in argv:
        record(state, "launches", {"argv": argv, "cwd": os.getcwd()})
        if state.get("launch_fails"):
            save(state)
            sys.stderr.write("fake claude: launch refused\n")
            return 1
        name = None
        for flag in ("-n", "--name"):
            if flag in argv:
                name = argv[argv.index(flag) + 1]
        sessions = state.setdefault("sessions", [])
        # Monotonic, never reused: a recycled id would hide a session being adopted
        # when a fresh one should have been started.
        serial = state.get("session_serial", 0) + 1
        state["session_serial"] = serial
        if not state.get("launch_invisible"):
            sessions.append(
                {
                    "id": state.get("next_session_id", f"s{serial}"),
                    "uuid": f"uuid-{serial}",
                    "name": name,
                    "cwd": os.getcwd(),
                    "state": state.get("launch_state", "working"),
                    "startedAt": state.get("launch_started_at"),
                }
            )
        state.pop("next_session_id", None)
        save(state)
        return 0

    sys.stderr.write(f"fake claude: unsupported invocation: {' '.join(argv)}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
