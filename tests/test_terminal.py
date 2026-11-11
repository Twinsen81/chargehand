"""The attach window: the argv that is run, the script file it opens, and what is refused."""

from __future__ import annotations

import inspect
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from chargehand import terminal

UNSAFE = (
    'claude attach x"; do shell script "rm',
    "claude attach $(id)",
    "claude attach x;id",
    "claude  attach x",
    "claude attach ../x",
    "claude attach -x",
    "claude attach " + "a" * 65,
    "",
)


class FakeRun:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_terminal_opens_a_private_script_through_launchservices(tmp_path):
    directory = tmp_path / "attach"
    fake = FakeRun()

    ok, message = terminal.open_window(
        "terminal", "claude attach abc-123", run=fake, platform="darwin", directory=directory
    )

    assert ok is True and "claude attach abc-123" in message
    (argv,) = fake.calls
    assert argv[:3] == ["/usr/bin/open", "-a", "Terminal"] and len(argv) == 4
    script = Path(argv[3])
    assert script.parent == directory and script.name.endswith(".command")
    assert mode(directory) == 0o700 and mode(script) == 0o700
    assert script.read_text() == (
        '#!/bin/sh\nrm -f -- "$0"\nexec "${SHELL:-/bin/sh}" -ic \'claude attach abc-123\'\n'
    )


def test_the_script_deletes_itself_before_it_runs_the_command(tmp_path):
    script = terminal.write_script("true", tmp_path / "attach")

    subprocess.run([str(script)], check=True, env={**os.environ, "SHELL": "/bin/sh"})

    assert not script.exists()


def test_the_script_hands_the_command_to_the_operators_shell_as_an_interactive_one(tmp_path):
    seen = tmp_path / "seen"
    shell = tmp_path / "shell"
    shell.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {seen}\n')
    shell.chmod(0o700)
    script = terminal.write_script("claude attach abc-123", tmp_path / "attach")

    subprocess.run([str(script)], check=True, env={**os.environ, "SHELL": str(shell)})

    assert seen.read_text().splitlines() == ["-ic", "claude attach abc-123"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_script_finds_a_command_that_is_an_alias_of_the_operators_shell(tmp_path):
    # An install whose `claude` is an alias in the shell's start file: a plain sh finds none.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("alias claude='echo ran'\n")
    script = terminal.write_script("claude attach abc-123", tmp_path / "attach")

    result = subprocess.run(
        [str(script)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        env={**os.environ, "HOME": str(home), "SHELL": shutil.which("bash")},
    )

    assert (result.returncode, result.stdout.strip()) == (0, "ran attach abc-123")


def test_the_script_takes_only_a_plain_command():
    with pytest.raises(ValueError):
        terminal.script_text("claude attach 'x'; rm -rf ~")


def test_each_script_has_its_own_unguessable_name(tmp_path):
    first = terminal.write_script("claude attach abc", tmp_path)
    second = terminal.write_script("claude attach abc", tmp_path)

    assert first != second
    assert len(first.stem.removeprefix("attach-")) == 16


def test_a_private_directory_that_was_open_to_others_is_closed_again(tmp_path):
    directory = tmp_path / "attach"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)

    terminal.write_script("claude attach abc", directory)

    assert mode(directory) == 0o700


def test_a_symlinked_directory_is_refused(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "attach").symlink_to(tmp_path / "elsewhere")

    ok, message = terminal.open_window(
        "terminal", "claude attach abc", run=FakeRun(), platform="darwin", directory=tmp_path / "attach"
    )

    assert ok is False and "not a directory" in message
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_stale_scripts_are_removed_when_the_next_one_is_written(tmp_path):
    old = terminal.write_script("claude attach old", tmp_path)
    fresh = terminal.write_script("claude attach fresh", tmp_path)
    past = old.stat().st_mtime - terminal.STALE_SECS - 1
    os.utime(old, (past, past))

    newest = terminal.write_script("claude attach next", tmp_path)

    assert not old.exists()
    assert fresh.exists() and newest.exists()


@pytest.mark.parametrize("command", UNSAFE)
@pytest.mark.parametrize("name", terminal.TERMINALS)
def test_an_unsafe_command_or_session_id_is_refused_and_writes_no_file(tmp_path, name, command):
    fake = FakeRun()

    ok, _ = terminal.open_window(name, command, run=fake, platform="darwin", directory=tmp_path / "a")

    assert ok is False
    assert fake.calls == []
    assert not (tmp_path / "a").exists()


def test_an_unknown_terminal_and_another_platform_are_refused(tmp_path):
    directory = tmp_path / "attach"
    fake = FakeRun()

    assert terminal.open_window("xterm", "claude attach abc", run=fake, platform="darwin",
                                directory=directory)[0] is False
    ok, message = terminal.open_window("terminal", "claude attach abc", run=fake, platform="linux",
                                       directory=directory)

    assert ok is False and "macOS only" in message
    assert fake.calls == [] and not directory.exists()


def test_a_failed_open_removes_its_script(tmp_path):
    directory = tmp_path / "attach"
    fake = FakeRun(returncode=1, stderr="LSOpenURLsWithRole() failed\x1b[2J")

    ok, message = terminal.open_window(
        "terminal", "claude attach abc", run=fake, platform="darwin", directory=directory
    )

    assert ok is False and "open exited 1" in message and "\x1b" not in message
    assert list(directory.iterdir()) == []


def test_terminal_is_never_driven_through_apple_events():
    source = inspect.getsource(terminal.write_script) + inspect.getsource(terminal.open_argv)
    assert "osascript" not in source
    assert 'tell application "Terminal"' not in inspect.getsource(terminal)


def test_iterm2_gets_the_command_as_an_argument_never_in_its_script():
    argv = terminal.osascript_argv("claude attach abc-123")

    assert argv[-1] == "claude attach abc-123"
    assert not any("abc-123" in part for part in argv[:-1])
    for bad in UNSAFE:
        with pytest.raises(ValueError):
            terminal.osascript_argv(bad)


def test_a_failed_osascript_says_how_to_allow_it_or_to_choose_terminal():
    fake = FakeRun(returncode=1, stderr="Not authorized to send Apple events to iTerm.\x1b[2J")

    ok, message = terminal.open_window("iterm2", "claude attach abc", run=fake, platform="darwin")

    assert ok is False
    assert "Not authorized" in message and "Automation" in message and "Terminal" in message
    assert "\x1b" not in message
