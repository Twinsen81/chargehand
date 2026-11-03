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
- The five-hour usage window. The subscription has only the weekly window now, so the status
  line wrapper records only `seven_day`, both reserves read only the weekly window, and the
  board's top bar shows one gauge. The `usage` object of `/api/board`, `/api/state` and
  `status --json` has no `five_hour` key any more. A `five_hour` window that a status line
  payload or an older usage file still carries is ignored without a message, and a turn
  that fails on a five-hour limit still parks the worker.
- The age limit of a usage reading. A reading older than 30 minutes no longer counts as
  unknown: the newest reading holds wakes and new workers however old it is, until the
  window's reset time has passed. The `usage` object has no `age_secs` key any more, and
  the gauge no longer turns grey because of age.

### Added

- Clone a worker, from a card's Clone form or with `chargehand clone <ITEM> --mode
  fresh|full`. A clone is a new worker with origin `clone`, on its original's role and
  item, in a worktree of its own branched from the original's current branch, so its
  commits carry over and the two never write the same tree. Fresh mode starts a new
  session; full mode starts with a copy of the original's conversation (`claude --resume
  --fork-session --bg`, run in the clone's worktree with its own name, hook settings,
  permission mode, model and effort), refused while the original is launching, running or
  blocked. The form starts with the worker's prompt template, model and effort
  (`GET /api/workers/<id>/clone`), which you can change, and the event kinds the clone
  follows, none by default, so only a reply reaches it. The board and the command line
  record the same `clone` request; the tick refuses it, without a worktree, when the
  repository is paused, the original's branch is gone, a cap or the usage reserve for new
  workers holds it. Schema version 9 adds `parent_id`, `clone_mode`, `prompt`, `model` and
  `effort` to `workers`, so a resumed launch and a `retry` of a clone use the chosen
  values; `retry` starts a new clone of the same original. A clone's card follows its
  original's card, `status --json` has `parent`, and cards show the worker's own model and
  effort.
- Archive a worker or a whole repository from the board. An archive is one zip under the
  state directory, `archive/<repository>/<role>-<item>-<date>-<time>-<id>.zip` with mode 0600,
  that holds the worktree, the session transcript as text, and the worker's ledger row and
  sessions as JSON. A live worker is cancelled first, through a cancel request; unpushed
  commits or uncommitted changes refuse the archive unless forced, as for discard. A new
  `jobs` table (schema version 8, which also adds `workers.archived_at`) records the work,
  and a thread of the daemon writes the zip, so a large worktree does not block the tick
  or the board; the card and the top bar show the job and its progress. The zip is
  written as a `.part` file, read back in full, synced and renamed; only then does the
  tick remove the worktree, the branch and the session through garbage collection, after
  the guard once more. A stop or a crash of the daemon during the zip leaves no archive
  and no removed worktree, and the job runs again at the next start. Archiving a
  repository hides it and archives each of its workers that still has a worktree; it
  refuses while the repository has live workers, unless forced. Unarchive shows it again.
  Restoring a worktree from an archive is a manual step, described in the README.
- The usage policy. `[usage]` in the machine configuration keeps a part of the
  subscription's weekly usage window for interactive work: no wake while it is above 100
  minus `wake_reserve_percent` (20 by default), and no new worker, from a start rule or the
  board, while it is above 100 minus `new_worker_reserve_percent` (30 by
  default). The daemon now reads the status line wrapper's usage file for these reserves;
  before, no reading reached the wake reserve. No usage file means no reserve; the newest
  reading counts however old it is, until the window's reset time has passed. Readings are
  kept per provider, and each runtime names the provider it draws on. A failed session whose
  listing names a usage limit is now parked as the StopFailure hook parks it. Once the
  reset time has passed, delivery resumes parked workers in the same session with a short
  continue message, oldest reset first and within the wake admission; a worker parked
  without a reset time still waits for `continue`. Parking uses no launch attempt and is
  notified once. The board has a Parked column with the reset time, shows on a queued or
  parked card why it waits, and shows a model-limit badge in the top bar when a turn fails
  on one model family's own limit.
- `chargehand export <file>` and `chargehand import <file>`. Export writes one zip archive
  with a versioned manifest: the machine configuration with home paths written as `~` and
  credential-bearing keys (such as `api_key_command`) left out, the machine-level role
  files, the user-level agent files the roles name, the notification hook, the status line
  entry of Claude Code's settings, and the board settings of the configured repositories.
  It refuses to write an archive in which a file still holds the home directory or the
  daemon's token. Import refuses a newer format version, a file outside the archive's
  layout, and a symlink; asks for each repository path and the worktree root (or takes
  `--repository NAME=PATH`, `--worktree-root` or stdin); never overwrites a file or a
  board setting without `--force`; and writes a new token. `doctor` lists what is left to
  do by hand, and marks each step it can check as done.
- `claude_bin` and `git_bin` accept `~` for the home directory.
- Permission prompts on the board. The daemon holds a session's `PermissionRequest` hook
  open, and the worker's card in Needs you shows the tool and its input with Allow, Deny
  and Always allow; the hook then returns the decision. Always allow records a rule for
  that worker alone (the same tool with the same input, matched by a hash of the whole
  input, a Bash call's description excepted), and the next such prompt is allowed without
  a card. A prompt is now a ledger
  record that is open, resolved (with the decision and who decided) or cancelled, with a
  new `permission_rules` table (schema version 7). The first answer wins: an answer in
  `claude attach` cancels the card, and a second click answers "already resolved". A click
  is taken only while a fresh session listing shows the permission dialog, so a click
  after an answer in the session is refused and cancels the card; while a hook waits, the
  daemon also checks the listing every 2 seconds, so such an answer clears the card. The
  permission hook waits `permission_timeout_secs`, a new `claude` role key (600 seconds by
  default); the daemon answers with no decision a little before that, and when it stops,
  so the session shows its own dialog. `POST /api/workers/<id>/answer` takes the answer.
- Board personalisation. Rename a repository or a worker, give it an icon (one emoji or a
  name from a small bundled set) and pin it; collapse a repository to its pinned cards, or
  hide it. The board groups the cards of each column by repository, pinned first, then by
  last activity. A hidden repository's workers still run, are supervised and notify; the
  top bar shows the hidden count and a filter that shows them again. The settings live in
  the ledger's new `board_state` table (schema version 6), so they survive a restart.
  `POST /api/repositories/<name>/board` and `POST /api/workers/<id>/board` write them.
  A display name never changes the configured name, the handle or the session name, and
  the CLI ignores the settings.
- Board actions. Every control command has a button on the board, and each one records
  the same request as the command, with origin `board` (the CLI records `cli`; the ledger
  moves to schema version 5). The API: `POST /api/workers` to start a worker from a role,
  an optional work item and the event kinds it follows on it; `POST
  /api/workers/<id>/pause` (the CLI's `stop`), `continue`, `retry`, `cancel`, `discard`,
  `reply` and `attach`; `POST /api/repositories/<name>/pause` and `/resume`, and
  `/api/pause` and `/api/resume` for all repositories; `GET /api/requests/<id>` for an
  outcome. Each `POST` needs the token, which the page asks for once and keeps in the
  browser. A worker started on the board is not held to one per role and item, but the
  pause and the caps apply; its worktree and branch end in its id. Reply puts one line
  into the worker's inbox as an event from the board. Attach opens iTerm2 or
  Terminal.app with `claude attach` on the worker's session. `status --json` shows each
  worker's `id`, `origin` and `handle`, and the origin of each pending request.
- Workers that share a role and an item are named by a handle with the worker's id,
  `role/item#id`, and every command that takes an item accepts it, `role/item`, and `#id`
  (quoted in a shell, as `'#7'`). A notification about such a worker carries the handle in
  `CHARGEHAND_ITEM` and `CHARGEHAND_ISSUE`, which already holds the role. `status --json`
  and the board show a finished start-rule worker as `role/item#id`; its log lines keep
  `role/item`. `cancel` and `discard` name the worker they acted on.
- An investigation log for the daemon. `serve --log-file` writes
  `~/Library/Logs/chargehand.log` through a rotating handler (10 MB, five old files), and
  `install` sets it up; the launchd job's own output moves to `chargehand.launchd.log`.
  `log_level` in `[serve]` (`debug`, `info`, `warning`; default `info`) sets the level, read
  on every tick. At `info` the log records each poll that found new events with its cursor
  and duration, the other polls of a source as one line at most every 15 minutes, each
  routed event with its decision and target worker, each hook post and why no worker
  matched, each accepted label write, each wake with its kinds and pointers, what the
  session listing says when it changes, each notification with the notify command's
  outcome, and a warning for a slow or late tick. The tick report goes through the same
  handler under `chargehand.tick`, so every line has one format. No line carries what a
  worker said or did, or an event's actor. `doctor` warns about a job that does not
  rotate its log.
- A read-only board at `http://127.0.0.1:<port>/`, served by the daemon: the usage
  windows, the launch throttle and the daemon's health in a top bar, and a card per
  worker (role, item, branch, worktree, model and effort, last prompt and last answer,
  wakes today, estimated tokens, attach command) in the columns Needs you, Working,
  Queued, Idle and Closed. One static page under a strict content security policy.

### Fixed

- A wake or a `continue` no longer puts a worker back to `running` when a hook of the new
  turn, such as a permission prompt, came in while it ran. The card stays in Needs you and
  answerable, and the next session diff sends no second notification.

### Changed

- Every report line about a worker starts with its handle, `role/item`, as in
  `launched: implementer/ABC-1` and `transition: implementer/ABC-1: running -> idle`.
  The printed report of `tick` also carries `notified` and `routed`.

- A worker whose turn ends and waits for events (`idle` or `queued`) no longer sends a
  notification; only blocked, parked, failed, done through the status file and the
  watchdog states do. The tick's
  transition lines name the worker's old and new state (the session state only when it
  explains the change), a closed pull request is reported as "item closed", the warning
  for a start event of an unconfigured repository is printed once, and `follow:` lines
  show the pull requests that workers start to follow.
- The tick runs in a resident daemon, `chargehand serve`, in place of the launchd timer:
  `install` writes a KeepAlive job that runs it, control commands are applied within a
  second or two, and a loopback API (`/api/health`, `/api/state`) with a token in a mode
  0600 file serves the status; `doctor` checks both, and sessions get deny rules for the
  port.
- Worker sessions post their start, turn ends, failures, notifications and permission
  prompts to the daemon through settings hooks, and the daemon updates the ledger at
  once; the session diff of each tick stays as the fallback. A usage-limit failure makes a
  worker `parked`, with the reset time when the message gives one, and nothing wakes it
  until `continue`. The ledger goes to version 4, in place.
- Workers post every comment, review and pull request body from a file in
  `~/.cache/gh-body/` (mode 0700) with `--body-file` or `gh api -F body=@<file>`, and
  delete it after the post, so a body that names a word the launch deny rules refuse no
  longer stops the worker. The shipped role prompts, the `pr-reviewer` agent file (which
  gains the Write tool) and the roles' allow rules use the body-file forms, and
  `probes/deny_rules.py` checks that they pass the rules.
- Each line of the wake message now names the event's kind before its pointer, for example
  `review_requested: https://github.com/o/r/pull/1`, so a woken worker knows what each link
  asks for. The shipped role prompts and the `pr-reviewer` agent file describe the kinds.
- The authorship marker that workers add to their comments is now
  `<!-- chargehand-worker -->`. The first spelling, `<!-- chargehand -->`, has a space after
  the name, so the launch deny rule `Bash(*chargehand *)` refused every `gh` command that
  carried it on its command line, and a worker could not post a review or a comment at all.
  The shipped role and agent templates use the new spelling; the sources still read the old
  one as marked, and a `command` source may report either.
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
- The router no longer wakes a worker for an event that occurred before the worker started
  (with a one-minute allowance for clock differences). The sources still read a few minutes
  back from their previous poll, but that overlap no longer reaches a new worker.

### Added

- `chargehand statusline -- '<command>'`, a status line wrapper that writes the
  subscription's weekly usage window to `usage_file` in `[serve]` and then
  runs your own status line command unchanged; `/api/state` and `status --json` show the
  reading, unknown once the window's reset time has passed.
- A `pr-reviewer` starter role and its agent file, written by `chargehand init --role
  pr-reviewer`: a review request starts a reviewer in a checkout of `base` that reads the
  pull request head from a separate checkout, `{item_checkout}`, so the author's Claude
  Code settings and hooks are never loaded. Its setup runs with git hooks turned off and
  never takes a program from the head, and runs again before each wake so the checkout
  follows the head, then without taking a program from the session's worktree either.
  Every git call runs with hooks and the fsmonitor off, so no repository config runs a
  program, and the guard refuses a worktree whose `.git` was redirected to a git directory
  the session wrote, read as data rather than followed. A wake refuses a reviewer whose item
  checkout is gone, a symlink or a redirected worktree, whose worktree left its branch, or
  whose Claude Code files changed since the setup step, and checks again after the refresh;
  `chargehand continue --force` accepts a file change after a look. A reply wakes it, its own marked comments do not, and a second
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

- The `github` source keeps the notification list's ETag and asks with `If-None-Match`,
  and an empty list, which has no `Last-Modified`, now ends the first poll too, so later
  polls read threads you already opened. `doctor`, and the source once a day, warn about a
  review request that has no notification thread, which usually means GitHub's "On GitHub"
  setting for participating notifications is off.
- One notification thread whose pull request `gh` cannot read (a server error, a timeout)
  no longer fails the whole `github` source on every tick. The thread is logged and
  skipped, the other threads and the followed pull requests arrive, and the list keeps its
  old `Last-Modified`, so the next poll checks that thread again. A review request on a
  repository that no `[[repository]]` names costs no extra call.
- The tick now calls the `mark` command of a `command` source. The first worker that the
  source's start rules start on an item shows its state there, `running`, `blocked` or
  `done`; a write that exits 3 or fails is written again on the next tick.
- A comment on a tracker issue wakes its worker in the same tick, as an event from a
  `[[source]]` does. The trackers are polled and routed before delivery; their queue
  starts still come after delivery, so a waiting worker gets a free slot first.
- An expected source failure, such as an outage, a timeout or a command that exits
  non-zero, is one error line per tick, without a traceback and without the source's name
  twice. An unexpected exception still logs its traceback.
- A refused `continue` says that no process is running when the respawn started none,
  instead of quoting the state of the session's last turn.
- A Linear issue with more than twenty pages of new comments goes on from the last page
  read on the next poll, instead of reading the same pages again on every poll.
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
