"""`export` and `import`: a setup carried from one home directory to another."""

from __future__ import annotations

import io
import json
import os
import stat
import tomllib
import zipfile
from pathlib import Path

import pytest

from chargehand import install, paths, serve, transfer
from chargehand.cli import EXIT_OK, EXIT_REFUSED, main
from chargehand.config import MachineConfig, load_machine_config
from chargehand.ledger import BOARD_REPOSITORY, BOARD_WORKER, open_ledger


class Machine:
    """One user account: a home directory with chargehand's and Claude Code's files in it."""

    def __init__(self, root: Path) -> None:
        self.home = root
        self.config_dir = root / ".config" / "chargehand"
        self.config = self.config_dir / "config.toml"
        self.state = root / "state"
        self.claude = root / ".claude"

    def enter(self, monkeypatch) -> "Machine":
        self.home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv(paths.CONFIG_ENV, str(self.config))
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(self.state))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.claude))
        return self

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.sqlite"


def _config_text(home: Path) -> str:
    return f"""\
# chargehand on the old machine; repositories live in {home}/code
poll_interval_secs = 120
max_run_hours = 5
worktree_root = "{home}/worktrees"
claude_bin = "{home}/.local/bin/claude"
deny_rules = [
  "Bash(*chargehand *)",
  "Bash(*claude stop*)",
]

[serve]
port = 7990
token_file = "{home}/.config/chargehand/board.token"
usage_file = "{home}/.cache/other-usage.json"

[notify]
command = "~/.config/chargehand/notify.sh --quiet"

[labels]
queued = "queue-me"
running = "running-now"
blocked = "help-me"

[[repository]]
name = "app"
path = "{home}/code/app"
tracker = {{ type = "linear", team = "ABC", api_key_command = "op read secret", keychain_service = "svc" }}
roles = ["implementer"]

[[repository]]
path = "{home}/code/other"
tracker = {{ type = "github", repo = "me/other", gh_bin = "{home}/bin/gh" }}

[[source]]
name = "tickets"
type = "command"
poll = "{home}/bin/poll --all"
token = "s3cr3t-value"
"""


@pytest.fixture
def old(tmp_path, monkeypatch) -> Machine:
    machine = Machine(tmp_path / "old").enter(monkeypatch)
    machine.config_dir.mkdir(parents=True)
    machine.config.write_text(_config_text(machine.home))
    notify = machine.config_dir / "notify.sh"
    notify.write_text("#!/bin/sh\necho \"$CHARGEHAND_ITEM\"\n")
    notify.chmod(0o755)
    (machine.config_dir / "roles").mkdir()
    (machine.config_dir / "roles" / "helper.toml").write_text(
        'prompt = "help with {item}"\nagent = "helper"\n'
    )
    repo_roles = machine.home / "code" / "app" / ".chargehand" / "roles"
    repo_roles.mkdir(parents=True)
    (repo_roles / "implementer.toml").write_text('prompt = "work on {item}"\nagent = "builder"\n')
    agents = machine.claude / "agents"
    agents.mkdir(parents=True)
    for name in ("helper", "builder", "unrelated"):
        (agents / f"{name}.md").write_text(f"# {name}\n")
    (machine.claude / "settings.json").write_text(json.dumps({
        "theme": "dark",
        "statusLine": {
            "type": "command",
            "command": f"{machine.home}/.local/bin/chargehand statusline -- '~/.claude/line.sh'",
        },
    }))
    serve.ensure_token(machine.config_dir / "board.token")
    with open_ledger(machine.ledger) as ledger:
        ledger.set_board_state(BOARD_REPOSITORY, "app", "name", "My App")
        ledger.set_board_state(BOARD_REPOSITORY, "app", "icon", "rocket")
        ledger.set_board_state(BOARD_REPOSITORY, "other", "hidden", "1")
        ledger.set_board_state(BOARD_REPOSITORY, "gone", "pinned", "1")
        ledger.set_board_state(BOARD_WORKER, "7", "name", "a worker")
    return machine


def _export(machine: Machine, target: Path, *extra: str) -> int:
    return main(["export", str(target), *extra])


def _members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zipped:
        return {name: zipped.read(name) for name in zipped.namelist()}


def _import(new: Machine, archive: Path, monkeypatch, answers: str = "", *extra: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(answers))
    return main(["import", str(archive), *extra])


def _rezip(archive: Path, target: Path, change) -> Path:
    members = _members(archive)
    change(members)
    with zipfile.ZipFile(target, "w") as zipped:
        for name, content in members.items():
            zipped.writestr(name, content)
    return target


def test_an_export_holds_the_setup_without_a_secret_or_a_home_path(old, tmp_path, capsys):
    archive = tmp_path / "setup.zip"

    assert _export(old, archive) == EXIT_OK

    members = _members(archive)
    assert sorted(members) == [
        "agents/builder.md", "agents/helper.md", "board.json", "config.toml", "manifest.json",
        "notify/notify.sh", "roles/helper.toml", "statusline.json",
    ]
    token = (old.config_dir / "board.token").read_text().strip()
    for name, content in members.items():
        text = content.decode()
        assert token not in text, name
        assert str(old.home) not in text, name
        assert "/Users/" not in text, name
        assert "s3cr3t-value" not in text and "op read secret" not in text, name
    manifest = json.loads(members["manifest.json"])
    assert manifest["format"] == transfer.FORMAT_VERSION
    assert manifest["members"] == sorted(set(members) - {"manifest.json"})
    assert {d["where"] for d in manifest["dropped"]} == {
        "[[repository]] app: tracker.api_key_command", "[[source]] tickets: token",
    }
    board = json.loads(members["board.json"])["rows"]
    assert {(r["key"], r["field"]) for r in board} == {("app", "name"), ("app", "icon"), ("other", "hidden")}
    out = capsys.readouterr().out
    assert "left out [[repository]] app: tracker.api_key_command" in out


def test_the_exported_configuration_keeps_what_it_does_not_rewrite(old, tmp_path):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK

    text = _members(archive)["config.toml"].decode()
    data = tomllib.loads(text)
    assert text.startswith("# chargehand on the old machine; repositories live in ~/code\n")
    assert data["poll_interval_secs"] == 120 and data["max_run_hours"] == 5
    assert data["deny_rules"] == ["Bash(*chargehand *)", "Bash(*claude stop*)"]
    assert data["labels"] == {"queued": "queue-me", "running": "running-now", "blocked": "help-me"}
    assert data["worktree_root"] == "~/worktrees"
    assert data["claude_bin"] == "~/.local/bin/claude"
    assert data["serve"] == {"port": 7990, "usage_file": "~/.cache/other-usage.json"}
    assert data["notify"]["command"] == "notify.sh --quiet"
    first, second = data["repository"]
    assert first["tracker"] == {"type": "linear", "team": "ABC", "keychain_service": "svc"}
    assert second["name"] == "other"
    assert second["path"] == "~/code/other"
    assert second["tracker"]["gh_bin"] == "~/bin/gh"
    assert data["source"][0] == {"name": "tickets", "type": "command", "poll": "~/bin/poll --all"}
    MachineConfig.parse(data)


def test_export_refuses_to_overwrite_an_archive_without_force(old, tmp_path, capsys):
    archive = tmp_path / "setup.zip"
    archive.write_text("keep me")

    assert _export(old, archive) == EXIT_REFUSED
    assert archive.read_text() == "keep me"
    assert "--force" in capsys.readouterr().err

    assert _export(old, archive, "--force") == EXIT_OK
    assert zipfile.is_zipfile(archive)


def test_export_refuses_a_file_that_holds_the_home_directory(old, tmp_path, capsys):
    (old.config_dir / "roles" / "helper.toml").write_text(
        f'prompt = "read {old.home}/notes.md for {{item}}"\n'
    )

    assert _export(old, tmp_path / "setup.zip") != EXIT_OK

    assert "roles/helper.toml contains the home directory" in capsys.readouterr().err
    assert not (tmp_path / "setup.zip").exists()


def test_an_import_restores_the_setup_in_another_home(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    old_token = (old.config_dir / "board.token").read_text()
    new = Machine(tmp_path / "new").enter(monkeypatch)
    capsys.readouterr()

    # One line per repository, then the worktree root; an empty line takes the default.
    answers = f"{new.home}/src/app\n\n/srv/worktrees\n"
    assert _import(new, archive, monkeypatch, answers) == EXIT_OK

    config = load_machine_config()
    assert config.repository_by_name("app").path == new.home / "src" / "app"
    assert config.repository_by_name("other").path == new.home / "code" / "other"
    assert config.worktree_root == Path("/srv/worktrees")
    assert config.claude_bin == f"{new.home}/.local/bin/claude"
    assert config.serve.token_file == new.config_dir / "board.token"
    text = new.config.read_text()
    assert 'path = "~/src/app"' in text
    assert "api_key_command" not in text
    notify = new.config_dir / "notify.sh"
    assert config.notify.command == "~/.config/chargehand/notify.sh --quiet"
    assert notify.read_text() == (old.config_dir / "notify.sh").read_text()
    assert stat.S_IMODE(notify.stat().st_mode) == 0o755
    assert (new.config_dir / "roles" / "helper.toml").is_file()
    assert sorted(p.name for p in (new.claude / "agents").iterdir()) == ["builder.md", "helper.md"]
    assert not (new.claude / "settings.json").exists()

    token = new.config_dir / "board.token"
    assert serve.token_problem(token) is None
    assert token.read_text() != old_token
    with open_ledger(new.ledger) as ledger:
        rows = {(r["kind"], r["key"], r["field"]): r["value"] for r in ledger.board_state()}
    assert rows == {
        (BOARD_REPOSITORY, "app", "name"): "My App",
        (BOARD_REPOSITORY, "app", "icon"): "rocket",
        (BOARD_REPOSITORY, "other", "hidden"): "1",
    }
    out = capsys.readouterr().out
    assert "wrote a new token" in out
    assert "restored 3 board setting(s)" in out
    assert "gh auth login" in out


def test_import_takes_the_answers_from_flags(old, tmp_path, monkeypatch):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    new = Machine(tmp_path / "new").enter(monkeypatch)
    monkeypatch.chdir(tmp_path)

    assert _import(
        new, archive, monkeypatch, "",
        "--repository", "app=checkouts/app", "--repository", f"other={new.home}/o",
        "--worktree-root", "~/wt",
    ) == EXIT_OK

    config = load_machine_config()
    assert config.repository_by_name("app").path == tmp_path / "checkouts" / "app"
    assert config.repository_by_name("other").path == new.home / "o"
    assert config.worktree_root == new.home / "wt"


def test_import_refuses_a_repository_the_archive_does_not_have(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, archive, monkeypatch, "", "--repository", "nope=/x") != EXIT_OK

    assert "no repository named nope" in capsys.readouterr().err
    assert not new.config.exists()


def test_a_second_import_overwrites_only_with_force(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    new = Machine(tmp_path / "new").enter(monkeypatch)
    assert _import(new, archive, monkeypatch) == EXIT_OK
    new.config.write_text(new.config.read_text() + "# edited here\n")
    capsys.readouterr()

    assert _import(new, archive, monkeypatch) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert str(new.config) in err and "board setting name of app" in err
    assert new.config.read_text().endswith("# edited here\n")

    assert _import(new, archive, monkeypatch, "", "--force") == EXIT_OK
    assert "# edited here" not in new.config.read_text()


def test_import_refuses_a_newer_format(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK

    def newer(members):
        manifest = json.loads(members["manifest.json"])
        manifest["format"] = 999
        members["manifest.json"] = json.dumps(manifest).encode()

    newer_archive = _rezip(archive, tmp_path / "newer.zip", newer)
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, newer_archive, monkeypatch) == EXIT_REFUSED

    assert "format version 999" in capsys.readouterr().err
    assert not new.config.exists()


@pytest.mark.parametrize("name", ["../escape.toml", "/etc/evil", "roles/../../x.toml", "other.txt"])
def test_import_refuses_a_member_outside_the_layout(old, tmp_path, monkeypatch, capsys, name):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    bad = _rezip(archive, tmp_path / "bad.zip", lambda members: members.update({name: b"x"}))
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, bad, monkeypatch) == EXIT_REFUSED

    assert "not a file an export writes" in capsys.readouterr().err
    assert not new.config.exists()
    assert not (tmp_path / "escape.toml").exists()


def test_import_refuses_a_symlink(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    members = _members(archive)
    bad = tmp_path / "link.zip"
    with zipfile.ZipFile(bad, "w") as zipped:
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            if name == "roles/helper.toml":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                content = b"/etc/passwd"
            zipped.writestr(info, content)
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, bad, monkeypatch) == EXIT_REFUSED

    assert "not a regular file" in capsys.readouterr().err


def test_import_refuses_a_member_its_manifest_does_not_list(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    bad = _rezip(archive, tmp_path / "bad.zip",
                 lambda members: members.update({"roles/extra.toml": b'prompt = "x"\n'}))
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, bad, monkeypatch) == EXIT_REFUSED

    assert "not the ones its manifest lists" in capsys.readouterr().err


def test_import_cleans_board_settings_as_the_board_does(old, tmp_path, monkeypatch):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK

    def tamper(members):
        members["board.json"] = json.dumps({"rows": [
            {"kind": "repository", "key": "app", "field": "name", "value": "Bad\x1b[31m\nName"},
            {"kind": "repository", "key": "app", "field": "icon", "value": "<script>"},
            {"kind": "repository", "key": "app", "field": "pinned", "value": "yes"},
            {"kind": "repository", "key": "app", "field": "owner", "value": "x"},
            {"kind": "worker", "key": "7", "field": "name", "value": "w"},
        ]}).encode()

    tampered = _rezip(archive, tmp_path / "tampered.zip", tamper)
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, tampered, monkeypatch) == EXIT_OK

    with open_ledger(new.ledger) as ledger:
        rows = [(r["key"], r["field"], r["value"]) for r in ledger.board_state()]
    assert len(rows) == 1
    assert rows[0][:2] == ("app", "name")
    assert "\x1b" not in rows[0][2] and "\n" not in rows[0][2]


def _doctor(capsys) -> list[dict]:
    main(["doctor", "--json", "--no-tracker"])
    return json.loads(capsys.readouterr().out)


def test_doctor_lists_the_steps_an_import_left(old, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    new = Machine(tmp_path / "new").enter(monkeypatch)
    assert _import(new, archive, monkeypatch) == EXIT_OK
    capsys.readouterr()

    checks = _doctor(capsys)

    steps = [c for c in checks if c["name"].startswith("imported")]
    by_detail = {c["detail"]: c["status"] for c in steps}
    assert any("api_key_command" in d and s == install.WARN for d, s in by_detail.items())
    assert any("[[source]] tickets: token" in d and s == install.WARN for d, s in by_detail.items())
    assert any("statusLine" in d and s == install.WARN for d, s in by_detail.items())
    assert any("Linear tracker its API key" in d and s == install.INFO for d, s in by_detail.items())
    assert any("notify.sh came from the other machine" in d for d in by_detail)
    assert any("chargehand install --load" in d or "chargehand serve" in d for d in by_detail)

    text = new.config.read_text().replace(
        'keychain_service = "svc"', 'keychain_service = "svc", api_key_command = "op read x"'
    )
    new.config.write_text(text)
    (new.claude / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "chargehand statusline -- ~/line.sh"}}
    ))

    steps = [c for c in _doctor(capsys) if c["name"].startswith("imported")]
    by_detail = {c["detail"]: c["status"] for c in steps}
    assert any("api_key_command" in d and s == install.OK for d, s in by_detail.items())
    assert any("statusLine" in d and s == install.OK for d, s in by_detail.items())
    assert any("[[source]] tickets: token" in d and s == install.WARN for d, s in by_detail.items())


def test_doctor_says_nothing_about_imports_without_the_steps_file(old, capsys):
    assert not [c for c in _doctor(capsys) if c["name"].startswith("imported")]


def test_a_rewritten_value_is_valid_toml():
    value = {"a b": 'quote " and \\ and \x7f', "list": [1, 2.5, True, "é"], "inner": {}}

    assert tomllib.loads("v = " + transfer._toml(value))["v"] == value


def test_a_repository_named_by_its_path_keeps_its_name(old, tmp_path, monkeypatch):
    archive = tmp_path / "setup.zip"
    assert _export(old, archive) == EXIT_OK
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, archive, monkeypatch, "", "--repository", "other=/elsewhere/renamed") == EXIT_OK

    assert load_machine_config().repository_by_name("other").path == Path("/elsewhere/renamed")


def test_a_home_path_in_a_value_export_cannot_rewrite_is_refused(old, tmp_path, capsys):
    text = old.config.read_text().replace(
        'type = "command"', f'type = "command"\nmark = "mark --log {old.home}/mark.log"'
    )
    old.config.write_text(text)

    assert _export(old, tmp_path / "setup.zip") != EXIT_OK

    assert "config.toml contains the home directory" in capsys.readouterr().err


def test_an_archive_that_is_not_a_zip_is_refused(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "setup.zip"
    bad.write_text("not a zip")
    new = Machine(tmp_path / "new").enter(monkeypatch)

    assert _import(new, bad, monkeypatch) == EXIT_REFUSED
    assert not new.config.exists()
    assert os.listdir(new.home) == []
