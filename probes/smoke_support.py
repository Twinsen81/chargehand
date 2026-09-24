#!/usr/bin/env python3
"""Scaffolding for the smoke-route probe: a scratch repo, a real tracker, a fault proxy.

Split out of ``smoke_route.py`` so the checks there read as checks. Nothing here decides
whether the runner behaved; it only builds the world the runner runs in and reports what
that world looks like afterwards.

Three pieces:

``Sandbox``
    A throwaway chargehand instance. Config, ledger, logs and worktrees all live under
    one directory, reached through the ``CHARGEHAND_*`` path overrides, so the probe can
    run on a machine that already has a real chargehand configured without touching it.
    The repository it launches into is a local clone of a local bare repository, so
    ``origin/main`` resolves and "does this worktree hold unpushed work" has a real
    answer rather than a degenerate one.

``TrackerAdmin``
    The calls a tracker adapter deliberately does not have. The runner can only move an
    issue between queue states; creating the issues to move, and deleting them
    afterwards, is the probe's own business and stays out of the package.

``FaultProxy``
    A pass-through in front of the tracker's API that can apply a mutation upstream and
    still report failure. That case is the reason the ledger separates an attempt's state
    from the queue status last verified, and it cannot be provoked from the outside any
    other way.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

LINEAR_ENDPOINT = "https://api.linear.app/graphql"

# Distinct from the defaults on purpose. A stray tick belonging to a real route must not
# be able to pick up a smoke issue, and this probe must not pick up a real one.
SMOKE_LABELS = {
    "queued": "chargehand-smoke",
    "running": "chargehand-smoke-running",
    "blocked": "chargehand-smoke-blocked",
}

# Cheap, deterministic, and slow enough that a tick immediately after the launch still
# catches the session working. It deliberately reads a file, so the turn is not so short
# that every run reports `done` before the first observation.
SMOKE_PROMPT = (
    "Read README.md in this directory, then write NOTES.md containing exactly two lines: "
    "the first is {issue}, the second is a one-sentence summary of README.md. "
    "Do not run any git commands. Stop when the file exists."
)


# ----- tracker administration ----------------------------------------------


class TrackerError(RuntimeError):
    pass


@dataclass
class IssueRef:
    id: str
    identifier: str
    url: str


class TrackerAdmin:
    """The setup and teardown half of a tracker, which the adapters deliberately lack."""

    def __init__(self, api_key: str, endpoint: str = LINEAR_ENDPOINT, timeout: float = 30.0):
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self._label_ids: dict[str, str] = {}

    def post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
                "User-Agent": "chargehand-smoke-probe",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
        except OSError as exc:
            raise TrackerError(f"request failed: {exc}")
        data = json.loads(body)
        if data.get("errors"):
            raise TrackerError("; ".join(str(e.get("message", e)) for e in data["errors"][:3]))
        return data["data"]

    def viewer(self) -> dict[str, str]:
        return self.post("query { viewer { id name email } }")["viewer"]

    def team(self, key: str) -> dict[str, str]:
        data = self.post(
            "query Team($key: String!) { teams(filter: {key: {eq: $key}}, first: 1) "
            "{ nodes { id key name } } }",
            {"key": key},
        )
        nodes = data["teams"]["nodes"]
        if not nodes:
            raise TrackerError(f"no team with key {key!r}")
        return nodes[0]

    def workflow_state(self, team_id: str, name: str) -> str:
        data = self.post(
            "query States($team: ID!) { workflowStates(filter: {team: {id: {eq: $team}}}, "
            "first: 50) { nodes { id name type } } }",
            {"team": team_id},
        )
        for node in data["workflowStates"]["nodes"]:
            if node["name"].lower() == name.lower():
                return node["id"]
        raise TrackerError(f"team has no workflow state named {name!r}")

    def ensure_label(self, team_id: str, name: str) -> str:
        if name in self._label_ids:
            return self._label_ids[name]
        data = self.post(
            "query Label($name: String!) { issueLabels(filter: {name: {eq: $name}}, first: 50) "
            "{ nodes { id name team { id } } } }",
            {"name": name},
        )
        for node in data["issueLabels"]["nodes"]:
            team = node.get("team") or {}
            if team.get("id") in (team_id, None):
                self._label_ids[name] = node["id"]
                return node["id"]
        created = self.post(
            "mutation MakeLabel($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) "
            "{ success issueLabel { id } } }",
            {"input": {"name": name, "teamId": team_id, "color": "#95a2b3"}},
        )["issueLabelCreate"]
        if not created["success"]:
            raise TrackerError(f"could not create label {name!r}")
        self._label_ids[name] = created["issueLabel"]["id"]
        return self._label_ids[name]

    def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        state_id: str,
        assignee_id: str,
        label_ids: list[str],
        description: str = "",
    ) -> IssueRef:
        data = self.post(
            "mutation Make($input: IssueCreateInput!) { issueCreate(input: $input) "
            "{ success issue { id identifier url } } }",
            {
                "input": {
                    "teamId": team_id,
                    "title": title,
                    "description": description,
                    "stateId": state_id,
                    "assigneeId": assignee_id,
                    "labelIds": label_ids,
                }
            },
        )["issueCreate"]
        if not data["success"]:
            raise TrackerError("issue creation reported failure")
        issue = data["issue"]
        return IssueRef(id=issue["id"], identifier=issue["identifier"], url=issue["url"])

    def issue(self, issue_id: str) -> dict[str, Any] | None:
        data = self.post(
            "query One($id: String!) { issue(id: $id) { id identifier url "
            "labels { nodes { name } } state { name type } } }",
            {"id": issue_id},
        )
        return data.get("issue")

    def labels_of(self, issue_id: str) -> list[str]:
        node = self.issue(issue_id)
        if node is None:
            return []
        return sorted(entry["name"] for entry in node["labels"]["nodes"])

    def set_state(self, issue_id: str, state_id: str) -> None:
        self.post(
            "mutation Move($id: String!, $state: String!) "
            "{ issueUpdate(id: $id, input: {stateId: $state}) { success } }",
            {"id": issue_id, "state": state_id},
        )

    def delete_issue(self, issue_id: str) -> bool:
        """Moves the issue to the workspace trash, where it is recoverable."""
        try:
            return bool(
                self.post(
                    "mutation Drop($id: String!) { issueDelete(id: $id) { success } }",
                    {"id": issue_id},
                )["issueDelete"]["success"]
            )
        except TrackerError:
            return False

    def queue(self, team_key: str, label: str, state: str | None) -> list[str]:
        """The queue exactly as the runner's adapter asks for it.

        Used to wait for a freshly created issue to become answerable *by that query*
        before a tick is allowed to look. A tick fired within a few hundred milliseconds
        of the creation call returning can miss it, which says nothing about the runner
        and everything about how fast the probe is driving the tracker.
        """
        issue_filter: dict[str, Any] = {
            "assignee": {"isMe": {"eq": True}},
            "labels": {"name": {"eq": label}},
            "team": {"key": {"eq": team_key}},
        }
        if state:
            issue_filter["state"] = {"name": {"eq": state}}
        data = self.post(
            "query Queued($filter: IssueFilter!) { issues(first: 50, filter: $filter) "
            "{ nodes { identifier } } }",
            {"filter": issue_filter},
        )
        return [node["identifier"] for node in data["issues"]["nodes"]]

    def issues_with_label(self, team_id: str, label: str) -> list[dict[str, Any]]:
        data = self.post(
            "query Tagged($team: ID!, $label: String!) { issues(first: 50, filter: "
            "{team: {id: {eq: $team}}, labels: {name: {eq: $label}}}) "
            "{ nodes { id identifier title } } }",
            {"team": team_id, "label": label},
        )
        return data["issues"]["nodes"]


# ----- fault injection ------------------------------------------------------


@dataclass
class Fault:
    """One scripted failure in front of the tracker's API.

    ``apply_upstream`` is the whole point: a mutation that reached the tracker and then
    reported failure is the case the ledger's separate ``label_state`` exists for, and no
    amount of unplugging the network produces it.
    """

    operation: str
    status: int = 502
    apply_upstream: bool = True
    times: int = 1
    used: int = 0

    def matches(self, operation: str) -> bool:
        return self.used < self.times and operation == self.operation


class FaultProxy:
    """A pass-through in front of the tracker, with a scripted fault plan."""

    def __init__(self, upstream: str = LINEAR_ENDPOINT, plan: list[Fault] | None = None):
        self.upstream = upstream
        self.plan: list[Fault] = plan or []
        self.calls: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("proxy is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/graphql"

    def arm(self, *faults: Fault) -> None:
        with self._lock:
            self.plan = list(faults)

    def start(self) -> str:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep the probe's output clean
                pass

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                status, payload = proxy.handle(body, self.headers.get("Authorization", ""))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def handle(self, body: bytes, authorization: str) -> tuple[int, bytes]:
        operation = operation_name(body)
        with self._lock:
            fault = next((f for f in self.plan if f.matches(operation)), None)
            if fault is not None:
                fault.used += 1
        if fault is None:
            status, payload = self._forward(body, authorization)
            self.calls.append({"operation": operation, "action": "forwarded", "status": status})
            return status, payload
        if fault.apply_upstream:
            upstream_status, _ = self._forward(body, authorization)
            self.calls.append(
                {
                    "operation": operation,
                    "action": "applied upstream, reported failure",
                    "upstream_status": upstream_status,
                    "status": fault.status,
                }
            )
        else:
            self.calls.append({"operation": operation, "action": "failed", "status": fault.status})
        return fault.status, json.dumps({"errors": [{"message": "injected fault"}]}).encode()

    def _forward(self, body: bytes, authorization: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.upstream,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": authorization,
                "User-Agent": "chargehand-smoke-probe",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except OSError as exc:
            return 599, json.dumps({"errors": [{"message": str(exc)}]}).encode()


def operation_name(body: bytes) -> str:
    """`query Queue($f: X!) { ... }` -> `Queue`; an anonymous document -> its keyword."""
    try:
        query = json.loads(body).get("query", "")
    except (ValueError, AttributeError):
        return "?"
    for line in query.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("query", "mutation")):
            continue
        keyword, _, rest = stripped.partition(" ")
        name = rest.strip().split("(")[0].split("{")[0].strip()
        return name or keyword
    return "?"


# ----- the sandbox ----------------------------------------------------------


@dataclass
class TickResult:
    returncode: int
    report: dict[str, Any] = field(default_factory=dict)
    stderr: str = ""

    def has(self, field_name: str, identifier: str) -> bool:
        return any(identifier in str(entry) for entry in self.report.get(field_name, []))


class Sandbox:
    """A throwaway chargehand instance, isolated by the path overrides it was given."""

    def __init__(
        self,
        root: Path,
        *,
        team: str,
        labels: dict[str, str] | None = None,
        prompt: str = SMOKE_PROMPT,
        permission_mode: str = "auto",
        endpoint: str | None = None,
        keychain_service: str | None = None,
        keychain_account: str | None = None,
        max_concurrent: int = 1,
        state_filter: str | None = "Todo",
    ):
        self.root = root
        self.team = team
        self.labels = dict(labels or SMOKE_LABELS)
        self.prompt = prompt
        self.permission_mode = permission_mode
        self.endpoint = endpoint
        self.keychain_service = keychain_service
        self.keychain_account = keychain_account
        self.max_concurrent = max_concurrent
        self.state_filter = state_filter

    # ----- layout -----------------------------------------------------------

    @property
    def origin(self) -> Path:
        return self.root / "origin.git"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def worktree_root(self) -> Path:
        return self.root / "worktrees"

    @property
    def config_file(self) -> Path:
        return self.root / "config" / "config.toml"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def ledger_file(self) -> Path:
        return self.state_dir / "ledger.sqlite"

    @property
    def notify_log(self) -> Path:
        return self.root / "notifications.jsonl"

    @property
    def marker_file(self) -> Path:
        """Proof that this directory is a sandbox and not somebody's work."""
        return self.root / ".chargehand-smoke-sandbox"

    @property
    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "CHARGEHAND_CONFIG": str(self.config_file),
            "CHARGEHAND_STATE_DIR": str(self.state_dir),
            "CHARGEHAND_LOG_FILE": str(self.root / "chargehand.log"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }

    # ----- construction -----------------------------------------------------

    def build(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker_file.write_text(
            "Written by probes/smoke_route.py. Its presence is what allows this directory "
            "to be deleted and rebuilt; without it the probe refuses to touch the path.\n",
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self._build_repo()
        self._write_notify_hook()
        self.config_file.write_text(self._machine_config(), encoding="utf-8")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _build_repo(self) -> None:
        """A local bare remote and a clone of it.

        `base = "origin/main"` has to resolve, `git fetch origin` has to work, and
        "does this worktree hold unpushed work" has to have a real answer - which a
        repository with no remote cannot give.
        """
        seed = self.root / "seed"
        self._git("init", "--quiet", "--bare", "-b", "main", str(self.origin))
        self._git("init", "--quiet", "-b", "main", str(seed))
        self._git("config", "user.email", "smoke@example.invalid", cwd=seed)
        self._git("config", "user.name", "chargehand smoke probe", cwd=seed)
        (seed / "README.md").write_text(
            "# scratch\n\nA throwaway repository the chargehand smoke probe launches into.\n",
            encoding="utf-8",
        )
        (seed / ".chargehand.toml").write_text(self._repo_config(), encoding="utf-8")
        self._git("add", "-A", cwd=seed)
        self._git("commit", "--quiet", "-m", "seed the scratch repository", cwd=seed)
        self._git("remote", "add", "origin", str(self.origin), cwd=seed)
        self._git("push", "--quiet", "origin", "main", cwd=seed)
        shutil.rmtree(seed)
        self._git("clone", "--quiet", str(self.origin), str(self.repo))
        self._git("config", "user.email", "smoke@example.invalid", cwd=self.repo)
        self._git("config", "user.name", "chargehand smoke probe", cwd=self.repo)

    def _repo_config(self) -> str:
        return (
            "base = \"origin/main\"\n"
            f"prompt = {json.dumps(self.prompt)}\n"
            f"permission_mode = {json.dumps(self.permission_mode)}\n"
            "branch_prefix = \"chargehand/\"\n"
        )

    def _write_notify_hook(self) -> None:
        hook = self.root / "notify.sh"
        hook.write_text(
            "#!/bin/sh\n"
            "# Records what the runner would have sent, so the probe can assert on it.\n"
            'printf \'{"issue":"%s","state":"%s","url":"%s","route":"%s","detail":"%s"}\\n\' \\\n'
            '  "$CHARGEHAND_ISSUE" "$CHARGEHAND_STATE" "$CHARGEHAND_URL" '
            '"$CHARGEHAND_ROUTE" "$CHARGEHAND_DETAIL" \\\n'
            f'  >> "{self.notify_log}"\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)

    def _machine_config(self) -> str:
        tracker: dict[str, Any] = {"type": "linear", "team": self.team}
        if self.state_filter:
            tracker["state"] = self.state_filter
        if self.endpoint:
            tracker["endpoint"] = self.endpoint
        if self.keychain_service:
            tracker["keychain_service"] = self.keychain_service
        if self.keychain_account:
            tracker["keychain_account"] = self.keychain_account
        entries = ", ".join(f"{key} = {json.dumps(value)}" for key, value in tracker.items())
        return (
            "# Written by the smoke probe. Throwaway.\n"
            "poll_interval_secs = 60\n"
            f"max_concurrent = {self.max_concurrent}\n"
            "max_open_attempts = 4\n"
            "max_run_hours = 6\n"
            "hard_stop_hours = 10\n"
            "min_free_disk_gb = 1\n"
            f"worktree_root = {json.dumps(str(self.worktree_root))}\n"
            "\n[notify]\n"
            f"command = {json.dumps(str(self.root / 'notify.sh'))}\n"
            "\n[labels]\n"
            f"queued = {json.dumps(self.labels['queued'])}\n"
            f"running = {json.dumps(self.labels['running'])}\n"
            f"blocked = {json.dumps(self.labels['blocked'])}\n"
            "\n[[route]]\n"
            "name = \"smoke\"\n"
            f"repo = {json.dumps(str(self.repo))}\n"
            f"tracker = {{ {entries} }}\n"
        )

    def rewrite_config(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            setattr(self, key, value)
        self.config_file.write_text(self._machine_config(), encoding="utf-8")

    def set_repo_prompt(self, prompt: str | None) -> None:
        """Swap the repository's prompt between checks.

        The runner reads `.chargehand.toml` from the clone, not from the worktree, so
        this takes effect on the next launch without a commit. Checks that need a run to
        still be working when a control command reaches it use a longer one.
        """
        self.prompt = prompt or SMOKE_PROMPT
        (self.repo / ".chargehand.toml").write_text(self._repo_config(), encoding="utf-8")

    # ----- driving it -------------------------------------------------------

    def cli(self, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "chargehand", *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
            check=False,
        )

    def tick(self, *, crash_after_step: int | None = None, timeout: float = 300.0) -> TickResult:
        args = ["tick", "--json"]
        if crash_after_step is not None:
            args += ["--crash-after-step", str(crash_after_step)]
        result = self.cli(*args, timeout=timeout)
        try:
            report = json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            report = {}
        return TickResult(result.returncode, report, result.stderr.strip())

    def status(self, *, verbose: bool = True) -> dict[str, Any]:
        args = ["status", "--json"]
        if verbose:
            args.append("--verbose-titles")
        result = self.cli(*args)
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {"error": result.stderr.strip()}

    # ----- inspection -------------------------------------------------------

    def attempts(self) -> list[dict[str, Any]]:
        """Read the ledger directly: the probe asserts on columns `status` does not show."""
        if not self.ledger_file.exists():
            return []
        connection = sqlite3.connect(f"file:{self.ledger_file}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("SELECT * FROM attempts ORDER BY id").fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def attempt(self, identifier: str) -> dict[str, Any] | None:
        rows = [row for row in self.attempts() if row["identifier"] == identifier]
        return rows[-1] if rows else None

    def notifications(self) -> list[dict[str, str]]:
        if not self.notify_log.exists():
            return []
        entries = []
        for line in self.notify_log.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def worktrees(self) -> dict[str, str]:
        output = self._git("worktree", "list", "--porcelain", cwd=self.repo)
        found: dict[str, str] = {}
        path = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                path = line.split(" ", 1)[1]
            elif line.startswith("branch ") and path:
                found[path] = line.split(" ", 1)[1].removeprefix("refs/heads/")
        return found

    def branches(self) -> list[str]:
        return self._git("branch", "--format=%(refname:short)", cwd=self.repo).splitlines()


# ----- session observation --------------------------------------------------


def list_sessions(claude_bin: str = "claude") -> list[dict[str, Any]]:
    result = subprocess.run(
        [claude_bin, "agents", "--json", "--all"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def session_under(worktree: Path, claude_bin: str = "claude") -> dict[str, Any] | None:
    target = str(worktree.resolve())
    for entry in list_sessions(claude_bin):
        cwd = entry.get("cwd")
        if cwd and str(Path(cwd).resolve()) == target:
            return entry
    return None


def wait_for_session(
    worktree: Path,
    states: tuple[str, ...],
    *,
    timeout: float = 240.0,
    interval: float = 3.0,
    claude_bin: str = "claude",
) -> dict[str, Any] | None:
    """Poll until the session under *worktree* reports one of *states*, or time runs out."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        entry = session_under(worktree, claude_bin)
        if entry is not None:
            last = entry
            if (entry.get("state") or entry.get("status")) in states:
                return entry
        time.sleep(interval)
    return last


def remove_sessions_under(root: Path, claude_bin: str = "claude") -> list[str]:
    """Stop and remove every session whose working directory is inside *root*.

    Scoped by path rather than by a recorded list of ids on purpose: a session started
    by a launch that was then interrupted never got recorded anywhere, and leaving it
    running after the probe exits is exactly the failure the probe is testing for.
    """
    removed = []
    try:
        resolved_root = root.resolve()
    except OSError:
        return removed
    for entry in list_sessions(claude_bin):
        cwd = entry.get("cwd")
        if not cwd:
            continue
        try:
            Path(cwd).resolve().relative_to(resolved_root)
        except (ValueError, OSError):
            continue
        session_id = entry.get("id") or entry.get("sessionId")
        if not session_id:
            continue
        subprocess.run([claude_bin, "stop", session_id], capture_output=True, check=False)
        subprocess.run([claude_bin, "rm", session_id], capture_output=True, check=False)
        removed.append(session_id)
    return removed


def is_sandbox(root: Path) -> bool:
    """Whether *root* is a directory a previous run of this probe built.

    The probe deletes and rebuilds whatever `--root` names. A mistyped path would
    otherwise take the answer with it, and this tool refuses to destroy unpushed work
    everywhere else it might.
    """
    return (root / ".chargehand-smoke-sandbox").is_file()


def keychain_key(service: str, account: str | None = None) -> str | None:
    argv = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        argv[2:2] = ["-a", account]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    return result.stdout.strip() or None if result.returncode == 0 else None


__all__ = [
    "Fault",
    "FaultProxy",
    "IssueRef",
    "Sandbox",
    "SMOKE_LABELS",
    "SMOKE_PROMPT",
    "TickResult",
    "TrackerAdmin",
    "TrackerError",
    "keychain_key",
    "list_sessions",
    "operation_name",
    "remove_sessions_under",
    "session_under",
    "wait_for_session",
]

