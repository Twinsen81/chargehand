"""`export` and `import`: one machine's setup, carried to another without its secrets.

The archive is a zip file with a manifest. It holds the machine configuration with its
machine-specific paths rewritten and the keys that can carry a credential left out, the
machine-level role files, the user-level agent files that the roles name, the
notification hook, the status line entry of Claude Code's settings, and the board
settings of the configured repositories. Repository-level role files stay with their
repository. The daemon's token, Keychain values and the rest of the ledger are never in it.

The standard library has no TOML writer, so the configuration is rewritten as text: its
comments and the keys chargehand does not know stay as they are, the values of known keys
are replaced, and the result is parsed again before it is used.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import stat
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from datetime import time as clock_time
from pathlib import Path

from chargehand import __version__, claude, install, paths, sanitize, serve
from chargehand import board as board_mod
from chargehand import ledger as ledger_mod
from chargehand.config import TOKEN_FILE_NAME, MachineConfig
from chargehand.errors import ChargehandError, ConfigError

# The archive layout this release writes. An import refuses a higher number, because a
# newer release may have put something in the archive that this one would drop silently.
FORMAT_VERSION = 1

MANIFEST = "manifest.json"
CONFIG_MEMBER = "config.toml"
BOARD_MEMBER = "board.json"
STATUSLINE_MEMBER = "statusline.json"
ROLES_DIR = "roles"
AGENTS_DIR = "agents"
NOTIFY_DIR = "notify"

MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_MEMBERS = 512

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_SAFE_NAME = re.compile(_NAME)
# Every member an archive may hold. A name outside this list, such as an absolute path or
# one with `..`, is refused before anything is read.
_MEMBER = re.compile(
    rf"{re.escape(MANIFEST)}|{re.escape(CONFIG_MEMBER)}|{re.escape(BOARD_MEMBER)}"
    rf"|{re.escape(STATUSLINE_MEMBER)}|{ROLES_DIR}/{_NAME}\.toml|{AGENTS_DIR}/{_NAME}\.md"
    rf"|{NOTIFY_DIR}/{_NAME}"
)

# Keys whose value can be, or can print, a credential. They are left out of the archive
# and listed as manual steps. `api_key_env` and `token_file` name where a secret is, and
# stay.
_CREDENTIAL_KEY = re.compile(
    r"(?:^|_)(?:api_key|api_key_command|token|secret|password|passwd|credentials?)$"
)
# Keys whose value is a command whose first word chargehand expands with `~`.
_COMMAND_KEYS = {("notify",): ("command",), ("source",): ("poll", "mark", "item_open")}
_REPOSITORY_ARRAYS = ("repository", "route")

DEFAULT_WORKTREE_ROOT = "~/chargehand-worktrees"

_HEADER = re.compile(r"^\s*(\[\[?)\s*([A-Za-z0-9_.-]+)\s*\]\]?\s*(?:#.*)?$")
_ASSIGNMENT = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*=(.*)$")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

_KEEP = object()
_DROP = object()


class Refused(ChargehandError):
    """An export or an import that would overwrite a file, or an archive this release refuses."""


# ----- the configuration as text ------------------------------------------------


def _toml(value: object) -> str:
    """*value* as a TOML value on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # JSON's escapes are all valid in a TOML basic string; DEL is the one character
        # TOML wants escaped and JSON leaves alone.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007F")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, (datetime, date, clock_time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        pairs = ", ".join(f"{_toml_key(str(k))} = {_toml(v)}" for k, v in value.items())
        return "{ " + pairs + " }"
    raise ConfigError(f"cannot write a {type(value).__name__} value as TOML")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml(key)


@dataclass(frozen=True)
class _Place:
    """Where an assignment is: its table's dotted name and the index of its array table."""

    table: tuple[str, ...]
    index: int


Edit = Callable[[_Place, str, object], object]
Insert = Callable[[_Place, bool], list[str]]


def _rewrite(text: str, edit: Edit, after_header: Insert | None = None) -> list[str]:
    """The lines of *text*, with each assignment replaced by what *edit* returns for it.

    *edit* returns ``_KEEP``, ``_DROP`` or a new value. A value over several lines is read
    as a whole. A line this does not understand, such as a dotted or a quoted key, is kept;
    the caller checks the parsed result, so nothing it should have changed gets through.
    """
    lines = text.splitlines()
    out: list[str] = []
    arrays: dict[str, int] = {}
    place = _Place((), 0)
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _HEADER.match(line)
        if header:
            parts = tuple(header.group(2).split("."))
            is_array = header.group(1) == "[["
            if is_array and len(parts) == 1:
                arrays[parts[0]] = arrays.get(parts[0], 0) + 1
            place = _Place(parts, max(arrays.get(parts[0], 0) - 1, 0))
            out.append(line)
            if after_header is not None:
                out.extend(after_header(place, is_array))
            i += 1
            continue
        assignment = _ASSIGNMENT.match(line)
        if not assignment or line.lstrip().startswith("#"):
            out.append(line)
            i += 1
            continue
        end, chunk = i, assignment.group(3)
        while True:
            try:
                value = tomllib.loads("v =" + chunk)["v"]
                break
            except tomllib.TOMLDecodeError:
                end += 1
                if end >= len(lines):
                    raise ConfigError(f"line {i + 1}: cannot read the value of '{assignment.group(2)}'")
                chunk += "\n" + lines[end]
        result = edit(place, assignment.group(2), value)
        if result is _KEEP:
            out.extend(lines[i : end + 1])
        elif result is not _DROP:
            out.append(f"{assignment.group(1)}{assignment.group(2)} = {_toml(result)}")
        i = end + 1
    return out


def _insert_at_root(lines: list[str], line: str) -> list[str]:
    """*lines* with *line* added to the root table, before the first table header."""
    first = next((n for n, text in enumerate(lines) if _HEADER.match(text)), len(lines))
    return [*lines[:first], line, *lines[first:]]


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _homes() -> list[str]:
    """The home directory as text, in each spelling a path may use. Empty for a bare `/`."""
    spellings: list[str] = []
    for candidate in (str(_home()), os.path.realpath(_home())):
        candidate = candidate.rstrip("/")
        if candidate and candidate not in spellings:
            spellings.append(candidate)
    return spellings


def _portable(value: str) -> str:
    """*value* with a leading home directory written as `~`."""
    for home in _homes():
        if value == home:
            return "~"
        if value.startswith(home + "/"):
            return "~" + value[len(home):]
    return value


def _program_word(path: Path) -> str:
    """*path* as the first word of a command that chargehand expands `~` in.

    A quoted `~` is not expanded, so the home form is used only when the rest of the path
    needs no quotes.
    """
    portable = _portable(str(path))
    if portable.startswith("~/") and shlex.quote(portable[1:]) == portable[1:]:
        return portable
    return shlex.quote(str(path))


def _expanded(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def _repository_name(entry: Mapping[str, object]) -> str:
    """The name the configuration gives a repository table, as `config.Repository` does."""
    name = entry.get("name")
    if isinstance(name, str):
        return name
    raw = entry.get("path", entry.get("repo", ""))
    return _expanded(str(raw)).name


def _names(data: Mapping[str, object], array: str) -> list[str]:
    entries = data.get(array, [])
    if not isinstance(entries, list):
        return []
    if array == "source":
        return [str(e.get("name", e.get("type", ""))) if isinstance(e, Mapping) else "" for e in entries]
    return [_repository_name(e) if isinstance(e, Mapping) else "" for e in entries]


@dataclass(frozen=True)
class Dropped:
    """A configuration key that export left out: what to show, and where to look for it."""

    where: str
    path: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"where": self.where, "path": list(self.path)}


@dataclass
class _ExportEdit:
    data: Mapping[str, object]
    notify_program: str | None
    notify_args: list[str]
    dropped: list[Dropped] = field(default_factory=list)

    def owner(self, place: _Place) -> tuple[str, ...]:
        """The first steps of a dropped key's path: the named table entry it is in."""
        top = place.table[0] if place.table else ""
        if top in _REPOSITORY_ARRAYS or top == "source":
            names = _names(self.data, top)
            name = names[place.index] if place.index < len(names) else str(place.index)
            return ("repository" if top in _REPOSITORY_ARRAYS else "source", name, *place.table[1:])
        return place.table

    def drop(self, place: _Place, keys: tuple[str, ...]) -> None:
        path = (*self.owner(place), *keys)
        if path[0] in ("repository", "source"):
            where = f"[[{path[0]}]] {path[1]}: {'.'.join(path[2:])}"
        else:
            where = ".".join(path)
        self.dropped.append(Dropped(where, path))

    def clean(self, place: _Place, keys: tuple[str, ...], value: object) -> object:
        """A table value without its credential keys, and with portable `*_bin` paths."""
        if isinstance(value, Mapping):
            cleaned = {}
            for key, item in value.items():
                if _CREDENTIAL_KEY.search(str(key)):
                    self.drop(place, (*keys, str(key)))
                    continue
                cleaned[key] = self.clean(place, (*keys, str(key)), item)
            return cleaned
        if isinstance(value, str) and keys and keys[-1].endswith("_bin"):
            return _portable(value)
        return value

    def __call__(self, place: _Place, key: str, value: object) -> object:
        if _CREDENTIAL_KEY.search(key):
            self.drop(place, (key,))
            return _DROP
        top = place.table[:1]
        if isinstance(value, Mapping):
            return self.clean(place, (key,), value)
        if not isinstance(value, str):
            return _KEEP
        if place.table == () and key == "worktree_root":
            return _portable(value)
        if place.table == ("serve",) and key in ("token_file", "usage_file"):
            default = (
                paths.config_dir() / TOKEN_FILE_NAME if key == "token_file" else paths.usage_file()
            )
            # The default follows the configuration directory on the other machine, so it
            # is left to the default there.
            return _DROP if _expanded(value) == default else _portable(value)
        if place.table == ("notify",) and key == "command":
            if self.notify_program is not None:
                return shlex.join([self.notify_program, *self.notify_args])
            return _portable(value)
        if len(place.table) == 1 and place.table[0] in _REPOSITORY_ARRAYS and key in ("path", "repo"):
            return _portable(value)
        if key in _COMMAND_KEYS.get(top, ()) or key.endswith("_bin"):
            return _portable(value)
        return _KEEP


def _export_config(
    text: str, data: Mapping[str, object], notify_program: str | None, notify_args: list[str]
) -> tuple[str, list[Dropped]]:
    edit = _ExportEdit(data, notify_program, notify_args)

    def name_line(place: _Place, is_array: bool) -> list[str]:
        # A repository named by its path's last part would change its name with the path
        # the import gives it, and the board settings are kept by name.
        if not (is_array and len(place.table) == 1 and place.table[0] in _REPOSITORY_ARRAYS):
            return []
        entries = data.get(place.table[0], [])
        entry = entries[place.index] if isinstance(entries, list) and place.index < len(entries) else {}
        if not isinstance(entry, Mapping) or "name" in entry:
            return []
        return [f"name = {_toml(_repository_name(entry))}"]

    lines = _rewrite(text, edit, name_line)
    # A comment does nothing, so a home path in one is only rewritten.
    homes = _homes()
    for n, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            for home in homes:
                line = line.replace(home, "~")
            lines[n] = line
    result = "\n".join(lines) + "\n"
    try:
        parsed = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the rewritten configuration does not parse: {exc}") from exc
    left = _credential_keys(parsed)
    if left:
        raise ConfigError(
            f"cannot leave out {', '.join(left)}: write each one as `key = value` in its own "
            "table, then export again"
        )
    return result, edit.dropped


def _credential_keys(value: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = f"{prefix}{key}"
            if _CREDENTIAL_KEY.search(str(key)):
                found.append(name)
            found.extend(_credential_keys(item, name + "."))
    elif isinstance(value, list):
        for item in value:
            found.extend(_credential_keys(item, prefix))
    return found


# ----- export --------------------------------------------------------------------


@dataclass
class Exported:
    path: Path
    members: list[str]
    dropped: list[Dropped]
    board_rows: int
    notify_member: str | None = None
    # One line per program the configuration names and the archive does not carry.
    kept: list[str] = field(default_factory=list)


def _role_agent(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            agent = tomllib.load(handle).get("agent")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return agent if isinstance(agent, str) and _SAFE_NAME.fullmatch(agent) else None


def _statusline_entry() -> dict[str, object] | None:
    entry = claude.read_status_line(claude.user_settings_file())
    if not claude.runs_statusline_wrapper(entry):
        return None
    assert isinstance(entry, Mapping)
    portable = dict(entry)
    command = str(entry["command"])
    for home in _homes():
        command = command.replace(home, "~")
    portable["command"] = command
    return portable


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    real = Path(os.path.realpath(path))
    return any(real.is_relative_to(os.path.realpath(root)) for root in roots)


def _kept(key: str, program: str, reason: str) -> dict[str, str]:
    """A program the configuration names and the archive does not carry."""
    return {"key": key, "program": _portable(program), "reason": reason}


def _notify_script(
    config: MachineConfig, config_dirs: Iterable[Path]
) -> tuple[Path | None, list[str], dict[str, str] | None]:
    """The notification hook's program and its arguments when the archive carries it, or why not.

    Only a script of the operator's own, in the home or the configuration directory, is
    carried. A program installed elsewhere, such as a system or Homebrew binary, may need
    its install location, so the command keeps naming it there.
    """
    if not config.notify.command:
        return None, [], None
    try:
        argv = shlex.split(os.path.expanduser(config.notify.command))
    except ValueError:
        return None, [], None
    if not argv:
        return None, [], None
    if "/" not in argv[0]:
        return None, [], _kept("notify.command", argv[0], "it is a program on PATH")
    program = Path(os.path.expanduser(argv[0]))
    if not program.is_file():
        return None, [], _kept("notify.command", str(program), "it is not a file on this machine")
    if not _SAFE_NAME.fullmatch(program.name):
        return None, [], _kept(
            "notify.command", str(program),
            "its file name is not one an archive holds (letters, digits, dot, dash, underscore)",
        )
    if not _inside(program, [_home(), *config_dirs]):
        return None, [], _kept(
            "notify.command", str(program),
            "it is installed outside the home and configuration directories",
        )
    reason = _cannot_travel(program)
    if reason is not None:
        return None, [], _kept("notify.command", str(program), reason)
    return program, argv[1:], None


def _kept_binaries(data: Mapping[str, object]) -> list[dict[str, str]]:
    """Every `*_bin` key that names a path rather than a program on PATH, such as `gh_bin`."""
    kept: list[dict[str, str]] = []

    def walk(value: object, label: str) -> None:
        if not isinstance(value, Mapping):
            return
        for key, item in value.items():
            where = f"{label}{key}"
            if isinstance(item, str) and str(key).endswith("_bin") and "/" in item:
                kept.append(_kept(where, os.path.expanduser(item), "the archive carries no binaries"))
            walk(item, where + ".")

    walk({k: v for k, v in data.items() if k not in (*_REPOSITORY_ARRAYS, "source")}, "")
    for array in (*_REPOSITORY_ARRAYS, "source"):
        entries = data.get(array, [])
        for entry, name in zip(entries if isinstance(entries, list) else [], _names(data, array)):
            walk(entry, f"[[{array}]] {name}: ")
    return kept


def _cannot_travel(program: Path) -> str | None:
    """Why the notify program cannot be copied as it is, or None when it can.

    A tool manager installs binaries and generated shims under the home directory too, and
    the operator cannot edit those, so they are named in the command instead of copied.
    """
    if program.stat().st_size > MAX_MEMBER_BYTES:
        return f"it is larger than {MAX_MEMBER_BYTES} bytes"
    content = program.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "it is not a text script"
    if "\x00" in text:
        return "it is not a text script"
    if any(home in text for home in _homes()):
        return (
            "it contains the home directory's path (if it is your own script, write the "
            "path as $HOME and export again)"
        )
    return None


def _check_limits(members: Mapping[str, bytes]) -> None:
    """Refuse an archive that import would refuse."""
    if len(members) + 1 > MAX_MEMBERS:
        raise ChargehandError(f"the archive would hold more than {MAX_MEMBERS} files")
    for name, content in members.items():
        if len(content) > MAX_MEMBER_BYTES:
            raise ChargehandError(
                f"{name} is larger than {MAX_MEMBER_BYTES} bytes, which import refuses"
            )


def _board_rows(names: Iterable[str], ledger_path: Path) -> list[dict[str, str]]:
    """The board settings of the configured repositories.

    Worker rows are keyed by local worker ids, which mean nothing on another machine.
    """
    if not ledger_path.exists():
        return []
    wanted = set(names)
    with ledger_mod.open_ledger(ledger_path) as ledger:
        rows = ledger.board_state()
    return [
        {"kind": str(r["kind"]), "key": str(r["key"]), "field": str(r["field"]), "value": str(r["value"])}
        for r in rows
        if r["kind"] == ledger_mod.BOARD_REPOSITORY and r["key"] in wanted
    ]


def _check_secrets(members: Mapping[str, bytes], token: str | None) -> None:
    for name, content in members.items():
        text = content.decode("utf-8", errors="replace")
        if token and token in text:
            raise ChargehandError(f"{name} contains the daemon's token; remove it and export again")
        for home in _homes():
            if home in text:
                raise ChargehandError(
                    f"{name} contains the home directory {home}; write it as ~ "
                    "(or $HOME in a script) and export again"
                )


def _write_atomically(target: Path, content: bytes, mode: int = 0o644) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(content)
        os.chmod(temporary, mode)
        # Replaces a symlink at *target* itself, rather than the file it points to.
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def export_setup(
    target: Path, *, config_path: Path, force: bool = False, ledger_path: Path | None = None,
    now: float | None = None,
) -> Exported:
    """Write the setup of this machine to the archive *target*."""
    if (target.exists() or target.is_symlink()) and not force:
        raise Refused(f"{target} already exists; pass --force to overwrite")
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"missing configuration file: {config_path}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc
    config = MachineConfig.parse(data, source=config_path)

    members: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    notify_program, notify_args, notify_kept = _notify_script(
        config, [paths.config_dir(), config_path.parent]
    )
    kept = _kept_binaries(data) + ([notify_kept] if notify_kept else [])
    notify: dict[str, object] | None = None
    if notify_program is not None:
        member = f"{NOTIFY_DIR}/{notify_program.name}"
        members[member] = notify_program.read_bytes()
        modes[member] = 0o755
        notify = {"member": member, "args": notify_args}

    config_text, dropped = _export_config(
        text, data, notify_program.name if notify_program else None, notify_args
    )
    members[CONFIG_MEMBER] = config_text.encode("utf-8")

    agents: set[str] = set()
    roles_dir = paths.config_dir() / "roles"
    if roles_dir.is_dir():
        for role in sorted(roles_dir.glob("*.toml")):
            if role.is_file() and _SAFE_NAME.fullmatch(role.stem):
                members[f"{ROLES_DIR}/{role.name}"] = role.read_bytes()
                agent = _role_agent(role)
                if agent:
                    agents.add(agent)
    for repository in config.repositories:
        for role_name in repository.roles:
            role = repository.path / ".chargehand" / "roles" / f"{role_name}.toml"
            agent = _role_agent(role) if role.is_file() else None
            if agent:
                agents.add(agent)
    for agent in sorted(agents):
        source = claude.user_agent_file(agent)
        if source.is_file():
            members[f"{AGENTS_DIR}/{agent}.md"] = source.read_bytes()

    statusline = _statusline_entry()
    if statusline is not None:
        members[STATUSLINE_MEMBER] = (
            json.dumps(claude.status_line_settings(statusline), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    rows = _board_rows(
        (r.name for r in config.repositories), ledger_path or paths.ledger_file()
    )
    if rows:
        members[BOARD_MEMBER] = (json.dumps({"rows": rows}, indent=2) + "\n").encode("utf-8")

    _check_limits(members)
    _check_secrets(members, serve.read_token(config.serve.token_file))

    created = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    manifest = {
        "format": FORMAT_VERSION,
        "chargehand": __version__,
        "created_at": created.isoformat(timespec="seconds"),
        "members": sorted(members),
        "notify": notify,
        "programs_kept": kept,
        "dropped": [d.as_dict() for d in dropped],
    }
    archive = json.dumps(manifest, indent=2) + "\n"
    _check_secrets({MANIFEST: archive.encode("utf-8")}, None)

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(_zip_info(MANIFEST, created), archive)
            for name in sorted(members):
                zipped.writestr(_zip_info(name, created, modes.get(name, 0o644)), members[name])
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Exported(
        target, sorted(members), dropped, len(rows), str(notify["member"]) if notify else None,
        [
            f"{k['program']} ({k['key']}) is not copied because {k['reason']}; the "
            "configuration still names it, so install it on the other machine"
            for k in kept
        ],
    )


def _zip_info(name: str, created: datetime, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=created.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


# ----- import --------------------------------------------------------------------


@dataclass
class Archive:
    manifest: dict[str, object]
    members: dict[str, bytes]

    @property
    def notify(self) -> tuple[str, list[str]] | None:
        notify = self.manifest.get("notify")
        if notify is None:
            return None
        return str(notify["member"]), [str(a) for a in notify["args"]]  # type: ignore[index]

    @property
    def dropped(self) -> list[Dropped]:
        return [
            Dropped(str(d["where"]), tuple(str(p) for p in d["path"]))  # type: ignore[index]
            for d in self.manifest.get("dropped", [])  # type: ignore[union-attr]
        ]


def read_archive(source: Path) -> Archive:
    """The members of the archive *source*, after every check that needs no answer."""
    try:
        zipped = zipfile.ZipFile(source)
    except FileNotFoundError as exc:
        raise ChargehandError(f"{source} does not exist") from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise Refused(f"{source} is not an archive chargehand wrote: {exc}") from exc
    with zipped:
        infos = zipped.infolist()
        if len(infos) > MAX_MEMBERS:
            raise Refused(f"{source} holds {len(infos)} files; an export holds at most {MAX_MEMBERS}")
        members: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            shown = sanitize.one_line(name, limit=80)
            if not _MEMBER.fullmatch(name):
                raise Refused(f"{source}: refusing {shown}, which is not a file an export writes")
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in (0, stat.S_IFREG):
                raise Refused(f"{source}: refusing {shown}, which is not a regular file")
            if info.file_size > MAX_MEMBER_BYTES:
                raise Refused(f"{source}: {shown} is larger than {MAX_MEMBER_BYTES} bytes")
            if name in members:
                raise Refused(f"{source}: {shown} is in it twice")
            members[name] = zipped.read(info)
    if MANIFEST not in members:
        raise Refused(f"{source} has no {MANIFEST}; it is not an archive chargehand wrote")
    try:
        manifest = json.loads(members.pop(MANIFEST))
    except ValueError as exc:
        raise Refused(f"{source}: {MANIFEST} is not JSON") from exc
    if not isinstance(manifest, dict):
        raise Refused(f"{source}: {MANIFEST} is not an object")
    version = manifest.get("format")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise Refused(f"{source}: {MANIFEST} has no format version")
    if version > FORMAT_VERSION:
        raise Refused(
            f"{source} has format version {version}, and this chargehand reads up to "
            f"{FORMAT_VERSION}; import it with a newer chargehand"
        )
    _check_manifest(source, manifest, members)
    return Archive(manifest, members)


def _check_manifest(source: Path, manifest: Mapping[str, object], members: Mapping[str, bytes]) -> None:
    listed = manifest.get("members")
    if not isinstance(listed, list) or sorted(map(str, listed)) != sorted(members):
        raise Refused(f"{source}: the files in it are not the ones its manifest lists")
    if CONFIG_MEMBER not in members:
        raise Refused(f"{source} has no {CONFIG_MEMBER}")
    notify = manifest.get("notify")
    if notify is not None:
        if (
            not isinstance(notify, Mapping)
            or notify.get("member") not in members
            or not str(notify.get("member")).startswith(NOTIFY_DIR + "/")
            or not isinstance(notify.get("args"), list)
            or not all(isinstance(a, str) for a in notify["args"])  # type: ignore[union-attr]
        ):
            raise Refused(f"{source}: the notify entry of its manifest is not valid")
    kept = manifest.get("programs_kept", [])
    if not isinstance(kept, list) or not all(
        isinstance(k, Mapping) and all(isinstance(k.get(f), str) for f in ("key", "program", "reason"))
        for k in kept
    ):
        raise Refused(f"{source}: the programs_kept entry of its manifest is not valid")
    dropped = manifest.get("dropped", [])
    if not isinstance(dropped, list) or not all(
        isinstance(d, Mapping)
        and isinstance(d.get("where"), str)
        and isinstance(d.get("path"), list)
        and d["path"]
        and all(isinstance(p, str) for p in d["path"])
        for d in dropped
    ):
        raise Refused(f"{source}: the dropped entry of its manifest is not valid")


@dataclass
class Imported:
    written: list[Path]
    board_rows: int
    token_file: Path
    token_created: bool
    steps: list[dict[str, object]]
    steps_file: Path


# Asked for each repository path and the worktree root: a question and its default, and
# the answer as typed. An empty answer takes the default.
Ask = Callable[[str, str], str]


def _answer(raw: str | None, default: str) -> str:
    value = (raw or "").strip()
    if not value:
        # The default is the archive's own configuration text, which the configuration
        # reads with `~` and `$VAR` expanded; it stays as it is.
        return default
    path = Path(os.path.expanduser(os.path.expandvars(value)))
    if not path.is_absolute():
        path = Path.cwd() / path
    return _portable(os.path.normpath(path))


@dataclass
class _ImportEdit:
    names: dict[tuple[str, int], str]
    repository_paths: Mapping[str, str]
    worktree_root: str
    notify_command: str | None
    saw_worktree_root: bool = False

    def __call__(self, place: _Place, key: str, value: object) -> object:
        if place.table == () and key == "worktree_root":
            self.saw_worktree_root = True
            return self.worktree_root
        if len(place.table) == 1 and place.table[0] in _REPOSITORY_ARRAYS and key in ("path", "repo"):
            name = self.names.get((place.table[0], place.index))
            if name in self.repository_paths:
                return self.repository_paths[name]
        if place.table == ("notify",) and key == "command" and self.notify_command is not None:
            return self.notify_command
        return _KEEP


def _board_import_rows(archive: Archive, names: Iterable[str]) -> list[tuple[str, str, str, str]]:
    """The board rows of the archive for configured repositories, cleaned as the board cleans them."""
    raw = archive.members.get(BOARD_MEMBER)
    if raw is None:
        return []
    try:
        rows = json.loads(raw).get("rows", [])
    except (ValueError, AttributeError) as exc:
        raise Refused(f"{BOARD_MEMBER} is not valid JSON") from exc
    wanted = set(names)
    allowed = ledger_mod.BOARD_FIELDS[ledger_mod.BOARD_REPOSITORY]
    cleaned: list[tuple[str, str, str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("kind") != ledger_mod.BOARD_REPOSITORY:
            continue
        key, name, value = row.get("key"), row.get("field"), row.get("value")
        if key not in wanted or name not in allowed or not isinstance(value, str):
            continue
        if name == "name":
            value = sanitize.one_line(value, limit=board_mod.NAME_LIMIT)
        elif name == "icon":
            value = value if board_mod.icon_glyph(value) else ""
        else:
            value = "1" if value == "1" else ""
        if value:
            cleaned.append((ledger_mod.BOARD_REPOSITORY, str(key), str(name), value))
    return cleaned


def _steps(archive: Archive, config: MachineConfig, notify_path: Path | None) -> list[dict[str, object]]:
    """What the import cannot do, for the operator and for `doctor`."""
    steps: list[dict[str, object]] = []
    for dropped in archive.dropped:
        steps.append({
            "name": "imported: config key",
            "kind": "config_key",
            "path": list(dropped.path),
            "detail": f"set {dropped.where} again; export leaves out keys that can carry a credential",
        })
    signed_in = False
    for repository in config.repositories:
        options = repository.tracker.options
        if repository.tracker.type == "linear":
            env = options.get("api_key_env", "CHARGEHAND_LINEAR_API_KEY")
            service = options.get("keychain_service", "chargehand-linear")
            steps.append({
                "name": "imported: tracker key",
                "kind": "note",
                "detail": f"repository {repository.name}: give the Linear tracker its API key, "
                          f"in ${env}, an api_key_command, or the login keychain under "
                          f"service '{service}'; `doctor` without --no-tracker checks it",
            })
        elif repository.tracker.type == "github" and not signed_in:
            signed_in = True
            steps.append({
                "name": "imported: tracker key",
                "kind": "note",
                "detail": "sign in to GitHub with `gh auth login`; `doctor` without "
                          "--no-tracker checks it",
            })
    raw = archive.members.get(STATUSLINE_MEMBER)
    if raw is not None:
        try:
            entry = claude.status_line_entry(json.loads(raw))
        except ValueError:
            entry = None
        if claude.runs_statusline_wrapper(entry):
            settings = claude.user_settings_file()
            wanted = json.dumps(claude.status_line_settings(entry), sort_keys=True)
            steps.append({
                "name": "imported: status line",
                "kind": claude.STATUS_LINE_STEP,
                "runtime": claude.ClaudeRuntime.name,
                "settings": str(settings),
                "detail": f"add the status line wrapper to {settings}, "
                          "adjusted to this machine; chargehand does not edit Claude Code's "
                          f"settings: {wanted}",
            })
    if sys.platform == "darwin":
        steps.append({
            "name": "imported: launchd",
            "kind": "launchd",
            "detail": "run `chargehand install --load` to start the daemon at login",
        })
    else:
        steps.append({
            "name": "imported: daemon",
            "kind": "note",
            "detail": "run `chargehand serve` under your own service manager",
        })
    if notify_path is not None:
        steps.append({
            "name": "imported: notify",
            "kind": "note",
            "detail": f"{notify_path} came from the other machine: check that its method "
                      "reaches you here, and give it any credential it reads",
        })
    for kept in archive.manifest.get("programs_kept", []):  # type: ignore[union-attr]
        steps.append({
            "name": "imported: program",
            "kind": "program",
            "program": kept["program"],
            "detail": f"install {kept['program']}, which {kept['key']} names; export did not "
                      f"copy it because {kept['reason']}",
        })
    return steps


def import_setup(
    source: Path,
    *,
    config_path: Path,
    ask: Ask,
    repository_paths: Mapping[str, str] | None = None,
    worktree_root: str | None = None,
    force: bool = False,
    ledger_path: Path | None = None,
    now: float | None = None,
) -> Imported:
    """Unpack the archive *source* on this machine.

    Asks *ask* for each repository path and the worktree root that *repository_paths* and
    *worktree_root* do not give. Writes nothing when a file it would write exists, unless
    *force*.
    """
    archive = read_archive(source)
    text = archive.members[CONFIG_MEMBER].decode("utf-8")
    try:
        exported = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"{source}: its {CONFIG_MEMBER} does not parse: {exc}") from exc
    try:
        MachineConfig.parse(exported)
    except ConfigError as exc:
        raise Refused(f"{source}: its {CONFIG_MEMBER} is not valid: {exc}") from exc
    names: dict[tuple[str, int], str] = {}
    defaults: dict[str, str] = {}
    for array in _REPOSITORY_ARRAYS:
        entries = exported.get(array, [])
        for index, name in enumerate(_names(exported, array)):
            names[(array, index)] = name
            entry = entries[index]
            defaults[name] = str(entry.get("path", entry.get("repo", "")))
    given = dict(repository_paths or {})
    unknown = sorted(set(given) - set(defaults))
    if unknown:
        raise ChargehandError(
            f"the archive has no repository named {', '.join(unknown)}; it has "
            f"{', '.join(defaults) or 'none'}"
        )

    config_dir = config_path.parent
    targets: dict[Path, tuple[bytes, int]] = {}
    notify = archive.notify
    notify_path: Path | None = None
    if notify is not None:
        notify_path = config_dir / notify[0].split("/", 1)[1]
        targets[notify_path] = (archive.members[notify[0]], 0o755)
    for member, content in archive.members.items():
        directory, _, name = member.partition("/")
        if directory == ROLES_DIR:
            targets[paths.config_dir() / "roles" / name] = (content, 0o644)
        elif directory == AGENTS_DIR:
            targets[claude.user_agent_file(name.removesuffix(".md"))] = (content, 0o644)
    steps_file = config_dir / install.IMPORT_STEPS_FILE
    planned = [config_path, steps_file, *targets]

    ledger_file = ledger_path or paths.ledger_file()
    board_names = list(defaults)
    rows = _board_import_rows(archive, board_names)
    conflicts = [str(p) for p in planned if p.exists() or p.is_symlink()]
    if rows and ledger_file.exists():
        with ledger_mod.open_ledger(ledger_file) as ledger:
            present = {(r["kind"], r["key"], r["field"]) for r in ledger.board_state()}
        conflicts.extend(
            f"board setting {field} of {key}" for kind, key, field, _ in rows if (kind, key, field) in present
        )
    if conflicts and not force:
        raise Refused(
            "import would overwrite " + ", ".join(conflicts) + "; pass --force to overwrite"
        )

    chosen = {
        name: _answer(
            given[name] if name in given else ask(f"Path of repository {name}", default), default
        )
        for name, default in defaults.items()
    }
    root_default = str(exported.get("worktree_root", DEFAULT_WORKTREE_ROOT))
    root = _answer(
        worktree_root if worktree_root is not None else ask("Worktree root", root_default),
        root_default,
    )
    notify_command = (
        " ".join([_program_word(notify_path), *map(shlex.quote, notify[1])])
        if notify and notify_path
        else None
    )
    edit = _ImportEdit(names, chosen, root, notify_command)
    lines = _rewrite(text, edit)
    if not edit.saw_worktree_root:
        lines = _insert_at_root(lines, f"worktree_root = {_toml(root)}")
    new_text = "\n".join(lines) + "\n"
    try:
        config = MachineConfig.parse(tomllib.loads(new_text), source=config_path)
    except (tomllib.TOMLDecodeError, ConfigError) as exc:
        raise Refused(f"the imported configuration is not valid: {exc}") from exc

    steps = _steps(archive, config, notify_path)
    created = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    record = {
        "imported_at": created.isoformat(timespec="seconds"),
        "from_chargehand": str(archive.manifest.get("chargehand", "")),
        "steps": steps,
    }

    written: list[Path] = []
    _write_atomically(config_path, new_text.encode("utf-8"))
    written.append(config_path)
    for target, (content, mode) in sorted(targets.items()):
        _write_atomically(target, content, mode)
        written.append(target)
    _write_atomically(steps_file, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    written.append(steps_file)

    if rows:
        with ledger_mod.open_ledger(ledger_file) as ledger, ledger.transaction():
            for kind, key, name, value in rows:
                ledger.set_board_state(kind, key, name, value, now=now)

    token_file = config.serve.token_file
    existed = token_file.exists() or token_file.is_symlink()
    serve.ensure_token(token_file)
    return Imported(written, len(rows), token_file, not existed, steps, steps_file)


__all__ = [
    "FORMAT_VERSION",
    "Archive",
    "Exported",
    "Imported",
    "Refused",
    "export_setup",
    "import_setup",
    "read_archive",
]
