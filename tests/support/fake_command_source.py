#!/usr/bin/env python3
"""The fake tracker as a command source, to check that the contract is sufficient.

It keeps a board in a JSON file, as the fake tracker keeps one in memory: issues with
labels, comments without text, closed issues. Run as the source's commands:

    fake_command_source.py STATE poll             # cursor on stdin, events on stdout
    fake_command_source.py STATE mark REF STATUS  # moves the labels; exit 3 is ambiguous
    fake_command_source.py STATE open REF         # {"open": true|false}

The queue becomes ``queued`` events (a ``labeled`` event from a ``[[source]]`` starts
nothing), closed issues ``done`` events, and comments after the cursor ``comment``
events. Each poll records the followed refs it was given. Tests drive the board through :class:`CommandBoard`. Standard library only, so
the script runs without the package on its path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
VIEWER = "tester@example.invalid"
LABELS = {"queued": "chargehand", "running": "chargehand-running", "blocked": "chargehand-blocked"}
EXIT_AMBIGUOUS = 3


class CommandBoard:
    """The board in *path*, shared by the test and the script."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.exists():
            self.write(
                {
                    "clock": 1_000_000,
                    "issues": {},
                    "comments": [],
                    "cursors": [],
                    "followed": [],
                    "calls": [],
                    "garbage": [],
                    "fail_next_mark": None,
                }
            )

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def write(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True))

    def update(self, **values: Any) -> None:
        state = self.read()
        state.update(values)
        self.write(state)

    def add(self, key: str, *, labels: tuple[str, ...] = (LABELS["queued"],)) -> None:
        state = self.read()
        state["clock"] += 1
        state["issues"][key] = {"labels": list(labels), "closed": False, "updated_at": state["clock"]}
        self.write(state)

    def comment(self, key: str, *, author: str = "someone@example.invalid", marked: bool = False) -> str:
        state = self.read()
        state["clock"] += 1
        comment_id = f"c{len(state['comments']) + 1}"
        state["comments"].append(
            {"id": comment_id, "item": key, "author": author, "created_at": state["clock"], "marked": marked}
        )
        self.write(state)
        return comment_id

    def close(self, key: str) -> None:
        state = self.read()
        state["clock"] += 1
        state["issues"][key].update(closed=True, updated_at=state["clock"])
        self.write(state)

    def labels_of(self, key: str) -> list[str]:
        return self.read()["issues"][key]["labels"]

    @property
    def cursors(self) -> list[str]:
        return self.read()["cursors"]

    @property
    def followed(self) -> list[str | None]:
        """What each poll found in CHARGEHAND_REFS."""
        return self.read()["followed"]

    @property
    def calls(self) -> list[list[str]]:
        return self.read()["calls"]


def _url(key: str) -> str:
    return f"https://tracker.invalid/{key}"


def poll(board: CommandBoard, cursor: str) -> list[str]:
    state = board.read()
    state["cursors"].append(cursor)
    state["followed"].append(os.environ.get("CHARGEHAND_REFS"))
    board.write(state)
    start = int(cursor) if cursor.isdigit() else 0
    lines = list(state["garbage"])
    for key, issue in sorted(state["issues"].items()):
        stamp = issue["updated_at"]
        if issue["closed"]:
            kind = "done"
        elif LABELS["queued"] in issue["labels"]:
            kind = "queued"
        else:
            continue
        lines.append(
            json.dumps(
                {
                    "kind": kind,
                    "item": key,
                    "url": _url(key),
                    "actor": "",
                    "occurred_at": stamp,
                    "dedupe_id": f"{kind}:{key}:{stamp}",
                    "pointer": _url(key),
                }
            )
        )
    for comment in state["comments"][start:]:
        key = comment["item"]
        lines.append(
            json.dumps(
                {
                    "kind": "comment",
                    "item": key,
                    "actor": comment["author"],
                    "occurred_at": comment["created_at"],
                    "dedupe_id": f"comment:{comment['id']}",
                    "pointer": f"{_url(key)}#{comment['id']}",
                    "marked": comment["marked"],
                }
            )
        )
    lines.append(json.dumps({"cursor": str(len(state["comments"]))}))
    return lines


def mark(board: CommandBoard, key: str, status: str) -> int:
    state = board.read()
    state["calls"].append(["mark", key, status])
    # One outcome for the next call, or a list of outcomes for the next calls in turn.
    failure, state["fail_next_mark"] = state["fail_next_mark"], None
    if isinstance(failure, list):
        failure, state["fail_next_mark"] = (failure[0] if failure else None), (failure[1:] or None)
    if failure == "failure":
        board.write(state)
        print("fake: the write failed", file=sys.stderr)
        return 1
    issue = state["issues"].get(key)
    if issue is None:
        board.write(state)
        print(f"fake: no issue {key}", file=sys.stderr)
        return 1
    managed = set(LABELS.values())
    issue["labels"] = [name for name in issue["labels"] if name not in managed]
    if status in LABELS:
        issue["labels"].append(LABELS[status])
    state["clock"] += 1
    issue["updated_at"] = state["clock"]
    board.write(state)
    if failure == "ambiguous":
        print("fake: 502 after the write landed", file=sys.stderr)
        return EXIT_AMBIGUOUS
    return 0


def item_open(board: CommandBoard, key: str) -> None:
    issue = board.read()["issues"].get(key)
    print(json.dumps({"open": issue is not None and not issue["closed"]}))


def main(argv: list[str]) -> int:
    board = CommandBoard(Path(argv[0]))
    command = argv[1]
    if command == "poll":
        print("\n".join(poll(board, sys.stdin.read())))
        return 0
    if command == "mark":
        return mark(board, argv[2], argv[3])
    if command == "open":
        item_open(board, argv[2])
        return 0
    print(f"fake: unknown command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
