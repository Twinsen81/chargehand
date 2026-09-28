"""The git operations the launch sequence and garbage collection need.

Every call is a plain argv with no shell, and the ones the launch sequence uses are
idempotent so a resumed launch can simply run them again.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chargehand.errors import GitError


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
        if cwd is not None:
            argv += ["-C", str(cwd)]
        argv += list(args)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
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

    def unpushed_commits(self, worktree: Path) -> int:
        """Commits on HEAD that no remote-tracking ref contains.

        Used as the refusal condition for `discard`: removing a worktree that still
        holds unpushed work destroys it silently.
        """
        result = self.run(
            ["rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=worktree, check=False
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
