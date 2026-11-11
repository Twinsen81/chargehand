"""The token of each worker session: how the daemon knows which worker calls it.

Each launch and each fork writes a new token for the worktree's session into a file of
mode 0600 in a directory of mode 0700 under the state directory, one file per worktree.
The session's hooks and its chargehand mod send it with every post, and the daemon maps
the token, not the session id or the working directory that a post names, to the worker.
A session never gets the board's token.

A token ends with its worker: the daemon refuses the token of a worker that ended, and
the file is deleted when the session is removed. A new launch in the same worktree
replaces it. Every launch's settings deny the file tools the whole token directory; the
mod still reads its own token file, because a `Read` deny rule does not bind the mod's
own file reads. Like the deny rules, this is a guardrail, not a wall: a process of the
same user can still read the files with a shell command.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from chargehand import paths

if TYPE_CHECKING:
    from chargehand.ledger import Ledger, Worker

DIR_NAME = "worker-tokens"
SUFFIX = ".token"


def token_dir() -> Path:
    return paths.state_dir() / DIR_NAME


def token_file_for(worktree: Path) -> Path:
    """The token file of the session in *worktree*."""
    digest = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()[:24]
    return token_dir() / f"{digest}{SUFFIX}"


def issue(worktree: Path) -> str:
    """Write a new token for the session in *worktree*, in one step, and return it.

    Raises OSError when the file cannot be written.
    """
    token = secrets.token_urlsafe(32)
    path = token_file_for(worktree)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(token + "\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return token


def _read(path: Path) -> str | None:
    """The token in *path*, or None when it is missing or not a private file of this user."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def read(worktree: Path) -> str | None:
    return _read(token_file_for(worktree))


def revoke(worktree: Path) -> None:
    """Delete the token of the session in *worktree*. Raises OSError when it cannot."""
    token_file_for(worktree).unlink(missing_ok=True)


def token_files() -> list[Path]:
    """Every token file there is now."""
    try:
        return sorted(
            entry for entry in token_dir().iterdir()
            if entry.name.endswith(SUFFIX) and not entry.name.startswith(".")
        )
    except OSError:
        return []


def _matching_file(token: str) -> Path | None:
    given = token.encode("utf-8")
    found = None
    # Every file is compared, in constant time each, so the time does not tell which matched.
    for path in token_files():
        known = _read(path)
        matches = known is not None and hmac.compare_digest(given, known.encode("utf-8"))
        if matches and found is None:
            found = path
    return found


def worker_for(ledger: "Ledger", token: str) -> "Worker | None":
    """The worker whose session has *token*, in any state, or None for an unknown token.

    A live worker comes first: a worker that ended can share its worktree with a newer one,
    whose launch replaced the token. The caller refuses a worker that ended.
    """
    if not token:
        return None
    path = _matching_file(token)
    if path is None:
        return None
    candidates = [*reversed(ledger.live_workers()), *reversed(ledger.workers_needing_gc())]
    return next((w for w in candidates if token_file_for(w.worktree_path) == path), None)


__all__ = [
    "issue",
    "read",
    "revoke",
    "token_dir",
    "token_file_for",
    "token_files",
    "worker_for",
]
