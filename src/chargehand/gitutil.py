"""The git operations the launch sequence and garbage collection need.

Every call is a plain argv with no shell, and the ones the launch sequence uses are
idempotent so a resumed launch can simply run them again.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chargehand.errors import GitError

# Overrides passed to every git call, so that no repository-controlled config runs a
# program: `core.hooksPath` names hook scripts, `core.fsmonitor` a program git runs on
# `status`, `ls-files` and `diff`. chargehand's git calls are all plumbing and need
# neither, and a worktree, or a `.git` file, the session can write must not run code
# through them. A command-line `-c` wins over any config the repository holds.
HARDEN = ("core.hooksPath=/dev/null", "core.fsmonitor=false")


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class Git:
    def __init__(self, binary: str = "git", *, timeout_secs: float = 300.0) -> None:
        self.binary = binary
        self.timeout_secs = timeout_secs

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> GitResult:
        argv = [self.binary]
        for override in HARDEN:
            argv += ["-c", override]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        argv += list(args)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                # A file name git prints need not decode as text; it must not end the tick.
                errors="replace",
                timeout=timeout or self.timeout_secs,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise GitError(f"'{self.binary}' not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"`{' '.join(argv)}` timed out after {exc.timeout:.0f}s") from exc
        except OSError as exc:
            raise GitError(f"could not run `{' '.join(argv)}`: {exc}") from exc
        result = GitResult(completed.returncode, completed.stdout, completed.stderr)
        if check and completed.returncode != 0:
            raise GitError(
                f"`{' '.join(argv)}` exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[:500]}"
            )
        return result

    # ----- queries ----------------------------------------------------------

    def is_repo(self, repo: Path) -> bool:
        return self.run(["rev-parse", "--git-dir"], cwd=repo, check=False).returncode == 0

    def list_worktrees(self, repo: Path) -> dict[str, dict[str, str]]:
        output = self.run(["worktree", "list", "--porcelain"], cwd=repo).stdout
        worktrees: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in output.splitlines():
            if not line.strip():
                if current.get("worktree"):
                    worktrees[current["worktree"]] = current
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current.get("worktree"):
            worktrees[current["worktree"]] = current
        return worktrees

    def worktree_branch(self, repo: Path, worktree: Path) -> str | None:
        entry = self.list_worktrees(repo).get(str(worktree.resolve()))
        if entry is None:
            entry = self.list_worktrees(repo).get(str(worktree))
        if entry is None:
            return None
        head = entry.get("branch")
        return head.removeprefix("refs/heads/") if head else None

    def renamed_from(self, repo: Path, branch: str, old: str) -> bool:
        """Whether *branch* got its name by renaming *old*, directly or in steps.

        The branch's reflog is the only record of a rename. A repository with reflogs
        turned off has none, and then the answer is no.
        """
        result = self.run(
            ["reflog", "show", "--format=%gs", f"refs/heads/{branch}"], cwd=repo, check=False
        )
        if result.returncode != 0:
            return False
        return any(
            line.startswith(f"Branch: renamed refs/heads/{old} to ")
            for line in result.stdout.splitlines()
        )

    def branch_exists(self, repo: Path, branch: str) -> bool:
        return (
            self.run(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False
            ).returncode
            == 0
        )

    def ref_exists(self, repo: Path, ref: str) -> bool:
        return self.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                        cwd=repo, check=False).returncode == 0

    def is_dirty(self, worktree: Path) -> bool:
        result = self.run(["status", "--porcelain"], cwd=worktree, check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def head_commit(self, worktree: Path, ref: str = "HEAD") -> str | None:
        """The full id of the commit *ref* (``HEAD``, ``FETCH_HEAD``) names in *worktree*, or None."""
        result = self.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          cwd=worktree, check=False)
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and is_commit_id(commit) else None

    def unpushed_commits(self, worktree: Path, *, not_in: Sequence[str] = ()) -> int:
        """Commits on HEAD that no remote-tracking ref contains, nor any of *not_in*.

        Used as the refusal condition for `discard`: removing a worktree that still
        holds unpushed work destroys it silently. *not_in* names commits that are known
        not to be the session's work, such as a pull request head it was given to review.
        """
        excluded = [commit for commit in not_in if is_commit_id(commit)]
        if len(excluded) != len(not_in):
            return -1
        result = self.run(
            ["rev-list", "--count", "HEAD", "--not", "--remotes", *excluded],
            cwd=worktree, check=False,
        )
        if result.returncode != 0:
            # An unreadable worktree is treated as holding work, never as safe to drop.
            return -1
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return -1

    # ----- launch steps -----------------------------------------------------

    def fetch(self, repo: Path, remote: str = "origin") -> None:
        self.run(["fetch", "--prune", remote], cwd=repo, timeout=600)

    def add_worktree(self, repo: Path, worktree: Path, branch: str, base: str) -> None:
        """Create the worktree on a placeholder branch, or accept an existing one.

        Idempotent: a resumed launch that already created the worktree must not fail,
        and a worktree that exists on a *different* branch is a real conflict.
        """
        existing = self.list_worktrees(repo)
        for key in (str(worktree), str(worktree.resolve()) if worktree.exists() else str(worktree)):
            entry = existing.get(key)
            if entry is None:
                continue
            head = (entry.get("branch") or "").removeprefix("refs/heads/")
            if head and head != branch:
                raise GitError(
                    f"{worktree} already exists as a worktree on branch '{head}', not '{branch}'"
                )
            return
        if worktree.exists() and any(worktree.iterdir()):
            raise GitError(f"{worktree} already exists and is not empty, but git does not know it")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add"]
        if self.branch_exists(repo, branch):
            args += [str(worktree), branch]
        else:
            args += ["-b", branch, str(worktree), base]
        self.run(args, cwd=repo, timeout=600)

    def workspace_manifest(
        self, worktree: Path, pathspecs: Sequence[str]
    ) -> dict[str, str] | None:
        """What *pathspecs* name in *worktree*, each with a digest of its content.

        Tracked, untracked and ignored files all count, because an agent reads a file
        whatever git thinks of it. A symlink counts by its target path and, when that is
        a file, by the file's content; a tracked file that is gone counts as missing.
        None when the worktree cannot be listed or read.
        """
        if not pathspecs:
            return {}
        listing = self.run(
            ["ls-files", "-z", "--cached", "--others", "--", *pathspecs], cwd=worktree, check=False
        )
        if listing.returncode != 0:
            return None
        manifest: dict[str, str] = {}
        try:
            for name in sorted(set(listing.stdout.split("\0")) - {""}):
                manifest[name] = _entry_digest(worktree / name)
        except OSError:
            return None
        return manifest

    def add_detached_worktree(self, repo: Path, worktree: Path, base: str) -> None:
        """Create a worktree with a detached HEAD at *base*, or accept an existing one."""
        existing = self.list_worktrees(repo)
        if str(worktree) in existing or (
            worktree.exists() and str(worktree.resolve()) in existing
        ):
            if worktree.exists():
                return
            # Git still lists a checkout whose directory is gone; forget it, then create it.
            self.run(["worktree", "prune"], cwd=repo)
        if worktree.exists() and any(worktree.iterdir()):
            raise GitError(f"{worktree} already exists and is not empty, but git does not know it")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.run(["worktree", "add", "--detach", str(worktree), base], cwd=repo, timeout=600)

    def remove_worktree(self, repo: Path, worktree: Path, *, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree))
        result = self.run(args, cwd=repo, check=False, timeout=300)
        if result.returncode != 0 and worktree.exists():
            raise GitError(
                f"could not remove worktree {worktree}: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )
        self.run(["worktree", "prune"], cwd=repo, check=False)

    def delete_branch(self, repo: Path, branch: str, *, force: bool = False) -> None:
        if not self.branch_exists(repo, branch):
            return
        self.run(["branch", "-D" if force else "-d", branch], cwd=repo, check=False)


def remote_of(base: str, default: str = "origin") -> str:
    """`origin/main` -> `origin`; a bare branch name keeps the default remote."""
    if "/" in base:
        candidate = base.split("/", 1)[0]
        if candidate and not candidate.startswith("."):
            return candidate
    return default


def is_commit_id(value: str) -> bool:
    """A full SHA-1 or SHA-256 commit id, so that it reaches argv as a commit and nothing else."""
    return len(value) in (40, 64) and all(c in "0123456789abcdef" for c in value)


def _entry_digest(path: Path) -> str:
    """The kind of *path* and a digest of what it holds; a path that is gone is ``missing``."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(status.st_mode):
        target = os.readlink(path)
        digest = "link:" + hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
        # A link to a file is read as that file, so its content is part of the record.
        if path.is_file():
            digest += ":" + _file_digest(path)
        return digest
    if stat.S_ISREG(status.st_mode):
        return "file:" + _file_digest(path)
    if stat.S_ISDIR(status.st_mode):
        return "directory"
    return "other"


def _file_digest(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
