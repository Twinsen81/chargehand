# chargehand

Label an issue, and a supervised [Claude Code](https://claude.com/product/claude-code)
session picks it up in a fresh git worktree on a machine you own.

> **Status: pre-alpha.** The runner works end to end — configuration, the Linear
> adapter, ledger-first launching, reconciliation, the label lifecycle, supervision,
> the watchdog, garbage collection, and the control CLI — and is covered by a test
> suite that fault-injects every launch step. The same sequence has been run against a
> real tracker and real Claude Code on a scratch repository, including a restart that
> killed a session mid-turn. It has not yet been left running unattended for a working
> day. Treat it as something to try on a
> scratch repository, not something to leave running. The design is in
> [docs/DESIGN.md](docs/DESIGN.md).

A *chargehand* is the worker in charge of a small crew. This tool is that for coding
agents: it watches your issue tracker, starts one background session per issue, keeps
track of what each one is doing, tells you when one needs you, and cleans up afterwards.
It runs on your own Mac, the one you already work on, not in a cloud.

## How it works

1. You assign an issue to yourself and add the queue label.
2. Within a few minutes the runner sees it, records a worker, swaps the label to
   "running", creates a worktree, runs your repo's setup script, and starts a Claude Code
   background session with the prompt, agent, model and effort the role configured.
3. The session works the issue. What "work" means is up to your repo: a slash command, a
   skill, or a plain prompt. A *role* file under `.chargehand/roles/` holds that, and a
   repository may enable several roles.
4. When a session needs you, the runner notices, notifies you, and marks the issue
   "blocked". You answer in Claude Code's own agent view; the session continues.
5. When the session finishes, the runner clears the label and later removes the worktree.

You steer it with a CLI: `status`, `watch`, `pause`, `cancel`, `retry`. A daemon,
`chargehand serve`, runs the tick and applies what you ask for; it listens on 127.0.0.1
only and needs a token for anything that changes state.

## Design principles

- **Per-assignee routing.** Your runner serves only issues assigned to you. There is no
  shared queue, so runners never compete and every unattended run has a named owner.
- **Safe by default.** Sessions start in Claude Code's auto permission mode. Issue text is
  never pasted into the launch prompt. Bypassing permissions is an explicit opt-in.
- **Crash-safe.** Launches are recorded before they have side effects and reconciled on
  every tick, so a crash never strands an issue or leaves a session unsupervised.
- **Small surface.** Standard-library Python, no runtime dependencies, one port on
  127.0.0.1 and nothing on the network.
- **Installed, not copied.** A repo contributes small config files, never scripts.

## Requirements

- macOS (launchd, Keychain). Linux may follow.
- Python 3.11 or newer.
- Claude Code, signed in with a subscription account.
- A tracker: Linear through an API key, or GitHub Issues through the `gh` CLI. Any other
  system can send events through a `command` source, an executable that prints JSON lines
  (see [docs/command-source.md](docs/command-source.md)).

## Trying it

```bash
python3 -m venv ~/.venvs/chargehand          # a packaged python refuses a direct install
~/.venvs/chargehand/bin/pip install -e .
export PATH="$HOME/.venvs/chargehand/bin:$PATH"

chargehand init --machine            # ~/.config/chargehand/config.toml and notify.sh
chargehand init --repo ~/code/my-app # .chargehand.toml and .chargehand/roles/implementer.toml
chargehand init --repo ~/code/my-app --role pr-reviewer  # a reviewer role and its agent file
chargehand doctor                    # check this machine
chargehand tick                      # one pass, in the foreground
chargehand status                    # what is running
```

`chargehand install` writes the launchd job that keeps the daemon, `chargehand serve`,
running; it does not load it until you pass `--load`, or run the `launchctl bootstrap`
line it prints. `install --load` also replaces the timer job of an older release, which
had the same label. The daemon runs a tick every minute (`tick_interval_secs` in
`[serve]`), and at once when a control command records a request. It serves
`/api/health` and `/api/state` on `127.0.0.1:7878` (`port` in `[serve]`) and writes its
token to `~/.config/chargehand/board.token` with mode 0600. The job records an absolute
path to the interpreter it was installed with, so it works whether or not the environment
is on your shell's PATH. It also records the PATH of the shell you run `install` from,
because launchd gives a job only a minimal one; `doctor` checks that `claude` and `git`
resolve through the job's PATH, not only through yours, that the daemon answers, and that
the token file has mode 0600. The daemon writes its log to `~/Library/Logs/chargehand.log`
(see [The log](#the-log)). To try the daemon without the job, run `chargehand serve`
in a terminal; ctrl-c stops it after the tick phase in progress.

Open <http://127.0.0.1:7878/> for the board: the usage windows, the launch throttle and the
daemon's health in the top bar, and a card per worker in the columns Needs you, Working,
Queued, Idle and Closed. It refreshes every second and is read-only.

To let the daemon see your subscription's five-hour and weekly usage windows, wrap your
status line command in `~/.claude/settings.json` with `chargehand statusline`. It writes
the two windows to `~/.cache/chargehand/usage.json` (`usage_file` in `[serve]`) and then
runs your command with the same input, so your status line does not change. chargehand
does not edit the settings file for you:

```json
"statusLine": {
  "type": "command",
  "command": "chargehand statusline -- '~/.claude/statusline.sh'"
}
```

`/api/state` then shows each window's percentage and reset time; a reading older than
30 minutes counts as unknown.

To share devices and emulators between parallel workers, let the repository's scripts
lease them through a lease tool such as [banksman](https://github.com/Twinsen81/banksman).
chargehand does not call it and needs no setting for it.

Steer it with `status`, `watch`, `logs`, `pause`, `cancel`, `stop`, `continue`, `retry`
and `discard`. Answers to a session's questions go through Claude Code's own agent view
(`claude agents`), not through this CLI. It is a plain CLI, so it works over SSH too if
you want to reach the machine from elsewhere, but nothing depends on that.

`chargehand templates` lists the bundled starter files: the two configuration
templates, a notification hook, the deny rules passed to launched sessions, and a
skill for driving this CLI from a Claude Code session.

## The log

The daemon writes `~/Library/Logs/chargehand.log` itself and rotates it at 10 MB, keeping
five old files (`chargehand.log.1` to `.5`). The launchd job's own output goes to
`~/Library/Logs/chargehand.launchd.log`, which only holds what the interpreter prints
before the log is set up, such as a crash at start. A job installed by an older release
writes the log through launchd and nothing rotates it; `doctor` warns, and
`chargehand install --load` fixes it.

The level is `log_level` in `[serve]`: `debug`, `info` (the default) or `warning`. The
daemon reads it again on every tick, so a change needs no restart; `-v` and `-vv` override
it for a `chargehand serve` in a terminal. Every line has the same format, a timestamp, the
level and the part of chargehand that wrote it, and every line about a worker starts with
its handle, `role/item`, so `grep implementer/ABC-1` finds all of them.

At `info` the log holds:

- what each tick changed: launches, transitions, wakes, notifications with their outcome
  (`sent`, `exit 2`, `no command configured`), label writes with the status before and
  after, collected workers, warnings and errors. A tick that changed nothing writes nothing;
- each poll that found new events, with the cursor before and after and how long it took.
  A source reads back over its previous poll, so most polls return only events seen
  before; those, and empty polls, are written as one line per source at most every 15
  minutes, when a poll fails, and when the daemon stops;
- each event that passed the dedupe filter: its source, kind, item, its dedupe id, and
  what routing did with it: the workers it went to, the worker it
  started, or why the start waits;
- each hook a session posted, with the first characters of the session id and the worker
  it moved, or why no worker matched; a hook refused for a stale token shows too;
- a warning for a tick that took longer than the interval, or that began more than two
  intervals after the previous one ended.

`debug` adds the HTTP access log, a line for every tick, and the waits for the tick lock.
`warning` keeps only warnings and errors.

The log never holds what a worker said or did in its session: no last answer, waiting
text, permission tool input, status file text, comment body or transcript path. It never
holds a comment author either, which on Linear is an email address. Those stay in the
ledger and on the board. Text from outside chargehand that may appear, such as a setup
script's error or a command's stderr, is capped and cut to one line.

When the log is not enough, the ledger keeps every routed event for 30 days. Look up an
event by the dedupe id in brackets on its log line, then the inbox rows it made:

```bash
sqlite3 ~/Library/Application\ Support/chargehand/ledger.sqlite \
  "SELECT source, kind, item, datetime(occurred_at,'unixepoch'), datetime(seen_at,'unixepoch'),
          start_pending FROM events WHERE dedupe_id = 'tracker:comment:1234';"
sqlite3 ~/Library/Application\ Support/chargehand/ledger.sqlite \
  "SELECT w.role || '/' || w.item, datetime(i.queued_at,'unixepoch'),
          datetime(i.delivered_at,'unixepoch')
   FROM inbox i JOIN workers w ON w.id = i.worker_id WHERE i.dedupe_id = 'tracker:comment:1234';"
```

No row in `events` means no poll returned the event. A row with no inbox row means no live
worker subscribed to its kind on that item when it arrived. An inbox row with no
`delivered_at` means the event still waits for a wake.

## Driving it from Claude Code

A Claude Code session can answer questions about the runner, such as "which runs are
blocked, and why?", and steer it when you ask. Install the bundled skill and its
permission rules for the sessions you start:

```bash
mkdir -p ~/.claude/skills/chargehand
chargehand templates skill/SKILL.md > ~/.claude/skills/chargehand/SKILL.md
chargehand templates assistant-settings.json
```

Add the `allow` and `ask` lists that the last command prints to the `permissions` object
in `~/.claude/settings.json`. User settings apply in every session you start. A project's
shared `.claude/settings.json` works too, but Claude Code holds back its `allow` rules
until you trust the folder, so the status read prompts until then; `ask` rules apply at
once.

With the rules in place, `status --json` runs without a prompt, because by default it
prints only identifiers, states, timings and URLs. Everything else prompts, in every
permission mode, auto and `bypassPermissions` included: each command that changes a run
or the runner, and the two reads that bring untrusted text into the session, `logs` and
`--verbose-titles`. A prompt protects you only when a person sees it. A hook, a script or
an app that answers permission prompts automatically removes that protection, and some
apps do this in their bypass mode. Check yours once with a request that cannot do harm,
such as cancelling an issue that does not exist with `--no-wait`: you should see a
prompt before anything runs.
`probes/assistant_rules.py` checks the rules and the skill against a real Claude Code.

## Notifications

The runner calls `~/.config/chargehand/notify.sh` when a worker needs you or ends:
blocked, parked, failed, done through the status file, or stopped by the watchdog. A
turn end that waits for events (`idle`) and a worker that ends because its item closed
are not notified; `chargehand status` shows them. As shipped it
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
