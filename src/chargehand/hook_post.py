"""Post a session hook's payload from stdin to the daemon, for events without http hooks.

A session runs this file as a `command` hook, by its path and with ``python -I``, so
nothing from the session's working directory is imported:

    python3 -I .../hook_post.py --port 7878 --token-file ~/.config/chargehand/board.token SessionStart

It reads the token from the token file, which must be a private regular file of this
user, so the token never appears on a command line. It prints nothing, because Claude
Code gives a SessionStart hook's output to the model, and it always exits 0 within a few
seconds: a daemon that is down or slow must never hold up or stop the session. Standard
library only, and no import from chargehand, so it runs with any Python 3.
"""

from __future__ import annotations

import argparse
import http.client
import os
import re
import stat
import sys

HOST = "127.0.0.1"
TIMEOUT_SECS = 3.0
# The daemon refuses a larger body.
MAX_BODY_BYTES = 1024 * 1024
_EVENT = re.compile(r"[A-Za-z]{1,64}")


def read_token(path: str) -> str | None:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def post(port: int, token: str, event: str, body: bytes, *, timeout: float = TIMEOUT_SECS) -> int | None:
    """The daemon's status code, or None when it did not answer."""
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request(
            "POST",
            f"/hooks/{event}",
            body=body,
            headers={
                "Host": f"{HOST}:{port}",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        response.read(MAX_BODY_BYTES)
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--token-file", required=True)
        parser.add_argument("event")
        args = parser.parse_args(argv)
        if not _EVENT.fullmatch(args.event) or not 0 < args.port < 65536:
            return 0
        body = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
        token = read_token(os.path.expanduser(args.token_file))
        if token and len(body) <= MAX_BODY_BYTES:
            post(args.port, token, args.event, body)
    except BaseException:  # noqa: BLE001 - a hook must never fail the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
