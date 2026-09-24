"""Linear adapter: GraphQL over an API key.

Two things drive the shape of this module. Label writes are single-label add/remove
mutations rather than a whole-list update, so a concurrent label edit by a human is
not clobbered. And every write is followed by a re-read, because the two mutations
run serially rather than atomically and a reported failure may still have landed.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Mapping
from typing import Any

from chargehand.errors import AmbiguousWrite, ConfigError, TrackerAuthError, TrackerError
from chargehand.trackers.base import Issue, Status, Tracker

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
        nodes = (self._post(_QUEUE_QUERY, {"filter": issue_filter}).get("issues") or {}).get(
            "nodes", []
        )
        return [self._to_issue(node) for node in nodes]

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
        """Drop the title slug from a Linear issue URL.

        Linear returns `/{workspace}/issue/{IDENTIFIER}/{slug-of-the-title}`. The slug is
        the title, lowercased and hyphenated — so passing the URL through verbatim would
        put issue text into the launch prompt and into notifications, both of which are
        documented as carrying none. The shortened form resolves to the same issue.
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
        )
