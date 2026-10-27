"""Linear adapter: GraphQL over an API key.

The poll reads the whole queue, one paged query as the queue listing always was, so a
start that waited is never lost and an empty queue costs one small query. The issues
that live workers follow are read in one more query, with their comments since each
issue's previous poll; that is what the cursor keeps. A comment body is read only to
find the authorship marker; it does not leave this module.

Two things drive the shape of the label writes. Label writes are single-label add/remove
mutations rather than a whole-list update, so a concurrent label edit by a human is
not clobbered. And every write is followed by a re-read, because the two mutations
run serially rather than atomically and a reported failure may still have landed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from chargehand.errors import AmbiguousWrite, ConfigError, TrackerAuthError, TrackerError
from chargehand.trackers.base import Comment, Issue, Status, Tracker, TrackerPoll

DEFAULT_ENDPOINT = "https://api.linear.app/graphql"
DEFAULT_KEY_ENV = "CHARGEHAND_LINEAR_API_KEY"
DEFAULT_KEYCHAIN_SERVICE = "chargehand-linear"

_ISSUE_FIELDS = """
  id
  identifier
  title
  url
  branchName
  labels { nodes { id name } }
  state { name type }
  updatedAt
"""

_QUEUE_QUERY = """
query Queue($filter: IssueFilter!) {
  issues(first: 50, filter: $filter) {
    nodes {
      %s
    }
  }
}
""" % _ISSUE_FIELDS

_ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    %s
  }
}
""" % _ISSUE_FIELDS

_VIEWER_QUERY = "query { viewer { id name email } }"

_POLL_QUERY = """
query Poll($filter: IssueFilter!, $after: String) {
  issues(first: 50, after: $after, filter: $filter) {
    nodes {
      %s
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % _ISSUE_FIELDS

# The body is asked for only to find the authorship marker.
_FOLLOWED_QUERY = """
query Followed($filter: IssueFilter!, $comments: CommentFilter) {
  issues(first: 50, filter: $filter) {
    nodes {
      %s
      comments(first: 50, filter: $comments) {
        nodes { id createdAt body user { id name email } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""" % _ISSUE_FIELDS

# The next pages of one issue's comments, with the same filter as the first page.
_COMMENTS_QUERY = """
query Comments($id: String!, $comments: CommentFilter, $after: String) {
  issue(id: $id) {
    comments(first: 50, after: $after, filter: $comments) {
      nodes { id createdAt body user { id name email } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# Pages of the queue read in one poll. Far more than one account has queued at once.
_MAX_QUEUE_PAGES = 10
_FOLLOWED_CHUNK = 50
# Pages of one issue's comments read in one poll. An issue with more left goes on from
# the last page on the next poll.
_MAX_COMMENT_PAGES = 20

# A comment can become readable a moment after a later one was already read, so every
# poll reads this far back again. The overlap costs nothing: the router drops what it has
# seen before. Comment times come from Linear's clock and are compared with this
# machine's, which the overlap also covers.
OVERLAP_SECS = 300

# `first` is a ceiling, not an expectation: with a team filter the answer is one label,
# and without one a workspace can hold a great many with the same name. A full page is
# treated as truncation rather than as a list to choose from.
_LABEL_PAGE = 50

_LABELS_QUERY = """
query Labels($filter: IssueLabelFilter!, $first: Int!) {
  issueLabels(filter: $filter, first: $first) {
    nodes { id name team { id key } }
  }
}
"""

_ADD_LABEL = "mutation Add($id: String!, $labelId: String!) { issueAddLabel(id: $id, labelId: $labelId) { success } }"
_REMOVE_LABEL = "mutation Remove($id: String!, $labelId: String!) { issueRemoveLabel(id: $id, labelId: $labelId) { success } }"


def _lookup_keychain(service: str, account: str | None) -> str | None:
    argv = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        argv[2:2] = ["-a", account]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


def _author(user: object) -> str:
    """The comment's author, named the way :meth:`LinearTracker.whoami` names the viewer."""
    if not isinstance(user, Mapping):
        return ""
    return str(user.get("email") or user.get("name") or user.get("id") or "")


def _marked(body: object) -> bool:
    # Imported here: the sources package imports the trackers package.
    from chargehand.sources.base import AUTHORSHIP_MARKER

    return isinstance(body, str) and AUTHORSHIP_MARKER in body


def _number(raw: object) -> float | None:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


@dataclass
class _Pages:
    """Where an issue's comments go on when one poll could not read them all.

    *after* is the end cursor of the last page read, *gt* the comment filter it belongs
    to, and *started* when the first of those pages was read.
    """

    after: str
    gt: str
    started: float

    @classmethod
    def load(cls, value: object) -> "_Pages | None":
        if not isinstance(value, dict):
            return None
        after, gt, started = value.get("after"), value.get("gt"), _number(value.get("started"))
        if not isinstance(after, str) or not isinstance(gt, str) or started is None:
            return None
        return cls(after, gt, started)

    def dump(self) -> dict[str, object]:
        return {"after": self.after, "gt": self.gt, "started": self.started}


@dataclass
class _Cursor:
    """When each followed issue was last read for comments. A followed issue seen for the
    first time is read from the previous poll on: its worker started after that poll and
    reads the issue itself. An issue with more comments than one poll reads also keeps
    where its pages go on."""

    polled_at: float | None = None
    items: dict[str, float] = field(default_factory=dict)
    pages: dict[str, _Pages] = field(default_factory=dict)

    @classmethod
    def load(cls, value: str | None) -> "_Cursor":
        try:
            data = json.loads(value) if value else {}
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        items = data.get("items")
        pages = data.get("pages")
        loaded = {
            str(key): _Pages.load(value)
            for key, value in (pages.items() if isinstance(pages, dict) else ())
        }
        return cls(
            polled_at=_number(data.get("polled_at")),
            items={
                str(key): float(since)
                for key, since in (items.items() if isinstance(items, dict) else ())
                if _number(since) is not None
            },
            pages={key: value for key, value in loaded.items() if value is not None},
        )

    def dump(self) -> str:
        data: dict[str, object] = {"polled_at": self.polled_at, "items": self.items}
        if self.pages:
            data["pages"] = {key: pages.dump() for key, pages in self.pages.items()}
        return json.dumps(data, sort_keys=True)


class LinearTracker(Tracker):
    type = "linear"

    _ALLOWED_OPTIONS = (
        "team",
        "state",
        "project",
        "endpoint",
        "api_key_env",
        "api_key_command",
        "keychain_service",
        "keychain_account",
        "timeout_secs",
    )

    def __init__(self, options: Mapping[str, Any], labels: Mapping[str, str]) -> None:
        unknown = sorted(set(options) - set(self._ALLOWED_OPTIONS))
        if unknown:
            raise ConfigError(f"tracker linear: unknown option(s): {', '.join(unknown)}")
        self._options = dict(options)
        self._labels = dict(labels)
        self._endpoint = str(options.get("endpoint", DEFAULT_ENDPOINT))
        self._timeout = float(options.get("timeout_secs", 30))
        self._team = options.get("team")
        self._state = options.get("state")
        self._project = options.get("project")
        self._api_key: str | None = None
        self._label_ids: dict[str, str] = {}
        self._viewer: str | None = None
        self.clock: Callable[[], float] = time.time

    def describe(self) -> str:
        bits = [self.type]
        if self._team:
            bits.append(f"team={self._team}")
        if self._state:
            bits.append(f"state={self._state}")
        return " ".join(bits)

    # ----- credentials ------------------------------------------------------

    def api_key(self) -> str:
        if self._api_key:
            return self._api_key
        env_name = str(self._options.get("api_key_env", DEFAULT_KEY_ENV))
        key = os.environ.get(env_name)
        if not key and self._options.get("api_key_command"):
            key = self._run_key_command(str(self._options["api_key_command"]))
        if not key:
            key = _lookup_keychain(
                str(self._options.get("keychain_service", DEFAULT_KEYCHAIN_SERVICE)),
                self._options.get("keychain_account"),  # type: ignore[arg-type]
            )
        if not key:
            raise TrackerAuthError(
                "linear: no API key. Set "
                f"${env_name}, configure api_key_command, or store it in the login keychain "
                f"under service "
                f"'{self._options.get('keychain_service', DEFAULT_KEYCHAIN_SERVICE)}'."
            )
        self._api_key = key
        return key

    @staticmethod
    def _run_key_command(command: str) -> str | None:
        import shlex

        try:
            result = subprocess.run(
                shlex.split(command), capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise TrackerAuthError(f"linear: api_key_command failed: {exc}") from exc
        if result.returncode != 0:
            raise TrackerAuthError(
                f"linear: api_key_command exited {result.returncode}"
            )
        return result.stdout.strip() or None

    # ----- transport --------------------------------------------------------

    def _post(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": dict(variables or {})}).encode()
        request = urllib.request.Request(
            self._endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key(),
                "User-Agent": "chargehand",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise TrackerError(f"linear: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TrackerError(f"linear: request failed: {exc}") from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise TrackerError(f"linear: response was not JSON: {body[:200]}") from exc
        if data.get("errors"):
            messages = "; ".join(
                str(error.get("message", error)) for error in data["errors"][:3]
            )
            raise TrackerError(f"linear: {messages}")
        result = data.get("data")
        if not isinstance(result, dict):
            raise TrackerError("linear: response had no data")
        return result

    # ----- the four calls ---------------------------------------------------

    def whoami(self) -> str:
        if self._viewer is None:
            viewer = self._post(_VIEWER_QUERY).get("viewer") or {}
            self._viewer = str(viewer.get("email") or viewer.get("name") or viewer.get("id") or "?")
        return self._viewer

    def list(self, status: Status) -> list[Issue]:
        nodes = (
            self._post(_QUEUE_QUERY, {"filter": self._filter_for(status)}).get("issues") or {}
        ).get("nodes", [])
        return [self._to_issue(node) for node in nodes]

    def _filter_for(self, status: Status) -> dict[str, Any]:
        label = self._label_for(status)
        if label is None:
            raise TrackerError("linear: cannot list issues by the 'done' status")
        issue_filter: dict[str, Any] = {
            "assignee": {"isMe": {"eq": True}},
            "labels": {"name": {"eq": label}},
        }
        if self._team:
            issue_filter["team"] = {"key": {"eq": self._team}}
        if self._project:
            issue_filter["project"] = {"name": {"eq": self._project}}
        # The workflow-state filter applies to the queue only: an issue that is already
        # running has been moved on by whatever the prompt runs, and must still be found.
        if self._state and status is Status.QUEUED:
            issue_filter["state"] = {"name": {"eq": self._state}}
        return issue_filter

    # ----- the poll -----------------------------------------------------------

    def poll(self, cursor: str | None, followed: Sequence[str]) -> TrackerPoll:
        state = _Cursor.load(cursor)
        now = self.clock()
        queued = self._queue()
        issues: list[Issue] = []
        comments: list[Comment] = []
        unfinished: dict[str, _Pages] = {}
        start = state.polled_at if state.polled_at is not None else now
        since = {issue_id: state.items.get(issue_id, start) for issue_id in followed}
        going_on = {key: pages for key, pages in state.pages.items() if key in since}
        if followed:
            issues, comments, unfinished = self._read_followed(since, going_on, now)
        # An issue with comments left keeps its time and goes on from its last page. Once
        # its pages are all read, it is read again from when they started, so a comment
        # added while they were read is not lost, whatever the order of the pages.
        state.items = {}
        for issue_id in followed:
            if issue_id in unfinished:
                state.items[issue_id] = since[issue_id]
            elif issue_id in going_on:
                state.items[issue_id] = going_on[issue_id].started
            else:
                state.items[issue_id] = now
        state.pages = unfinished
        state.polled_at = now
        return TrackerPoll(queued=queued, followed=issues, comments=comments, cursor=state.dump())

    def _queue(self) -> list[Issue]:
        """The whole queue, on every poll: a start that waited longer than the router keeps
        it is offered again, and the listing tells a waiting start whether its issue is
        still queued."""
        issue_filter = self._filter_for(Status.QUEUED)
        issues: list[Issue] = []
        after: str | None = None
        for _ in range(_MAX_QUEUE_PAGES):
            page = (
                self._post(_POLL_QUERY, {"filter": issue_filter, "after": after}).get("issues")
                or {}
            )
            issues += [self._to_issue(node) for node in page.get("nodes") or []]
            info = page.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                break
        return issues

    def _read_followed(
        self, since: Mapping[str, float], going_on: Mapping[str, _Pages], now: float
    ) -> tuple[list[Issue], list[Comment], dict[str, _Pages]]:
        """The followed issues, their comments since each one's time, and where the
        comments go on for each issue whose comments did not fit in the pages one poll
        reads.

        Every page of comments is read through ``pageInfo``, so nothing depends on the
        order in which Linear returns them. An issue in *going_on* goes on from its last
        page, with the filter that page was read with, instead of from its first page.
        """
        ids = sorted(since)
        gt = _iso(min(since.values()) - OVERLAP_SECS)
        comment_filter = {"createdAt": {"gt": gt}}
        issues: list[Issue] = []
        comments: list[Comment] = []
        unfinished: dict[str, _Pages] = {}
        for offset in range(0, len(ids), _FOLLOWED_CHUNK):
            variables = {
                "filter": {"id": {"in": ids[offset : offset + _FOLLOWED_CHUNK]}},
                "comments": comment_filter,
            }
            page = self._post(_FOLLOWED_QUERY, variables).get("issues") or {}
            for node in page.get("nodes") or []:
                issue = self._to_issue(node)
                if issue.id not in since:
                    continue
                issues.append(issue)
                after = since[issue.id] - OVERLAP_SECS
                pages = going_on.get(issue.id)
                found = self._comments_after(issue.id, pages) if pages is not None else None
                if pages is not None and found is not None:
                    entries, end = found
                    if end is not None:
                        unfinished[issue.id] = _Pages(end, pages.gt, pages.started)
                else:
                    entries, end = self._all_comments(
                        issue.id, node.get("comments") or {}, comment_filter
                    )
                    if end is not None:
                        unfinished[issue.id] = _Pages(end, gt, now)
                for entry in entries:
                    created = _timestamp(entry.get("createdAt"))
                    if not entry.get("id") or created is None or created <= after:
                        continue
                    comments.append(
                        Comment(
                            id=str(entry["id"]),
                            issue=issue,
                            author=_author(entry.get("user")),
                            created_at=created,
                            marked=_marked(entry.get("body")),
                        )
                    )
        return issues, comments, unfinished

    def _all_comments(
        self,
        issue_id: str,
        first: Mapping[str, Any],
        comment_filter: Mapping[str, Any],
        *,
        pages: int = _MAX_COMMENT_PAGES - 1,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """The comment entries of *first* and of up to *pages* pages after it, and the end
        cursor of the last page when more are left after it."""
        entries = list(first.get("nodes") or [])
        info = first.get("pageInfo") or {}
        for _ in range(pages):
            if not info.get("hasNextPage") or not info.get("endCursor"):
                return entries, None
            variables = {"id": issue_id, "comments": comment_filter, "after": info["endCursor"]}
            connection = (
                (self._post(_COMMENTS_QUERY, variables).get("issue") or {}).get("comments") or {}
            )
            entries += connection.get("nodes") or []
            info = connection.get("pageInfo") or {}
        if info.get("hasNextPage") and info.get("endCursor"):
            return entries, str(info["endCursor"])
        return entries, None

    def _comments_after(
        self, issue_id: str, pages: _Pages
    ) -> tuple[list[Mapping[str, Any]], str | None] | None:
        """Go on from where an earlier poll stopped reading the issue's comments.

        None when Linear no longer takes the cursor: the issue is then read from its own
        time again, from its first page, as before the pages started.
        """
        first = {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": pages.after}}
        try:
            return self._all_comments(
                issue_id, first, {"createdAt": {"gt": pages.gt}}, pages=_MAX_COMMENT_PAGES
            )
        except TrackerAuthError:
            raise
        except TrackerError:
            return None

    def get(self, issue_id: str) -> Issue | None:
        node = self._post(_ISSUE_QUERY, {"id": issue_id}).get("issue")
        return self._to_issue(node) if node else None

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

        write_error: Exception | None = None
        try:
            if to_add is not None:
                self._post(_ADD_LABEL, {"id": issue.id, "labelId": self._label_id(to_add)})
            for name in to_remove:
                self._post(_REMOVE_LABEL, {"id": issue.id, "labelId": self._label_id(name)})
        except TrackerError as exc:
            # A reported failure is not proof the write did not land. Re-read decides.
            write_error = exc

        try:
            fresh = self.get(issue.id)
        except TrackerError as exc:
            raise AmbiguousWrite(
                f"linear: could not verify the label write on {issue.identifier}: {exc}"
            ) from exc
        if fresh is None:
            raise AmbiguousWrite(
                f"linear: {issue.identifier} disappeared while its labels were being written"
            )
        if not self._matches(fresh, status):
            if write_error is not None:
                raise AmbiguousWrite(
                    f"linear: label write on {issue.identifier} failed and did not land: "
                    f"{write_error}"
                )
            raise AmbiguousWrite(
                f"linear: label write on {issue.identifier} reported success but the re-read "
                f"shows labels {sorted(fresh.labels)}"
            )
        return fresh

    # ----- helpers ----------------------------------------------------------

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
            raise TrackerError(f"linear: no label configured for status {status.value}") from exc

    def _label_id(self, name: str) -> str:
        """Resolve one label name, asking the tracker for that name only.

        Enumerating the workspace instead would be both slower and wrong. A shared
        workspace accumulates labels without bound - one observed here holds more than
        fifteen thousand - so any fixed page budget silently stops short, and the labels
        that fall outside it are reported as not existing at all. Since the listing comes
        back newest first, the label that disappears is the one that has been in use
        longest: the route would work for months and then fail every launch.
        """
        key = name.lower()
        cached = self._label_ids.get(key)
        if cached is not None:
            return cached
        # Narrow on the server where the route names a team. A shared workspace here holds
        # 250 teams and 114 label names used by more than 25 of them, so asking for a name
        # alone and taking what comes back would miss the one this route means.
        label_filter: dict[str, Any] = {"name": {"eqIgnoreCase": name}}
        if self._team:
            label_filter["team"] = {"key": {"eq": self._team}}
        nodes = (
            self._post(_LABELS_QUERY, {"filter": label_filter, "first": _LABEL_PAGE}).get(
                "issueLabels"
            )
            or {}
        ).get("nodes", [])
        candidates = [
            node for node in nodes if str(node.get("name", "")).lower() == key and node.get("id")
        ]
        if len(nodes) >= _LABEL_PAGE:
            raise TrackerError(
                f"linear: more than {_LABEL_PAGE} labels are named '{name}'; set `team` on "
                f"the route so the right one can be identified, or rename the label"
            )
        chosen = self._pick_label(name, candidates)
        self._label_ids[key] = str(chosen["id"])
        return self._label_ids[key]

    def _pick_label(self, name: str, candidates: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        """A label name is unique per team, not per workspace.

        Two teams may each own a label called `chargehand`, and writing the wrong one
        would move an issue into a queue this route does not watch. The route's own team
        wins; a workspace-level label is the fallback; anything still ambiguous is
        reported rather than guessed at.
        """
        if not candidates:
            raise TrackerError(
                f"linear: no label named '{name}' exists in this workspace; create it, or "
                f"change the label names in the route configuration"
            )
        if self._team:
            for node in candidates:
                if (node.get("team") or {}).get("key") == self._team:
                    return node
        workspace = [node for node in candidates if not node.get("team")]
        if workspace:
            return workspace[0]
        if len(candidates) == 1:
            return candidates[0]
        teams = ", ".join(
            sorted(str((node.get("team") or {}).get("key") or "?") for node in candidates)
        )
        raise TrackerError(
            f"linear: several teams have a label named '{name}' ({teams}) and none of them "
            f"is this route's team; set `team` on the route, or rename the label"
        )

    def refresh_labels(self) -> None:
        """Drop the per-tick label-id cache; labels can be created between ticks."""
        self._label_ids = {}

    @staticmethod
    def _canonical_url(url: object) -> str | None:
        """Keep a Linear issue URL only up to its identifier.

        Linear's links can end in a title slug, `/{IDENTIFIER}/{slug-of-the-title}`: the
        title, lowercased and hyphenated. The API's `url` field carried none when checked
        against a real workspace, but nothing promises that, and a slug passed through
        would put issue text into the launch prompt and into notifications, both of which
        are documented as carrying none. The shortened form resolves to the same issue.
        """
        if not isinstance(url, str) or not url:
            return None
        parts = urlsplit(url)
        segments = parts.path.split("/")
        for index, segment in enumerate(segments):
            if segment == "issue" and index + 1 < len(segments):
                trimmed = "/".join(segments[: index + 2])
                return urlunsplit((parts.scheme, parts.netloc, trimmed, "", ""))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @staticmethod
    def _to_issue(node: Mapping[str, Any]) -> Issue:
        labels = tuple(
            str(entry["name"])
            for entry in ((node.get("labels") or {}).get("nodes") or [])
            if entry.get("name")
        )
        state = node.get("state") or {}
        state_type = str(state.get("type") or "")
        return Issue(
            id=str(node.get("id", "")),
            identifier=str(node.get("identifier", "")),
            url=LinearTracker._canonical_url(node.get("url")),
            title=str(node.get("title") or ""),
            branch_name=node.get("branchName"),
            labels=labels,
            state=state.get("name"),
            closed=state_type in ("completed", "canceled"),
            updated_at=_timestamp(node.get("updatedAt")),
        )
