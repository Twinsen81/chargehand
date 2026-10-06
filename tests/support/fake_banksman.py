#!/usr/bin/env python3
"""A stand-in for the `banksman` command.

State lives in the JSON file named by $FAKE_BANKSMAN_STATE: the resources that `status`
reports, the leases that the next `reap` ends, and how a call fails. Every call is
recorded, so a test can read back exactly which commands the runner ran. The tests
never need a real banksman install.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The schema number of the banksman release that this fake copies.
SCHEMA = 5


def state_path() -> Path:
    return Path(os.environ["FAKE_BANKSMAN_STATE"])


def load() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


def save(state: dict) -> None:
    state_path().write_text(json.dumps(state, indent=2))


def main(argv: list[str]) -> int:
    state = load()
    state.setdefault("calls", []).append(argv)
    save(state)
    if state.get("fail"):
        sys.stderr.write("fake banksman: forced failure\n")
        return 1
    if len(argv) != 2 or argv[1] != "--json":
        sys.stderr.write(f"fake banksman: unsupported invocation: {' '.join(argv)}\n")
        return 2
    if state.get("output") is not None:
        sys.stdout.write(state["output"])
        return 0
    schema = state.get("schema", SCHEMA)
    command = argv[0]
    if command == "version":
        document = {"schema": schema, "version": state.get("version", "0.1.0")}
    elif command == "reap":
        document = {"schema": schema, "reaped": state.pop("reap", [])}
        save(state)
    elif command == "status":
        document = {"schema": schema, "resources": state.get("resources", []), "notes": []}
    else:
        sys.stderr.write(f"fake banksman: unsupported command: {command}\n")
        return 2
    sys.stdout.write(json.dumps(document))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
