# chargehand — design

**Status:** design. The repository is a scaffold; nothing described here is implemented yet.
Resolved decisions and how to change them are in [DECISIONS.md](../DECISIONS.md); the threat
model is in [SECURITY.md](../SECURITY.md).

## 1. Goals and non-goals

Goals:

- Label an issue and a Claude Code session starts on a machine you own, in a fresh git
  worktree, with a prompt your repository configured.
- A crash, a reboot, or an ambiguous tracker write never strands an issue or leaves a session
  running unsupervised.
- You learn when a run needs you, and you can see and steer everything from another machine.
- Parallel runs share scarce local resources — devices, emulators, heavy builds — without
  trampling each other, and a dead run can never hold a resource forever.

Non-goals:

- A shared team queue. Routing is per assignee.
- Cloud execution, merging pull requests, or deciding *how* an issue is worked. chargehand is
  deliberately dumb: it launches a prompt and supervises the session. The intelligence lives
  in the repository's own skills and instructions.

## 2. Architecture

```
Issue: queue label + assigned to me (+ optional state filter)
        │  poll every few minutes
        ▼
launchd timer ──► chargehand tick
   1. reconcile incomplete launches
   2. apply recorded control requests
   3. reap void leases, watchdog long runs
   4. diff session states, notify on change      ◄── claude agents --json --all
   5. admit new issues (routes, ledger, capacity)
   6. ledger → label → worktree → setup → claude --bg "<prompt>"
        │
        ▼
Claude Code supervisor (background sessions, one per issue)
        │
        ▼
whatever the prompt produces — typically a draft pull request

Operator: ssh + chargehand status|watch|cancel…  ·  Claude Code agent view for replies
```

Components:

| Component | Purpose |
|---|---|
| Runner (tick, ledger, reconciler, notifier, watchdog, garbage collection) | Turns queued issues into supervised sessions |
| Control CLI | See and steer the runner, locally or over SSH; no server |
| Tracker adapters | `linear`, `github`, and a `command` adapter for anything else |
| Lease pool — deferred; the first releases ship a no-op pool | Optional leases for devices, emulators, and build slots |

## 3. Trigger and routing

An issue is picked up when all of these hold: it carries the queue label, it is assigned to
the owner of the credentials the runner uses, it matches the route's optional state filter,
and the local ledger has no non-terminal attempt for it.

- **Per-assignee filtering removes cross-machine races by construction.** Two developers'
  runners never see the same issue. Assigning the issue *is* the routing decision, and the
  pull request's author, its reviewer, and the Claude Code quota all follow the assignee. One
  person with two machines enables the timer on exactly one.
- **Queue the issue before work starts, not by marking it "in progress".** Whatever the
  prompt runs is free to move the issue forward once it decides the work is feasible; if it
  declines, the issue should not be left looking active.
- **Polling, not webhooks.** No public endpoint and no tracker admin rights are needed.

Label lifecycle (names are configurable):

| Label | Meaning | Set by |
|---|---|---|
| `chargehand` | Queued | you |
| `chargehand-running` | A session is working on it | runner, at pickup |
| `chargehand-blocked` | Waiting for you, cancelled, failed, or needs a look | runner |
| *(none)* | Finished | runner |

Re-arm an issue by adding the queue label again; a new attempt starts only when the previous
one is terminal and its worktree is gone.

### Tracker adapters

The runner needs four operations and never reads issue bodies: `whoami`, `list(status)`,
`mark(issue, status)` with status ∈ queued / running / blocked / done, and `get(issue)` for
the verify-by-re-read after every write.

| Adapter | Notes |
|---|---|
| `linear` | GraphQL with an API key. Supports a workflow-state filter. Labels are changed with single-label add/remove mutations so concurrent label edits are not clobbered. |
| `github` | Through the `gh` CLI, so there is no token handling. No workflow states: the trigger is label + assignee + open. |
| `command` | The escape hatch: you configure `list`, `mark`, `get`, and `whoami` commands that print a small JSON contract. |

Tracker writes are treated as unreliable: a write that reports failure may have been applied,
and label swaps made of two mutations are not atomic. Every write is followed by a re-read,
and anything ambiguous is left for reconciliation (see "Launch sequence").

## 4. Configuration

Two layers, so a repository can be shared without sharing machine details.

Machine layer — `~/.config/chargehand/config.toml`:

```toml
poll_interval_secs = 180
max_concurrent = 2                  # launch throttle: sessions currently working
max_open_attempts = 4               # working + blocked; bounds the resume burst
max_run_hours = 6                   # watchdog: notify
hard_stop_hours = 10                # watchdog: stop the session
worktree_root = "~/chargehand-worktrees"

[notify]
command = "~/.config/chargehand/notify.sh"   # receives ISSUE, STATE, URL as env vars

[[route]]
repo = "~/code/my-app"
tracker = { type = "github", repo = "me/my-app" }

[[route]]
repo = "~/code/other"
tracker = { type = "linear", team = "ABC", state = "Todo" }
```

Repository layer — `.chargehand.toml`, checked in:

```toml
base = "origin/main"
setup = "scripts/setup_worktree.sh {worktree}"
prompt = "/work-issue {issue}"      # or just "{issue}"
permission_mode = "auto"
status_file = "/tmp/chargehand/{issue}/status.json"   # optional, see "Supervision"
```

Template variables are `{issue}`, `{url}`, `{worktree}`, and `{branch}`. There is deliberately
no `{title}` or `{body}`: the prompt is a trusted user message, so issue text must reach the
agent as a tool result it fetches itself, never inlined.

## 5. Launch sequence

### Admission

A new launch needs both `working < max_concurrent` and `working + blocked < max_open_attempts`.

Blocked runs deliberately do not hold a launch slot: a run waiting overnight for an answer
would otherwise stall the queue. The runner also cannot gate resumes — replies go straight to
the session. So `max_concurrent` is a launch throttle, not the memory bound. The memory bound
is the pool's counted slots (see "Lease pool"): however many sessions resume at once, only so many heavy
builds or emulators run. A session that is reading code or waiting for a slot is cheap.

### Steps

Write-ahead: the ledger row is written before any external side effect, every step is
idempotent, and the row's `step` advances after each one.

1. **Ledger first.** Insert the attempt: `state = launching`, `step = 0`.
2. Swap the labels, then re-read the issue to verify. A write that reports failure is not
   assumed to have failed; the row stays `launching` and reconciliation settles it.
3. Fetch, then create the worktree on a placeholder branch (skip if it already exists).
   The session may rename the branch later.
4. Run the repository's setup script in the worktree. Failure marks the attempt failed.
5. Start the background session, named after the issue, in the configured permission mode.
   Starting inside a linked worktree means Claude Code does not create one of its own.
6. Record the session ids. `state = running`.

### Reconciliation (start of every tick)

launchd runs one tick at a time, so a row still `launching` when a tick starts is an
interrupted launch, not a concurrent one.

- **Ledger side:** for a row stuck in `launching`, look for a session whose working
  directory is the row's worktree or whose name is the issue identifier. Found → adopt it.
  Not found → resume from the recorded step. After three attempts → fail loudly and mark the
  issue blocked. Never silently re-queue; that would loop.
- **Tracker side:** an issue assigned to you that carries the "running" label but has no
  live ledger row is marked blocked and reported. This catches a lost ledger and label writes
  that landed without a row.
- **Session side:** a background session under the worktree root with no ledger row is
  adopted when the issue can be derived from its name or path, otherwise reported. No
  unattended session runs outside the watchdog.

The ledger is a SQLite database under `~/Library/Application Support/chargehand/`, which
survives reboots.

## 6. Supervision, notifications, questions

**Session state** comes from `claude agents --json --all`, diffed against the ledger on every
tick. A notification hook is not used: the relevant hook types fire only while Claude Code's
agent view is open in a terminal, which a headless machine cannot guarantee.

| Session state | Meaning | Action |
|---|---|---|
| `working` | A turn is running | — |
| `blocked` | Needs you: a question, a permission prompt | notify, mark blocked |
| `done` | The turn finished | read the status file if configured, then notify |
| `failed` / `stopped` | Error or stopped | notify, mark blocked |

`done` is ambiguous: a session that asked its question as plain text and ended its turn also
reads `done`. A repository can disambiguate with the **status-file protocol** — whatever the
prompt runs writes a small JSON file at its yield points and when it finishes:

```json
{ "state": "needs-input | complete | aborted", "stop_reason": "…", "pr_url": "…" }
```

Without a status file, `done` means "finished or waiting — go look".

**Notifications** run a command you configure: a push service, email, anything. The payload
is only the issue identifier, the state, and the issue URL, because it usually crosses a
third-party relay. Worst-case latency is one poll interval.

**Questions stay in the session.** There is no tracker polling and no reply parsing: the
session asks, the runner notifies you and marks the issue blocked, and you answer in Claude
Code's agent view (`ssh` to the machine, then `claude agents`). The session continues with
its full context, and the next tick flips the label back.

**Watchdog.** Background sessions have no spend cap, so the runner enforces wall-clock limits:
notify after `max_run_hours`, stop the session after `hard_stop_hours`.

**Garbage collection.** When the issue is closed or its pull request is merged or closed, the
session and its worktree are removed — never when commits are unpushed. Low disk space is
reported.

## 7. Control: a CLI over SSH, no server

The runner usually lives on another machine, so it needs a control surface. The safest one
adds nothing to the attack surface: a CLI reached over SSH, which the machine needs anyway.
chargehand has no remote mode of its own; remote access is the operating system's job.

```
chargehand status [--json]          # queue, attempts, sessions, machine — no free text
chargehand watch                    # self-refreshing status table
chargehand logs <ISSUE>             # tail of the session's output — explicitly untrusted
chargehand pause [--route <name>]   # stop admitting new launches; running sessions continue
chargehand resume [--route <name>]
chargehand cancel <ISSUE>           # stop the session, release leases, mark blocked; keep the worktree
chargehand stop <ISSUE>             # pause one run
chargehand continue <ISSUE>         # respawn it; the saved conversation resumes
chargehand retry <ISSUE>            # new attempt for a failed or cancelled one
chargehand discard <ISSUE> --yes    # remove session, worktree, branch; refuses with unpushed commits
chargehand tick                     # run a tick now
```

- **One writer of side effects.** A mutating command never calls `claude`, git, or the
  tracker. It records the request in the ledger, kick-starts the tick, and waits for the tick
  to apply it. A `cancel` therefore cannot race a launch in progress.
- **Untrusted text.** Issue titles and agent output are attacker-influenced, so `status` and
  `watch` omit free text by default; titles need `--verbose`, output needs `logs`. Everything
  printed is stripped of terminal control sequences.
- **An AI assistant as the interface.** Driving the CLI from a Claude Code session on your
  working machine is convenient, and it is the one real risk of this design: status output
  lands in a session that holds your permissions. The shipped skill therefore reads only
  `status --json`, and every mutating command sits behind a Claude Code *ask* rule, which
  prompts in every permission mode.
- **Agents versus the control plane.** Sessions run as the same user, so they could call the
  CLI or stop other sessions. Sessions that chargehand launches get deny rules for both.
  Running agents under a separate user is the stronger option.
- **Optional later:** a read-only, loopback-only status page with no mutating endpoints.

## 8. Lease pool (deferred, optional)

Parallel agents on one machine fight over scarce things: a phone on USB, a limited number of
emulators, memory for heavy builds. A lock held by "whoever started it" becomes permanent as
soon as that run dies. The pool uses **leases** instead. It is optional everywhere: a
repository's scripts use it when it is installed and behave exactly as before when it is not.
The first releases ship a no-op pool so the runner can be proven on its own.

- **Lease files** live in a machine-global temporary directory, one per resource, guarded by
  a file lock held only for bookkeeping. A lease records its owner (the worktree), the owning
  session process, the processes currently using the resource, and its deadlines.
- **Reserve before start.** The lease is written, in a `booting` state with its own deadline,
  before the resource is started. A resource that is still starting therefore always has a
  lease and cannot be mistaken for an orphan.
- **A lease is void** when its boot deadline passes; when its owner session is dead *and*
  nothing touched it for a grace period; when it has been idle too long; or when its hard cap
  expires. Owner death alone is not proof of idleness: Claude Code carries a session's
  background commands across a process restart, and those keep touching the lease.
- **Renewal needs no agent cooperation.** A project's device scripts touch the lease on every
  call; long-running ones keep a touch loop alive for their own lifetime.
- **Fencing works both ways.** Scripts register themselves as users on entry and check
  ownership; the touch loop aborts its own script when the lease is lost. For a void lease
  the reaper first stops further checks from passing, terminates the registered users, and
  confirms they are gone. Something that can be killed (an emulator) is killed — that is the
  fence. Something that cannot (a physical device) is reassigned **only after** its users are
  confirmed dead; otherwise it is quarantined until a human clears it.
- **Reaping** runs at the start of every pool command and on every runner tick.
- **Resource kinds are configuration, not code:** a kind declares how to `discover` its
  instances and what to run `on_acquire` (reset) and `on_void` (kill). Counted kinds — build
  slots, ports, GPUs — need no hooks. Presets can ship for common cases.
- **Build slots** are the memory bound described under "Launch sequence": a build acquires a slot when it starts and
  releases it when it ends, so direct build invocations are covered too.

## 9. Running it on a headless Mac

- A logged-in GUI session is required: Claude Code's credentials and tracker keys live in the
  login keychain, and launchd agents run in that session.
- With FileVault, a plain reboot stalls at the pre-boot unlock screen with no network. Use
  `sudo fdesetup authrestart`.
- Keep a laptop awake with the lid closed: `sudo pmset -a disablesleep 1`, on power.
- Use key-only SSH. Screen Sharing covers the rare need for the GUI.
- launchd starts jobs with a minimal `PATH`; everything the tick shells out to must be
  reachable through the `PATH` set in the job definition.

## 10. Failure modes

| Failure | Behavior |
|---|---|
| Machine reboots | Sessions show as failed and can be respawned from their saved state; the ledger survives; leases vanish together with the resources they described. |
| Tick crashes mid-launch, or a tracker write lands but reports failure | The ledger row came first; the next tick adopts, resumes, or fails the attempt loudly. |
| Session process restarts mid-run | Background commands carry over and keep touching leases; the grace period keeps them valid. |
| Agent hangs holding a resource | Idle expiry voids the lease; the reaper terminates its users; the watchdog later stops the session. |
| Run parked waiting for you | It should release its leases before asking; if it does not, the dead-owner check frees them once the idle session process is stopped. |
| Several blocked runs resumed at once | Sessions wake together; builds and emulators queue on their slots. |
| Tracker unavailable | The tick logs and exits; the next one retries. |
| Control command during a tick | Recorded, then applied by the next, kick-started tick. |
| Prompt injection through issue text | Mitigated, not eliminated — see `SECURITY.md`. |

## 11. Roadmap

- Runner with a no-op pool: configuration, the `linear` adapter, ledger-first launch,
  reconciliation, label lifecycle, state diffing, the control CLI, the launchd job.
- Control surface: `watch`, `logs`, request plumbing through the tick, output sanitizing,
  launch-time deny rules, the assistant skill with its ask rules.
- Notifications, watchdog, garbage collection, the status-file protocol.
- The lease pool and its presets.
- `github` and `command` adapters, `init` / `doctor` / `install`, packaging, first release.
- Optional: the read-only status page, agents under a separate user.

Acceptance for the runner is tested against a throwaway repository and a cheap prompt, with
fault injection after every launch step, before any expensive real run.

## 12. Open questions

- Does `claude respawn` continue an interrupted turn by itself, or wait for a prompt?
- Will Claude Code offer a scriptable way to send a message to a background session? That
  would allow a `reply` command.
- Does `claude --bg` accept per-launch settings, so deny rules can be passed at launch rather
  than living in user settings?
- Do commands started by Claude Code's shell tool run in their own process group? That decides
  how pool-aware scripts isolate the group the reaper signals.
