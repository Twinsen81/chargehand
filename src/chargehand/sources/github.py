"""GitHub through the ``gh`` CLI: an event source, and a tracker for GitHub Issues.

This is the only module that knows ``gh``. gh holds the credentials, so chargehand
never handles a token.

The source polls in two parts. The notifications endpoint of the signed-in account
gives review requests, comments, mentions and CI activity on any repository, and asks
with ``If-Modified-Since``, so an unchanged list costs nothing against the rate limit;
it is asked no more often than its ``X-Poll-Interval`` header allows. Then, for each pull
request a live worker subscribes to, the source reads the review comments, the reviews,
the issue comments, and the commit statuses and check runs on the head commit, which is
where external CI systems report.

Comment and review bodies are read only to find the authorship marker, and they do not
leave this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from chargehand import sanitize
from chargehand.config import Repository
from chargehand.errors import AmbiguousWrite, ConfigError, TrackerAuthError, TrackerError
from chargehand.sources.base import AUTHORSHIP_MARKER, Event, Source, WorkItem
from chargehand.trackers.base import Issue, Status, Tracker

log = logging.getLogger(__name__)

DEFAULT_GH = "gh"
DEFAULT_TIMEOUT_SECS = 30.0
# What GitHub asks for when a response does not say.
DEFAULT_POLL_INTERVAL_SECS = 60
# Timestamps come from GitHub's clock and are compared with this machine's. The overlap
# costs nothing, because the router drops what it has seen before.
CLOCK_SKEW_SECS = 300
PAGE_SIZE = 100

# A notification's reason, as the event kind it becomes. The other reasons (assign,
# author, state_change, subscribed, ...) say nothing a worker acts on.
NOTIFICATION_KINDS = {
    "review_requested": "review_requested",
    "comment": "comment",
    "mention": "mention",
    "team_mention": "mention",
    "ci_activity": "ci_status",
}

# A user or organization name is letters, digits and single hyphens. Enterprise managed
# users add an underscore and a short code, so underscore is accepted too.
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_REF = re.compile(r"([^/#\s]+)/([^/#\s]+)#([0-9]{1,10})")
_PLAIN_OWNER = re.compile(r"[a-z0-9-]+")
_API_PATH = re.compile(r"/repos/([^/]+)/([^/]+)/(pulls|issues)/([0-9]{1,10})$")


_UNKNOWN = object()


class NotFound(TrackerError):
    """gh answered 404 (or 410): the object is gone or this account cannot see it."""


# ----- names -------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def make_ref(owner: object, repo: object, number: object) -> Ref | None:
    """A reference from GitHub's own fields, or None when any part is not a plain name.

    The parts end up in a worktree path, a branch name and argv, so anything that GitHub
    would not itself accept as a name is skipped rather than repaired.
    """
    if not isinstance(owner, str) or not isinstance(repo, str):
        return None
    if isinstance(number, bool):
        return None
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if not isinstance(number, int) or number <= 0:
        return None
    if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) or repo in (".", ".."):
        return None
    return Ref(owner, repo, number)


def parse_ref(value: str) -> Ref | None:
    """``owner/repo#number``, as stored in a worker's ``item_id``."""
    match = _REF.fullmatch(value or "")
    return make_ref(match.group(1), match.group(2), match.group(3)) if match else None


def item_key(ref: Ref) -> str:
    """A path-safe name for *ref*, unique across repositories, at most 64 characters.

    GitHub names are case-insensitive, so the key is lower case. The plain form is
    ``owner_repo_number``; it cannot be split two ways, because an owner has no underscore
    and the number is the trailing digits. Any other name gets a digest of the reference
    in place of the parts that did not fit, and ends in ``-`` and eight hex digits, which
    no plain key does.
    """
    owner, repo = ref.owner.lower(), ref.repo.lower()
    plain = f"{owner}_{repo}_{ref.number}"
    if (
        _PLAIN_OWNER.fullmatch(owner)
        and ".." not in plain
        and not plain.endswith(".lock")
        and sanitize.is_safe_identifier(plain)
    ):
        return plain
    digest = hashlib.sha256(f"{owner}/{repo}#{ref.number}".encode()).hexdigest()[:8]
    tail = f"_{ref.number}-{digest}"
    stem = re.sub(r"[^a-z0-9_-]", "-", f"{owner}_{repo}")[: 64 - len(tail)]
    return f"{stem}{tail}"


def html_url(ref: Ref, *, pull: bool) -> str:
    return f"https://github.com/{ref.full_name}/{'pull' if pull else 'issues'}/{ref.number}"


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login(user: object) -> str:
    if isinstance(user, Mapping):
        return str(user.get("login") or "")
    return ""


def _marked(body: object) -> bool:
    return isinstance(body, str) and AUTHORSHIP_MARKER in body


# ----- the gh command line -----------------------------------------------------


def _json_values(text: str) -> list[Any]:
    """Every JSON value in *text*: ``gh api --paginate`` prints one per page, back to back."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index, end = 0, len(text)
    while True:
        while index < end and text[index].isspace():
            index += 1
        if index >= end:
            return values
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError as exc:
            raise TrackerError(f"github: gh printed something that is not JSON: {exc}") from exc
        values.append(value)


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


def _split_response(text: str) -> Response | None:
    """Status line, headers and body of ``gh api -i`` output; None without a status line."""
    head, separator, body = text.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    match = re.match(r"HTTP/[0-9.]+ ([0-9]{3})", lines[0]) if lines else None
    if not match:
        return None
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return Response(int(match.group(1)), headers, body if separator else "")


class Gh:
    """Runs ``gh``. Never through a shell, never with a terminal to prompt on."""

    def __init__(self, binary: str, timeout: float) -> None:
        self.binary = binary
        self.timeout = timeout

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
        }
        try:
            return subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError as exc:
            raise TrackerError(
                f"github: {self.binary} not found; install the GitHub CLI or set gh_bin"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TrackerError(f"github: gh did not answer within {self.timeout:.0f}s") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise TrackerError(f"github: could not run gh: {exc}") from exc

    def check(self, result: subprocess.CompletedProcess[str], what: str) -> None:
        if result.returncode == 0:
            return
        detail = sanitize.one_line(result.stderr or result.stdout or "", limit=300)
        if result.returncode == 4:
            raise TrackerAuthError(f"github: gh is not signed in ({detail}); run `gh auth login`")
        if re.search(r"HTTP (404|410)\b", detail):
            raise NotFound(f"github: {what}: {detail}")
        raise TrackerError(f"github: {what}: gh exited {result.returncode}: {detail}")

    def api(self, path: str, *, paginate: bool = False) -> list[Any]:
        """The JSON values a GET of *path* returns, one per page."""
        args = ["api", "-X", "GET", path]
        if paginate:
            args.insert(1, "--paginate")
        result = self.run(args)
        self.check(result, path.split("?")[0])
        return _json_values(result.stdout)

    def api_list(self, path: str, *, key: str | None = None) -> list[Mapping[str, Any]]:
        """Every element of a paginated list, or of the list under *key* on each page."""
        entries: list[Mapping[str, Any]] = []
        for page in self.api(path, paginate=True):
            if key is not None:
                page = page.get(key) if isinstance(page, Mapping) else None
            if not isinstance(page, list):
                raise TrackerError(f"github: {path.split('?')[0]}: expected a list")
            entries.extend(entry for entry in page if isinstance(entry, Mapping))
        return entries

    def api_one(self, path: str) -> Mapping[str, Any]:
        values = self.api(path)
        if len(values) != 1 or not isinstance(values[0], Mapping):
            raise TrackerError(f"github: {path.split('?')[0]}: expected one object")
        return values[0]

    def conditional(self, path: str, if_modified_since: str | None) -> Response:
        """A GET that shows the response headers, for ``If-Modified-Since``.

        gh treats every status above 299 as a failure, a 304 too, so the exit code alone
        cannot tell "nothing changed" from an error: the status line decides.
        """
        args = ["api", "-i", "-X", "GET", path]
        if if_modified_since:
            args += ["-H", f"If-Modified-Since: {if_modified_since}"]
        result = self.run(args)
        response = _split_response(result.stdout)
        if response is not None and (response.status == 304 or result.returncode == 0):
            return response
        self.check(result, path.split("?")[0])
        raise TrackerError(f"github: {path.split('?')[0]}: gh printed no status line")

    def login(self) -> str:
        login = self.api_one("user").get("login")
        if not isinstance(login, str) or not login:
            raise TrackerError("github: gh api user returned no login")
        return login


def _gh_from(options: Mapping[str, object], where: str) -> Gh:
    binary = options.get("gh_bin", DEFAULT_GH)
    if not isinstance(binary, str) or not binary.strip():
        raise ConfigError(f"{where}.gh_bin: expected a non-empty string")
    timeout = options.get("timeout_secs", DEFAULT_TIMEOUT_SECS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"{where}.timeout_secs: expected a positive number")
    return Gh(os.path.expanduser(binary), float(timeout))


def _check_options(options: Mapping[str, object], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(options) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown option(s): {', '.join(unknown)}")


# ----- the source --------------------------------------------------------------


@dataclass
class Cursor:
    """What the source remembers between polls, saved as JSON by the router."""

    # The notifications list: the server's Last-Modified, and when it may be asked next.
    last_modified: str | None = None
    poll_interval: int = DEFAULT_POLL_INTERVAL_SECS
    next_poll_at: float = 0.0
    # When the previous poll ran, and, per followed item, when it was last read.
    polled_at: float | None = None
    items: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, value: str | None) -> "Cursor":
        if not value:
            return cls()
        try:
            data = json.loads(value)
        except ValueError:
            log.warning("github: unreadable cursor; starting again")
            return cls()
        if not isinstance(data, dict):
            return cls()
        items = data.get("items")
        try:
            return cls(
                last_modified=(
                    data["last_modified"] if isinstance(data.get("last_modified"), str) else None
                ),
                poll_interval=int(data.get("poll_interval") or DEFAULT_POLL_INTERVAL_SECS),
                next_poll_at=float(data.get("next_poll_at") or 0.0),
                polled_at=float(data["polled_at"]) if data.get("polled_at") is not None else None,
                items={
                    str(k): float(v)
                    for k, v in (items.items() if isinstance(items, dict) else ())
                    if isinstance(v, (int, float))
                },
            )
        except (TypeError, ValueError):
            log.warning("github: unreadable cursor; starting again")
            return cls()

    def dump(self) -> str:
        return json.dumps(
            {
                "last_modified": self.last_modified,
                "poll_interval": self.poll_interval,
                "next_poll_at": self.next_poll_at,
                "polled_at": self.polled_at,
                "items": self.items,
            },
            sort_keys=True,
        )


class GithubSource(Source):
    """``[[source]] type = "github"``: options ``gh_bin`` and ``timeout_secs``.

    Work items are pull requests and issues, ``owner/repo#number``. A start event belongs
    to the ``[[repository]]`` whose ``github`` key names its ``owner/repo``.
    """

    type = "github"
    _OPTIONS = ("gh_bin", "timeout_secs")

    def __init__(self, name: str, options: Mapping[str, object]) -> None:
        super().__init__(name, options)
        _check_options(options, self._OPTIONS, f"source {name}")
        self.gh = _gh_from(options, f"source {name}")
        self.clock: Callable[[], float] = time.time
        self._login: str | None = None
        self._teams: set[object] | None | object = _UNKNOWN

    def whoami(self) -> str:
        if self._login is None:
            self._login = self.gh.login()
        return self._login

    def poll(
        self, cursor: str | None, items: Sequence[WorkItem]
    ) -> tuple[list[Event], str | None]:
        state = Cursor.load(cursor)
        now = self.clock()
        followed = {item.key for item in items}
        events: list[Event] = []
        if now >= state.next_poll_at:
            events += self._poll_notifications(state, followed, now)
        for item in items:
            ref = parse_ref(item.ref)
            if ref is None:
                continue
            # An item seen for the first time is read from the previous poll on: the
            # worker that follows it was started after that poll and reads the item
            # itself, so what came before is not news to it.
            since = state.items.get(item.key, state.polled_at if state.polled_at else now)
            try:
                events += self._poll_item(ref, since)
            except TrackerAuthError:
                raise
            except TrackerError as exc:
                # One item this account can no longer read (SSO, a server error) must not
                # hold back the review requests of every other repository. The item keeps
                # its old time, so the next poll reads it again from there.
                log.warning("github: could not read %s: %s", ref, exc)
                state.items[item.key] = since
                continue
            state.items[item.key] = now
        state.items = {key: value for key, value in state.items.items() if key in followed}
        state.polled_at = now
        return events, state.dump()

    def item_open(self, item: WorkItem) -> bool:
        ref = parse_ref(item.ref)
        if ref is None:
            return False
        try:
            issue = self.gh.api_one(f"repos/{ref.full_name}/issues/{ref.number}")
        except NotFound:
            return False
        return issue.get("state") == "open"

    def repository_for(
        self, item: WorkItem, repositories: Sequence[Repository]
    ) -> Repository | None:
        ref = parse_ref(item.ref)
        name = (item.hint or (ref.full_name if ref else "")).lower()
        if not name:
            return None
        return next(
            (r for r in repositories if r.github is not None and r.github.lower() == name), None
        )

    def describe(self) -> str:
        return f"{self.name} (github through {self.gh.binary})"

    # ----- notifications ------------------------------------------------------

    def _poll_notifications(self, state: Cursor, followed: set[str], now: float) -> list[Event]:
        path = f"notifications?per_page={PAGE_SIZE}"
        # The first poll reads the unread threads only, so that turning the source on does
        # not replay the account's history. Later polls read the threads that were read
        # too: the operator receives the review requests, and opening one in a browser
        # before the next poll must not hide it. `since` keeps that list short.
        if state.last_modified:
            path += "&all=true"
            try:
                since = parsedate_to_datetime(state.last_modified).timestamp()
            except (TypeError, ValueError):
                since = None
            if since is not None:
                path += f"&since={_iso(since - CLOCK_SKEW_SECS)}"
        response = self.gh.conditional(path, state.last_modified)
        interval = response.headers.get("x-poll-interval", "")
        if interval.isdigit():
            state.poll_interval = max(int(interval), 1)
        state.next_poll_at = now + state.poll_interval
        if response.status == 304:
            return []
        if response.headers.get("last-modified"):
            state.last_modified = response.headers["last-modified"]
        values = _json_values(response.body)
        if len(values) != 1 or not isinstance(values[0], list):
            raise TrackerError("github: notifications: expected a list")
        events = []
        for entry in values[0]:
            event = self._notification_event(entry, followed) if isinstance(entry, Mapping) else None
            if event is not None:
                events.append(event)
        return events

    def _notification_event(self, entry: Mapping[str, Any], followed: set[str]) -> Event | None:
        kind = NOTIFICATION_KINDS.get(str(entry.get("reason")))
        subject = entry.get("subject") if isinstance(entry.get("subject"), Mapping) else {}
        if kind is None or subject.get("type") not in ("PullRequest", "Issue"):
            return None
        match = _API_PATH.search(str(subject.get("url") or ""))
        if not match:
            # CI activity names a check suite, not a pull request: the per-item poll
            # reports it for the pull requests that a worker follows.
            return None
        ref = make_ref(match.group(1), match.group(2), match.group(4))
        if ref is None:
            return None
        item = self._work_item(ref, pull=match.group(3) == "pulls")
        # The per-item poll reports a followed item's comments and CI itself, with the
        # ids of the objects; the notification would be a second copy of each. A review
        # request is the one thing only the notification says.
        if item.key in followed and kind != "review_requested":
            return None
        if kind == "review_requested" and not self._review_requested_now(ref):
            return None
        updated = str(entry.get("updated_at") or "")
        return Event(
            source=self.name,
            kind=kind,
            item=item,
            actor="",
            occurred_at=_timestamp(updated) or self.clock(),
            dedupe_id=f"notification:{entry.get('id')}:{updated}",
            pointer=item.url or "",
        )

    def _review_requested_now(self, ref: Ref) -> bool:
        """Is a review of the open pull request *ref* still asked of this account?

        A thread keeps its reason when later activity updates it, so a comment on a pull
        request whose review was finished long ago would otherwise read as a new review
        request. GitHub drops the account from the requested reviewers once it submits a
        review, and adds it again on a re-request. A team request counts when the account
        is in the team; when gh may not list the account's teams (the token lacks the
        org scope), any requested team counts.
        """
        try:
            pull = self.gh.api_one(f"repos/{ref.full_name}/pulls/{ref.number}")
        except NotFound:
            return False
        if pull.get("state") != "open":
            return False
        reviewers = pull.get("requested_reviewers") or []
        teams = pull.get("requested_teams") or []
        me = self.whoami().lower()
        if any(_login(user).lower() == me for user in reviewers):
            return True
        if not teams:
            return False
        mine = self._team_ids()
        return mine is None or any(
            isinstance(team, Mapping) and team.get("id") in mine for team in teams
        )

    def _team_ids(self) -> set[object] | None:
        """The ids of the account's teams, once per source; None when gh may not list them."""
        if self._teams is _UNKNOWN:
            try:
                self._teams = {
                    team.get("id") for team in self.gh.api_list(f"user/teams?per_page={PAGE_SIZE}")
                }
            except TrackerAuthError:
                raise
            except TrackerError as exc:
                log.warning("github: cannot list the account's teams, so any team counts: %s", exc)
                self._teams = None
        return self._teams  # type: ignore[return-value]

    # ----- one followed item --------------------------------------------------

    def _work_item(self, ref: Ref, *, pull: bool, url: object = None) -> WorkItem:
        return WorkItem(
            source=self.name,
            key=item_key(ref),
            ref=str(ref),
            url=sanitize.safe_url(url) or html_url(ref, pull=pull),
            hint=ref.full_name,
        )

    def _poll_item(self, ref: Ref, since: float) -> list[Event]:
        base = f"repos/{ref.full_name}"
        try:
            pull = self.gh.api_one(f"{base}/pulls/{ref.number}")
        except NotFound:
            pull = None
        if pull is None:
            try:
                issue = self.gh.api_one(f"{base}/issues/{ref.number}")
            except NotFound:
                log.info("github: %s is gone or not visible; skipping it", ref)
                return []
            item = self._work_item(ref, pull=False, url=issue.get("html_url"))
            return self._comments(item, f"{base}/issues/{ref.number}/comments", since, "issue_comment")

        item = self._work_item(ref, pull=True, url=pull.get("html_url"))
        review_comments = self._list_since(f"{base}/pulls/{ref.number}/comments", since)
        events = self._comment_events(item, review_comments, since, "review_comment")
        events += self._comments(item, f"{base}/issues/{ref.number}/comments", since, "issue_comment")
        # A review with no body (inline comments only, or a plain approval) cannot carry
        # the marker itself; its inline comments can.
        marked_reviews = {
            entry.get("pull_request_review_id")
            for entry in review_comments
            if _marked(entry.get("body"))
        }
        events += self._reviews(item, f"{base}/pulls/{ref.number}/reviews", since, marked_reviews)
        sha = (pull.get("head") or {}).get("sha") if isinstance(pull.get("head"), Mapping) else None
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            events += self._statuses(item, f"{base}/commits/{sha}/statuses", since)
            events += self._check_runs(item, f"{base}/commits/{sha}/check-runs", since)
        if pull.get("state") == "closed":
            closed_at = str(pull.get("closed_at") or "")
            events.append(
                Event(
                    source=self.name,
                    kind="pr_closed",
                    item=item,
                    actor=_login(pull.get("merged_by")),
                    occurred_at=_timestamp(closed_at) or self.clock(),
                    dedupe_id=f"pr_closed:{item.key}:{closed_at}",
                    pointer=item.url or "",
                )
            )
        return events

    def _event(
        self, item: WorkItem, kind: str, *, dedupe: str,
        actor: str, at: float, url: object = None, marked: bool = False,
    ) -> Event:
        return Event(
            source=self.name,
            kind=kind,
            item=item,
            actor=actor,
            occurred_at=at,
            dedupe_id=dedupe,
            pointer=sanitize.safe_url(url) or item.url or "",
            marked=marked,
        )

    def _list_since(self, path: str, since: float) -> list[Mapping[str, Any]]:
        return self.gh.api_list(
            f"{path}?per_page={PAGE_SIZE}&since={_iso(since - CLOCK_SKEW_SECS)}"
        )

    def _comments(self, item: WorkItem, path: str, since: float, prefix: str) -> list[Event]:
        return self._comment_events(item, self._list_since(path, since), since, prefix)

    def _comment_events(
        self, item: WorkItem, entries: Iterable[Mapping[str, Any]], since: float, prefix: str
    ) -> list[Event]:
        events = []
        for entry in entries:
            at = _timestamp(entry.get("created_at"))
            if at is None or at < since - CLOCK_SKEW_SECS or entry.get("id") is None:
                continue
            events.append(
                self._event(
                    item, "comment", dedupe=f"{prefix}:{entry['id']}",
                    actor=_login(entry.get("user")), at=at, url=entry.get("html_url"),
                    marked=_marked(entry.get("body")),
                )
            )
        return events

    def _reviews(
        self, item: WorkItem, path: str, since: float, marked_reviews: set[object]
    ) -> list[Event]:
        events = []
        for entry in self.gh.api_list(f"{path}?per_page={PAGE_SIZE}"):
            # A pending review has no submission time and is visible to its author only.
            at = _timestamp(entry.get("submitted_at"))
            if at is None or at < since - CLOCK_SKEW_SECS or entry.get("id") is None:
                continue
            events.append(
                self._event(
                    item, "review", dedupe=f"review:{entry['id']}",
                    actor=_login(entry.get("user")), at=at, url=entry.get("html_url"),
                    marked=_marked(entry.get("body")) or entry["id"] in marked_reviews,
                )
            )
        return events

    def _statuses(self, item: WorkItem, path: str, since: float) -> list[Event]:
        events = []
        for entry in self.gh.api_list(f"{path}?per_page={PAGE_SIZE}"):
            # Every state change is a new status with its own id; a pending one is not news.
            at = _timestamp(entry.get("updated_at") or entry.get("created_at"))
            if (
                entry.get("state") == "pending" or at is None
                or at < since - CLOCK_SKEW_SECS or entry.get("id") is None
            ):
                continue
            events.append(
                self._event(
                    item, "ci_status", dedupe=f"status:{entry['id']}",
                    actor=_login(entry.get("creator")), at=at, url=item.url,
                )
            )
        return events

    def _check_runs(self, item: WorkItem, path: str, since: float) -> list[Event]:
        events = []
        for entry in self.gh.api_list(f"{path}?per_page={PAGE_SIZE}", key="check_runs"):
            at = _timestamp(entry.get("completed_at"))
            if (
                entry.get("status") != "completed" or at is None
                or at < since - CLOCK_SKEW_SECS or entry.get("id") is None
            ):
                continue
            app = entry.get("app") if isinstance(entry.get("app"), Mapping) else {}
            # A re-run keeps the id and completes again, so the completion time is part of
            # the id of the event.
            events.append(
                self._event(
                    item, "ci_status",
                    dedupe=f"check_run:{entry['id']}:{entry.get('completed_at')}",
                    actor=str(app.get("slug") or ""), at=at, url=entry.get("html_url"),
                )
            )
        return events


# ----- GitHub Issues as a repository's queue -----------------------------------


class GithubTracker(Tracker):
    """``tracker = { type = "github", repo = "owner/repo" }``: GitHub Issues as the queue.

    No workflow states: an issue is queued when it is open, assigned to the signed-in
    account and carries the queue label. Every label write is followed by a re-read.
    """

    type = "github"
    _OPTIONS = ("repo", "gh_bin", "timeout_secs")

    def __init__(self, options: Mapping[str, Any], labels: Mapping[str, str]) -> None:
        _check_options(options, self._OPTIONS, "tracker github")
        repo = options.get("repo")
        parts = repo.split("/") if isinstance(repo, str) else []
        if len(parts) != 2 or make_ref(parts[0], parts[1], 1) is None:
            raise ConfigError("tracker github: `repo` must name a repository as owner/repo")
        self.repo = str(repo)
        self.gh = _gh_from(options, "tracker github")
        self._labels = dict(labels)
        self._viewer: str | None = None

    def describe(self) -> str:
        return f"{self.type} repo={self.repo}"

    def whoami(self) -> str:
        if self._viewer is None:
            self._viewer = self.gh.login()
        return self._viewer

    def list(self, status: Status) -> list[Issue]:
        label = self._label_for(status)
        if label is None:
            raise TrackerError("github: cannot list issues by the 'done' status")
        path = (
            f"repos/{self.repo}/issues?state=open&per_page={PAGE_SIZE}"
            f"&labels={quote(label, safe='')}&assignee={quote(self.whoami(), safe='')}"
        )
        issues = []
        for entry in self.gh.api_list(path):
            # The issues endpoint lists pull requests too.
            if "pull_request" in entry:
                continue
            issue = self._to_issue(entry)
            if issue is not None:
                issues.append(issue)
        return issues

    def get(self, issue_id: str) -> Issue | None:
        """Re-read one issue by its ``owner/repo#number``, or by its item key.

        A session adopted with no ledger row behind it knows only its item key.
        """
        ref = parse_ref(issue_id) or self._ref_for_key(issue_id)
        if ref is None:
            return None
        try:
            entry = self.gh.api_one(f"repos/{ref.full_name}/issues/{ref.number}")
        except NotFound:
            return None
        return self._to_issue(entry)

    def mark(self, issue: Issue, status: Status) -> Issue:
        target = self._label_for(status)
        managed = {name.lower() for name in self._labels.values()}
        current = {name.lower() for name in issue.labels}
        to_add = target if target and target.lower() not in current else None
        to_remove = [
            name for name in issue.labels if name.lower() in managed
            and (target is None or name.lower() != target.lower())
        ]
        if to_add is None and not to_remove:
            return issue
        ref = parse_ref(issue.id)
        if ref is None:
            raise TrackerError(f"github: {issue.id!r} is not an owner/repo#number reference")

        args = ["issue", "edit", str(ref.number), "--repo", ref.full_name]
        if to_add is not None:
            args += ["--add-label", to_add]
        for name in to_remove:
            args += ["--remove-label", name]
        write_error: Exception | None = None
        try:
            self.gh.check(self.gh.run(args), f"issue edit {issue.identifier}")
        except TrackerError as exc:
            # A reported failure is not proof the write did not land. Re-read decides.
            write_error = exc

        try:
            fresh = self.get(issue.id)
        except TrackerError as exc:
            raise AmbiguousWrite(
                f"github: could not verify the label write on {issue.identifier}: {exc}"
            ) from exc
        if fresh is None:
            raise AmbiguousWrite(
                f"github: {issue.identifier} disappeared while its labels were being written"
            )
        if not self._matches(fresh, status):
            if write_error is not None:
                raise AmbiguousWrite(
                    f"github: label write on {issue.identifier} failed and did not land: "
                    f"{write_error}"
                )
            raise AmbiguousWrite(
                f"github: label write on {issue.identifier} reported success but the re-read "
                f"shows labels {sorted(fresh.labels)}"
            )
        return fresh

    def _ref_for_key(self, key: str) -> Ref | None:
        match = re.search(r"_([0-9]{1,10})(?:-[0-9a-f]{8})?$", key or "")
        if not match:
            return None
        owner, repo = self.repo.split("/")
        ref = make_ref(owner, repo, match.group(1))
        return ref if ref is not None and item_key(ref) == key else None

    def _matches(self, issue: Issue, status: Status) -> bool:
        target = self._label_for(status)
        managed = {name.lower() for name in self._labels.values()}
        present = {name.lower() for name in issue.labels if name.lower() in managed}
        return present == ({target.lower()} if target else set())

    def _label_for(self, status: Status) -> str | None:
        if status is Status.DONE:
            return None
        try:
            return self._labels[status.value]
        except KeyError as exc:  # pragma: no cover - guarded by config validation
            raise TrackerError(f"github: no label configured for status {status.value}") from exc

    def _to_issue(self, entry: Mapping[str, Any]) -> Issue | None:
        parts = self.repo.split("/")
        url_match = _API_PATH.search(str(entry.get("url") or ""))
        if url_match:
            ref = make_ref(url_match.group(1), url_match.group(2), url_match.group(4))
        else:
            ref = make_ref(parts[0], parts[1], entry.get("number"))
        if ref is None:
            return None
        labels = tuple(
            str(label["name"])
            for label in entry.get("labels") or []
            if isinstance(label, Mapping) and label.get("name")
        )
        state = str(entry.get("state") or "")
        return Issue(
            id=str(ref),
            identifier=item_key(ref),
            url=sanitize.safe_url(entry.get("html_url")) or html_url(ref, pull=False),
            title=str(entry.get("title") or ""),
            labels=labels,
            state=state,
            closed=state == "closed",
        )


__all__ = [
    "GithubSource",
    "GithubTracker",
    "NOTIFICATION_KINDS",
    "Ref",
    "item_key",
    "make_ref",
    "parse_ref",
]
