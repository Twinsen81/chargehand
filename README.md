# chargehand

Label an issue, and a supervised [Claude Code](https://claude.com/product/claude-code)
session picks it up in a fresh git worktree on a machine you own.

> **Status: pre-alpha.** The runner works end to end — configuration, the Linear
> adapter, ledger-first launching, reconciliation, the label lifecycle, supervision,
> the watchdog, garbage collection, and the control CLI — and is covered by a test
> suite that fault-injects every launch step. The same sequence has been run against a
> real tracker and real Claude Code on a scratch repository, including a restart that
> killed a session mid-turn. It has not yet been left running unattended for a working
> day, the lease pool is a no-op, and the `github` and `command` adapters are not
> written. Treat it as something to try on a scratch repository, not something to leave
> running. The design is in [docs/DESIGN.md](docs/DESIGN.md).

A *chargehand* is the worker in charge of a small crew. This tool is that for coding
agents: it watches your issue tracker, starts one background session per issue, keeps
track of what each one is doing, tells you when one needs you, and cleans up afterwards.
It runs on your own Mac, the one you already work on, not in a cloud.

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

You steer it with a CLI — `status`, `watch`, `pause`, `cancel`, `retry` — and there is
deliberately no server to expose.

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
python3 -m venv ~/.venvs/chargehand          # a packaged python refuses a direct install
~/.venvs/chargehand/bin/pip install -e .
export PATH="$HOME/.venvs/chargehand/bin:$PATH"

chargehand init --machine            # ~/.config/chargehand/config.toml and notify.sh
chargehand init --repo ~/code/my-app # .chargehand.toml in the repository
chargehand doctor                    # check this machine
chargehand tick                      # one pass, in the foreground
chargehand status                    # what is running
```

`chargehand install` writes the launchd job that runs the tick on a timer; it does not
load it until you pass `--load`, or run the `launchctl bootstrap` line it prints. The job
records an absolute path to the interpreter it was installed with, so it works whether or
not the environment is on your shell's PATH. Each tick appends timestamped lines to
`~/Library/Logs/chargehand.log`.

Steer it with `status`, `watch`, `logs`, `pause`, `cancel`, `stop`, `continue`, `retry`
and `discard`. Answers to a session's questions go through Claude Code's own agent view
(`claude agents`), not through this CLI. It is a plain CLI, so it works over SSH too if
you want to reach the machine from elsewhere, but nothing depends on that.

`chargehand templates` lists the bundled starter files: the two configuration
templates, a notification hook, the deny rules passed to launched sessions, and a
skill for driving this CLI from a Claude Code session.

## Notifications

The runner calls `~/.config/chargehand/notify.sh` on every state change. As shipped it
only prints, and the runner captures what it prints, so nothing reaches you until you
enable a method in that file. It has commented examples of a macOS notification, an
email, and a push service, and you can enable more than one. The hook receives only the
issue identifier, the state, the issue URL, and a short reason the runner writes, never
issue text or agent output.

**macOS notification.** Uncomment the `osascript` example. macOS shows these
notifications as coming from Script Editor, as banners that close after a few seconds;
they stay in Notification Center. To keep them on screen until you close them, set
Script Editor's notifications to Persistent in System Settings > Notifications.

**Email.** macOS has no mail relay configured, so the example sends through an SMTP
server with `curl`. Set it up once:

1. Create a mailbox that you use only for these alerts. Any process that runs as your
   user, a launched session included, can read its password from the keychain, so the
   password must not protect anything else.
2. For Gmail, turn on 2-Step Verification for that account, then create an app password
   at <https://myaccount.google.com/apppasswords>. Work and school accounts often cannot
   create one.
3. Store the password in the login keychain. The command asks for it, so it stays out of
   your shell history:

   ```bash
   security add-generic-password -s chargehand-smtp -a alerts@example.com -w
   ```

4. In `notify.sh`, uncomment the email example, and set `sender` to that mailbox and
   `recipient` to the address that should get the alerts. For another provider, change
   the server URL.

**Test it** by running the hook by hand with a sample payload:

```bash
CHARGEHAND_ISSUE=TEST CHARGEHAND_STATE=blocked CHARGEHAND_DETAIL="test" \
  ~/.config/chargehand/notify.sh; echo "exit $?"
```

Exit code 0 means that every method you enabled succeeded. When the hook fails, the
runner logs it and sends the session's new state again on the next tick.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
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
