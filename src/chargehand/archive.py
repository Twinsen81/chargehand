"""Archives: what a finished worker leaves, in one zip file, written off the tick.

An archive holds the worker's worktree (and its item checkout, when it has one), its
session transcript as text (and the transcript of each earlier session that a refresh
replaced), its ledger row and its sessions as JSON, and its handoff note when one exists.
It is written under the state directory as
``archive/<repository>/<role>-<item>-<date>-<time>-<id>.zip``, with mode 0600: a transcript
can hold anything the session read.

The tick records an archive as a job (see :data:`chargehand.ledger.JOB_STATES`), and
:class:`JobRunner` writes it on a thread of the daemon, so a large worktree blocks
neither the tick nor the board. The file is written as ``<name>.zip.part``, read back in
full to check every member's CRC, synced, and only then renamed to its final name. A stop
of the daemon, or a crash, leaves at most a ``.part`` file, which the next start deletes
before it runs the job again. Nothing here removes anything else: once the archive is in
place the job is ``written``, and the tick removes the worktree and the session through
its garbage collection, as the one writer of git and of the agent's sessions.

Restoring is a manual step: unzip the archive, add a worktree on the pushed branch with
``git worktree add``, and copy the files of ``worktree/`` into it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from chargehand import ledger as ledger_mod
from chargehand import paths, sanitize
from chargehand.ledger import Job, Ledger, Worker

log = logging.getLogger(__name__)

ARCHIVE_DIR = "archive"
PART_SUFFIX = ".part"
# The members besides the files of the trees.
WORKER_MEMBER = "worker.json"
SESSIONS_MEMBER = "sessions.json"
TRANSCRIPT_MEMBER = "transcript.txt"
# The transcript of each session that a refresh replaced, oldest first, from 1.
EARLIER_TRANSCRIPT_MEMBER = "transcript-{n}.txt"
HANDOFF_MEMBER = "handoff.md"
# The member prefix of the worktree, and of the item checkout of a `checkout = "item"` role.
WORKTREE_TREE = "worktree"
ITEM_CHECKOUT_TREE = "item-checkout"

CHUNK_BYTES = 1024 * 1024
# A file this large gets a zip64 entry from the start: zipfile cannot add one later.
ZIP64_FROM_BYTES = 1024 * 1024 * 1024
# How often a running job writes its progress to the ledger, at most.
PROGRESS_SECS = 0.5
# How often the job thread looks for a queued job.
JOB_POLL_SECS = 1.0
# How long a stop of the daemon waits for the job thread to leave its job.
JOB_STOP_SECS = 5.0

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ArchiveError(Exception):
    """The archive could not be written or did not pass its check."""


class Interrupted(Exception):
    """The daemon stops; the job starts again on its next start."""


def archive_root() -> Path:
    return paths.state_dir() / ARCHIVE_DIR


def _name_part(value: str) -> str:
    return _UNSAFE.sub("_", value).strip("._")[:64] or "_"


def archive_path(worker: Worker, now: float, root: Path | None = None) -> Path:
    """Where *worker*'s archive goes. The time and the worker id keep two names apart."""
    date = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(now))
    parts = [_name_part(worker.role)]
    if worker.item:
        parts.append(_name_part(worker.item))
    parts += [date, str(worker.id)]
    return (root or archive_root()) / _name_part(worker.repository) / f"{'-'.join(parts)}.zip"


def part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + PART_SUFFIX)


def records(
    worker: Worker,
    sessions: Sequence[ledger_mod.SessionRecord],
    *,
    transcript: str | None,
    handoff: str | None = None,
    earlier: Sequence[str | None] = (),
) -> dict[str, str]:
    """The members that are not files of a tree: the rows as JSON, and the texts.

    *earlier* are the transcripts of the sessions that a refresh replaced, oldest first.
    """
    members = {
        WORKER_MEMBER: json.dumps(dataclasses.asdict(worker), indent=2, sort_keys=True) + "\n",
        SESSIONS_MEMBER: json.dumps(
            [dataclasses.asdict(session) for session in sessions], indent=2, sort_keys=True
        ) + "\n",
    }
    if transcript:
        members[TRANSCRIPT_MEMBER] = transcript
    for n, text in enumerate(earlier, start=1):
        if text:
            members[EARLIER_TRANSCRIPT_MEMBER.format(n=n)] = text
    if handoff:
        members[HANDOFF_MEMBER] = handoff
    return members


@dataclass(frozen=True)
class Summary:
    files: int
    bytes: int


@dataclass(frozen=True)
class _Entry:
    name: str
    path: Path
    info: os.stat_result


def _walk(trees: Sequence[tuple[str, Path]]) -> Iterator[_Entry]:
    """Every file, symbolic link and empty directory of the trees, without following a link.

    A link is stored as a link, so a link to a file outside the worktree does not bring
    that file into the archive. Sockets, pipes and devices are left out.
    """
    for prefix, root in trees:
        if not root.is_dir() or root.is_symlink():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            relative = base.relative_to(root)
            names = sorted(dirnames) + sorted(filenames)
            descend = []
            for name in names:
                path = base / name
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    descend.append(name)
                    continue
                if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    yield _Entry(f"{prefix}/{(relative / name).as_posix()}", path, info)
            dirnames[:] = sorted(descend)
            if not names:
                inner = "" if relative == Path(".") else f"{relative.as_posix()}/"
                yield _Entry(f"{prefix}/{inner}", base, os.lstat(base))


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def write_archive(
    dest: Path,
    *,
    members: Mapping[str, str],
    trees: Sequence[tuple[str, Path]],
    progress: Callable[[str], None] = lambda text: None,
    stopping: Callable[[], bool] = lambda: False,
) -> Summary:
    """Write the zip at *dest*: *members* as text files, then every entry of *trees*.

    The zip is complete at *dest* or not there at all. ArchiveError when it does not pass
    its check, Interrupted when *stopping* answers true; the ``.part`` file is gone then.
    """
    dest.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    part = part_path(dest)
    entries = list(_walk(trees))
    total = sum(entry.info.st_size for entry in entries if stat.S_ISREG(entry.info.st_mode))
    done, last = 0, 0.0

    def report(force: bool = False) -> None:
        nonlocal last
        now = time.monotonic()
        if force or now - last >= PROGRESS_SECS:
            last = now
            share = f"{min(100, 100 * done // total)}%" if total else "100%"
            progress(f"zipping: {share} of {_human(total)}, {len(entries)} entries")

    def check() -> None:
        if stopping():
            raise Interrupted()

    def advance(copied: int) -> None:
        nonlocal done
        done += copied
        report()

    try:
        handle = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "wb") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for name, text in members.items():
                    zf.writestr(_text_info(name), text.encode("utf-8", "replace"))
                report(force=True)
                for entry in entries:
                    check()
                    mode = entry.info.st_mode
                    if stat.S_ISLNK(mode):
                        info = _info(entry.name, entry.info)
                        zf.writestr(info, os.readlink(entry.path).encode("utf-8", "surrogateescape"))
                    elif stat.S_ISDIR(mode):
                        zf.writestr(_info(entry.name, entry.info), b"")
                    else:
                        _copy_file(zf, entry, check, advance)
                    report()
            raw.flush()
            os.fsync(raw.fileno())
        progress(f"checking the archive: {_human(part.stat().st_size)}")
        _verify(part, set(members), check)
        os.replace(part, dest)
        _fsync_dir(dest.parent)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return Summary(files=len(entries), bytes=dest.stat().st_size)


def _copy_file(
    zf: zipfile.ZipFile, entry: _Entry, check: Callable[[], None], advance: Callable[[int], None]
) -> None:
    info = _info(entry.name, entry.info)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.file_size = entry.info.st_size
    with entry.path.open("rb") as source, zf.open(
        info, "w", force_zip64=entry.info.st_size >= ZIP64_FROM_BYTES
    ) as target:
        while True:
            check()
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            target.write(chunk)
            advance(len(chunk))


def _info(name: str, info: os.stat_result) -> zipfile.ZipInfo:
    stamp = time.localtime(max(info.st_mtime, 315532800))[:6]
    entry = zipfile.ZipInfo(name, date_time=stamp)
    entry.create_system = 3
    entry.external_attr = (info.st_mode & 0xFFFF) << 16
    if stat.S_ISDIR(info.st_mode):
        entry.external_attr |= 0x10
    return entry


def _text_info(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o600) << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    return entry


def _verify(part: Path, required: set[str], check: Callable[[], None]) -> None:
    """Read every member back, as ``ZipFile.testzip`` does, but in chunks a stop can end."""
    try:
        with zipfile.ZipFile(part) as zf:
            missing = required - set(zf.namelist())
            if missing:
                raise ArchiveError(f"the archive lacks {', '.join(sorted(missing))}")
            for info in zf.infolist():
                with zf.open(info) as member:
                    while member.read(CHUNK_BYTES):
                        check()
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveError(f"the archive failed its check: {exc}") from None


def _fsync_dir(directory: Path) -> None:
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def remove_stale_parts(root: Path | None = None) -> int:
    """Delete every ``.part`` file under the archive directory. Only at the start of the
    job thread, when no job runs."""
    base = root or archive_root()
    removed = 0
    if not base.is_dir():
        return 0
    for part in base.rglob(f"*.zip{PART_SUFFIX}"):
        try:
            part.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not delete the unfinished archive %s: %s", part, exc)
    return removed


# ----- the job thread ----------------------------------------------------------------


Transcript = Callable[[Ledger, Worker], "str | None"]


class JobRunner:
    """Runs the queued jobs, one at a time, on a thread of its own.

    It writes only the archive file and the job's row. A job that a stop or a crash
    interrupted is ``running`` in the ledger; :meth:`recover` puts it back in the queue
    and deletes its ``.part`` file, so it runs again from the start.
    """

    def __init__(
        self,
        ledger_path: Path,
        *,
        transcript: Transcript = lambda ledger, worker: None,
        written: Callable[[], None] = lambda: None,
        poll_secs: float = JOB_POLL_SECS,
        root: Path | None = None,
    ) -> None:
        self.ledger_path = ledger_path
        self._transcript = transcript
        # Called when an archive is in place, so the daemon ticks soon and removes the rest.
        self._written = written
        self._poll_secs = poll_secs
        self._root = root
        self.stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="chargehand-jobs", daemon=True)
        self._thread.start()

    def close(self, timeout: float = JOB_STOP_SECS) -> None:
        self.stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def recover(self) -> None:
        with Ledger(self.ledger_path) as ledger:
            for job in ledger.jobs_in_state([ledger_mod.JOB_RUNNING]):
                ledger.update_job(
                    job, expect=ledger_mod.JOB_RUNNING, state=ledger_mod.JOB_QUEUED,
                    progress="interrupted by a stop of the daemon; it starts again",
                )
                log.info("job %s (%s %s) was interrupted; it starts again", job.id, job.kind, job.target)
        removed = remove_stale_parts(self._root)
        if removed:
            log.info("deleted %s unfinished archive file(s)", removed)

    def _loop(self) -> None:
        with Ledger(self.ledger_path) as ledger:
            while not self.stopping.is_set():
                try:
                    ran = self.run_queued(ledger)
                except Exception:
                    log.exception("the job thread failed; it tries again")
                    ran = 0
                if not ran:
                    self.stopping.wait(self._poll_secs)

    def run_queued(self, ledger: Ledger) -> int:
        """Run every queued job once. Returns how many ran."""
        ran = 0
        for job in ledger.jobs_in_state([ledger_mod.JOB_QUEUED]):
            if self.stopping.is_set():
                break
            self.run_job(ledger, job)
            ran += 1
        return ran

    def run_job(self, ledger: Ledger, job: Job) -> None:
        if job.kind != ledger_mod.JOB_ARCHIVE:
            ledger.update_job(job, expect=ledger_mod.JOB_QUEUED, state=ledger_mod.JOB_FAILED,
                              error=f"unknown job kind {sanitize.one_line(job.kind, limit=40)}")
            return
        worker = ledger.get(int(job.target)) if job.target.isdigit() else None
        running = ledger.update_job(
            job, expect=ledger_mod.JOB_QUEUED, state=ledger_mod.JOB_RUNNING,
            started_at=time.time(), progress="collecting the files",
        )
        if running is None:
            return
        if worker is None or not job.result_path:
            ledger.update_job(running, state=ledger_mod.JOB_FAILED,
                              error=f"no worker {job.target}" if worker is None else "no archive path")
            return
        dest = Path(job.result_path)
        trees = [(str(name), Path(path)) for name, path in job.args.get("trees", [])]
        log.info("%s: writing its archive (job %s)", worker.handle, job.id)

        def progress(text: str) -> None:
            ledger.update_job(running, expect=ledger_mod.JOB_RUNNING, progress=text)

        try:
            transcript = self._transcript(ledger, worker)
            sessions = ledger.sessions_of(worker)
            # Each session that a refresh replaced, read as the worker's own was.
            earlier = [
                self._transcript(ledger, dataclasses.replace(
                    worker, session_id=record.session_id, transcript_path=record.transcript_path,
                ))
                for record in sessions
                if record.kind != ledger_mod.SESSION_WAKE and record.session_id
                and record.session_id != worker.session_id
            ]
            members = records(
                worker, sessions, transcript=transcript, handoff=worker.handoff_note,
                earlier=earlier,
            )
            summary = write_archive(
                dest, members=members, trees=trees, progress=progress,
                stopping=self.stopping.is_set,
            )
        except Interrupted:
            ledger.update_job(running, expect=ledger_mod.JOB_RUNNING, state=ledger_mod.JOB_QUEUED,
                              progress="interrupted by a stop of the daemon; it starts again")
            log.info("%s: archive interrupted by the stop; it starts again", worker.handle)
            return
        except (OSError, ArchiveError, zipfile.LargeZipFile, ValueError, RuntimeError) as exc:
            error = sanitize.one_line(f"could not write the archive: {exc}", limit=300)
            ledger.update_job(running, expect=ledger_mod.JOB_RUNNING, state=ledger_mod.JOB_FAILED,
                              error=error, progress=None)
            log.error("%s: %s", worker.handle, error)
            return
        except Exception as exc:
            # Anything else, such as a ledger that stayed locked: a job left `running` would
            # hold its worker until the next start of the daemon.
            log.exception("%s: the archive job %s failed", worker.handle, job.id)
            error = sanitize.one_line(
                f"the archive job failed: {type(exc).__name__}: {exc}", limit=300
            )
            ledger.update_job(running, expect=ledger_mod.JOB_RUNNING, state=ledger_mod.JOB_FAILED,
                              error=error, progress=None)
            return
        ledger.update_job(
            running, expect=ledger_mod.JOB_RUNNING, state=ledger_mod.JOB_WRITTEN,
            progress=(f"archive written: {summary.files} entries, {_human(summary.bytes)}; "
                      f"the next tick removes the worktree and the session"),
        )
        log.info("%s: archive written to %s", worker.handle, dest)
        self._written()


__all__ = [
    "ARCHIVE_DIR",
    "ArchiveError",
    "Interrupted",
    "JobRunner",
    "Summary",
    "archive_path",
    "archive_root",
    "part_path",
    "records",
    "remove_stale_parts",
    "write_archive",
]
