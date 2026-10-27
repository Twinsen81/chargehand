# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- chargehand no longer runs banksman. The pool module, the reap at the start of every tick,
  the leases in `status`, and the banksman check in `doctor` are gone. banksman finds a
  worker's leases by itself, ends them when the worker's process ends, and reaps on its own,
  so the call only tied chargehand to banksman's output schema. `pool_bin` is still
  accepted for one release, and `doctor` says that it is ignored. The deny rule for
  `banksman admin` stays in the launch deny rules.

### Changed

- The runner's unit of work is now a *worker*: one member of a *role*, usually bound to
  one work *item*. A role may have any number of workers. The ledger's `attempts` table
  becomes `workers`, with `role`, `origin`, `start_rule`, `runtime`, `item`, `item_id`
  and `repository` columns, a `sessions` table for the agent sessions a worker owns over
  its life, and a `wakes` counter; a version 1 ledger is migrated in place, with the old
  table kept as `attempts_v1` for one release. The database allows at most one live
  worker per role and item among the workers a start rule created, because a start event
  arrives again on every poll; workers created by hand are not limited, and the item may
  be empty.
- Roles. How a worker thinks and when it runs moves out of `.chargehand.toml` into a role
  file, `<repo>/.chargehand/roles/<name>.toml` or `~/.config/chargehand/roles/<name>.toml`:
  an optional Claude Code agent definition (`agent`), `model`, `effort`, the first
  `prompt`, `permission_mode`, `allow` rules, an optional role `setup` that runs after the
  repository's, a `status_file`, `[[start]]` rules that create a worker when an event
  arrives (`on`, optional `repositories`, `unless_subscribed` with `role` or `any`, and the
  `subscribe` and `close_on` event kinds for the worker's item), and `[policy]` caps such
  as `max_workers`. `runtime` names the coding agent; `claude` is the only one so far. The
  queue label is one per issue, so a queued issue that any live worker holds is skipped,
  whichever role holds it. A repository whose `.chargehand.toml` still carries `prompt` and
  has no role file gets its `implementer` role from it, so an older configuration keeps
  working. `init` writes both files; `doctor` checks every enabled
  role and the agent file it names.
- Machine configuration: `[[repository]]` with `path` and `roles` replaces `[[route]]`
  with `repo`, and `max_open_workers` replaces `max_open_attempts`; the old spellings are
  still accepted. Template variables gain `{item}` and `{item_url}`; `{issue}` and `{url}`
  render the same values.
- A worker's worktree is `<worktree_root>/<role>/<item>`, its placeholder branch
  `<prefix><role>/<item>`, and its session is named `<role>/<item>`. Sessions and
  worktrees from before this change are still recognised.
- Control commands take an item and, when several roles hold it, `--role`; `pause` and
  `resume` take `--repository`. `status --json` reports `workers` and `repositories`, each
  repository with its roles. Notifications carry `CHARGEHAND_ITEM`, `CHARGEHAND_ROLE` and
  `CHARGEHAND_REPOSITORY`; `CHARGEHAND_ISSUE` and `CHARGEHAND_ROUTE` stay set.
- The Claude Code adapter passes `--agent`, `--model`, `--effort` and `--plugin-dir` at
  launch, and can wake a stopped background session with a new prompt by its full
  session id, recognising when Claude Code started a copy instead.
- Claude Code is now the `claude` runtime behind a runtime interface (launch, wake, stop,
  session states, attach command, fork) with declared capabilities. The tick, the control
  commands and reconciliation reach it only through that interface, and a role's runtime
  checks its own role-file keys. `status --json` reports each worker's and session's
  `runtime`.

### Added

- A `pr-reviewer` starter role and its agent file, written by `chargehand init --role
  pr-reviewer`: a review request starts a reviewer in a checkout of `base` that reads the
  pull request head from a separate checkout, `{item_checkout}`, so the author's Claude
  Code settings and hooks are never loaded. Its setup runs with git hooks turned off and
  never takes a program from the head, and runs again before each wake so the checkout
  follows the head. A wake refuses a reviewer whose worktree left its branch or whose
  Claude Code files changed since the setup step; `chargehand continue --force` accepts
  them after a look. A reply wakes it, its own marked comments do not, and a second
  review request wakes it again.
  A start rule's `close_on` event on a worker's primary item ends the worker, and a start
  event for a repository that is not configured is reported once. `tick` prints wakes and
  held wakes, and `status` shows how to attach to idle and queued workers.
- A `command` event source for any system without a built-in source: a configured
  executable reads the cursor on stdin and prints events as JSON lines, with optional
  `mark` and `item_open` commands. The contract is documented in `docs/command-source.md`.
- Delivery. Once per tick, each idle worker with events in its inbox gets one wake that
  lists the events' pointers and continues the same session, admitted against
  `max_concurrent`, the role's `max_wakes_per_day` and the usage reserve. The events are
  marked delivered only after the wake, so a crash in between delivers them again. Workers
  gain the live states `idle` (a turn ended without a question, which was "go look"
  before) and `queued` (a wake was held back), and a worker follows the pull request of
  its own pushed branch for comments, reviews and CI results. A resume that starts a copy
  pauses the worker and notifies.
- A repository's tracker is now an event source, and the tick reaches it only that way.
  The queue becomes `labeled` events through the router, and the issues that live workers
  follow give `comment` and `done` events, so a comment on an issue lands in the inbox of
  every worker that follows it; a comment a worker posted with the authorship marker does
  not. The `linear` adapter still lists the whole queue on each poll, and reads the
  followed issues and their comments since each one's previous poll in one more query.
  The implementer role, from a pre-roles `.chargehand.toml` or from the starter role file,
  subscribes to `comment` and declares `close_on = ["done"]`. A queue start waits in the
  router while the caps, a pause, a live worker on the issue or a broken role file hold it
  back, is tried after the state diff so the tick that frees a slot uses it, and is
  dropped once its issue has left the queue.
- A `github` event source and a `github` tracker, both through the `gh` CLI. The source
  polls the account's notifications with `If-Modified-Since` at the pace that GitHub's
  `X-Poll-Interval` asks for, and the pull requests that workers follow for comments,
  reviews, commit statuses, check runs and the close; a `[[repository]]` maps its events by
  `github = "owner/repo"`. The tracker lets GitHub Issues be a repository's queue.
- Event sources and a publish-and-subscribe router. A `[[source]]` table in the machine
  configuration names a poller (`name`, `type`, and the type's own options); each tick
  polls every source after the watchdog, drops events seen before and comments that a
  chargehand worker posted as the signed-in account (by an authorship marker), puts one
  inbox row per matching live subscriber, and then runs the enabled roles' start rules,
  which start a worker subscribed to its item unless a suitable worker already has it. A
  start that the caps defer is kept for up to a week and tried again. Only a worker the
  queue label started changes the label, recorded as `owns_label` when it is created. The
  ledger moves to version 3 with `subscriptions`, `inbox`, `events` and `cursors` tables
  and the `owns_label` column; a version 2 ledger gains them in place.
- Runner: a `tick` that reconciles interrupted launches, applies recorded control
  requests, runs the watchdog, diffs session states against the ledger, admits queued
  issues within the launch throttle, launches them ledger-row-first, and collects
  finished runs.
- SQLite ledger with a partial unique index that makes a duplicate live attempt per
  issue impossible rather than merely unlikely, and a `label_state` column that keeps
  an unverified tracker write separate from the attempt's own state.
- Linear tracker adapter behind a four-call interface (`whoami`, `list`, `mark`,
  `get`), using single-label add/remove mutations and verifying every write by
  re-reading the issue.
- One module that knows the Claude Code command line, with defensive parsing of
  `claude agents --json --all` — an unrecognized session state is reported as
  `unknown` rather than guessed at.
- Control CLI: `status`, `watch`, `logs`, `pause`, `resume`, `cancel`, `stop`,
  `continue`, `retry`, `discard`, `tick`, `install`, `uninstall`, `doctor`, `init`,
  `templates`. Mutating commands record a request and let a tick apply it.
- Watchdog (wall-clock limits), notifications through a command you configure, the
  status-file protocol, and garbage collection that refuses to destroy unpushed work.
- `probes/deny_rules.py`, which checks the launch-time deny rules against a real Claude
  Code install by running each attempt twice, with and without the rules, so a refusal
  can be told apart from a model that simply declined.
- `probes/smoke_route.py`, which runs the whole sequence against a real tracker and real
  Claude Code on a scratch repository: the launch, a crash after every launch step, label
  writes that report failure after they have already been applied, each control command,
  and what a session reports at the end of several shapes of turn. It runs as a throwaway
  instance beside any real one, and a proxy in front of the tracker injects the write
  faults, which cannot be provoked from outside.
- `probes/assistant_rules.py`, which checks the bundled assistant skill and settings
  against a real Claude Code, in auto and `bypassPermissions` mode. It loads the shipped
  settings file as it is and runs every gated command in the spellings an assistant
  produces by accident rather than by evasion. It then installs the skill in a scratch
  project and asks what an operator asks: a status question must be answered without a
  prompt, in Manual mode too, and a request to cancel, stop, continue, retry, discard or
  pause must be refused at its command and go no further.
- Lease-pool interface with a no-op implementation, so the call sites exist before the
  pool does.
- The connection to the lease pool, banksman, in one module that runs its command and
  reads its JSON. When `pool_bin` (`banksman` by default) resolves through the job's
  `PATH`, each tick reaps void leases first, and `status` lists the leases; otherwise the
  runner keeps its no-op pool. A holder's worktree path needs `--verbose-titles`, as other
  agents that share the pool choose it. `doctor` reports a missing pool as information,
  and a pool whose output has an unknown `schema` number as an error. Launched sessions
  get one more deny rule, for the pool's operator commands under `banksman admin`.
- Starter templates: machine and repository configuration, a notification hook, the
  deny rules passed to launched sessions, and a skill for driving the CLI from a
  Claude Code session.
- Test suite built on a fake `claude` binary and an in-memory tracker, including
  fault injection after every launch step.
- A launch records its worktree as a trusted workspace before starting a session in it.
  Claude Code refuses to start a background session in a directory nobody has accepted a
  trust dialog for, and every worktree the runner makes is seconds old, so without this
  no launch succeeds at all. The flag is the one its own refusal names. A configuration
  file that cannot be parsed is left alone and the launch proceeds with a warning; the
  behaviour can be turned off with `trust_worktrees = false`.
- A README section on notifications: how to enable a method in the hook, how to set up
  email with a mailbox used only for alerts and an app password in the login keychain,
  and how to test the hook by hand. `init --machine` now says that the hook it writes
  only prints until a method is enabled, because a new setup otherwise notifies nobody
  and looks as if it works.
- `doctor` resolves `claude` and `git` through the `PATH` in the installed launchd job
  rather than through its own, and checks that the program the job runs still exists.
  launchd gives a job only the `PATH` its definition sets, so a tool that resolved in the
  shell running `doctor` could still be missing from the job. That showed up as a "not
  found" inside a tick long after setup looked fine; a removed virtual environment showed
  up as nothing at all, because launchd could not start the job and so nothing reached
  its log. `install` reports the same problems when it writes the job.

### Fixed

- A worker woken by a resume no longer stays `running` after its turn. Claude Code 2.1.311
  lists a resumed background session as `working` with `status: idle` once the resumed
  turn has ended, so the adapter reads that pair as done.
- A launch that Claude Code refuses as an untrusted workspace writes the worktree's trust
  entry again and retries at once, up to three times in the same launch step. A Claude
  Code process that is already running can write `~/.claude.json` back without the entry
  that the launch wrote a moment before. With several workers starting close together,
  the second launch could fail this way and wait for the next tick. The refusal is not
  retried when `trust_worktrees = false`, when the entry cannot be written, or when the
  entry is still in the file after the refusal, because the cause is then something else.
- Sessions that the scheduled job starts run at the priority a terminal gives. The job was
  a launchd `Background` job, and everything it starts inherits that type: the daemon that
  `claude --bg` starts when none is running, every session the daemon hosts, and every
  build those sessions run. CPU-bound work in such a session took about three times as
  long as from a terminal. The daemon also hosts background sessions started by hand, so
  while it was up, those were slowed too. The job is now an `Interactive` job. Run
  `chargehand install --load` again to rewrite and reload it.
- `discard`, `retry` and the collection of closed issues delete a run's branch that the
  session renamed. They deleted only the placeholder name, which no longer exists after a
  rename, so the renamed branch stayed in the repository. A branch counts as the run's
  only when git's reflog records that it was renamed from the placeholder. A branch the
  session switched to in any other way is kept, and the result says so without naming it,
  because the session chose the name.
- `status --json` no longer prints text a session wrote. A pull-request URL taken from
  Claude Code's session listing was stored and printed as it came, while the one from a
  status file was already checked; both must now be a plain URL. A branch a session
  renamed after the tracker's convention carries a slug of the issue title, and an adopted
  session is recorded under the branch it is on, so such a branch is shown only with
  `--verbose-titles`.
- `logs` and `--verbose-titles` prompt when an assistant runs them with the bundled
  settings. They were allowed like `status`, although they are the two reads that bring
  issue and agent text into a session that holds the operator's permissions, so only the
  skill's instructions stood between that text and the session.
- Flags can no longer be abbreviated. `status --verbose` was accepted as
  `--verbose-titles`, so a rule that names the full flag would not have seen it.
- The bundled skill pointed a session at `waiting_for`, which `status --json` does not
  carry by default, and so toward the flag that adds untrusted text; it now reads
  `waiting` and gives the `attach` command. It also tells a session to stop when a prompt
  is declined rather than reach the same result another way, explains why an ended run
  can still carry the blocked label, and no longer lists `watch`, which never exits.
- A control command issued while a tick is running is applied by the scheduled job. The
  command kick-started the job once, and launchd ignores a kick-start while the job is
  running; that tick could already be past the point where it reads requests. A command
  issued right after another one, while the first one's tick was still finishing, waited
  the full thirty seconds and then applied the request in a tick of its own, outside the
  job's environment and missing from its log. Observed live with `cancel` followed by
  `discard`, and again with `pause` followed by `resume`. The command now repeats the
  kick-start every two seconds until a tick applies the request. launchd starts a job at
  most once every ten seconds, so the second of two quick commands takes up to about that
  long.
- Control command output no longer has a space before the colon when there is no target,
  as in `pause : all routes paused`.
- A notification that failed is sent again on the next tick. A session's new state was
  recorded as notified whether or not the command succeeded, so one failed send, such as
  the first tick after a wake while the network is still down, left a blocked run with
  nobody told. That includes the last notification of a run that a status file marks
  complete or aborted: a finished attempt is no longer part of the state diff, so that
  notification waits in the ledger and is tried again for up to a day. One-time events
  (a failed launch, the watchdog, a cancel) are still sent once, and a failed send now
  also appears as a warning in the tick's output.
- A control command no longer kick-starts the scheduled job unless this process is the
  instance that job runs. The LaunchAgent carries no path overrides, so it always ticks
  the default configuration and ledger: a second instance, which `CHARGEHAND_CONFIG`,
  `CHARGEHAND_STATE_DIR` and `--config` all exist to allow, was asking launchd to run a
  tick against somebody else's ledger and then waiting for a request that tick would
  never see.
- Label lookup narrows on the server when the route names a team, and refuses a full page
  of results rather than choosing from a truncated one. Asking for a name alone is not
  enough: a workspace measured here holds 250 teams and 114 label names used by more than
  25 of them, so a queue label that is an ordinary word could not be resolved at all.
- An ambiguous tracker write no longer spends one of an attempt's launch retries. The
  attempt is held and retried, which is right, but a tracker that answers a correct write
  with a stale read would previously fail a launch in three ticks with nothing wrong.
  A plain tracker failure already handed the retry back; this makes the unverifiable case
  behave the same way. Both were observed live.
- The bundled assistant settings gate every mutating command in every spelling. They were
  anchored on the program name, so `chargehand cancel X` prompted while
  `/usr/local/bin/chargehand cancel X` and `sh -c 'chargehand cancel X'` ran without one.
  Neither is an evasion: the first is what an assistant produces after running
  `which chargehand`. This is the same hole the launch-time deny rules were fixed for, in
  the list that guards the other direction. `uninstall` and `init` had no rule at all.
- The launchd job's argument list is now complete in every case. Built without a console
  script on `PATH`, which is what an unactivated virtual environment looks like, it named
  the interpreter and then `tick`, so launchd would have tried to run a file called
  `tick`. Only one caller happened to pass a correct list of its own.
- The test suite no longer writes into Claude Code's own configuration file. A launch
  marks its worktree trusted, so the suite had been adding an entry for every temporary
  directory it ever created.

### Changed

- The lease pool is now planned as an external tool instead of a part of chargehand. How
  the runner connects to it is not decided yet; until then the runner keeps its no-op pool.
- The runner does not release leases when it stops a session. The pool ends the leases of
  a session whose agent process has ended, after its grace period, so the release on
  cancel, on the watchdog's hard stop, on discard, and in collection is gone.
- Every line `chargehand tick` prints starts with the date and time. The scheduled job
  appends this output to its log, and without a time the log could not show whether ticks
  ran on schedule or when an error began. `--json` output is unchanged.
- `install` suggests `launchctl kickstart` without `-k` to run a tick by hand. `-k` kills a
  tick that is already running, which can abort a launch in the middle of a step.

- The bundled notification hook has commented examples of a macOS notification and of an
  email sent through an SMTP server with the password read from the login keychain, next to
  the push service it had. The macOS example passes the values to AppleScript as arguments;
  the previous one put them into the script text, where a quote would break it. Each
  example records a failure instead of exiting, so one method that fails does not skip the
  next one.

- The Linear adapter resolves a label by asking for that name, instead of enumerating the
  workspace's labels and looking the name up in the result. Enumerating was bounded at
  5,000 labels on the grounds that no real workspace has that many; a shared one measured
  during acceptance holds more than 15,000, and the listing comes back newest first, so the
  label that falls outside the window is whichever has been in use longest. A route would
  have worked for months and then failed every launch with "no label named X exists". The
  lookup is also team-aware now: label names are unique per team, not per workspace, so a
  name that several teams use resolves to the route's own team or is reported rather than
  guessed at. As a side effect a tick that writes a label makes two or three small requests
  instead of twenty large ones.

- The deny rules passed to launched sessions now lead with a wildcard instead of
  anchoring on the program name. A Claude Code `Bash` rule matches command text rather
  than the program behind it, so the previous `Bash(chargehand:*)` stopped
  `chargehand cancel X` while leaving `/usr/local/bin/chargehand cancel X` and
  `sh -c 'chargehand cancel X'` untouched. Also covers `claude kill`, which is
  `claude stop` under its documented alias, and the flags that would start a session
  without any of these rules.

- Project scaffold: `chargehand` Python package (standard library only, Python 3.11+)
  with a `chargehand` console script, a pytest suite, and CI.
- Design document ([docs/DESIGN.md](docs/DESIGN.md)), recorded decisions
  ([DECISIONS.md](DECISIONS.md)), security policy and threat model
  ([SECURITY.md](SECURITY.md)), and contribution guide.
