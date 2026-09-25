# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
- `probes/assistant_rules.py`, which checks that the bundled assistant settings really
  do gate every mutating command against a real Claude Code, in the spellings an
  assistant produces by accident rather than by evasion.
- Lease-pool interface with a no-op implementation, so the call sites exist before the
  pool does.
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

### Fixed

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

- Every line `chargehand tick` prints starts with the date and time. The scheduled job
  appends this output to its log, and without a time the log could not show whether ticks
  ran on schedule or when an error began. `--json` output is unchanged.

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
