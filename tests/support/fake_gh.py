#!/usr/bin/env python3
"""A stand-in for the `gh` binary: a small GitHub kept in a JSON state file.

The state lives in the file named by $FAKE_GH_STATE. A test puts pull requests,
issues, comments, reviews, statuses, check runs and notifications there, runs the
source or the tracker against this script, and reads back every argv it was given.
It answers like gh does: `gh api` prints the JSON of one page after another with
`--paginate`, `-i` prints the status line and the headers first, and any status above
299, a 304 too, makes it exit 1 with `gh: ...` on stderr.

The test-side helper is :class:`GhState`; the script itself is :func:`main`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

LAST_MODIFIED = "Mon, 26 Oct 2026 10:00:00 GMT"


def state_path() -> Path:
    return Path(os.environ["FAKE_GH_STATE"])


def load() -> dict:
    path = state_path()
    return json.loads(path.read_text() or "{}") if path.exists() else {}


def save(state: dict) -> None:
    state_path().write_text(json.dumps(state, indent=2))


class GhState:
    """Reads and writes the fake's state file, for the tests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # The default time of a notification; None is this machine's clock. A test that
        # gives the source a fixed clock sets the same time here.
        self.now: float | None = None
        self.write({"login": "operator", "poll_interval": 60, "page_size": 100})

    def read(self) -> dict:
        return json.loads(self.path.read_text() or "{}")

    def write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state, indent=2))

    def update(self, change) -> None:
        state = self.read()
        change(state)
        self.write(state)

    def set(self, **values) -> None:
        self.update(lambda state: state.update(values))

    @property
    def calls(self) -> list[list[str]]:
        return self.read().get("calls", [])

    def clear_calls(self) -> None:
        self.set(calls=[])

    def notify(
        self, reason: str, ref: str, *, kind: str = "PullRequest", thread: str | None = None,
        updated_at: str | None = None, last_modified: str | None = None,
        unread: bool = True,
    ) -> None:
        """Add or update a notification thread, and move Last-Modified on.

        The thread is updated now unless *updated_at* says otherwise: the router drops an
        event that occurred before its subscriber started.
        """
        if updated_at is None:
            now = time.time() if self.now is None else self.now
            updated_at = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        owner_repo, number = ref.split("#")
        segment = "pulls" if kind == "PullRequest" else "issues"

        def change(state):
            threads = state.setdefault("notifications", [])
            thread_id = thread or str(len(threads) + 1)
            threads[:] = [t for t in threads if t["id"] != thread_id]
            threads.append(
                {
                    "id": thread_id,
                    "reason": reason,
                    "unread": unread,
                    "updated_at": updated_at,
                    "subject": {
                        "type": kind,
                        "url": f"https://api.github.com/repos/{owner_repo}/{segment}/{number}",
                    },
                    "repository": {
                        "full_name": owner_repo,
                        "html_url": f"https://github.com/{owner_repo}",
                    },
                }
            )
            state["last_modified"] = last_modified or f"Mon, 26 Oct 2026 10:{len(threads):02d}:00 GMT"

        self.update(change)

    def add_pull(
        self, ref: str, *, sha: str = "abc1234", state: str = "open",
        reviewers=("operator",), teams=(), branch: str | None = None, **extra,
    ) -> None:
        owner_repo, number = ref.split("#")
        head = {"sha": sha} if branch is None else {"sha": sha, "ref": branch}

        def change(data):
            data.setdefault("pulls", {})[ref] = {
                "number": int(number),
                "state": state,
                "html_url": f"https://github.com/{owner_repo}/pull/{number}",
                "head": head,
                "requested_reviewers": [{"login": login} for login in reviewers],
                "requested_teams": [{"id": team, "slug": f"team-{team}"} for team in teams],
                **extra,
            }

        self.update(change)

    def add_issue(self, ref: str, *, labels=(), assignees=("operator",), state="open", title="t"):
        owner_repo, number = ref.split("#")

        def change(data):
            data.setdefault("issues", {})[ref] = {
                "number": int(number),
                "title": title,
                "state": state,
                "html_url": f"https://github.com/{owner_repo}/issues/{number}",
                "url": f"https://api.github.com/repos/{owner_repo}/issues/{number}",
                "labels": [{"name": name} for name in labels],
                "assignees": [{"login": login} for login in assignees],
            }

        self.update(change)

    def add(self, collection: str, key: str, entry: dict) -> None:
        """Append to a list: issue_comments, review_comments, reviews (by ref), statuses,
        check_runs (by head sha)."""
        self.update(lambda data: data.setdefault(collection, {}).setdefault(key, []).append(entry))

    def labels_of(self, ref: str) -> list[str]:
        return [label["name"] for label in self.read()["issues"][ref]["labels"]]


# ----- the script ----------------------------------------------------------------


def fail(message: str, code: int = 1) -> int:
    sys.stderr.write(f"gh: {message}\n")
    return code


def emit_pages(values: list, page_size: int, paginate: bool, wrap: str | None = None) -> None:
    pages = [values[i : i + page_size] for i in range(0, len(values), page_size)] or [[]]
    if not paginate:
        pages = pages[:1]
    for page in pages:
        body = {"total_count": len(values), wrap: page} if wrap else page
        sys.stdout.write(json.dumps(body))


def since_filter(entries: list, query: dict, field: str) -> list:
    since = query.get("since", [None])[0]
    if not since:
        return entries
    return [e for e in entries if (e.get(field) or e.get("created_at") or "") >= since]


def notifications(state: dict, args: list[str], query: dict) -> int:
    """The notifications list. Like GitHub, an empty list has an ETag but no Last-Modified.

    Either validator answers 304 when it matches. The Date header is the state's `date`,
    or the fixed time the tests use.
    """
    headers = {}
    for index, arg in enumerate(args):
        if arg == "-H":
            name, _, value = args[index + 1].partition(":")
            headers[name.strip().lower()] = value.strip()
    body = since_filter(state.get("notifications", []), query, "updated_at")
    # Without all=true, GitHub lists the unread threads only.
    if query.get("all", ["false"])[0] != "true":
        body = [thread for thread in body if thread.get("unread", True)]
    text = json.dumps(body)
    etag = f'W/"{hashlib.sha1(text.encode()).hexdigest()}"'
    last_modified = state.get("last_modified", LAST_MODIFIED) if body else None
    interval = state.get("poll_interval", 60)
    date = state.get("date", LAST_MODIFIED)
    show = "-i" in args
    if headers.get("if-none-match") == etag or (
        last_modified and headers.get("if-modified-since") == last_modified
    ):
        if show:
            sys.stdout.write(
                f"HTTP/2.0 304 Not Modified\r\nDate: {date}\r\nEtag: {etag}\r\n"
                f"X-Poll-Interval: {interval}\r\n\r\n"
            )
        return fail("HTTP 304")
    if show:
        sys.stdout.write(
            f"HTTP/2.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
            f"Date: {date}\r\nEtag: {etag}\r\n"
            + (f"Last-Modified: {last_modified}\r\n" if last_modified else "")
            + f"X-Poll-Interval: {interval}\r\n\r\n"
            + text
        )
        return 0
    # Without -i, one page by `page` and `per_page`, newest first, as GitHub lists them.
    body = sorted(body, key=lambda thread: thread.get("updated_at") or "", reverse=True)
    size = int(query.get("per_page", [state.get("page_size", 100)])[0])
    if "--paginate" in args:
        emit_pages(body, size, True)
        return 0
    start = (int(query.get("page", ["1"])[0]) - 1) * size
    sys.stdout.write(json.dumps(body[start : start + size]))
    return 0


def search_review_requests(state: dict, query: dict, paginate: bool) -> int:
    """`search/issues` for `is:pr is:open review-requested:@me`, the only query asked.

    A pull request's `updated_at` defaults to the fixed time the tests use.
    """
    q = query.get("q", [""])[0]
    if q != "is:pr is:open review-requested:@me":
        return fail(f"the fake gh does not search for {q!r}")
    login = state.get("login", "operator")
    teams = set(state.get("teams") or ())
    found = []
    for ref, pull in state.get("pulls", {}).items():
        owner_repo, _ = ref.split("#")
        requested = {r["login"] for r in pull.get("requested_reviewers", [])}
        requested_teams = {t["id"] for t in pull.get("requested_teams", [])}
        if pull["state"] != "open" or (login not in requested and not requested_teams & teams):
            continue
        updated_at = pull.get("updated_at", "2026-10-26T10:00:00Z")
        found.append(
            {
                "number": pull["number"],
                "state": "open",
                "html_url": pull["html_url"],
                "repository_url": f"https://api.github.com/repos/{owner_repo}",
                "pull_request": {"url": f"https://api.github.com/repos/{owner_repo}/pulls/{pull['number']}"},
                "created_at": pull.get("created_at", updated_at),
                "updated_at": updated_at,
            }
        )
    emit_pages(found, int(state.get("page_size", 100)), paginate, "items")
    return 0


def api(state: dict, args: list[str]) -> int:
    paginate = "--paginate" in args
    positional, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-X", "-H"):
            skip = True
        elif not arg.startswith("-"):
            positional.append(arg)
    if not positional:
        return fail("no endpoint")
    parts = urlsplit(positional[0])
    path, query = parts.path.strip("/"), parse_qs(parts.query)
    page_size = int(state.get("page_size", 100))
    segments = path.split("/")

    if path == "user":
        sys.stdout.write(json.dumps({"login": state.get("login", "operator")}))
        return 0
    if path == "notifications":
        return notifications(state, args, query)
    if path == "search/issues":
        return search_review_requests(state, query, paginate)
    if path == "user/teams":
        if state.get("teams") is None:
            return fail("Resource not accessible by integration (HTTP 403)")
        emit_pages([{"id": team} for team in state["teams"]], page_size, paginate)
        return 0
    if len(segments) < 4 or segments[0] != "repos":
        return fail("Not Found (HTTP 404)")
    owner_repo = f"{segments[1]}/{segments[2]}"
    rest = segments[3:]

    if rest == ["issues"]:
        wanted = set(filter(None, unquote(query.get("labels", [""])[0]).split(",")))
        assignee = query.get("assignee", [None])[0]
        found = []
        for ref, issue in state.get("issues", {}).items():
            if not ref.startswith(owner_repo + "#") or issue["state"] != query.get("state", ["open"])[0]:
                continue
            names = {label["name"] for label in issue["labels"]}
            logins = {a["login"] for a in issue.get("assignees", [])}
            if wanted <= names and (assignee is None or assignee in logins):
                found.append(issue)
        found += state.get("extra_issue_list_entries", [])
        emit_pages(found, page_size, paginate)
        return 0

    if rest == ["pulls"]:
        # `head` is owner:branch; a pull request from this repository has its owner.
        head = unquote(query.get("head", [""])[0])
        found = [
            pull
            for ref, pull in state.get("pulls", {}).items()
            if ref.startswith(owner_repo + "#")
            and pull["state"] == query.get("state", ["open"])[0]
            and (not head or head == f"{segments[1]}:{pull['head'].get('ref')}")
        ]
        emit_pages(found, page_size, paginate)
        return 0

    if len(rest) >= 2 and rest[0] in ("pulls", "issues") and rest[1].isdigit():
        ref = f"{owner_repo}#{rest[1]}"
        pull = state.get("pulls", {}).get(ref)
        issue = state.get("issues", {}).get(ref)
        if len(rest) == 2:
            if rest[0] == "pulls":
                if pull is None:
                    return fail("Not Found (HTTP 404)")
                sys.stdout.write(json.dumps(pull))
                return 0
            if issue is None and pull is None:
                return fail("Not Found (HTTP 404)")
            if issue is None:
                issue = {
                    "number": pull["number"], "state": pull["state"],
                    "html_url": pull["html_url"], "pull_request": {}, "labels": [],
                }
            sys.stdout.write(json.dumps(issue))
            return 0
        collection = {
            ("pulls", "comments"): "review_comments",
            ("pulls", "reviews"): "reviews",
            ("issues", "comments"): "issue_comments",
        }.get((rest[0], rest[2]))
        if collection is None or (pull is None and issue is None):
            return fail("Not Found (HTTP 404)")
        entries = state.get(collection, {}).get(ref, [])
        if collection != "reviews":
            entries = since_filter(entries, query, "updated_at")
        emit_pages(entries, page_size, paginate)
        return 0

    if len(rest) == 3 and rest[0] == "commits":
        sha = rest[1]
        if rest[2] == "statuses":
            emit_pages(state.get("statuses", {}).get(sha, []), page_size, paginate)
            return 0
        if rest[2] == "check-runs":
            emit_pages(state.get("check_runs", {}).get(sha, []), page_size, paginate, "check_runs")
            return 0
    return fail("Not Found (HTTP 404)")


def issue_edit(state: dict, args: list[str]) -> int:
    number = args[2]
    repo = args[args.index("--repo") + 1]
    issue = state.get("issues", {}).get(f"{repo}#{number}")
    if issue is None:
        return fail(f"Could not resolve to an issue or pull request with the number of {number}.")
    if state.get("edit_lands") is False:
        return fail("HTTP 502: Bad Gateway") if state.get("edit_fails") else 0
    names = [label["name"] for label in issue["labels"]]
    for index, arg in enumerate(args):
        if arg == "--add-label" and args[index + 1] not in names:
            names.append(args[index + 1])
        if arg == "--remove-label":
            names = [n for n in names if n != args[index + 1]]
    issue["labels"] = [{"name": name} for name in names]
    if state.get("edit_fails"):
        return fail("HTTP 502: Bad Gateway")
    return 0


def pr_checkout(state: dict, args: list[str]) -> int:
    """`gh pr checkout <url|number> --detach`, in the current directory.

    The head is fetched from the pull request's `checkout_from` (a path that stands in
    for a fork) or from origin, by its branch, and checked out detached. A pull request
    with no branch leaves HEAD where it is, detached.
    """
    if "--detach" not in args:
        return fail("the fake gh checks pull requests out with --detach only")
    target = next((arg for arg in args[2:] if not arg.startswith("-")), "")
    pull = next(
        (
            pull for pull in state.get("pulls", {}).values()
            if target in (pull.get("html_url"), str(pull.get("number")))
        ),
        None,
    )
    if pull is None:
        return fail(f"no pull requests found for {target}")
    branch = (pull.get("head") or {}).get("ref")
    commands = [["git", "checkout", "--detach"]]
    if branch:
        commands = [
            ["git", "fetch", pull.get("checkout_from") or "origin", f"refs/heads/{branch}"],
            ["git", "checkout", "--detach", "FETCH_HEAD"],
        ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return fail(result.stderr.strip() or "git failed")
    return 0


def main(argv: list[str]) -> int:
    state = load()
    state.setdefault("calls", []).append(argv)
    save(state)
    for failure in state.get("fail", []):
        if failure["match"] in " ".join(argv):
            return fail(failure.get("stderr", "HTTP 500: Server Error"), failure.get("exit", 1))
    if argv[:1] == ["api"]:
        return api(state, argv[1:])
    if argv[:2] == ["issue", "edit"]:
        code = issue_edit(state, argv)
        save(state)
        return code
    if argv[:2] == ["pr", "checkout"]:
        return pr_checkout(state, argv)
    return fail(f"unknown command {' '.join(argv[:2])}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
