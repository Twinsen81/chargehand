"""The record of a worktree's agent files, which the wake check compares."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import run_git
from chargehand.gitutil import Git

PATHSPECS = (":(glob)**/.claude", ":(glob)**/.claude/**", ".mcp.json", ":(glob)**/CLAUDE.md")


@pytest.fixture
def worktree(tmp_path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    run_git("init", "--initial-branch=main", cwd=path)
    (path / ".claude" / "agents").mkdir(parents=True)
    (path / ".claude" / "agents" / "reviewer.md").write_text("agent\n")
    (path / "AGENTS.md").write_text("notes\n")
    (path / "CLAUDE.md").symlink_to("AGENTS.md")
    (path / "src").mkdir()
    (path / "src" / "code.py").write_text("x = 1\n")
    (path / ".gitignore").write_text(".claude/settings.local.json\n")
    run_git("add", "-A", cwd=path)
    run_git("commit", "-m", "init", cwd=path)
    return path


def test_the_record_holds_tracked_untracked_and_ignored_agent_files_only(worktree):
    (worktree / ".claude" / "settings.local.json").write_text("{}")
    (worktree / ".mcp.json").write_text("{}")
    (worktree / "notes.txt").write_text("not an agent file\n")

    manifest = Git().workspace_manifest(worktree, PATHSPECS)

    assert set(manifest) == {
        ".claude/agents/reviewer.md", ".claude/settings.local.json", ".mcp.json", "CLAUDE.md",
    }
    assert manifest[".mcp.json"].startswith("file:")


def test_a_file_behind_a_symlink_is_part_of_the_record(worktree):
    before = Git().workspace_manifest(worktree, PATHSPECS)
    (worktree / "AGENTS.md").write_text("changed instructions\n")

    after = Git().workspace_manifest(worktree, PATHSPECS)

    assert before["CLAUDE.md"].startswith("link:")
    assert after["CLAUDE.md"] != before["CLAUDE.md"]
    assert {k: v for k, v in after.items() if k != "CLAUDE.md"} == {
        k: v for k, v in before.items() if k != "CLAUDE.md"
    }


def test_a_directory_that_became_a_symlink_is_seen_by_its_target(worktree, tmp_path):
    elsewhere = tmp_path / "elsewhere" / ".claude"
    elsewhere.mkdir(parents=True)
    (elsewhere / "settings.json").write_text("{}")
    shutil.rmtree(worktree / ".claude")
    (worktree / ".claude").symlink_to(elsewhere)

    manifest = Git().workspace_manifest(worktree, PATHSPECS)

    assert manifest[".claude"].startswith("link:")
    assert manifest[".claude/agents/reviewer.md"] == "missing"
    (worktree / ".claude").unlink()
    (worktree / ".claude").symlink_to(tmp_path)
    assert Git().workspace_manifest(worktree, PATHSPECS)[".claude"] != manifest[".claude"]


def test_the_record_is_none_outside_a_repository_and_empty_without_pathspecs(worktree, tmp_path):
    assert Git().workspace_manifest(tmp_path, PATHSPECS) is None
    assert Git().workspace_manifest(worktree, ()) == {}


def test_a_configured_fsmonitor_program_does_not_run(tmp_path):
    """core.fsmonitor names a program git runs on status/ls-files; the -c override wins."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git("init", "--initial-branch=main", cwd=repo)
    (repo / "file.txt").write_text("x\n")
    run_git("add", "-A", cwd=repo)
    run_git("commit", "-m", "init", cwd=repo)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    run_git("config", "core.fsmonitor", str(hook), cwd=repo)

    # Every read the wake check makes goes through Git, so none may run the program.
    assert Git().workspace_manifest(repo, (":(glob)**/*.txt",)) is not None
    assert Git().is_dirty(repo) is False
    assert Git().unpushed_commits(repo) >= 0
    assert not marker.exists()


def test_a_configured_hook_does_not_run_for_a_git_call(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git("init", "--initial-branch=main", cwd=repo)
    marker = tmp_path / "hook-ran"
    hooks = repo / "hooks"
    hooks.mkdir()
    (hooks / "post-commit").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    (hooks / "post-commit").chmod(0o755)
    run_git("config", "core.hooksPath", str(hooks), cwd=repo)
    # A commit needs an identity; the runner has none set globally, so set it in the repo.
    run_git("config", "user.email", "t@example.invalid", cwd=repo)
    run_git("config", "user.name", "t", cwd=repo)
    (repo / "file.txt").write_text("x\n")

    git = Git()
    git.run(["add", "-A"], cwd=repo)
    git.run(["commit", "-m", "init"], cwd=repo)

    assert not marker.exists()
