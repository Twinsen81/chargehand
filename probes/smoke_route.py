#!/usr/bin/env python3
"""Run the whole launch-and-control sequence against a real tracker and real Claude Code.

The test suite asserts all of this against a fake `claude` and a fake tracker. What this
probe checks is whether those fakes were faithful: every claim below is one the suite
already makes, re-made against the real thing, on a scratch repository with a cheap
prompt so proving the mechanics costs neither a real repository nor a real task.

What it covers
--------------

``happy``
    Label an issue and, within one tick, the ledger row is complete, the label has
    swapped, the worktree exists and the session is supervised.

``crash``
    ``tick --crash-after-step N`` for every step a launch can die at, confirming the next
    tick adopts, resumes, or fails loudly, and that neither a stranded label nor an
    unsupervised session is left behind.

``ambiguous``
    A label write that reports failure after it has already been applied, and a
    verification read that fails after a write landed. These are the cases the ledger's
    separate ``label_state`` column exists for; they cannot be provoked from outside, so
    a proxy in front of the tracker applies the mutation upstream and reports failure
    anyway.

``control``
    ``pause``, ``resume``, ``stop``, ``continue``, ``cancel``, ``retry`` and ``discard``,
    each checked against the ledger, the tracker's labels and ``claude agents`` - plus a
    cancel recorded while a launch is interrupted mid-sequence.

``states``
    What a background session actually reports at the end of a turn, across several
    shapes of ending: finished, asked a direct question, declared itself stuck, parked on
    a permission prompt. The runner maps ``state`` onto the ledger and clears labels on
    the strength of it, so the vocabulary has to be pinned by observation rather than
    assumption.

Running it
----------

    python3 probes/smoke_route.py --team ABC
    python3 probes/smoke_route.py --team ABC --only happy,crash
    python3 probes/smoke_route.py --team ABC --keep --json

It creates issues on the team it is pointed at, assigned to whoever owns the API key, and
deletes them afterwards unless ``--keep`` is passed. It starts real background sessions,
so it costs a small amount of usage. Everything else it touches - configuration, ledger,
logs, repository, worktrees - lives under one throwaway directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_support import (  # noqa: E402
    SMOKE_LABELS,
    Fault,
    FaultProxy,
    IssueRef,
    Sandbox,
    TrackerAdmin,
    TrackerError,
    keychain_key,
    remove_sessions_under,
    session_under,
    wait_for_session,
)

PASS = "pass"
FAIL = "FAIL"
INFO = "info"
SKIP = "skip"

# Long enough that a session is still working when the next control command reaches it.
# `stop` and `continue` say nothing useful about a session that already finished.
LONG_PROMPT = (
    "Read README.md in this directory. Then create five files, one at a time: "
    "note1.md, note2.md, note3.md, note4.md and note5.md. Each must contain one line "
    "summarising README.md. Do not run any git commands."
)

SECTIONS = ("happy", "crash", "ambiguous", "control", "states")


@dataclass
class Check:
    section: str
    name: str
    verdict: str
    detail: str = ""
    observations: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "name": self.name,
            "verdict": self.verdict,
            "detail": self.detail,
            "observations": self.observations,
        }


class Probe:
    def __init__(self, sandbox: Sandbox, admin: TrackerAdmin, *, team: str, keep: bool):
        self.sandbox = sandbox
        self.admin = admin
        self.team_key = team
        self.keep = keep
        self.checks: list[Check] = []
        self.session_samples: list[dict[str, Any]] = []
        self.issues: list[IssueRef] = []
        # Every identifier this run created, including ones already torn down. The stray
        # checks are scoped to it: an unrelated issue that happens to carry a smoke label
        # is someone else's problem, and a previous case's leftovers must not be read as
        # this case's failure.
        self.mine: set[str] = set()
        # How long each created issue took to become answerable by the runner's own queue
        # query. Reported because a tracker that indexes slowly is a real constraint on
        # how soon after a label a tick can usefully run.
        self.queue_delays: list[float] = []
        self.team_id = ""
        self.state_id = ""
        self.viewer = ""
        self.label_ids: dict[str, str] = {}

    # ----- recording --------------------------------------------------------

    def record(self, section: str, name: str, verdict: str, detail: str = "", **observations):
        check = Check(section, name, verdict, detail, observations)
        self.checks.append(check)
        marker = {PASS: "  ok  ", FAIL: " FAIL ", INFO: " note ", SKIP: " skip "}[verdict]
        print(f"[{marker}] {section}/{name}" + (f" - {detail}" if detail else ""), flush=True)
        return check

    def expect(self, section: str, name: str, condition: bool, detail: str = "", **observations):
        return self.record(section, name, PASS if condition else FAIL, detail, **observations)

    def sample_session(self, label: str, entry: dict[str, Any] | None, expectation: str = ""):
        """Every run that ends a turn contributes to the session-state question."""
        sample = {
            "label": label,
            "expected": expectation,
            "state": (entry or {}).get("state"),
            "status": (entry or {}).get("status"),
            "waitingFor": (entry or {}).get("waitingFor"),
        }
        self.session_samples.append(sample)
        return sample

    # ----- tracker fixtures -------------------------------------------------

    def connect(self) -> None:
        viewer = self.admin.viewer()
        self.viewer = viewer.get("email") or viewer.get("name") or viewer["id"]
        team = self.admin.team(self.team_key)
        self.team_id = team["id"]
        self.state_id = self.admin.workflow_state(self.team_id, "Todo")
        for key, name in self.sandbox.labels.items():
            self.label_ids[key] = self.admin.ensure_label(self.team_id, name)
        print(
            f"tracker: {team['key']} ({team['name']}), assignee {self.viewer}, "
            f"labels {', '.join(self.sandbox.labels.values())}",
            flush=True,
        )
        self.assignee_id = viewer["id"]

    def preflight(self) -> bool:
        """Refuse to start on top of a previous run's leftovers."""
        stale = []
        for key in ("queued", "running", "blocked"):
            stale += self.admin.issues_with_label(self.team_id, self.sandbox.labels[key])
        if stale:
            names = ", ".join(entry["identifier"] for entry in stale)
            self.record(
                "setup", "no leftovers on the tracker", FAIL,
                f"issues already carry a smoke label: {names}",
            )
            return False
        self.record("setup", "no leftovers on the tracker", PASS)
        return True

    def new_issue(self, title: str, *, queued: bool = True) -> IssueRef:
        issue = self.admin.create_issue(
            team_id=self.team_id,
            title=f"smoke: {title}",
            state_id=self.state_id,
            assignee_id=self.assignee_id,
            label_ids=[self.label_ids["queued"]] if queued else [],
            description=(
                "Created by the chargehand smoke probe to exercise the runner against a "
                "real tracker. Safe to delete."
            ),
        )
        self.issues.append(issue)
        self.mine.add(issue.identifier)
        if queued:
            self.wait_until_queued(issue)
        return issue

    def wait_until_queued(self, issue: IssueRef, timeout: float = 30.0) -> bool:
        """Hold until the tracker will answer the runner's own queue query with *issue*.

        Measured on Linear: an issue is not returned by a filtered query for a couple of
        hundred milliseconds after the creation call has already returned its identifier.
        A tick fired inside that window admits nothing, which is a fact about how fast
        the probe drives the tracker, not about the runner. Waiting here keeps the checks
        below about the runner.
        """
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            if issue.identifier in self.admin.queue(
                self.team_key, self.sandbox.labels["queued"], self.sandbox.state_filter
            ):
                self.queue_delays.append(time.monotonic() - started)
                return True
            time.sleep(0.25)
        self.record("setup", f"{issue.identifier} reached the queue", FAIL,
                    f"still not answerable by the runner's query after {timeout:.0f}s")
        return False

    def labels_of(self, issue: IssueRef) -> list[str]:
        managed = set(self.sandbox.labels.values())
        return sorted(set(self.admin.labels_of(issue.id)) & managed)

    def label_key(self, issue: IssueRef) -> str:
        names = self.labels_of(issue)
        if len(names) != 1:
            return f"{names}"
        for key, name in self.sandbox.labels.items():
            if name == names[0]:
                return key
        return names[0]

    # ----- shared assertions ------------------------------------------------

    def assert_no_strays(self, section: str) -> None:
        """The invariant every fault-injection case shares.

        A stranded label is an issue the tracker says is running that nothing is working
        on. An unsupervised session is one under the worktree root that no live ledger
        row owns. Either one means a crash left the world inconsistent.
        """
        running = self.admin.issues_with_label(self.team_id, self.sandbox.labels["running"])
        live = {
            row["identifier"]
            for row in self.sandbox.attempts()
            if row["state"] in ("launching", "running", "blocked", "paused")
        }
        stranded = [
            entry["identifier"]
            for entry in running
            if entry["identifier"] in self.mine and entry["identifier"] not in live
        ]
        self.expect(
            section, "no stranded running label", not stranded,
            f"stranded: {stranded}" if stranded else "",
            stranded=stranded,
        )

        owned = {row["worktree"] for row in self.sandbox.attempts() if row["state"] != "cancelled"}
        unsupervised = []
        for entry in _sessions_under(self.sandbox.worktree_root):
            cwd = entry.get("cwd") or ""
            if cwd not in owned and (entry.get("state") or entry.get("status")) in (
                "working", "busy"
            ):
                unsupervised.append(entry.get("id") or entry.get("sessionId"))
        self.expect(
            section, "no unsupervised session", not unsupervised,
            f"unsupervised: {unsupervised}" if unsupervised else "",
            unsupervised=unsupervised,
        )

    def teardown_issue(self, issue: IssueRef, *, discard: bool = True) -> None:
        """Take a case's issue out of the world before the next case starts.

        The wait at the end is not politeness. Every case shares one team and one set of
        labels, so an issue still answering a label query after its case ended is
        indistinguishable from the next case's own stray, and would fail a check that has
        nothing wrong with it.
        """
        if discard and self.sandbox.attempt(issue.identifier):
            self.sandbox.cli("discard", issue.identifier, "--yes", "--force", "--wait", "60")
        if self.keep:
            return
        self.admin.delete_issue(issue.id)
        self.issues = [entry for entry in self.issues if entry.id != issue.id]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not any(
                issue.identifier in [e["identifier"] for e in
                                     self.admin.issues_with_label(self.team_id, label)]
                for label in self.sandbox.labels.values()
            ):
                self.mine.discard(issue.identifier)
                return
            time.sleep(2)
        self.record("teardown", f"{issue.identifier} left the queue", INFO,
                    "it still answers a label query 30s after deletion")

    # ----- section: the happy path ------------------------------------------

    def section_happy(self) -> None:
        section = "happy"
        issue = self.new_issue("happy path")
        result = self.sandbox.tick()
        self.expect(section, "tick launched the issue", result.has("launched", issue.identifier),
                    f"report: {result.report.get('launched')} {result.report.get('errors')}",
                    report=result.report)

        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "ledger row is complete",
                    row.get("state") == "running" and row.get("step") == 4
                    and bool(row.get("session_id")),
                    f"state={row.get('state')} step={row.get('step')} "
                    f"session={row.get('session_id')}",
                    row={k: row.get(k) for k in
                         ("state", "step", "session_id", "label_state", "session_state")})
        self.expect(section, "label_state records a verified write",
                    row.get("label_state") == "running", f"label_state={row.get('label_state')}")
        self.expect(section, "the label swapped on the tracker",
                    self.label_key(issue) == "running", f"labels={self.labels_of(issue)}")

        worktree = _worktree_of(row) or Path("/nonexistent")
        self.expect(section, "the worktree exists", worktree.is_dir(), str(worktree))
        self.expect(section, "the worktree is a linked worktree of the repo",
                    str(worktree) in self.sandbox.worktrees(),
                    f"worktrees={list(self.sandbox.worktrees())}")

        entry = session_under(worktree)
        self.expect(section, "a session is running in the worktree", entry is not None,
                    f"session={entry}")
        self.expect(section, "the launch recorded the session as working",
                    row.get("session_state") in ("working", "done"),
                    f"session_state={row.get('session_state')}")

        # Claude Code documents that a background session started inside a linked
        # worktree skips its own worktree isolation. The runner depends on that and has
        # never verified it: an extra worktree here would mean the session is working
        # somewhere the runner will not collect, and the file it writes lands elsewhere.
        before = set(self.sandbox.worktrees())
        finished = wait_for_session(worktree, ("done", "blocked", "failed"), timeout=300)
        self.sample_session("runner happy path", finished, "done")
        after = set(self.sandbox.worktrees())
        self.expect(section, "no nested worktree appeared", before == after,
                    f"added: {sorted(after - before)}", worktrees=sorted(after))
        self.expect(section, "the session did its work in that worktree",
                    (worktree / "NOTES.md").exists(),
                    f"files={sorted(p.name for p in worktree.iterdir())}")

        result = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        # With no status file configured, `done` is deliberately not treated as success:
        # a session that asked its question as plain text reads the same way.
        self.expect(section, "a bare `done` parks the run rather than closing it",
                    row.get("state") == "blocked",
                    f"state={row.get('state')} session_state={row.get('session_state')}")
        self.expect(section, "the blocked label followed", self.label_key(issue) == "blocked",
                    f"labels={self.labels_of(issue)}")
        states = [entry["state"] for entry in self.sandbox.notifications()]
        self.expect(section, "the operator was notified", bool(states), f"notifications={states}")
        self.assert_no_strays(section)
        self.teardown_issue(issue)

    # ----- section: fault injection -----------------------------------------

    def section_crash(self) -> None:
        for step in (0, 1, 2, 3):
            self._crash_case(step)

    def _crash_case(self, step: int) -> None:
        section = "crash"
        issue = self.new_issue(f"crash after step {step}")
        first = self.sandbox.tick(crash_after_step=step)
        self.expect(section, f"step {step}: the tick died mid-launch",
                    first.returncode != 0 and "aborted" in first.stderr.lower(),
                    f"rc={first.returncode} stderr={first.stderr[:120]}",
                    report=first.report)

        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, f"step {step}: the row survives as an interrupted launch",
                    row.get("state") == "launching" and row.get("step") == step,
                    f"state={row.get('state')} step={row.get('step')}")
        expected_label = "queued" if step < 1 else "running"
        self.expect(section, f"step {step}: the label matches how far the launch got",
                    self.label_key(issue) == expected_label,
                    f"labels={self.labels_of(issue)}, expected {expected_label}")
        worktree = _worktree_of(row)
        exists = worktree is not None and worktree.is_dir()
        self.expect(section, f"step {step}: the worktree exists only past step 2",
                    exists == (step >= 2), f"worktree={worktree} exists={exists}")

        second = self.sandbox.tick()
        self.expect(section, f"step {step}: the next tick resumed the launch",
                    second.has("resumed", issue.identifier) or second.has("adopted",
                                                                          issue.identifier),
                    f"resumed={second.report.get('resumed')} "
                    f"adopted={second.report.get('adopted')} "
                    f"errors={second.report.get('errors')}",
                    report=second.report)
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, f"step {step}: the attempt completed",
                    row.get("state") == "running" and row.get("step") == 4
                    and bool(row.get("session_id")),
                    f"state={row.get('state')} step={row.get('step')} "
                    f"session={row.get('session_id')}")
        self.expect(section, f"step {step}: the launch counter advanced once",
                    row.get("launch_attempts") == 2,
                    f"launch_attempts={row.get('launch_attempts')}")
        self.expect(section, f"step {step}: the running label is set",
                    self.label_key(issue) == "running", f"labels={self.labels_of(issue)}")
        self.expect(section, f"step {step}: exactly one attempt exists for the issue",
                    len([r for r in self.sandbox.attempts()
                         if r["identifier"] == issue.identifier]) == 1)
        worktree = _worktree_of(row)
        entry = session_under(worktree) if worktree else None
        self.expect(section, f"step {step}: the session is supervised", entry is not None,
                    f"session={entry}")
        self.assert_no_strays(section)
        if worktree is not None:
            self.sample_session(
                f"runner resumed from step {step}",
                wait_for_session(worktree, ("done", "blocked", "failed"), timeout=300),
                "done",
            )
        self.teardown_issue(issue)

    # ----- section: ambiguous tracker writes --------------------------------

    def section_ambiguous(self) -> None:
        section = "ambiguous"
        proxy = FaultProxy()
        proxy.start()
        original_endpoint = self.sandbox.endpoint
        self.sandbox.rewrite_config(endpoint=proxy.url)
        try:
            self._ambiguous_reported_failure(proxy)
            self._ambiguous_partial_write(proxy)
            self._ambiguous_unverifiable(proxy)
        finally:
            proxy.stop()
            self.sandbox.rewrite_config(endpoint=original_endpoint)

    def _ambiguous_reported_failure(self, proxy: FaultProxy) -> None:
        """The stubbed 502 from the checklist: applied upstream, reported as failed."""
        section = "ambiguous"
        # The label swap is an add followed by a remove. Faulting the remove is what
        # leaves the tracker in the state the write intended while the caller is told it
        # failed - faulting the add would leave both labels on and is the next case.
        proxy.arm(Fault(operation="Remove", status=502, apply_upstream=True, times=1))
        issue = self.new_issue("tracker write reported failure but landed")
        result = self.sandbox.tick()
        applied = [call for call in proxy.calls if call["action"].startswith("applied")]
        self.expect(section, "the fault was injected after the mutation reached the tracker",
                    bool(applied), f"proxy={applied}")
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "a write that landed is accepted on its re-read",
                    row.get("state") == "running" and row.get("step") == 4,
                    f"state={row.get('state')} step={row.get('step')} "
                    f"errors={result.report.get('errors')} "
                    f"skipped={result.report.get('skipped')}",
                    report=result.report, proxy=list(proxy.calls))
        self.expect(section, "the verified label state is recorded",
                    row.get("label_state") == "running", f"label_state={row.get('label_state')}")
        self.expect(section, "the tracker carries only the running label",
                    self.label_key(issue) == "running", f"labels={self.labels_of(issue)}")
        self.assert_no_strays(section)
        self.teardown_issue(issue)

    def _ambiguous_partial_write(self, proxy: FaultProxy) -> None:
        """Half the swap applied: the add landed, the remove never ran."""
        section = "ambiguous"
        proxy.arm(Fault(operation="Add", status=502, apply_upstream=True, times=1))
        issue = self.new_issue("tracker write applied half the swap")
        first = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "an unverifiable write holds the launch at step 0",
                    row.get("state") == "launching" and row.get("step") == 0,
                    f"state={row.get('state')} step={row.get('step')} "
                    f"skipped={first.report.get('skipped')}",
                    report=first.report, proxy=list(proxy.calls))
        self.expect(section, "the reason is recorded on the attempt",
                    bool(row.get("last_error")), f"last_error={row.get('last_error')}")
        self.expect(section, "the tick reported the ambiguity",
                    bool(first.report.get("errors")), f"errors={first.report.get('errors')}")
        second = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "the next tick settles it and completes the launch",
                    row.get("state") == "running" and row.get("step") == 4,
                    f"state={row.get('state')} step={row.get('step')} "
                    f"errors={second.report.get('errors')}")
        self.expect(section, "the tracker ends with only the running label",
                    self.label_key(issue) == "running", f"labels={self.labels_of(issue)}")
        self.assert_no_strays(section)
        self.teardown_issue(issue)

    def _ambiguous_unverifiable(self, proxy: FaultProxy) -> None:
        """Both mutations landed, and the read that would prove it failed."""
        section = "ambiguous"
        proxy.arm(Fault(operation="Issue", status=502, apply_upstream=False, times=1))
        issue = self.new_issue("verification read failed after the write landed")
        first = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "an unverifiable write is not assumed to have failed",
                    row.get("state") == "launching" and row.get("label_state") is None,
                    f"state={row.get('state')} label_state={row.get('label_state')} "
                    f"skipped={first.report.get('skipped')}",
                    report=first.report, proxy=list(proxy.calls))
        self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "the re-read on the next tick completes the launch",
                    row.get("state") == "running" and row.get("label_state") == "running",
                    f"state={row.get('state')} label_state={row.get('label_state')}")
        self.assert_no_strays(section)
        self.teardown_issue(issue)

    # ----- section: the control surface -------------------------------------

    def section_control(self) -> None:
        section = "control"
        self.sandbox.set_repo_prompt(LONG_PROMPT)
        try:
            self._control_pause(section)
            issue = self._control_lifecycle(section)
            if issue is not None:
                self.teardown_issue(issue)
            self._control_cancel_in_flight(section)
        finally:
            self.sandbox.set_repo_prompt(None)

    def _control_pause(self, section: str) -> None:
        paused = self.sandbox.cli("pause", "--route", "smoke", "--wait", "60")
        status = self.sandbox.status()
        route = (status.get("routes") or [{}])[0]
        self.expect(section, "pause stops admission", route.get("paused") is True,
                    f"rc={paused.returncode} route={route.get('paused')}")

        issue = self.new_issue("admission while paused")
        result = self.sandbox.tick()
        self.expect(section, "a paused route admits nothing",
                    not result.has("launched", issue.identifier)
                    and self.sandbox.attempt(issue.identifier) is None,
                    f"skipped={result.report.get('skipped')}")

        self.sandbox.cli("resume", "--route", "smoke", "--wait", "60")
        status = self.sandbox.status()
        route = (status.get("routes") or [{}])[0]
        self.expect(section, "resume restores admission", route.get("paused") is False,
                    f"route={route.get('paused')}")
        # `resume` records a request and runs a tick to apply it, and that same tick
        # admits. So the launch may already have happened before the probe ticks again;
        # what matters is that the held issue is running, not which tick started it.
        result = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "the held issue launches once resumed",
                    row.get("state") == "running" and row.get("step") == 4,
                    f"state={row.get('state')} step={row.get('step')} "
                    f"launched={result.report.get('launched')} "
                    f"errors={result.report.get('errors')}")
        self.teardown_issue(issue)

    def _control_lifecycle(self, section: str) -> IssueRef | None:
        issue = self.new_issue("stop, continue, cancel, retry, discard")
        launch = self.sandbox.tick()
        if not launch.has("launched", issue.identifier):
            self.record(section, "lifecycle setup", FAIL,
                        f"the run did not start: {launch.report.get('errors')}")
            return issue
        row = self.sandbox.attempt(issue.identifier) or {}
        worktree = Path(row["worktree"])
        first_attempt_id = row["id"]

        stopped = self.sandbox.cli("stop", issue.identifier, "--wait", "120")
        row = self.sandbox.attempt(issue.identifier) or {}
        entry = session_under(worktree)
        self.expect(section, "stop pauses the run and the session",
                    row.get("state") == "paused"
                    and (entry or {}).get("state") in ("stopped", "done", None),
                    f"rc={stopped.returncode} state={row.get('state')} "
                    f"session={(entry or {}).get('state')} out={stopped.stdout.strip()[:120]}")
        self.expect(section, "stop shows on the tracker as blocked",
                    self.label_key(issue) == "blocked", f"labels={self.labels_of(issue)}")

        resumed = self.sandbox.cli("continue", issue.identifier, "--wait", "120")
        row = self.sandbox.attempt(issue.identifier) or {}
        entry = session_under(worktree)
        self.expect(section, "continue respawns the session",
                    row.get("state") == "running",
                    f"rc={resumed.returncode} state={row.get('state')} "
                    f"session={(entry or {}).get('state')} out={resumed.stdout.strip()[:120]}",
                    session=entry)
        self.record(section, "what respawn does to an interrupted turn", INFO,
                    f"session reports {(entry or {}).get('state')}/{(entry or {}).get('status')} "
                    f"after respawn", session=entry)
        self.expect(section, "continue restores the running label",
                    self.label_key(issue) == "running", f"labels={self.labels_of(issue)}")

        cancelled = self.sandbox.cli("cancel", issue.identifier, "--wait", "120")
        row = self.sandbox.attempt(issue.identifier) or {}
        entry = session_under(worktree)
        self.expect(section, "cancel ends the attempt and stops the session",
                    row.get("state") == "cancelled"
                    and (entry or {}).get("state") in ("stopped", "done", "failed", None),
                    f"rc={cancelled.returncode} state={row.get('state')} "
                    f"session={(entry or {}).get('state')}")
        self.expect(section, "cancel keeps the worktree", worktree.is_dir(), str(worktree))
        self.expect(section, "cancel shows on the tracker as blocked",
                    self.label_key(issue) == "blocked", f"labels={self.labels_of(issue)}")
        self.assert_no_strays(section)

        cancelled_session = (self.sandbox.attempt(issue.identifier) or {}).get("session_id")
        retried = self.sandbox.cli("retry", issue.identifier, "--force", "--wait", "120")
        self.expect(section, "retry is accepted", retried.returncode == 0,
                    f"rc={retried.returncode} "
                    f"out={(retried.stdout or retried.stderr).strip()[:160]}")
        # The tick that applies the retry also admits, so by now the issue is already
        # running again under a new attempt. Asserting on the momentary queued label
        # would be asserting on a state the runner is designed not to linger in.
        result = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "a retried issue starts a fresh attempt",
                    row.get("id") != first_attempt_id and row.get("state") == "running"
                    and row.get("step") == 4,
                    f"attempt={row.get('id')} (was {first_attempt_id}) state={row.get('state')} "
                    f"errors={result.report.get('errors')}")
        # The point of the previous session being removed rather than merely stopped: it
        # keeps this issue's name and working directory, so a retry that left it in place
        # would adopt it and report success without starting anything.
        self.expect(section, "the retry started a new session, not the cancelled one",
                    bool(row.get("session_id")) and row.get("session_id") != cancelled_session,
                    f"session={row.get('session_id')} (cancelled was {cancelled_session})")
        self.expect(section, "the cancelled session is gone from the listing",
                    all(entry.get("id") != cancelled_session
                        for entry in _sessions_under(self.sandbox.worktree_root)),
                    f"still listed: {cancelled_session}")

        worktree = _worktree_of(row) or Path("/nonexistent")
        branch = row.get("branch", "")
        discarded = self.sandbox.cli("discard", issue.identifier, "--yes", "--force", "--wait",
                                     "120")
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "discard removes the worktree, the branch and the session",
                    not worktree.is_dir() and branch not in self.sandbox.branches()
                    and session_under(worktree) is None,
                    f"rc={discarded.returncode} worktree={worktree.is_dir()} "
                    f"branch_present={branch in self.sandbox.branches()} "
                    f"out={(discarded.stdout or discarded.stderr).strip()[:160]}")
        self.expect(section, "discard leaves the attempt terminal",
                    row.get("state") in ("cancelled", "failed", "done"),
                    f"state={row.get('state')}")
        self.assert_no_strays(section)
        return issue

    def _control_cancel_in_flight(self, section: str) -> None:
        """A cancel recorded while a launch is half-finished.

        Reconciliation runs before recorded requests, so the tick that applies the cancel
        first finishes the interrupted launch. That ordering is deliberate - it means the
        cancel acts on a session the runner knows about rather than racing one it does
        not - and it is the one thing here the fakes can only model, never prove.
        """
        issue = self.new_issue("cancel while a launch is in flight")
        interrupted = self.sandbox.tick(crash_after_step=2)
        row = self.sandbox.attempt(issue.identifier) or {}
        self.expect(section, "the launch is interrupted mid-sequence",
                    row.get("state") == "launching" and (row.get("step") or 4) < 4,
                    f"rc={interrupted.returncode} state={row.get('state')} "
                    f"step={row.get('step')} errors={interrupted.report.get('errors')}",
                    report=interrupted.report)

        recorded = self.sandbox.cli("cancel", issue.identifier, "--no-wait")
        self.expect(section, "a cancel can be recorded against an unfinished launch",
                    recorded.returncode == 0,
                    f"rc={recorded.returncode} out={(recorded.stdout or recorded.stderr)[:120]}")

        result = self.sandbox.tick()
        row = self.sandbox.attempt(issue.identifier) or {}
        worktree = _worktree_of(row)
        entry = session_under(worktree) if worktree else None
        self.expect(section, "the tick finishes the launch, then cancels it",
                    row.get("state") == "cancelled",
                    f"state={row.get('state')} requests={result.report.get('requests')}")
        self.expect(section, "the cancelled run leaves no live session",
                    entry is None or (entry.get("state") in ("stopped", "done", "failed")),
                    f"session={entry}")
        self.expect(section, "the issue ends up blocked, not running",
                    self.label_key(issue) == "blocked", f"labels={self.labels_of(issue)}")
        self.assert_no_strays(section)
        self.teardown_issue(issue)

    # ----- section: what a session reports at the end of a turn --------------

    def section_states(self) -> None:
        """The open question: does a plain-text question always read `blocked`?

        The runner clears a label and collects a worktree on the strength of this answer,
        so it has to come from several shapes of ending rather than the two that were
        sampled when the vocabulary was first pinned.
        """
        section = "states"
        cases = [
            ("finished cleanly", "auto",
             "Create NOTES.md in this directory containing one line: finished. Then stop.",
             "done"),
            ("asked a direct question", "auto",
             "I want you to add a summary file here, but I have not decided what to call "
             "it. Do not create anything. Ask me what the filename should be, then end "
             "your turn and wait for my answer.",
             "blocked"),
            ("declared itself stuck without a question mark", "auto",
             "Update the deployment credentials for this project. Do not guess and do not "
             "create files. If the information you need is not here, state plainly that "
             "you cannot proceed, then end your turn.",
             "blocked"),
            ("finished but flagged a caveat", "auto",
             "Create NOTES.md containing one line: caveat. Then tell me one thing you were "
             "unsure about while doing it, and end your turn.",
             "done"),
            ("parked on a permission prompt", "manual",
             "Delete README.md from this directory using the Bash tool and the rm command. "
             "Do not use any other tool.",
             "blocked"),
        ]
        for index, (name, mode, prompt, expectation) in enumerate(cases, start=1):
            entry = self._run_bare_session(f"STATE-{index}", prompt, mode)
            sample = self.sample_session(name, entry, expectation)
            observed = sample["state"]
            self.record(
                section, name,
                PASS if observed == expectation else INFO,
                f"state={observed} status={sample['status']} waitingFor={sample['waitingFor']} "
                f"(expected {expectation})",
                **sample,
            )
        self._summarise_states(section)

    def _run_bare_session(self, name: str, prompt: str, mode: str) -> dict[str, Any] | None:
        """Start one background session directly, outside the runner.

        The question is about Claude Code's vocabulary, not about the tick, and going
        through the runner would need an issue per phrasing for no extra evidence.
        """
        worktree = self.sandbox.worktree_root / name
        subprocess.run(
            ["git", "-C", str(self.sandbox.repo), "worktree", "add", "-b",
             f"chargehand/{name}", str(worktree), "origin/main"],
            capture_output=True, check=False,
        )
        if not worktree.is_dir():
            return None
        subprocess.run(
            ["claude", "--bg", "-n", name, "--permission-mode", mode, prompt],
            cwd=str(worktree), capture_output=True, text=True, timeout=300, check=False,
        )
        return wait_for_session(worktree, ("done", "blocked", "failed"), timeout=300)

    def _summarise_states(self, section: str) -> None:
        ended = [s for s in self.session_samples if s["state"]]
        asked = [s for s in ended if s["expected"] == "blocked"]
        finished = [s for s in ended if s["expected"] == "done"]
        misread_questions = [s for s in asked if s["state"] != "blocked"]
        misread_finishes = [s for s in finished if s["state"] != "done"]
        self.expect(
            section, "every run that asked something reported blocked",
            not misread_questions,
            f"read as something else: {[(s['label'], s['state']) for s in misread_questions]}",
            samples=len(asked),
        )
        self.record(
            section, "runs that finished and did not report done",
            INFO if misread_finishes else PASS,
            f"{[(s['label'], s['state']) for s in misread_finishes]}"
            if misread_finishes else f"{len(finished)} samples, all `done`",
            samples=len(finished),
        )
        verdict = (
            "`state` separated asking from finishing on every sample"
            if not misread_questions and not misread_finishes
            else "at least one ending was reported as the wrong kind; `done` must stay "
                 "conservative and the status file stays load-bearing"
        )
        self.record(section, "verdict on the session-state vocabulary", INFO, verdict,
                    samples=[s for s in self.session_samples])

    # ----- teardown ---------------------------------------------------------

    def cleanup(self) -> dict[str, Any]:
        removed = remove_sessions_under(self.sandbox.worktree_root)
        deleted = []
        if not self.keep:
            for issue in list(self.issues):
                if self.admin.delete_issue(issue.id):
                    deleted.append(issue.identifier)
        return {"sessions_removed": removed, "issues_deleted": deleted, "kept": self.keep}

    # ----- reporting --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        counts = {verdict: 0 for verdict in (PASS, FAIL, INFO, SKIP)}
        for check in self.checks:
            counts[check.verdict] += 1
        return {
            "checks": [check.as_dict() for check in self.checks],
            "session_samples": self.session_samples,
            "counts": counts,
            "queue_delays_secs": [round(delay, 2) for delay in self.queue_delays],
            "ok": counts[FAIL] == 0,
        }

    def render(self) -> str:
        width = max((len(f"{c.section}/{c.name}") for c in self.checks), default=20)
        lines = [f"{'CHECK'.ljust(width)}  VERDICT  DETAIL", "-" * (width + 40)]
        for check in self.checks:
            lines.append(
                f"{f'{check.section}/{check.name}'.ljust(width)}  "
                f"{check.verdict:<7}  {check.detail[:90]}"
            )
        counts = self.summary()["counts"]
        lines.append("")
        lines.append(
            f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[INFO]} noted, "
            f"{counts[SKIP]} skipped"
        )
        if self.queue_delays:
            lines.append(
                f"tracker took {min(self.queue_delays):.2f}-{max(self.queue_delays):.2f}s to "
                f"answer its own queue query for a newly labelled issue "
                f"({len(self.queue_delays)} samples)"
            )
        if self.session_samples:
            lines.append("")
            lines.append("session states observed at the end of a turn")
            lines.append(f"  {'RUN':<44} {'STATE':<9} {'STATUS':<9} WAITING FOR")
            for sample in self.session_samples:
                lines.append(
                    f"  {sample['label'][:44]:<44} {str(sample['state']):<9} "
                    f"{str(sample['status']):<9} {sample['waitingFor'] or ''}"
                )
        return "\n".join(lines)


def _worktree_of(row: dict[str, Any]) -> Path | None:
    """`Path("")` is the current directory, which is a directory - and always exists."""
    worktree = row.get("worktree")
    return Path(worktree) if worktree else None


def _sessions_under(root: Path) -> list[dict[str, Any]]:
    from smoke_support import list_sessions

    try:
        resolved = root.resolve()
    except OSError:
        return []
    found = []
    for entry in list_sessions():
        cwd = entry.get("cwd")
        if not cwd:
            continue
        try:
            Path(cwd).resolve().relative_to(resolved)
        except (ValueError, OSError):
            continue
        found.append(entry)
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team", required=True, help="tracker team key the probe writes to")
    parser.add_argument("--root", default=".context/smoke",
                        help="throwaway directory for the sandbox")
    parser.add_argument("--only", help=f"comma-separated subset of: {', '.join(SECTIONS)}")
    parser.add_argument("--keep", action="store_true",
                        help="leave the issues, sessions and sandbox in place")
    parser.add_argument("--reuse", action="store_true",
                        help="reuse an existing sandbox directory instead of rebuilding it")
    parser.add_argument("--keychain-service", default="chargehand-linear")
    parser.add_argument("--keychain-account")
    parser.add_argument("--json", action="store_true", help="emit the transcript as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sections = [s.strip() for s in args.only.split(",")] if args.only else list(SECTIONS)
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        print(f"unknown section(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    api_key = keychain_key(args.keychain_service, args.keychain_account)
    if not api_key:
        print(
            f"no API key in the login keychain under service '{args.keychain_service}'. "
            f"Store one, or pass --keychain-service.",
            file=sys.stderr,
        )
        return 2

    root = Path(args.root).expanduser().resolve()
    if root.exists() and not args.reuse:
        remove_sessions_under(root / "worktrees")
        shutil.rmtree(root)
    sandbox = Sandbox(
        root,
        team=args.team,
        labels=SMOKE_LABELS,
        keychain_service=args.keychain_service,
        keychain_account=args.keychain_account,
    )
    if not args.reuse:
        sandbox.build()

    probe = Probe(sandbox, TrackerAdmin(api_key), team=args.team, keep=args.keep)
    started = time.time()
    try:
        probe.connect()
        if not probe.preflight():
            return 1
        for name in sections:
            print(f"\n--- {name} ---", flush=True)
            getattr(probe, f"section_{name}")()
    except TrackerError as exc:
        probe.record("setup", "tracker reachable", FAIL, str(exc))
    except KeyboardInterrupt:
        probe.record("setup", "interrupted", FAIL, "stopped by the operator")
    finally:
        cleanup = probe.cleanup()

    summary = probe.summary()
    summary["cleanup"] = cleanup
    summary["duration_secs"] = round(time.time() - started, 1)
    summary["sandbox"] = str(root)
    transcript = root / "transcript.json"
    try:
        transcript.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print()
        print(probe.render())
        print()
        print(f"transcript: {transcript}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

