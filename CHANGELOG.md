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
- Lease-pool interface with a no-op implementation, so the call sites exist before the
  pool does.
- Starter templates: machine and repository configuration, a notification hook, the
  deny rules passed to launched sessions, and a skill for driving the CLI from a
  Claude Code session.
- Test suite built on a fake `claude` binary and an in-memory tracker, including
  fault injection after every launch step.

### Changed

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
