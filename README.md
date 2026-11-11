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

Open <http://127.0.0.1:7878/> for the board: the weekly usage window, the launch throttle, a
badge when a turn hit one model's own limit, and the daemon's health in the top bar, and a
card per worker in the columns Needs you, Working, Queued (with the reason each worker
waits), Parked (with the reset time), Idle and Closed. It refreshes every second. Its
buttons do what the control commands do: start a worker from a role, with an optional work
item and the event kinds on it that wake the worker; pause, continue, cancel, retry or
discard a worker; reply to a worker with one line of text, which reaches it as an event,
the way a comment would, and answers a question that ended the worker's turn; attach,
which opens a Terminal.app window (the default) or an iTerm2 window (chosen on the page)
running `claude attach` on the worker's session; and pause or resume a repository or all of them. The buttons
need the daemon's token: paste the content of the token file into the token field once
and press Enter, or leave the field, and the browser keeps it for the page. The page
checks the token at once and shows its state next to the field: "no token", "checking",
"token accepted" or "token not valid". It checks again when the page loads and when the
daemon was restarted or unreachable, so a restart with a new token file shows "token not
valid" without running an action; "Forget token" removes it and shows "no token".
Workers started on the board are named by a handle with their id, such as
`implementer/ABC-1#7`, which every command that takes an item also accepts.

Attach in Terminal.app needs no macOS permission: the daemon writes a one-line script
(`exec claude attach <session id>`) into a private directory under its state directory
and opens it with `/usr/bin/open -a Terminal`; the script deletes itself before it runs
the command. iTerm2 asks for confirmation before it runs any file it is asked to open, so
attach in iTerm2 still goes through Apple events: macOS asks once whether the daemon's
Python may control iTerm2 (System Settings, Privacy & Security, Automation). If you do not
want to give that permission, choose Terminal on the page.

Clone on a card starts a new worker from that one, in a worktree of its own branched from
the worker's current branch, so its commits carry over and the two never write the same
files; uncommitted changes stay with the original. The form starts with the worker's prompt
template, model and effort, which you can change. Fresh mode starts a new session; full
mode starts with a copy of the worker's conversation (only while the worker is not in the
middle of a turn or a prompt); summary mode starts a new session that first reads the
worker's handoff note through the `read_handoff` tool. When that note is older than the
worker's last turn, chargehand first wakes the worker once to write a current note, and the
clone waits for it for up to 10 minutes; then it starts from the stored note, and its card
says so. The clone follows no events on the item unless you name the
kinds, so only your replies reach it. Its card follows the original's card and says "clone
of" the original. From a terminal: `chargehand clone implementer/ABC-1#7 --mode full
--model sonnet --prompt-file next.txt`, with `--effort`, `--prompt` and `--subscribe KIND`
as further options.

The cards of each column are grouped by repository. With Edit you can rename a repository
or a worker on the board, give it an icon (one emoji or a name such as `rocket` or `bug`)
and pin it, and collapse or hide a repository. A board pin only sorts: pinned first, then
by last activity, and a collapsed repository still shows its pinned cards. It does not keep
a session running, which Claude Code's own pin does. A hidden repository's workers still run
and notify; the top bar shows how many repositories are hidden, and "show" brings them back.
These settings are kept in the ledger, so they survive a restart, and the CLI does not use
them: commands take the configured names and handles.

Archive on a card zips what the worker leaves and then removes it: the worktree (and the
item checkout of a `checkout = "item"` role), the session transcript as text, and the
worker's ledger row and sessions as JSON, in
`<state directory>/archive/<repository>/<role>-<item>-<date>-<time>-<id>.zip` with mode 0600. A
live worker is cancelled first. As with discard, unpushed commits or uncommitted changes
refuse the archive unless you tick force; a forced archive keeps the files, not the
commits. The zip is written by the daemon in the background, so the board stays
responsive; the card and the top bar show the job and its progress. Only once the zip is
complete and has passed its check does the next tick remove the worktree, the branch and
the session. A stop or a crash of the daemon during the zip leaves no archive and no
removed worktree, and the next start of the daemon writes it again. Archive repository
(under Edit on the repository's heading) hides the repository and archives each of its
workers that still has a worktree; it refuses while the repository has live workers, unless
forced, which cancels them first. Unarchive only shows the repository again.

To restore a worktree from an archive, by hand: unzip it somewhere, add a worktree on the
pushed branch (`git worktree add <path> <branch>` in the repository), and copy the files
under `worktree/` into it. `transcript.txt`, `worker.json` and `sessions.json` are for
reading; chargehand does not take an archived worker back.

When a worker's session asks for permission to run a tool, its card in Needs you shows the
tool and its input with Allow, Deny and Always allow, and the session waits for your
answer. Always allow also allows the same tool with the same input for that worker from
then on, without a card; it never changes the role, the repository or Claude Code's
settings. You can still answer in `claude attach`, and the card goes away. The session
waits up to `permission_timeout_secs` (a role key, 600 seconds by default) and then shows
its own dialog, which only `claude attach` can answer; so does a prompt that was open
when the daemon stopped.

A worker asks you a question with the `ask` tool of the chargehand mod; `AskUserQuestion`
is denied for every worker. The question shows on its card in Needs you with its options
as buttons and a one-line answer field. The worker ends its turn after it asks, and your
answer reaches it as a reply, as its next turn. A worker reports its progress with the
`report` tool: the card shows the state and the summary, a pull request link becomes the
card's link and the worker follows that pull request, and `done` or `failed` ends the
worker when its turn ends, which clears or keeps the label as a finished worker does. A
worker that never reports is supervised by its hooks and its session state as before.
The `status_file` key of a role is deprecated: it still works in this release and goes
away in a later one; use `report` and `ask` in the role's prompt instead.

The daemon reads your subscription's weekly usage window by itself. Every 5 minutes it
starts a short `claude -p` process in an empty directory and sends it Claude Code's
`get_usage` control request; Claude Code answers with the usage that it fetches with its
own sign-in. chargehand reads no token and no Keychain item, the request runs no turn
and no hooks, and it writes no transcript. The reading goes to
`~/.cache/chargehand/usage.json` (`usage_file` in `[serve]`), and `/api/state` and the
board show the window's percentage and reset time. The request is experimental in Claude
Code and can change with any release: when it fails, times out or gets an answer without
the window, the daemon logs one line and keeps the last reading, and the bar stays grey
until there is one. `chargehand doctor` shows which source wrote the last reading and how
old it is. Set the interval, or turn the request off with 0, in `[usage]`:

```toml
[usage]
poll_secs = 300   # at least 60; 0 turns the request off
```

Two more sources write the same file, and the newest reading counts however old it is,
until its reset time has passed; `source` in `/api/state` says which wrote it. Each worker
session posts its reading after its turns, through the chargehand mod that every worker
loads. And a status line command wrapped with `chargehand statusline` writes the window
from its input and then runs your command with the same input, so your status line does not
change; other windows in the input are ignored. chargehand does not edit the settings file
for you:

```json
"statusLine": {
  "type": "command",
  "command": "chargehand statusline -- '~/.claude/statusline.sh'"
}
```

The reading also keeps a part of the weekly window for your own sessions. No wake starts
while the window is above 80 %, and no new worker starts while it is above 70 %; set other
values in `[usage]`:

```toml
[usage]
wake_reserve_percent = 20        # no wake above 100 minus this
new_worker_reserve_percent = 30  # no new worker while the week is above 100 minus this
```

Without a usage file there is no reserve. While no source can write a new reading, the
reserves use the last one. Once the window's reset time has passed, that reading holds
nothing.

A turn that ends on a usage limit parks its worker instead of failing it: you are notified
once, and after the reset the worker continues in the same session by itself, one at a time
within `max_concurrent`.

Three limits keep wakes from using up the week. `max_concurrent` in the machine
configuration caps the working turns of all repositories together, launches and wakes. In
a role's `[policy]`, `debounce_secs` holds a wake until no new event has arrived for that
many seconds, so a burst of review comments becomes one wake, and at most five times that
long after the first waiting event; it is 0 by default, and the role templates set 120. A
reply from the board, a due `request_wake` timer and a summary clone's request for a
current handoff note do not wait. `max_wakes_per_day` (12 by default) caps the wakes of
one worker over the last 24 hours: a worker at its cap stays in Queued with the reason on
its card, and you are notified once. Each card shows the wakes in
that window and an estimate of the tokens in the worker's session. A `/loop` that a role's
prompt starts runs its own turns, which chargehand does not start: these limits do not
count them, and no cap stops them.

To share devices and emulators between parallel workers, let the repository's scripts
lease them through a lease tool such as [banksman](https://github.com/Twinsen81/banksman).
chargehand does not call it and needs no setting for it.

Steer it with `status`, `watch`, `logs`, `pause`, `cancel`, `stop`, `continue`, `retry`
and `discard`. Answers to a session's questions go through Claude Code's own agent view
(`claude agents`), not through this CLI. It is a plain CLI, so it works over SSH too if
you want to reach the machine from elsewhere, but nothing depends on that.

To move your setup to another machine or user account, run `chargehand export setup.zip`
there and `chargehand import setup.zip` on the new one. The archive holds the machine
configuration with `~` for your home directory, the role files in
`~/.config/chargehand/roles/` and the agent files in `~/.claude/agents/` that the roles
name, the notification hook, your status line entry, and the board's names, icons, pins
and hidden repositories (not those of workers, which belong to this machine's ledger). It
holds no token, no Keychain value and no configuration key that can carry a credential,
such as `api_key_command`. The notification hook is copied as it is, when it is a text
script in your home or configuration directory: if you wrote a credential into it, such
as a private push topic, the archive holds it too. A binary, a tool's generated shim or a
program installed elsewhere is not copied; the command keeps naming it, and import lists
it as a program to install, as it does for any `*_bin` key given as a path.
Import asks for the path of each repository and for the worktree root
(or takes `--repository NAME=PATH` and `--worktree-root PATH`, or one line per answer on
stdin), refuses to overwrite a file without `--force`, and writes a new token. It does
not edit `~/.claude/settings.json` and does not install the launchd job: `chargehand
doctor` lists those steps, and the tracker key, until they are done.

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
its handle, `role/item`, so `grep implementer/ABC-1` finds all of them. A worker started on
the board has `role/item#id` there. A start-rule worker keeps `role/item` in the log also
after it ends, while `status --json` then shows it as `role/item#id`, which a command takes.

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
text, permission tool input, question, report summary, handoff note, status file text,
comment body or transcript path. It never
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
blocked, parked, failed, done through a report or the status file, stopped by the
watchdog, or held by its daily wake cap (`daily-cap`). A
turn end that waits for events (`idle`) and a worker that ends because its item closed
are not notified; `chargehand status` shows them. A turn can end while a background shell
that it started still runs; Claude Code then lists the session as busy, with the state
working or done, and the board and `status` show the worker as idle with the session state
`background`. When no new turn has started 30 minutes after that turn end (`background_shell_mins` in the machine
configuration; 0 turns it off), you are notified once, because a shell that never ends,
such as a dev server, can hide a session that waits for you. This applies to workers
launched by this release or a later one. As shipped it
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
