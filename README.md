# chargehand

Label an issue, and a supervised [Claude Code](https://claude.com/product/claude-code)
session picks it up in a fresh git worktree on a machine you own.

> **Status: pre-alpha.** The runner works end to end — configuration, the Linear
> adapter, ledger-first launching, reconciliation, the label lifecycle, supervision,
> the watchdog, garbage collection, and the control CLI — and is covered by a test
> suite that fault-injects every launch step. It has not yet been run unattended
> against a real tracker on a dedicated machine, the lease pool is a no-op, and the
> `github` and `command` adapters are not written. Treat it as something to try on a
> scratch repository, not something to leave running. The design is in
> [docs/DESIGN.md](docs/DESIGN.md).

A *chargehand* is the worker in charge of a small crew. This tool is that for coding
agents: it watches your issue tracker, starts one background session per issue, keeps
track of what each one is doing, tells you when one needs you, and cleans up afterwards.
It runs on your own Mac — typically a spare one — not in a cloud.

## How it works

1. You assign an issue to yourself and add the queue label.
2. Within a few minutes the runner sees it, records the attempt, swaps the label to
   "running", creates a worktree, runs your repo's setup script, and starts a Claude Code
   background session with the prompt your repo configured.
3. The session works the issue. What "work" means is up to your repo: a slash command, a
   skill, or a plain prompt.
4. When a session needs you, the runner notices, notifies you, and marks the issue
   "blocked". You answer in Claude Code's own agent view; the session continues.
5. When the session finishes, the runner clears the label and later removes the worktree.

You steer it with a CLI over SSH — `status`, `watch`, `pause`, `cancel`, `retry` — and
there is deliberately no server to expose.

## Design principles

- **Per-assignee routing.** Your runner serves only issues assigned to you. There is no
  shared queue, so runners never compete and every unattended run has a named owner.
- **Safe by default.** Sessions start in Claude Code's auto permission mode. Issue text is
  never pasted into the launch prompt. Bypassing permissions is an explicit opt-in.
- **Crash-safe.** Launches are recorded before they have side effects and reconciled on
  every tick, so a crash never strands an issue or leaves a session unsupervised.
- **Small surface.** Standard-library Python, no runtime dependencies, no listening port.
- **Installed, not copied.** A repo contributes one small config file, never scripts.

## Requirements

- macOS (launchd, Keychain). Linux may follow.
- Python 3.11 or newer.
- Claude Code, signed in with a subscription account.
- A tracker: Linear today, through an API key. GitHub Issues and a generic command
  adapter are planned.

## Trying it

```bash
python3 -m pip install -e '.[test]'

chargehand init --machine            # ~/.config/chargehand/config.toml
chargehand init --repo ~/code/my-app # .chargehand.toml in the repository
chargehand doctor                    # check this machine
chargehand tick                      # one pass, in the foreground
chargehand status                    # what is running
```

`chargehand install` writes the launchd job that runs the tick on a timer; it does not
load it until you pass `--load`, or run the `launchctl bootstrap` line it prints.

Steer it from another machine over SSH — `status`, `watch`, `logs`, `pause`, `cancel`,
`stop`, `continue`, `retry`, `discard`. Answers to a session's questions go through
Claude Code's own agent view (`ssh <runner> -t claude agents`), not through this CLI.

`chargehand templates` lists the bundled starter files: the two configuration
templates, a notification hook, the deny rules passed to launched sessions, and a
skill for driving this CLI from a Claude Code session.

## Development

```bash
python3 -m pip install -e '.[test]'
pytest -q
```

The suite needs no Claude Code install, no tracker, and no network: it runs against a
fake `claude` binary and an in-memory tracker. Crash safety is covered two ways —
interrupting a launch after each step, and failing a step while the next tick is
resuming it — checking each time that the tick adopts, resumes, or fails loudly.

See [CONTRIBUTING.md](CONTRIBUTING.md). Design decisions and how to change them are in
[DECISIONS.md](DECISIONS.md); the security model is in [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE).
