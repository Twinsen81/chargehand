# chargehand — design

**Status:** the runner and the control surface are implemented and tested against a fake
Claude Code and a fake tracker, and the whole sequence has been run against a real tracker
and real Claude Code on a scratch repository, including a crash after every launch step and a
reboot that killed a session mid-turn. The connection to the lease pool is tested only
against a fake pool, and the `github` and `command` adapters are not written. It has not
yet been left running unattended for a working day.
Resolved decisions and how to change them are in [DECISIONS.md](../DECISIONS.md); the threat
model is in [SECURITY.md](../SECURITY.md).

## 1. Goals and non-goals

Goals:

- Label an issue and a Claude Code session starts on a machine you own, in a fresh git
  worktree, with a prompt your repository configured.
- A crash, a reboot, or an ambiguous tracker write never strands an issue or leaves a session
  running unsupervised.
- You learn when a run needs you, and you can see and steer everything from one command.
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

Operator: chargehand status|watch|cancel…  ·  Claude Code agent view for replies
```

Components:

| Component | Purpose |
|---|---|
| Runner (tick, ledger, reconciler, notifier, watchdog, garbage collection) | Turns queued issues into supervised sessions |
| Control CLI | See and steer the runner; no server |
| Tracker adapters | `linear`, `github`, and a `command` adapter for anything else |
| Lease pool (banksman, an external tool; a no-op pool when it is not installed) | Optional leases for devices, emulators, and build slots |

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
| `linear` | GraphQL with an API key. Supports a workflow-state filter. Labels are changed with single-label add/remove mutations so concurrent label edits are not clobbered. Issue URLs are shortened to their identifier form, because Linear's own URLs end in a slug built from the title. |
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
permission_mode = "auto"            # auto | manual | acceptEdits | dontAsk | plan | bypassPermissions
status_file = "/tmp/chargehand/{issue}/status.json"   # optional, see "Supervision"
branch_prefix = "chargehand/"       # the placeholder branch each run starts on
```

Template variables are `{issue}`, `{url}`, `{worktree}`, `{branch}`, and `{repo}`. There is
deliberately no `{title}` or `{body}`: the prompt is a trusted user message, so issue text must
reach the agent as a tool result it fetches itself, never inlined. A configured template that
names any other variable is a configuration error, not a literal.

A command template is split into arguments *before* its variables are substituted, and nothing
is run through a shell, so a value containing spaces or shell metacharacters stays one argument.

## 5. Launch sequence

### Admission

A new launch needs both `working < max_concurrent` and `working + blocked < max_open_attempts`.

Blocked runs deliberately do not hold a launch slot: a run waiting overnight for an answer
would otherwise stall the queue. The runner also cannot gate resumes — replies go straight to
the session. So `max_concurrent` is a launch throttle, not the memory bound. The memory bound
belongs to the lease pool (see "Lease pool"): however many sessions resume at once, only so many heavy
builds or emulators should run. A session that is reading code or waiting for a slot is cheap.

### Steps

Write-ahead: the ledger row is written before any external side effect, every step is
idempotent, and the row's `step` advances after each one.

1. **Ledger first.** Insert the attempt: `state = launching`, `step = 0`.
2. Swap the labels, then re-read the issue to verify. A write that reports failure is not
   assumed to have failed; the row stays `launching` and reconciliation settles it.
3. Fetch, then create the worktree on a placeholder branch (skip if it already exists).
   The session may rename the branch later. Removing the worktree deletes the branch
   under its new name too, when git's reflog records the rename; a branch the session
   switched to in any other way is kept, and the result says so.
4. Run the repository's setup script in the worktree. Failure marks the attempt failed.
5. Start the background session, named after the issue, in the configured permission mode.
   Starting inside a linked worktree means Claude Code does not create one of its own.
6. Find the new session in the listing by name or working directory and record its ids.
   `state = running`. Looking it up rather than scraping the launch output is what makes
   this step idempotent: a tick that died between starting the session and recording it
   adopts the session it already started instead of starting a second one.

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
survives reboots. A partial unique index over non-terminal rows makes two live attempts for
one issue impossible at the database level rather than by a check the caller might skip. The
queue status last *verified* on the tracker is stored separately from the attempt's own
state, so a write that could not be confirmed is retried on the next tick instead of being
assumed to have landed.

launchd will not run two instances of one job, but `chargehand tick` is also a command, so
the tick takes an exclusive file lock rather than assuming it is alone. The kernel releases
that lock when its holder dies, so a killed tick needs no recovery.

## 6. Supervision, notifications, questions

**Session state** comes from `claude agents --json --all`, diffed against the ledger on every
tick. A notification hook is not used: the relevant hook types fire only while Claude Code's
agent view is open in a terminal, which an unattended run cannot guarantee.

| Session state | Meaning | Action |
|---|---|---|
| `working` | A turn is running | — |
| `blocked` | Needs you: a question, a permission prompt | notify, mark blocked |
| `done` | The turn finished | read the status file if configured, then notify |
| `failed` / `stopped` | Error, stopped, or killed with the machine | notify, mark blocked |

Those states are read from a `state` field, which a background entry carries alongside a
coarser `status`. The two disagree, and preferring the right one is load-bearing: a session
that finished reports `state: done` with `status: idle`, while a session that asked a
question and ended its turn reports `state: blocked` with the same `status: idle`. Reading
`status` would call the second one finished and clear its label while it was still waiting.

That also means `done` is less ambiguous than it first appears: Claude Code distinguishes
"ended its turn having finished" from "ended its turn having asked something". Across every
run the smoke probe has observed, nothing that wanted input was reported `done`: not a
direct question, not a flat statement that the session could not proceed, not a permission
prompt. So a session that is waiting never has its label cleared. The converse does not
hold. About one finished run in five reported `blocked` anyway, and the same prompt
reported `done` the other four times. That is not confined to runs that hedged: a session
told to write one file and stop has done it. `blocked` is therefore a reason to go and
look rather than proof that anything is waiting, and a bare `done` still says nothing
about *how* a run finished. The runner stays conservative on both counts.

A repository disambiguates that with the **status-file protocol**: whatever the prompt runs
writes a small JSON file at its yield points and when it finishes.

```json
{ "state": "needs-input | complete | aborted", "stop_reason": "...", "pr_url": "..." }
```

The file is what separates a run that succeeded from one that gave up, and it is where a
pull-request URL comes from. Without it, `done` means "finished or waiting, go look".

**Notifications** run a command you configure: a desktop notification, email, a push service,
anything. The bundled hook has commented examples of the first three. The payload
is only the issue identifier, the state, the issue URL, and a short reason the runner itself
writes, because it usually crosses a third-party relay. Nothing authored by an agent, a
setup script, or the tracker goes into it; that stays local and is reached through
`status --verbose-titles` and `logs`. Worst-case latency is one poll interval. A session's new
state counts as notified only when the command succeeds, so a send that failed, for example
on the first tick after a wake while the network is still down, is tried again on the next
tick. A finished run's last notification is tried again for up to a day.

**Questions stay in the session.** There is no tracker polling and no reply parsing: the
session asks, the runner notifies you and marks the issue blocked, and you answer in Claude
Code's agent view (`claude agents`). The session continues with
its full context, and the next tick flips the label back.

**Watchdog.** Background sessions have no spend cap, so the runner enforces wall-clock limits:
notify after `max_run_hours`, stop the session after `hard_stop_hours`.

**Garbage collection.** When the issue is closed or its pull request is merged or closed, the
session and its worktree are removed — never when commits are unpushed. Low disk space is
reported.

## 7. Control: a CLI, no server

The control surface is a CLI, and nothing else. There is no port, no token, and no browser
to attack. chargehand runs on the machine you work on and is steered from a terminal on it.

It is a plain CLI, so it also works over SSH if you want to reach the machine from
elsewhere, but nothing depends on that and it is not part of what is tested. Reaching a
machine remotely is the operating system's job, not this tool's.

```
chargehand status [--json]          # queue, attempts, sessions, machine — no free text
chargehand watch                    # self-refreshing status table
chargehand logs <ISSUE>             # tail of the session's output — explicitly untrusted
chargehand pause [--route <name>]   # stop admitting new launches; running sessions continue
chargehand resume [--route <name>]
chargehand cancel <ISSUE>           # stop the session, mark blocked; keep the worktree
chargehand stop <ISSUE>             # pause one run
chargehand continue <ISSUE>         # respawn it; the saved conversation resumes
chargehand retry <ISSUE>            # new attempt for a failed or cancelled one
chargehand discard <ISSUE> --yes    # remove session, worktree, branch; refuses with unpushed commits
chargehand tick                     # run a tick now
```

- **One writer of side effects.** A mutating command never calls `claude`, git, or the
  tracker. It records the request in the ledger, kick-starts the scheduled tick when that job
  is loaded, otherwise runs a tick itself under the tick lock, and waits for the outcome.
  launchd ignores a kick-start while a tick is running, and that tick can be past the point
  where it reads requests, so the command repeats the kick-start until a tick applies the
  request. launchd also starts a job at most once every ten seconds and holds a kick-start
  back until then, so the second of two quick commands can take that long. The command runs
  a tick itself only when the job has not applied the request within 30 seconds. A
  `cancel` therefore cannot race a launch in progress. Reconciliation runs *before* recorded
  requests, so a cancel issued during a launch acts on a session that is known rather than
  leaving one that the dying tick had already started running unsupervised.
- **Untrusted text.** Issue titles and agent output are attacker-influenced, so `status` and
  `watch` omit free text by default; titles and a session's waiting text need
  `--verbose-titles`, output needs `logs`. That covers what a session can put into fields
  that look structured: a pull-request URL it reports is kept only when it is a plain URL,
  and a branch it renamed, which then carries a slug of the issue title, is shown only
  with `--verbose-titles`. Flags cannot be abbreviated, so the rules that gate a flag see
  its full name. Everything printed is stripped of terminal control sequences.
- **An AI assistant as the interface.** Driving the CLI from a Claude Code session is
  convenient, and it is the one real risk of this design: status output lands in a session
  that holds your permissions. So the shipped settings let exactly one read run without a
  prompt, `status --json`, which by default carries no free text. Everything else sits
  behind a Claude Code *ask* rule, which prompts in every permission mode, auto and
  `bypassPermissions` included: every mutating command, and the two reads that bring issue
  or agent text into the session, `logs` and `--verbose-titles`. Gating those two reads
  makes "only when asked" a rule rather than the model's judgement. The rules lead with a
  wildcard rather than anchoring on the program name, for the same reason the launch-time
  deny rules do: a rule matches the command text, so an anchored one gates
  `chargehand cancel X` and misses both `/usr/local/bin/chargehand cancel X` and
  `sh -c 'chargehand cancel X'`. The skill tells the session to stop when a prompt is
  declined, not to reach the same result another way. An ask rule makes Claude Code ask
  whatever runs the session, so it protects nothing where a hook or an app answers
  prompts automatically. `probes/assistant_rules.py` checks the rules, and the skill in
  conversation, against a real install, because those claims are about Claude Code rather
  than about this tool.
- **Agents versus the control plane.** Sessions run as the same user, so they could call the
  CLI, stop other sessions, or start a session without any of these restrictions. Launched
  sessions get deny rules for all three, and one more for the lease pool's operator
  commands, which all start with `banksman admin`. The other pool commands stay open,
  because the repository's scripts use them. A `Bash` rule matches command text rather
  than the program behind it, so every pattern leads with a wildcard to cover an absolute
  path and a `sh -c '...'` wrapper alongside the bare name; `probes/deny_rules.py` checks
  each form against a real install. That makes the rules a guardrail against the careless
  path and not a boundary against a deliberate one. Running agents under a separate user
  is the stronger option.
- **Optional later:** a read-only, loopback-only status page with no mutating endpoints.

## 8. Lease pool

Parallel runs on one machine compete for scarce things: a phone on USB, a limited number of
emulators, memory for heavy builds. Sharing them safely is the job of a lease pool, which is
an external tool and not part of chargehand: banksman. The repository's own scripts lease
what they need from it. The runner's part is small.

- **Optional.** The runner uses the pool when `pool_bin` (`banksman` by default) resolves
  through the job's `PATH`, and a no-op pool otherwise. `doctor` resolves it the same way.
  A missing command is information, not a fault, and a pool whose output this version
  cannot read is an error.
- **A command, never an import.** One module runs `banksman` and reads its JSON. An import
  would add a runtime dependency, and a copy inside chargehand's own environment can
  differ from the command that the scripts call, while both work on the same lease files.
  Every JSON document carries a `schema` number, and output with a number that this
  version does not know is refused.
- **Reap and status, nothing else.** Each tick starts with `banksman reap`, which takes
  back the resources of void leases, and `chargehand status` lists the leases. A pool that
  fails is reported, and the tick goes on.
- **No release by the runner.** banksman records the agent process of every lease, and a
  lease whose agent process has ended becomes void after a grace period, 5 minutes by
  default. When the runner stops a session, for a cancel, the watchdog, a discard, or
  collection, the leases of that session therefore end without the runner, and a later
  reap takes their resources back. A release by the runner would need either the worktree,
  which several agents can share, or the agent process, which has already ended when the
  runner knows that the session stopped. The cost: after a stop, a resource can stay held
  for the grace period and one more tick.

## 9. Leaving it running

- A logged-in session is required: Claude Code's credentials and tracker keys live in the
  login keychain, and launchd agents run in that session. A locked screen is fine; a logged
  out one is not.
- launchd starts jobs with a minimal `PATH`; everything the tick shells out to must be
  reachable through the `PATH` set in the job definition. `install` copies the `PATH` of the
  shell it runs in, and `doctor` resolves `claude`, `git`, and the lease pool's command
  through the job's `PATH` rather than its own. A repository's setup script inherits the
  same `PATH`.
- The job runs as launchd's `Interactive` process type, which gives it the priority a
  terminal gets. Everything the tick starts inherits the type, and that includes every
  session: `claude --bg` starts the daemon that hosts background sessions when none is
  running, and the daemon keeps the type, also when it restarts itself for an upgrade.
  With the `Background` type, sessions ran at the lowest scheduling priority and CPU-bound
  work took about three times as long. The daemon also hosts the sessions you start by
  hand, so a throttled daemon would slow those too.
- Runs share the machine with you. Keep `max_concurrent` low until the repository's
  scripts lease their devices through the lease pool, because until then nothing stops two
  runs from reaching for the same device or emulator.
- Sessions do not survive a restart. The ledger, the labels and the worktrees do, and the
  next tick parks any run whose session is gone, so a restart costs progress but never
  consistency.

## 10. Failure modes

| Failure | Behavior |
|---|---|
| Machine reboots | Observed: the ledger, the labels and the worktrees survive, the scheduled job runs a tick by itself, the killed session reads `failed`, and the run is parked as blocked with one attempt recorded. `continue` brings the session back but not the work it had running, so resuming is left to you. |
| Tick crashes mid-launch, or a tracker write lands but reports failure | The ledger row came first; the next tick adopts, resumes, or fails the attempt loudly. |
| Agent hangs | The watchdog notifies after `max_run_hours` and stops the session after `hard_stop_hours`. |
| Several blocked runs resumed at once | Sessions wake together; `max_open_attempts` bounds how many there can be. |
| Tracker unavailable | The tick logs and exits; the next one retries. |
| Control command during a tick | Recorded, then applied by the next, kick-started tick. |
| Prompt injection through issue text | Mitigated, not eliminated — see `SECURITY.md`. |

## 11. Roadmap

Done:

- Runner with a no-op pool: configuration, the `linear` adapter, ledger-first launch,
  reconciliation, label lifecycle, state diffing, the control CLI, the launchd job.
- Control surface: `watch`, `logs`, request plumbing through the tick, output sanitizing,
  launch-time deny rules, the assistant skill with its ask rules.
- Notifications, watchdog, garbage collection, the status-file protocol.
- `init`, `doctor`, `install`, and the bundled templates.
- The connection to the lease pool: reap on every tick, the leases in `status`, the pool's
  command and schema in `doctor`, and the deny rule for its operator commands.

Next:

- Run it unattended for a day against a real tracker.
- Run several sessions at once with the lease pool and with repository scripts that lease
  through it.
- `github` and `command` adapters, packaging, first release.
- Optional: the read-only status page, agents under a separate user.

Acceptance for the runner runs against a throwaway repository and a cheap prompt, with fault
injection after every launch step, before any expensive real run. `probes/smoke_route.py`
drives the whole of it against a real tracker and real Claude Code. The reboot drill and the
reboot drill stays manual, because a restart cannot be scripted from the machine under
test.

## 12. Open questions

- Should a run that reports `working` with `status: idle` for long enough be treated as
  stalled rather than healthy? A short window would misread the gap between two tool
  calls; a long one is what the watchdog already does, only at `max_run_hours`.
- Will Claude Code offer a scriptable way to send a message to a background session? That
  would allow a `reply` command. Until then, answering a question means attaching.

Settled by inspecting Claude Code 2.1.270, and by running background sessions and the
deny-rule probe against 2.1.278:

- `claude --bg` does accept `--settings` with a JSON string, so the deny rules for
  runner-launched sessions are passed per launch rather than living in user settings.
  Settings that fail validation are dropped silently in a non-interactive run, so the
  payload is built rather than templated and the probe is what confirms it arrived.
- A `Bash` deny rule matches the command text after Claude Code splits compound commands
  and strips a fixed wrapper list. It does not follow the program: an anchored
  `Bash(chargehand:*)` misses `/usr/local/bin/chargehand cancel X` and
  `sh -c 'chargehand cancel X'`. A leading `*` matches anywhere in the text and covers
  both. `claude kill` is `claude stop` under an alias and needs its own pattern. Every
  form in `probes/deny_rules.py` was refused with the shipped rules and ran without
  them, so the rules do reach a background launch and do bite. What they bite is a list
  of spellings, which is the distinction `SECURITY.md` keeps.
- `claude agents --json --all` prints a bare JSON array. A background entry carries `id`,
  `sessionId`, `name`, `cwd`, `pid`, `kind`, a millisecond `startedAt`, and both `state`
  and `status`.
- `id` is the short id that `--bg` prints, and it is what `attach`, `logs`, `stop` and `rm`
  take. `sessionId` is a UUID and is not interchangeable with it. Interactive entries carry
  only `sessionId`.
- The observed vocabulary is `working`, `done` and `blocked` from `state`, and `busy`,
  `idle` and `waiting` from `status`. A permission prompt reads `blocked` with
  `waitingFor: "permission prompt"`; a question asked in plain text reads `blocked` with no
  `waitingFor` at all.

Settled by running the whole sequence against a real tracker and real Claude Code, with the
smoke probe:

- A background session started inside a linked worktree really does skip Claude Code's own
  worktree isolation. The repository's worktree list is unchanged by the launch and the
  session works in the tree it was given. The launch sequence depends on this; it was
  documented but never verified, and an extra worktree would have meant every run working
  somewhere the runner does not collect.
- `claude respawn` continues the interrupted turn by itself rather than waiting for a
  prompt: a session stopped mid-turn reports `working` again immediately after. So
  `continue` needs nothing beyond the respawn.
- Every launch step can be interrupted and resumed. Killing a launch after each of its
  steps in turn left, each time, a row the next tick recognised, exactly one attempt per
  issue, a label that matched how far the launch had got, no issue stranded under the
  running label, and no session running outside the ledger.
- A label write that reaches the tracker and then reports failure is accepted once the
  re-read proves it landed; one that half-applies, and one whose verification read fails,
  both hold the attempt where it was and settle on the following tick. The separation
  between an attempt's state and the queue status last *verified* is what makes that work.
- **Resolve a label by name, never by enumerating them.** A shared workspace accumulates
  labels without bound (one measured here holds more than fifteen thousand), and the
  listing comes back newest first. Any fixed page budget therefore stops short, and the
  label it stops short of is whichever has been in use longest: a route would work for
  months and then fail every launch with "no label named X exists". Label names are also
  unique per team rather than per workspace, so a name has to be resolved against the
  route's own team or reported as ambiguous.
- A tracker takes a fraction of a second to make a newly labelled issue answerable by a
  filtered query. Irrelevant at any sane poll interval, and the reason acceptance waits for
  the queue rather than ticking the instant an issue is created.
- **A session killed with the machine reads `failed`.** A background session interrupted
  by a reboot survives in the listing with `state: failed`, no `status` and no `pid`, so
  the runner parks the run as blocked and says so rather than treating it as finished.
  The ledger, the label and the worktree all come through the restart intact, the
  scheduled job runs a tick by itself, and the issue is not launched a second time.
- **A reboot does not resume the work, and should not.** `continue` does bring the
  session back, but `respawn` restores the conversation, not what the session had
  running: a child process it started is gone with the machine. A session whose turn had
  ended while it waited on that process comes back with nothing to wait for and no new
  instruction, and then sits there reporting `working` with `status: idle`. Parking the
  run as blocked and telling the operator is therefore the right default; resuming
  automatically would put the running label back on an issue nothing is working on.
- **`working` with `status: idle` is a stall, and the runner cannot currently tell.**
  It reads `state` first, sees `working`, and reports a healthy run. Nothing catches it
  until the watchdog fires at `max_run_hours`. The pair had not been observed before;
  every earlier sample was `working`/`busy`, `done`/`idle`, `blocked`/`idle` or
  `blocked`/`waiting`.
- **A label write can report success and still be followed by a stale read.** Observed
  twice against a real tracker. The write had landed; the verification read returned the
  labels from before it. The attempt is left where it was and the next write settles it,
  so nothing is lost, but the tick reports an error and exits non-zero for a write that
  was in fact correct.
- The control CLI works from a stripped environment (short `PATH`, no shell profile), and
  the login keychain is still readable there, which is what the launchd job needs.
  `claude` itself is not on a minimal `PATH`; `doctor` says so, and a tick refuses with the
  fix in the message rather than failing obscurely.

Settled by launching a session through the scheduled job, with Claude Code 2.1.283 and
2.1.284:

- **A session gets the scheduled job's environment.** `claude --bg` starts a daemon when
  none is running, the daemon detaches, and every session it hosts inherits the job's
  `PATH` and its process type. Under launchd's `Background` type, the daemon, the session
  and every command the session ran had the lowest scheduling priority, and a CPU-bound
  benchmark took 20.9 s in the session against 7.0 s from a terminal. Neither
  `setpriority` nor `taskpolicy -B` moves a child out of a type that launchd applied, so
  the fix is the job's own type (see "Leaving it running"). The daemon exits a few seconds
  after its last session ends. When Claude Code updates itself during a run, the daemon
  restarts onto the new version with the same type, and the session continues.
- **A slash command works as the launch prompt**, so `prompt` can name a skill.
- **A question asked with the `AskUserQuestion` tool reads `blocked`**, with
  `status: waiting` and `waitingFor: "input needed"`, beside the two shapes above. The
  runner marks the run blocked and notifies, the same as for the other two.
