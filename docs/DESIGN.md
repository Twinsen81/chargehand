# chargehand — design

**Status:** the runner and the control surface are implemented and tested against a fake
Claude Code and a fake tracker, and the whole sequence has been run against a real tracker
and real Claude Code on a scratch repository, including a crash after every launch step and a
reboot that killed a session mid-turn. It has not
yet been left running unattended for a working day. The unit of work is a *worker* of a
*role* (section 4); the board on the daemon's loopback API is read-only so far.
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
launchd KeepAlive job ──► chargehand serve: a tick every tick_interval_secs,
                          and at once when a control request waits
   1. reconcile incomplete launches
   2. apply recorded control requests
   3. watchdog long runs
   4. follow the pull request of each worker's pushed branch
   5. poll the event sources, route: inboxes, then start rules
   6. diff session states, notify on change      ◄── claude agents --json --all
   7. poll each repository's tracker as a source, route: inboxes and closes
   8. deliver: one wake per idle worker with events (the outbox of a session with the
      mod; else stop, then claude --resume)
   9. sync the tracker labels and the status of source items
  10. admit the tracker's starts: the queue label is a start rule
      (repositories, roles' start rules, ledger, capacity)
  11. collect garbage
   launch: ledger → label → worktree → setup → claude --bg "<prompt>"
        │
        ▼
Claude Code supervisor (background sessions, one per worker)
        │
        ▼
whatever the prompt produces — typically a draft pull request

Operator: chargehand status|watch|cancel…  ·  Claude Code agent view for replies
          GET 127.0.0.1:7878/api/state, /api/health (the daemon's loopback API)
```

Components:

| Component | Purpose |
|---|---|
| Runner (tick, ledger, reconciler, notifier, watchdog, garbage collection) | Turns queued issues into supervised workers |
| Roles | Per-repository templates for workers: prompt, agent, model, effort, permissions, start rules |
| Runtimes | The coding agent behind one interface; `claude` is the only one that ships |
| Daemon (`chargehand serve`) | Runs the tick on a timer and applies control requests; serves a loopback API with a token |
| Control CLI | See and steer the runner; records requests for the daemon |
| Tracker adapters | `linear` and `github` |
| Event sources | `github`, the repository's tracker, and a `command` source for anything else |
| A lease tool such as banksman (external, optional, never called by chargehand) | Leases for devices, emulators, and build slots, taken by the repository's own scripts |

## 3. Trigger and routing

An issue is picked up when all of these hold: it carries the queue label, it is assigned to
the owner of the credentials the runner uses, it matches the repository's optional state
filter, a role enabled in the repository has a start rule for the queue label, and the
ledger has no live worker for it in any role. The label is one per issue and every worker the
queue starts drives it, so re-adding the label while a worker runs starts nothing.

- **Per-assignee filtering removes cross-machine races by construction.** Two developers'
  runners never see the same issue. Assigning the issue *is* the routing decision, and the
  pull request's author, its reviewer, and the Claude Code quota all follow the assignee. One
  person with two machines runs the daemon on exactly one.
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

Re-arm an issue by adding the queue label again; a new worker starts only when the previous
one is terminal and its worktree is gone. The label is one per issue, so of several roles
that start on it, the first one listed in the repository's `roles` takes the issue.

### Tracker adapters

The runner needs four operations and never reads issue bodies: `whoami`, `list(status)`,
`mark(issue, status)` with status ∈ queued / running / blocked / done, and `get(issue)` for
the verify-by-re-read after every write. An adapter may add `poll(cursor, followed)`, which
returns the whole queue, the followed issues as they are now, and their comments since the
cursor without their text; the default re-reads each followed issue and has no comments.

The tick reaches a repository's tracker only as an event source named after the tracker
type (see "Event sources and the router"). The queue becomes a `labeled` event for each
queued issue, and the issues that live workers follow give `comment` and `done` events. A
`labeled` event is news again once the issue changed, so a re-armed issue starts a new
worker. Every poll lists the whole queue, as admission always did: a queue start that the
router gave up after a week comes back from the next listing. The cursor is kept per
repository. Every tracker is polled after the state diff and before delivery, and its
events go through the router's filters, inboxes and closes there, so a comment on a
worker's issue wakes the worker in the same tick. The tracker's starts wait for the
admission step after delivery, which offers them to the start rules oldest first: a slot
that a finished worker frees is used in the same tick, and a waiting worker gets a slot
before the queue starts a new one. Each waiting start is tried once per tick. A paused
repository is not polled. A queue start waits while a live worker holds the issue or while a role file does
not load, and is dropped once the issue has left the queue.

| Adapter | Notes |
|---|---|
| `linear` | GraphQL with an API key. Supports a workflow-state filter. Labels are changed with single-label add/remove mutations so concurrent label edits are not clobbered. Issue URLs are shortened to their identifier form, because Linear's own URLs end in a slug built from the title. The poll lists the queue in one paged query, and reads the followed issues and their comments since each issue's previous poll in one more query, with a five-minute overlap that the router's dedupe absorbs; the comments are read page by page, whatever their order, and an issue with more than twenty pages left goes on from the last page read on the next poll, then is read again from when its pages started. A comment body is read only for the authorship marker. |
| `github` | Through the `gh` CLI, so there is no token handling. No workflow states: the trigger is label + assignee + open. `tracker = { type = "github", repo = "owner/repo" }`; labels change with `gh issue edit` and are verified by a re-read. |

Any other system is connected as a `command` source, not as a tracker adapter (see "Event
sources and the router").

Tracker writes are treated as unreliable: a write that reports failure may have been applied,
and label swaps made of two mutations are not atomic. Every write is followed by a re-read,
and anything ambiguous is left for reconciliation (see "Launch sequence").

### Event sources and the router

A source is a poller behind one interface: `whoami`, `poll(cursor, items)` that returns
events and the next cursor, an optional `mark` for label writes, and `item_open` for garbage
collection. It is configured as a `[[source]]` table with a `name`, a `type`, and the type's
own options. An event names its source, kind, work item, actor, time, a dedupe id and a
pointer (a URL or identifier the worker fetches itself). It never carries a body: a source
reads a comment only to find chargehand's authorship marker, `<!-- chargehand-worker -->`,
and the text stays in the source. No space follows the name, because the launch deny rule
`Bash(*chargehand *)` refuses the name followed by a space in a command, and a body can
still end up on a command line; the first spelling, `<!-- chargehand -->`, is still read
as marked. Kinds are bare (`comment`, any source) or qualified by the source name
(`github.comment`).

Delivery is publish and subscribe. A worker subscribes to work items, each with a set of
event kinds; the subscription to the item a worker was created for is its primary one. Every
live subscriber gets every matching event in its own inbox, so any number of workers can
follow one pull request or issue. A source that has to ask about each item it follows is
given the union of the live subscriptions.

The router runs once per tick after the polls, in this order:

1. Filters: drop an event seen before, by its source and dedupe id, even one that matched
   no subscriber; drop an event whose actor is the signed-in account when its comment
   carries the authorship marker. That filter covers every worker, not only the author:
   workers post as you, and two workers on one pull request would otherwise wake each other
   in turn.
2. Delivery: one inbox row per matching live subscriber. The inbox is unique per worker and
   event, so routing an event twice adds nothing. A subscriber does not get an event that
   occurred more than a minute before the worker started: the sources read some minutes
   back from their previous poll so that a late write is not lost, and on a new worker's
   first read that overlap would only bring events that the worker reads by itself at its
   start. The minute allows for the clock difference between a source and this machine.
   The event is still recorded as seen, and still goes to the start rules.
3. Close: an event that a worker's start rule names in `close_on`, on the worker's primary
   item, ends that worker. The session is stopped first, then the worker is `done`, and
   garbage collection removes the session and the worktree, never over unpushed commits.
   A worker that is working finishes its turn first; the tick then finds its item closed.
   On an item the worker only follows, the same event is an ordinary delivery.
4. Start rules of the enabled roles, checked in code. A rule whose `unless_subscribed` check
   finds a live worker that has the item (`role`: of its own role, `any`: of any role)
   starts none, and the event goes to the inbox of the rule's own workers on the item, so a
   second review request wakes the reviewer that has the pull request (not one that started
   after the request, by the rule in step 2). Otherwise it
   launches a worker under the same caps as the queue, with a primary subscription carrying
   the rule's `subscribe` kinds. A start that the caps, a pause or a previous worktree hold
   back is kept and offered to the rules again on the next tick. A start event for a
   repository that no `[[repository]]` names is ignored, and reported once per source and
   repository when a role would start on its kind.

The `github` source works through the `gh` CLI, so chargehand handles no token, and it is
the only module that knows `gh`. It polls in two parts. The notifications endpoint of the
signed-in account gives `review_requested`, `comment`, `mention` and `ci_status` (GitHub's
`ci_activity`); it is asked with `If-Modified-Since` and `If-None-Match`, so an unchanged
list costs nothing against the rate limit, and no more often than its `X-Poll-Interval`
header allows. The first poll reads the unread threads only, so turning the source on does
not replay the account's history; later polls read the threads you have already read too,
from the list's `Last-Modified` on, or from the `Date` of the previous response when the
list was empty and had no `Last-Modified`, so a review request opened in a browser before
the poll still arrives. The source needs the account's "On GitHub" setting for
participating notifications (in GitHub's notification settings): while it is off, GitHub
sends review requests and mentions by email only, the list stays empty, and nothing
starts. GitHub has no API for the setting, so `doctor`, and the source once a day, compare
the open pull requests that ask the account for a review (`is:pr is:open
review-requested:@me`) with the `review_requested` threads, read ones included, and warn
with the item keys of the requests older than ten minutes that have no thread. The check
reads at most 500 threads, newest first; on a longer list it skips the requests made before
the oldest thread it read, because their thread may be further down. A review request counts only
while the pull request is open and still asks the account, or a team it is in, for a review,
because a notification thread keeps its reason when later activity updates it. When `gh`
may not list the account's teams, any requested team counts. Then, for each pull request a live worker subscribes to, the source reads the
review comments, the reviews, the issue comments, and the commit statuses and check runs on
the head commit, which gives `comment`, `review`, `ci_status` and `pr_closed`; a followed
item gets only review requests from the notifications, so nothing arrives twice. An item
that `gh` cannot read is logged and read again from the same point on the next poll; it
does not hold back the other items. The same holds for a notification thread whose pull
request cannot be read: the other threads arrive, and the list keeps its old
`Last-Modified`, so the next poll asks for the same window again. A review request on a
repository that no `[[repository]]` names is not checked with a call of its own. A review counts as marked when its body or one of its
inline comments carries the authorship marker. Dedupe ids are GitHub's object ids. An item is `owner/repo#number`; its key is `owner_repo_number` in
lower case, or a shortened form with a digest when a name does not fit a path or a branch
name, and a name that GitHub itself would refuse is skipped. A start event belongs to the
`[[repository]]` whose `github` key names its `owner/repo`; on any other repository it
starts nothing. `gh_bin` and `timeout_secs` are its options.

The `command` source connects any other system without a plugin framework: chargehand runs
a configured executable that reads the cursor on stdin, and the refs of the followed items
in `CHARGEHAND_REFS`, and prints one JSON object per event and a last line with the new
cursor. Optional commands answer `item_open` and `mark` (exit 3 means the write may or may
not have landed). The commands run as subprocesses with a
timeout and without a shell, each output line is checked against a closed event shape, and a
line that does not fit is logged and skipped. A start event belongs to one of the
repositories listed in the source's `repositories`. The contract is a public surface and is
documented in [command-source.md](command-source.md).

The source's cursor is saved after its events are routed, so a source that fails is one
error in the report and its events arrive on a later tick. An expected failure, such as an
outage, a timeout or a command that exits non-zero, is that one line on every tick it
lasts, without a traceback; only an unexpected exception also logs its traceback. The queue label starts its
workers through the same router, as a `labeled` event from the repository's tracker; a
`labeled` event from a `[[source]]` starts nothing. The implementer role starts on it,
subscribes to `comment` and declares `close_on = ["done"]`; a worker whose issue closes
without that event still ends when the tick finds the issue closed, as before. A `[[source]]` may
share its name with a tracker type, such as a `github` source next to a GitHub Issues
queue: they share subscriptions, so a comment that the source reports reaches the worker,
the tracker leaves the followed items to that source, and the tracker's dedupe ids carry
their own prefix. Only a worker the queue label started changes the label;
every other worker on the issue only reads it. In the same way, of the workers that a
`[[source]]` with a `mark` command started on one item, only the first one shows its state
there: the tick calls that source's `mark` with `running`, `blocked` or `done` as the
worker's state changes, records the status once the command confirms it, and calls it
again on the next tick after a failed or ambiguous write. The ledger records which worker
does this when the worker is created, so an edit of a role file cannot move the label to
another worker. A worker started
by another event ends when its source reports its primary item closed. A start URL from a
source reaches the prompt only when it is a plain http(s) URL, and a start that waits longer
than a week is given up with a warning.

## 4. Configuration

Two layers, so a repository can be shared without sharing machine details.

Machine layer — `~/.config/chargehand/config.toml`:

```toml
poll_interval_secs = 180
max_concurrent = 2                  # working turns, across all repositories: launches and wakes
max_open_workers = 4                # working + blocked; bounds the resume burst
max_run_hours = 6                   # watchdog: notify
hard_stop_hours = 10                # watchdog: stop the session
background_shell_mins = 30          # notify when a background shell outlives its turn this long
worktree_root = "~/chargehand-worktrees"

[notify]
command = "~/.config/chargehand/notify.sh"   # receives ITEM, ROLE, STATE, URL, REPOSITORY as env vars

[[repository]]
name = "my-app"
path = "~/code/my-app"
github = "me/my-app"                # owner/repo: what github source events are mapped by
tracker = { type = "github", repo = "me/my-app" }
roles = ["implementer"]             # role files enabled in this repository

[[repository]]
path = "~/code/other"
tracker = { type = "linear", team = "ABC", state = "Todo" }

[[source]]                          # an event source; the other keys belong to its type
name = "github"                     # defaults to the type; qualifies kinds as github.<kind>
type = "github"
```

`[[route]]` with `repo` and `max_open_attempts` are the older spellings and stay accepted.

The daemon reads two more tables. Every key is optional; these are the defaults:

```toml
[serve]
port = 7878                         # on 127.0.0.1; no key binds another address
token_file = "~/.config/chargehand/board.token"   # next to config.toml; mode 0600
tick_interval_secs = 60             # how often the daemon ticks
usage_file = "~/.cache/chargehand/usage.json"     # the usage readings go here

[usage]
wake_reserve_percent = 20           # no wake while the week is above 80 %
new_worker_reserve_percent = 30     # no new worker while the week is above 70 %
poll_secs = 300                     # how often the daemon asks Claude Code; 0 is off
```

The reserves keep a part of the subscription's weekly usage window for your own sessions (see
"Admission" in section 5 and "Delivery" in section 6).

`poll_interval_secs` no longer sets how often the tick runs. It is still the refresh
interval of `watch`, capped at 30 seconds.

Repository layer — `.chargehand.toml`, checked in:

```toml
base = "origin/main"
setup = "scripts/setup_worktree.sh {worktree}"
branch_prefix = "chargehand/"       # the placeholder branch each worker starts on
```

Role files — `.chargehand/roles/<name>.toml` in the repository, or
`~/.config/chargehand/roles/<name>.toml` for every repository that has none of its own:

```toml
# .chargehand/roles/implementer.toml
runtime = "claude"                  # the coding agent, see "Runtimes"; claude is the default
agent = "implementer"               # optional: .claude/agents/implementer.md, passed as --agent
model = "opus"                      # optional, passed as --model
effort = "high"                     # optional, passed as --effort
prompt = "/work-issue {item}"       # or just "{item}"
permission_mode = "auto"            # auto | manual | acceptEdits | dontAsk | plan | bypassPermissions
allow = ["Bash(gh pr *)"]           # optional allow rules for this role's sessions
permission_timeout_secs = 600       # optional: how long a permission prompt waits for the board
setup = "scripts/prepare.sh {worktree}"   # optional, runs after the repository's setup
status_file = "/tmp/chargehand/{item}/status.json"   # optional, see "Supervision"

[[start]]                           # create a worker when an issue gets the queue label
on = "labeled"                      # any tracker; "linear.labeled" names one
unless_subscribed = "role"          # for later sources; the queue skips an item any worker has

[policy]
max_workers = 2                     # open workers of this role, across repositories
max_wakes_per_day = 12              # wakes of one worker over the last 24 hours
debounce_secs = 120                 # optional: a wake waits this long after the newest event
refresh_after_wakes = 20            # optional: a fresh session after this many wakes; 0 for never
refresh_after_tokens = 120000       # optional: a fresh session past about this many tokens; 0 for never
```

A repository whose `.chargehand.toml` still carries `prompt`, `permission_mode` and
`status_file`, and has no role file, gets an `implementer` role built from them, so a
configuration from before roles keeps working.

Template variables are `{item}`, `{item_url}`, `{item_checkout}`, `{worktree}`, `{branch}`,
and `{repo}`; `{issue}` and `{url}` are the older names for the first two. `{item_checkout}`
is the separate checkout of the item's head for a role with `checkout = "item"`, and the
worktree for any other role. There is deliberately no
`{title}` or `{body}`: the prompt is a trusted user message, so issue text must reach the
agent as a tool result it fetches itself, never inlined. A configured template that names any
other variable is a configuration error, not a literal.

### Roles and workers

A role is a template; a worker is one member of it, with its own worktree and session, and
a role may have any number of workers. A worker usually has one work item, the issue it was
created for. Its worktree is `<worktree_root>/<role>/<item>`, its placeholder branch
`<branch_prefix><role>/<item>`, and its session is named `<role>/<item>`, which is also how
the CLI and the logs name it.

A start rule creates a worker on its own. A start event arrives again on every poll, so the
ledger allows at most one live worker per role and item among the workers that start rules
created, enforced by the database. The ledger already records each worker's origin, so a
later way to create workers by hand is not held to that limit.

chargehand ships two starter roles, which `chargehand init --role <name>` writes into a
repository. `implementer` takes issues from the queue. `pr-reviewer` starts on
`github.review_requested`, follows `comment`, `review` and `ci_status` on its pull request
and ends on `pr_closed`; it comes with a Claude Code agent file,
`.claude/agents/pr-reviewer.md`, which must be on the base branch or in `~/.claude/agents`,
because the session starts in a checkout of `base`.

With `checkout = "item"` the session's worktree is still a checkout of `base`: the pull
request head is its author's content, and Claude Code loads settings, hooks and
instructions from a session's working directory (see SECURITY.md). The head goes into a
second, detached worktree, `<worktree_root>/.items/<role>/<item>`, where the role's
`setup` step runs, with git hooks turned off and its program taken from the base checkout,
the main checkout or an absolute `PATH` entry; for the reviewer that is `gh pr checkout
{item_url} --detach`. The step runs again before each wake, so the checkout follows the
item's head, and then its program comes from the main checkout or `PATH` only, because the
session has worked in the base checkout since; a wake whose step fails waits like one a
cap holds. Every git call chargehand makes, and the setup subprocess, run with
`core.hooksPath` and `core.fsmonitor` off, so no repository config runs a program. The
prompt names the checkout as `{item_checkout}`, and the session may read it without a
prompt. After the setup step, the tick records the files its runtime loads from a working
directory (for Claude Code: `.claude/`, `.mcp.json`, `CLAUDE.md`, `CLAUDE.local.md`;
tracked, untracked and ignored, symlinks by their target). Before each wake and each
`continue`, it checks that the item checkout is still a detached worktree that git lists
at its own path, that neither checkout's `.git` was replaced with a symlink or redirected
to a git directory the session wrote (the `.git` file is read as data, compared with the
record the main checkout keeps), that the session's worktree is still on its branch and
that those files are as recorded, and it checks again after the refresh step so a step
aimed at the session's own worktree shows as a worktree off its branch; otherwise the
worker is paused with a `wake-fault`, and `continue --force` records the files as they
are, for an operator who has looked at them, while a worktree off its branch or a gone or
redirected item checkout is not accepted. The launch checks that the role's agent file is
in the worktree or the user's files, because an agent file that is only in the main
checkout is not there. The commit the setup step leaves the item checkout on is recorded,
so garbage collection does not count the pull request's own commits as unpushed work, also
when they come from a fork or a fetch pruned their branch; a newer head that a later `gh
pr checkout` fetched counts as the pull request's too. Garbage collection removes both
checkouts, and either one with commits of the session's own keeps both.

Workers post as you. Both starter roles tell the worker in the prompt to end every comment
it posts with the authorship marker, so its own comments wake no worker; a comment without
the marker wakes the worker once, and the role's daily wake cap stops a loop.

Workers post every body from a file. A launch deny rule matches the whole command text, so
a body on the command line that names a denied word is refused: a review of a change to
the deny rules names them, and the worker would stop with nothing for the operator to
answer. Both starter roles, and the reviewer's agent file, tell the worker to write each
body with its file tool, not a shell command, to a new file in `~/.cache/gh-body/`, to
post it with `--body-file` (`gh pr create`, `gh pr comment`, `gh pr review`) or
`-F body=@<file>` (`gh api`), and to delete it after the post. The file's content never
reaches the rules. The directory is outside every checkout, so a body file cannot be
committed and does not count as untracked work for garbage collection or the wake checks.
It is per user, and the worker creates it with mode 0700 before its first write, so
another account on the machine cannot read a draft or replace it before the post; a shared
directory under `/tmp` would allow both. `mkdir -m` does not change a directory that
already exists, so a directory created another way keeps its own mode. The roles' allow rules let the worker create the directory and write
there without a prompt, and name only the `--body-file` forms of the `gh` commands. The
reviewer's agent file lists the Write tool for this, and says to use it for nothing else.
Deleting a file has no rule, and `gh api` has none either, not even for the reply
endpoint: a `*` also matches spaces, so `rm ~/.cache/gh-body/*` would also match a second
path, and a reply rule would also match a call that merges the pull request with the rest
of the pattern in its arguments. Those calls go to the auto-mode classifier.

### Runtimes

A runtime is the coding agent a role's workers run on. Each runtime is one module, and only
that module knows the agent's command line. The interface is small: launch a session in a
worktree, wake it with a message, stop it, list the sessions with their states, the command
an operator runs to open one, and fork. Session ids are opaque text that only the runtime
reads.

Each runtime declares its capabilities: live attach, in-place wake, permission prompts that
can be answered from outside the session, usage readings, fork, and whether a supervisor
outside chargehand hosts its processes. The tick and the CLI check a capability, never the
runtime's name, and the board will show what a worker's runtime cannot do.

Role files stay flat. The common keys (`runtime`, `prompt`, `model`, `effort`, `checkout`,
`setup`, `status_file`, `[[start]]`, `[policy]`) sit next to the keys of the role's runtime,
and the runtime checks its own: `agent`, `permission_mode`, `allow` and
`permission_timeout_secs` belong to `claude`.
A key that neither the common set nor the runtime knows is an error, so a role written for
one runtime does not start silently on another.

`claude` is the only runtime that ships and the default. It wakes a session by stopping its
process, confirming that it stopped, and resuming it by the full session id with stdin
closed; a resume that starts a copy instead is reported as a fault. With the chargehand mod
in the session, the wake goes into the session's outbox instead, and the process goes on
(see "The chargehand mod" in section 6). The test suite has a
second runtime with fewer capabilities, whose processes chargehand hosts, and the tick tests
that must behave the same run on both.

Fork copies a session into a new one that keeps the conversation; a clone in full mode
uses it (section 7). For `claude` it is `claude --resume <full session id> --fork-session
--bg -n <name> --permission-mode <mode> --settings <file> --model <model> --effort
<effort> "<prompt>"`, run in the clone's own worktree with stdin `/dev/null`, after the
same trust entry and settings file as a launch. Observed on 2026-11-03 with Claude Code
2.1.317, against an idle original in another worktree of the same repository: the resume
finds the conversation from the new worktree; the copy lists with the new worktree as its
working directory and the given name, its transcript is kept for the new directory, and
the original's transcript and session are unchanged; the copy keeps the earlier turns
(it answered with a word the original was told to remember) and runs its new turn on the
given model and effort, in the given permission mode; `-n` and `--settings` apply, and
its hooks reach the daemon under its own session id, SessionStart included, with stdin
`/dev/null`; a stop and an in-place resume of the copy keep its model, effort and
permission mode; and the original still wakes in place in its own session. A copy
without the flags falls back to the default effort and permission mode, so they are
always passed again.

A command template is split into arguments *before* its variables are substituted, and nothing
is run through a shell, so a value containing spaces or shell metacharacters stays one argument.

## 5. Launch sequence

### Admission

A new launch needs `working < max_concurrent`, `working + blocked < max_open_workers`, the
repository's own `max_concurrent` when it has one, the role's `max_workers` when it has
one, and the usage reserve for new workers: the weekly window of the role's provider at or
below 100 minus `new_worker_reserve_percent` (70 % by default). The usage readings are kept
per provider (window kind, percent used, reset time), and each runtime names the provider
its sessions draw on; only `claude` has readings, from the usage file that the daemon's
own usage request, the worker sessions and the status line wrapper write. No usage file is
no reading, and no reading means no reserve. The newest reading counts however old it is:
when the usage request fails, the last reading is the best one there is. Once the weekly window's reset time has passed, its percentage belongs
to the week before, so the reading holds nothing. A window without a reset time holds
nothing either, because nothing would end it. The check covers the start
rules, the board's new workers, clones and every later way to start one. A start that the reserve
holds waits as one that a cap holds. A request (a new worker from the board, a clone, or the
`retry` of either) is refused with the reason and is not tried again; a refusal by the reserve
names the time of the weekly reset, because only the reset lets a new worker start again.

Blocked runs deliberately do not hold a launch slot: a run waiting overnight for an answer
would otherwise stall the queue. The runner also cannot gate resumes — replies go straight to
the session. So `max_concurrent` is a launch throttle, not the memory bound. The memory bound
belongs to a lease tool (see "Sharing devices"): however many sessions resume at once, only
so many heavy builds or emulators should run. A session that is reading code or waiting for a slot is cheap.

### Steps

Write-ahead: the ledger row is written before any external side effect, every step is
idempotent, and the row's `step` advances after each one.

1. **Ledger first.** Insert the worker: `state = launching`, `step = 0`.
2. Swap the labels, then re-read the issue to verify. A write that reports failure is not
   assumed to have failed; the row stays `launching` and reconciliation settles it.
3. Fetch, then create the worktree on a placeholder branch (skip if it already exists).
   A clone's branch starts from its original's current branch (the branch the original's
   worktree is on now, which follows a rename, else its recorded branch), so its commits,
   pushed or not, carry over; its uncommitted changes do not. Every other worker's branch
   starts from `base`. The session may rename the branch later. Removing the worktree deletes the branch
   under its new name too, when git's reflog records the rename; a branch the session
   switched to in any other way is kept, and the result says so.
4. Run the repository's setup script in the worktree, then the role's. Failure marks the
   worker failed.
5. Start the background session, named `<role>/<item>`, with the role's agent, model,
   effort, permission mode and allow rules. A clone uses the prompt template, model and
   effort chosen for it where it has them, filled in with its own worktree and branch;
   in full mode the session is a fork of the original's, with one line before the prompt
   that names the clone's worktree and branch and says that the original's worktree
   belongs to another worker. In summary mode the session is new and loads the mod; the
   line before the prompt names the item (or the original's id handle), the
   `read_handoff` tool, the clone's worktree and branch and the original's worktree, and
   never the note itself. Right before the launch, the original's handoff note and its
   time are copied into the clone's own row, so the tool gives the clone that note, and a
   later note or a discard of the original does not change it. A note is current when it
   was written at or after the start of the original's last turn (`working_since`, set at
   the launch, at each wake and when the listing shows a turn that the operator started);
   the end of the turn is never compared, because a note written in a turn is always
   older than its end. When the note is not current and a wake can reach the original
   (it has a session and is not ended or paused), this step waits: it puts one inbox
   entry of kind `handoff` with the pointer
   `write-a-current-note-with-the-handoff-tool-for-clone-<id>` into the original's inbox,
   once per clone, and delivery wakes the original with it under the usual caps; the
   `handoff` tool's description says that a wake line `handoff: ...` asks for a current
   note in that turn. Waiting uses up no launch attempt, and the clone's card shows the
   reason. After 10 minutes from the clone's creation, or at once when no wake can reach
   the original, the clone starts from the stored note and its card says so; without any
   note it fails. A summary clone that ends while it waits (cancel, discard, a failure)
   takes its inbox entry back from the original and its line off its card, so the
   original is not woken for it later.
   Starting inside a linked worktree means Claude Code does not create one of its own.
6. Find the new session in the listing by name or working directory and record its ids.
   `state = running`. Looking it up rather than scraping the launch output is what makes
   this step idempotent: a tick that died between starting the session and recording it
   adopts the session it already started instead of starting a second one.

### Reconciliation (start of every tick)

Ticks never overlap (see the tick lock below), so a row still `launching` when a tick
starts is an interrupted launch, not a concurrent one.

- **Ledger side:** for a row stuck in `launching`, look for a session whose working
  directory is the row's worktree or whose name is the worker's name (or the issue
identifier alone, for a session from before roles). Found → adopt it.
  Not found → resume from the recorded step. After three attempts → fail loudly and mark the
  issue blocked. Never silently re-queue; that would loop.
- **Tracker side:** an issue assigned to you that carries the "running" label but has no
  live ledger row is marked blocked and reported. This catches a lost ledger and label writes
  that landed without a row.
- **Session side:** a background session under the worktree root with no ledger row is
  adopted when the role and the issue can be derived from its name or path, otherwise
  reported. An adopted session's primary subscription keeps the time of the row it
  replaces, so the events since that start still reach it; its new row has the time of the
  adoption, so a status file from the earlier run stays stale. A worktree is compared by
  its real path, because the listing names a worktree under a linked root (such as `/tmp`
  on macOS) by the path that the link leads to. A session that a row of the `sessions`
  table names for a worker, but that is not that worker's current session, such as one
  that a refresh replaced, is never adopted: it belongs to its worker, and one that works
  again is reported once. No unattended session runs outside the watchdog.

The ledger is a SQLite database under `~/Library/Application Support/chargehand/`, which
survives reboots. A partial unique index over live rows that a start rule created makes two
such workers of one role for one issue impossible at the database level rather than by a
check the caller might skip. Every session a worker owns is recorded in a `sessions` table.
Subscriptions, inboxes, the events the router has seen, and each source's cursor are kept
in the same database.
The queue status last *verified* on the tracker is stored separately from the worker's own
state, so a write that could not be confirmed is retried on the next tick instead of being
assumed to have landed.

The daemon runs one tick at a time, but `chargehand tick` is also a command, so every tick
takes an exclusive file lock rather than assuming it is alone. The daemon holds the lock
only while one of its ticks runs, so a tick by hand waits for it or reports that a tick is
running. The kernel releases the lock when its holder dies, so a killed tick needs no
recovery.

## 6. Supervision, notifications, questions

**Session state** comes from two places. Each session is launched with settings hooks that
post to the daemon: `SessionStart` with its source, `UserPromptSubmit` when a turn
starts (its prompt is never read), `Stop` with the last answer, `StopFailure` with the
error category, `Notification` with its type, and `PermissionRequest` with the tool and its
input. These fire in unattended background sessions. Five of them are hooks of type `http`.
Claude Code does not run an `http` hook on `SessionStart`, so that one is a `command` hook:
it runs `hook_post.py` from the installed package with `python -I`, which imports nothing
from the worktree, reads the session's token from its token file, posts its stdin to the
daemon, prints nothing (the output of a `SessionStart` command reaches the model) and
exits 0 within a few seconds, also when no daemon answers. The settings of a launch with hooks are in a file of mode 0600 under the state directory,
one per worktree, so the token is never on a command line. The file stays while the
session exists, because a resume, a respawn and an attach apply the saved `--settings`
again; it is deleted when the session is removed (`discard`, `retry`, garbage collection).
In most launches whose stdin is /dev/null or a pipe already at its end, a new session runs
no `SessionStart` hook; it runs one in every launch whose stdin is a pipe that stays open.
So a launch gets an empty pipe that chargehand closes when the launcher exits, and does not
depend on the daemon's own stdin, which is /dev/null under launchd. The daemon applies each
post to the ledger at once, through the same transition and notification code as the tick.
`claude agents --json --all`, diffed against the ledger on every tick, stays the fallback
for anything a hook missed: a session launched before the daemon wrote its token, a post
while the daemon was down, a process that died.

**A token per session.** Each launch and each fork writes a new token for its session
into `worker-tokens/` under the state directory (mode 0600, directory 0700, one file per
worktree). The session's hooks and its mod send it, and the daemon maps the token to the
worker: the token, not the session id or the working directory that a post names, says
which worker posts. The board's token never goes to a session. A token ends with its
worker: the daemon refuses the token of a worker that ended (its mod's outbox poll is
answered `ended`), a new launch in the same worktree replaces it, and the file is
deleted with the settings file when the session is removed. A tool call acts as the
token's worker, whatever session id it names. A hook, a hello, a lifecycle or usage post,
or an outbox poll from a session that is not the worker's current one, such as a copy
that a resume started in the same worktree with the same settings, is ignored: the post
changes nothing, and the poll gets no message. Every launch's settings deny `Read` and
`Edit` (which covers Write) on the board's token file and on the whole `worker-tokens/`
directory, a launch without hooks too, so a session launched earlier is also denied the
token files of sessions launched after it. A launch with hooks also denies them on every
file in `session-settings/` (no tool of a session reads one: Claude Code loads its
`--settings` itself). The session's own token file is under the denied directory as well:
the mod reads it with `$.fs.read`, which a `Read` deny rule does not bind. No `Write` rule
is written: Claude Code 2.1.322 warns at the start that a `Write` deny rule never matches
and to use `Edit`. Like the deny rules, this is a guardrail: a shell command of the same
user can still read another session's file. The token is not kept in the ledger: the launch
runs in the runtime, which has no ledger, and the daemon finds the file that holds a
token and then the newest worker in that file's worktree, a live one first.

How a session gets the token, observed with Claude Code 2.1.322 in `--bg` sessions that
loaded the mod with `--plugin-dir`: a variable in the settings `env` of the launch reaches
the mod (`$.env.get`) and the session's Bash tool. A variable in the environment of the
process that runs `claude --bg` reaches the session only when that launch also starts the
background service that hosts the sessions; a later launch's environment does not reach
its session, which inherits the service's environment (see also "Settled by launching a
session through the scheduled job"). The mod's `$.fs.read` read files that a `Read` deny
rule named, by a file rule and by a `dir/**` rule, while the Read tool was refused on
both. So the token goes into the settings file, in the http hooks' `Authorization`
header, and into its token file, which the settings `env` names for the mod and the
`SessionStart` command names for hook_post.py.

**Hooks.** A post goes to `POST /hooks/<event>` with the session's token, which an http
hook carries in its `Authorization` header and the `SessionStart` command reads from the
session's token file; a post without a valid token changes nothing. The runtime reads
the payload (`claude` reads `session_id`, `cwd`, `transcript_path`,
`last_assistant_message`, `error`, `notification_type`, `tool_name`, `tool_input` and
`source`), and the post maps to the worker whose token it carries; a worker that ended
changes nothing. A session launched before sessions had their own token posts with the
board's token, which `/hooks` still accepts, and the daemon logs that once per session;
its post maps to the live worker with that session id, else to the live worker without a
session id yet whose worktree is the session's working directory, after symlinks are
resolved. A post that maps to no live worker changes nothing. Every answer
but a held permission prompt's is `{}`: a `Stop` gets no decision that would keep the turn
running, and a `SessionStart` gets no context for the model. A hook that times out, or
finds no daemon, is an error that Claude Code reports and goes past; the session runs on,
and the next tick reads its state from the listing.

**Permission prompts on the board.** For a runtime with board permissions, the daemon
holds a `PermissionRequest` post open, and the worker's card in Needs you shows the tool
and its input with Allow, Deny and Always allow. The `PermissionRequest` hook waits
`permission_timeout_secs` (a `claude` role key, 600 seconds by default, 20 to 3600); the
other hooks keep their 10 seconds. A session keeps the timeout of its launch, and the
daemon reads it from the session's settings file: a session launched with the old
10 seconds is not held. A prompt is a ledger record: `open`, then `resolved` (with the
decision and who decided: `board` or `rule`) or `cancelled` (by the `session`). The board's
answer, `POST /api/workers/<id>/answer` with `prompt` and `answer` (`allow`, `deny` or
`always`), resolves the row only while it is open and a hook still holds it, so the first
answer wins and a second click answers 409, "already resolved"; the held hook then
returns Claude Code's decision. Before it writes, the answer asks the runtime whether the
session still shows a permission dialog (`claude agents`: `blocked`, waiting for
"permission prompt"). Claude Code keeps the hook's connection open after an answer in
`claude attach`, and no hook may come for a long time after it, but the listing shows the
answer within a fraction of a second. When the session no longer shows the dialog, the
click is refused with 409, "already resolved", and the record is cancelled at once; when
the listing cannot be read, the click is refused and the record stays open. Always
allow also records a rule for that worker: the same tool with the same input, matched by
a hash of the whole input (except a Bash call's `description`, which the model words
anew for each call). The next such post
is allowed at once, with no card and no notification, and is recorded as resolved by the
rule. A rule never changes the role, the repository or Claude Code's settings.

The hook answers `{}`, so the session shows its own dialog, in three cases: the record is
cancelled because the session no longer shows the dialog; five seconds before the
hook's timeout; and when the daemon stops. A record is cancelled when the worker leaves
`blocked`, when its turn or session ends, and when a wake or a continue starts a new turn
or session; a record from that new turn, recorded while the wake or the continue ran,
stays open, and so does one recorded during a launch. An answer in `claude attach` sends
no hook, and the tick may be a minute away, so while it holds hooks the daemon also looks
at the session listing, half a second after a hold starts and then every 2 seconds, one
listing for all held hooks on a thread of its own, and cancels a record whose session
showed the dialog and no longer does. After a timeout or a restart the record is
*released*: it stays `open`, because the session still shows the dialog, with
`released_at` set, so no hook holds it, and the card says to answer in the session. The
record has a `kind` and an `options` list, so a question with its own options uses the
same record and card: the mod's `ask` tool records one as kind `question`, which no hook
holds. The card shows its options as buttons and a one-line answer field. The first answer
resolves the record (`resolved`, by the board) and is recorded as a `reply` request, so the
tick puts it into the worker's inbox and delivery wakes the worker with it, through the
exception for a blocked worker whose turn ended with a question; a second answer gets 409.
Any reply, from the card's Reply or from `chargehand reply`, answers the question the same
way. A question is cancelled when the worker leaves `blocked` by another path: a pause, an
end, or a new turn once the turn that asked it has ended, such as one that the operator
starts in `claude attach`. A permission prompt of the same turn, and its answer, leave it
open. Every worker launches with
`AskUserQuestion` denied in its launch settings, beside the configured deny rules, so no
configuration drops it: the session asks through `ask` instead.
`--resume` re-applies the settings of the launch, so a worker launched before the hooks
existed is followed by the diff alone.

| Post | Effect |
|---|---|
| `SessionStart` | the worker is `running` |
| `UserPromptSubmit` | the worker is `running` |
| `Stop` | as a `done` session: the worker's report or open question, then the status file, then `idle` |
| `StopFailure`, usage limit | the worker is `parked`, with the reset time when the message gives one; a limit of one model family also sets the board's model-limit badge |
| `StopFailure`, any other error | the worker is `blocked`, "session failed" |
| `PermissionRequest` | the worker is `blocked`, the prompt is recorded with its tool and input, and the post waits for the board's answer; with a matching Always allow rule, it is allowed at once and nothing else changes |
| `Notification` `permission_prompt` | the worker is `blocked` |
| `Notification`, other types | no change |

A hook never moves a `launching` or `paused` worker. The tick and the hooks write the same
rows from two threads, so the diff's writes and a hook's state write succeed only while
the row has the state and the last hook they were read with; the loser reads the row
again. A listing that the tick asked
for before a hook arrived is older than that hook, and the diff leaves that worker until
the next tick. The last answer, the tool input and the other payload text are untrusted:
they are cleaned of control sequences and capped when stored, kept out of notifications,
and escaped when the page renders them. `/api/state` shows each worker's last hook, its
last answer, its reset time and its open permission prompts. An open prompt is cancelled
when the worker leaves `blocked` by any path, or when its turn or session ends, a `stop` or
a `cancel` included.

The usage-limit category has not been observed yet (a missing model gives
`model_not_found`), so it is matched defensively: a category such as `rate_limit` or
`usage_limit`, or an unnamed one whose message says so. The raw category is kept in
`last_error`. The reset time is read from an epoch, an ISO 8601 time, or a clock time such
as "resets 3pm (Europe/Paris)"; a time it cannot read leaves the reset unknown. The same
check reads a `failed` session in the listing: a category or a message in its error
fields that names a usage limit parks the worker as the hook does, for a session whose
hook did not arrive. The listing can show such a failure hours late, for example after the
daemon was down, so a clock time from it is placed after the end of the failed turn when
the listing gives that time, and otherwise after the start of the turn, at most five hours
back. The listing of Claude Code 2.1.319 gives neither: its entries have no error fields
and no end time, so there only the StopFailure hook parks a worker. A message that names a
model family's own limit, such as "Opus weekly limit reached", also records that family
for the board's top bar, because the usage window has no per-model part. Claude Code can
still name the limit of a window that the subscription no longer has (it had a shorter
window until 2026-11-03); such a message parks too.

| Session state | Meaning | Action |
|---|---|---|
| `working` | A turn is running | — |
| `background` | Listed as `working`, or `done` but busy, while the turn ended and no turn started since: a background shell runs | keep the state of the turn end; notify an `idle` or `queued` worker once after `background_shell_mins` |
| `blocked` | Needs you: a question, a permission prompt | notify, mark blocked |
| `done` | The turn finished | a report of `done` or `failed` from this turn ends the worker; an open question from the `ask` tool blocks it; else read the status file if configured; the worker is `idle`, which is not notified, or `blocked`, `done` or `failed` as the report, the question or the status file says, which is |
| `failed` / `stopped` | Error, stopped, or killed with the machine | notify, mark blocked |

A live worker is `launching`, `running` (a turn runs), `idle` (the turn ended without a
question and the worker waits for events), `queued` (events wait in its inbox for a wake
that admission held back), `blocked` (it needs you), `paused` (stopped by `stop`) or
`parked` (a usage limit ended its turn; it waits for the reset). On the tracker, `idle`,
`queued` and `parked` keep the blocked label, as a finished turn did before: the operator
may want to look at what the turn left. The watchdog and the session diff never move a
`parked` worker. Parking is not a failure: it uses no launch attempt and no retry, and it
is notified once. Once its reset time has passed, delivery resumes the worker in the same
session with a short continue message, oldest reset first, through the same admission as
a wake, so a long list of parked workers does not use the new window at once. A worker
parked without a reset time waits for `continue`, which respawns it. The resume also waits
while the tick's listing does not show the session with its turn ended or stopped, and the
card says why: the session has a turn or a prompt, it is not in the listing, its state is
not recognised, or the listing failed, which the tick also reports as an error. The reset
time belongs to `parked`: every state write other than `parked` clears it, so a worker that
leaves `parked` by any path, such as a turn that the operator started and that ended, keeps
no old reset time.

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

A worker disambiguates that with its **report**: the mod's `report(state, summary, links)`
tool (see "The chargehand mod" below). A report of `done` or `failed` in a turn ends the
worker at the end of that turn, as the status file's `complete` and `aborted` do: `done`
clears the label, and `failed` keeps the summary as the worker's problem, out of the
notification. A report counts for the turn it was made in, from the last wake on (with a
margin of a few seconds, because the wake is recorded once its resume returned), or from
the launch. `working` and `needs-input` are card text only. A plain https pull request
link among the links becomes the card's link, and the worker follows that pull request as
it follows the pull request of its own branch. A worker that never calls `report` is still
supervised by the hooks and the state diff, as below.

The **status-file protocol** is deprecated: it stays for one release, for repositories
whose prompts still write it, and goes away in a later release. Whatever the prompt runs
writes a small JSON file at its yield points and when it finishes.

```json
{ "state": "needs-input | complete | aborted", "stop_reason": "...", "pr_url": "..." }
```

The file is what separates a run that succeeded from one that gave up, and it is where a
pull-request URL comes from. Without it, or when this worker has not written it yet, a
`done` session makes the worker `idle`. `needs-input` makes it `blocked`, so no event wakes
it over a question; a reply from the board does (see "Delivery").

**Delivery.** Once per tick, after the state diff, each `idle` or `queued` worker with
events in its inbox gets one wake. Its events become one message: a header line, then one
line per event with its kind, a colon and its pointer, for example
`review_requested: https://github.com/o/r/pull/1`, and nothing else. Two events of one kind
on one pointer give one line; one pointer with two kinds gives two lines. A pointer that is
not a plain http(s) URL or identifier is left out, and a kind that is not a plain lower-case
name (`[a-z][a-z0-9_]{0,63}`) is written as `event`, so its pointer still arrives. A reply
typed on the board (section 7) is the one line that carries text:
`reply from the operator: <line>`, cleaned to one line of at most 500 characters. The role
prompts and the `pr-reviewer` agent file say what each kind asks the worker to do. The
wake waits first for the role's `debounce_secs` (0 by default): until no new event has
arrived for that long, so a burst of review comments becomes one wake, but at most five
times that long after the oldest waiting event, so a steady stream of events, such as CI
results during a long run, does not hold the worker for ever. A reply from the
board, a due `request_wake` timer and a summary clone's request for a current handoff
note (section 5) do not wait, because the operator waits for the answer, the worker chose
the time itself, and the clone's launch waits for the note; the waiting events go along
with them. The
wake must then pass admission: the number of working turns below `max_concurrent`, counted
over the workers of all repositories, the role's
`max_wakes_per_day` over the last 24 hours, and the usage reserve: the weekly window of the
worker's provider at or below 100 minus `wake_reserve_percent` (80 % by default). No
reading means no reserve; a reading counts however old it is, until the window's reset
time has passed (see "Admission" in section 5). A worker that waits becomes `queued` and
keeps its events, and the reason it waits is kept for the board. A worker that reaches its
daily cap is notified once, with the state `daily-cap`, and again only after its next wake.
A `/loop` that a role's prompt starts runs its own turns, which chargehand does not start:
the debounce and the daily cap do not count them, and no cap stops them. While such a turn
runs, the worker is `running` and holds one slot of `max_concurrent`. A session whose
chargehand mod said hello gets the message in its outbox (see "The chargehand mod" below).
Any other session is woken by the runtime: `claude` stops a live idle process, confirms
that it stopped, and resumes it by the full session id. It reads the listing again first:
a session that started a turn or opened a prompt since the tick's listing is left alone,
and its events wait. After the wake, one ledger transaction marks
the events delivered and records the wake on the worker and as a `wake` row in `sessions`.
The worker becomes `running` only while no hook came in since delivery read it: a hook
of the new turn, such as a permission prompt, wrote the state the session shows, and the
wake then adds only its own record. `continue` writes its state the same way.
A crash between the two leaves the events in the inbox, and a later tick wakes the worker
with them again, which costs one turn. A resume that starts a copy is a fault: the worker
is paused with the reason, keeps its session and its events, and you are notified. A
`blocked`, `paused`, `parked` or `launching` worker is not woken; its events wait.
The one exception is a reply from the board to a `blocked` worker whose turn ended with a
question: the reply is the answer, so it wakes the worker, through the same admission,
with every event that waits. The session then lists `state: blocked` with `status: idle`
(or `blocked` without a process), or `done` when the `ask` tool or the status file asked
the question, and no permission prompt is open in the ledger. After `claude stop` a question-blocked
session lists `stopped`; the reply then waits, and `continue` respawns the session first.
Another event does not wake it, because only the operator answers a question. A
permission prompt and an `AskUserQuestion` dialog list `status: waiting`; a stop would
discard them, so the reply waits until the turn ends. An answer that admission holds
keeps the reason on the worker's card, as a queued worker does.

**Session refresh.** Each wake continues the same session, so the worker keeps what it
read and decided. A long session gets more expensive with every wake, because each wake
reads the whole conversation again, and after an hour without a turn its prompt cache is
cold. The role's `refresh_after_wakes` and `refresh_after_tokens` set a limit; without
them, or with 0, a worker keeps one session. Once the current session of an `idle` or `queued`
worker has taken `refresh_after_wakes` wakes (the `wake` rows since its newest session
row), or holds at least `refresh_after_tokens` tokens, its next admitted wake starts a
fresh session for the same worker instead. The size is the context that the mod reported
after the last turn, as a count, or as a percentage of a 200,000-token window; without a
report, it is the transcript's size on disk at about four bytes per token. The mod sends
the count, which decides. The ledger keeps no window size, so a percentage alone reads a
session of a model with a larger window as smaller than it is: the refresh comes later,
never earlier. A runtime
whose fresh session would not load the chargehand mod, and so has no tool for the note,
keeps the session, with a warning. The tick reads the listing again and leaves a session
alone that started a turn or opened a prompt since its listing; otherwise it stops the
old session and confirms the stop, so two sessions never run in one worktree, drops the
old session's open outbox message, and launches a new one in the same worktree under the
worker's name, with the mod. Its first prompt is chargehand's own text, which names the
item and the `read_handoff` tool, then the role's prompt, then the waiting events as the
usual lines. When no note is stored, the text says so and sends the session to the
worktree, its branch and the item instead of the tool. The handoff note never reaches
that prompt: the session reads it as the tool's result. So a role with a refresh limit
should have its worker call `handoff` at the end of every turn; the tool's description
asks for that too. One ledger transaction then switches the worker's session ids, clears
what described the old session (its context figures), keeps the old transcript path on
the old session's row, records a `refresh` row in `sessions`, which the daily wake cap
counts, and records the wake. The old session is stopped and kept, with its row, for
`claude --resume`; discard, retry and garbage collection remove every session of the
worker, and an archive holds the transcript of each one. The launch writes a new token
for the new session, and its hooks and its mod's hello find the worker by that token, as
during a launch; until the new ids are recorded, a post with that token from any session
id counts as the worker's. A hook that came in during the launch, such as a permission prompt, is newer
than the wake, and keeps the state it wrote; a post that the old session sent while it
stopped is older, because the tick reads the worker again once the stop is confirmed. A
crash in that window leaves the events in the inbox, and the next state diff takes the
new session as the worker's, with the same switch as a refresh that ran to its end. The resume of
a `parked` worker, the answer to a question that the session asked since its last wake
(whether the listing shows that turn end as `blocked` or as `done`), and a summary
clone's request for a current note stay in the session they belong to, because only that
session holds the conversation they are about; the refresh then comes at the next wake.
A reply that answers no question refreshes as any event does, so the limit also holds
for a worker that follows no item and gets only replies. The card lists the worker's
sessions.

**The chargehand mod.** Every worker session loads a small Claude Code plugin that ships
in the package (`templates/mod/`, plugin name `chargehand`) with `--plugin-dir`, at its
launch and at a full-mode clone; Claude Code stores the flag with the job, so the mod
comes back after a stop and a resume or a respawn. The session loads a copy of the mod
in the state directory (`mod/`), which each launch brings up to date: Claude Code writes
its API types and a `tsconfig.json` into the folder of a mod it loads, and the copy keeps
them out of the installed package and the checkout. The launch settings tell it where the
daemon is, in their `env`: the port and the session's own token file, never the token
or the board's token file. Without them it
posts nothing; it has no default port. It is the transport of the agent channel:

- At the session start it says hello (`POST /channel/hello`), registers each tool of the
  daemon's registry (name, description and input schema, defined once in the daemon, so a
  new tool needs no change in the mod), and posts the session start. The hello is the
  record that this session takes its wakes from the outbox. The model sees the tools as
  `mcp__chargehand__<tool>`, and the launch settings allow them, so no permission mode
  asks for them. In this release: `report`, `ask`, `handoff`, `read_handoff`, `request_wake` and
  `subscribe(item, kinds)`, which subscribes the worker to an item that a source names by
  its URL or identifier.
- A tool call goes to `POST /channel/tool/<name>`, and the daemon's answer is the result.
  `report` keeps the state, a one-line summary (300 characters) and up to ten plain
  http(s) links on the worker (see "report" above). `ask(question, options)` records an
  open question with up to ten options, in the same record as a permission prompt, and
  returns at once with a result that tells the model to end its turn; at the end of that
  turn the worker is `blocked`. A newer question replaces an older one. A blocking ask is
  not built: a main-conversation tool call that runs over two minutes moves to a
  background task, and the model would go on without the answer. `handoff(note)` stores
  the note (16 KiB at most) and its time on the worker, for a later session of the same
  work; it never reaches a log line or a notification. `read_handoff` returns the stored
  note to the session that calls it, or a line that says no note is stored; it is how a
  fresh session after a refresh gets the note. `request_wake(after_secs)` puts a
  timer into the worker's inbox, due after 1 second to 7 days, one per worker: a later
  call replaces the one that waits. Delivery takes it once it is due, as any event, with
  the line `timer: request_wake`, through the same admission, and the daily cap counts
  the wake.
- A timer polls `POST /channel/outbox` every `outbox_poll_secs` (5). A message there is
  submitted as the session's next turn once the session is idle (`$.prompt.submit`), and
  `POST /channel/outbox/confirm` says that the turn started. Once the worker is done,
  failed or cancelled, its session can live on; the poll then answers `ended`, which the
  daemon logs once, and the mod stops polling. A paused worker's mod keeps polling. The
  poll is never held open: a hook runs at most 10 seconds and one fetch at most 30.
- Each turn end of the main thread posts `/channel/lifecycle/turn_complete` and the
  session's rate-limit windows to `/channel/usage`; the session end posts
  `/channel/lifecycle/session_end`.

Every post carries the session's own token, which says which worker posts. A tool call
acts as that worker whatever session id it names; a hello, a lifecycle or usage post, or
an outbox poll from a session that is not the worker's current one is ignored (see "A
token per session"). The channel refuses the board's token. A lifecycle post goes through the same transitions as the hook it matches, and
the settings hooks stay, because permission prompts and usage limits come only from them,
so one turn end can arrive twice and the transitions take it once. The mod never answers
a permission prompt and never submits text that the daemon did not hand it; the wake
message carries kinds and pointers only.

The outbox holds at most one open message per worker. Delivery puts the wake message
there, with the inbox rows it carries; events that come in before the mod takes it join it,
and later ones wait for the next wake. A stopped process is started again with `claude
respawn` first. The confirm marks the events delivered and records the wake, once, in the
same transaction as a wake by resume; the daily cap counts it the same way, and the global
cap and the launch throttle count a wake in an outbox as a working turn. A worker that is
stopped, paused or cancelled gets no wake from its outbox: its message is dropped, and its
events wait. A message that the mod does not take within a minute is dropped, the session
no longer counts as having the mod, and the wake stops and resumes it; so does a message
that the session refused to submit. A taken
message without a confirm is handed out again after two minutes, for a mod that was
reloaded before its submit.

When a worker's branch is on its remote and has an open pull request, the tick subscribes
the worker to that pull request for `comment`, `review` and `ci_status`, once, so a review
comment on an implementer's pull request wakes the implementer. The pull request is not its
primary item, so its close does not end the worker. The source is asked only about a
pushed branch. The follow is made up to one tick after the push, so a comment can be older
than the follow; it still reaches the worker, because the router compares an event with the
start of the worker and not with the time of the subscription.

**Notifications** run a command you configure: a desktop notification, email, a push service,
anything. The bundled hook has commented examples of the first three. The payload
is only the issue identifier, the state, the issue URL, and a short reason the runner itself
writes, because it usually crosses a third-party relay. Nothing authored by an agent, a
setup script, or the tracker goes into it; that stays local and is reached through
`status --verbose-titles` and `logs`. Worst-case latency is one poll interval. A session's new
state counts as notified only when the command succeeds, so a send that failed, for example
on the first tick after a wake while the network is still down, is tried again on the next
tick. A finished run's last notification is tried again for up to a day. Only a worker that
needs you or ends is notified: `blocked`, `parked`, `failed`, `done` through the status file,
and the watchdog states, and a worker that reaches its daily wake cap (`daily-cap`, once
until its next wake; see "Delivery"). A worker that ends because its item closed is not notified. A turn end that waits for events (`idle` or `queued`) is not, because a worker that answers
ten comments ends ten turns; `status` and the board show it. The turn end clears the record
of the last notified session state, so the next question is notified even when it is the
same state as the one before.

The tick prints one line per change: a transition names the worker's old and new state, and
the session state only when the new state alone does not say why (`<item>: running ->
blocked (session failed)`); `follow:` lines name the pull requests that workers started to
follow. `serve` prints the changes that a session hook makes in the same lines, and nothing
for a hook that changes nothing.

**Questions stay in the session.** There is no tracker polling and no reply parsing: the
session asks, the runner notifies you and marks the issue blocked, and you answer in Claude
Code's agent view (`claude agents`). The session continues with
its full context, and the next tick flips the label back.

**Watchdog.** Background sessions have no spend cap, so the runner enforces wall-clock limits:
notify after `max_run_hours`, stop the session after `hard_stop_hours`.

**Background shells.** A turn can end while a background shell that it started still
runs. Claude Code then lists the session with `status: busy`, next to `state: working` or,
more often, `state: done`, while its hooks count turns: `Stop` at the turn end, and about a
minute later the idle `Notification`. The listing names no background shell, through
Claude Code 2.1.322. Taking whichever came last made the card flip from idle to running a
few seconds after every turn end. So a turn end is recorded with its time and session.
While no turn has started since (no `UserPromptSubmit`, `SessionStart`, permission prompt
or wake), a listing that shows the session working, or done but busy, is read as
`background`: the worker keeps the state its turn end gave it (`idle`, `queued`, or
`blocked` for a question), and the board and `status` show that state with the session
state `background`. A busy and done listing without a recorded turn end is a plain turn
end. A shell that ends starts a new turn by itself, with a `UserPromptSubmit` (observed
with Claude Code 2.1.322), or leaves the session idle in the listing, which drops the
record. A shell that never ends, such as a dev server or `tail -f`, would hide a session
that waits for the operator, so an `idle` or `queued` worker is notified once, as `idle`,
when the turn ended `background_shell_mins` ago (30 by default; 0 never notifies). The
worker stays `idle`, so delivery may wake it: through the outbox the mod submits the wake
as a new turn while the shell runs. A stop and resume, for a session without the mod,
waits while the listing shows the session working, but stops one listed as done and busy,
which ends the shell. Neither the watchdog nor `max_concurrent` counts the time, because
no turn runs and no usage is spent. Only sessions launched with the `UserPromptSubmit` hook
get this reading, because a resume keeps the hooks of the launch: for a session launched
before it, a turn that the operator starts in `claude attach` sends no hook, so a listing
that shows it working still makes the worker `running`.

**Garbage collection.** When the issue is closed or its pull request is merged or closed, the
session and its worktree are removed — never when commits are unpushed. Low disk space is
reported.

## 7. Control: a CLI and a loopback daemon

chargehand runs on the machine you work on and is steered from a terminal on it. A
resident daemon, `chargehand serve`, does the work: it runs the tick every
`tick_interval_secs`, applies control requests, and serves a small HTTP API on 127.0.0.1.
The CLI is how you steer it.

It is a plain CLI, so it also works over SSH if you want to reach the machine from
elsewhere, but nothing depends on that and it is not part of what is tested. Reaching a
machine remotely is the operating system's job, not this tool's. The daemon's API is
reachable from the machine itself and from nowhere else.

```
chargehand status [--json]          # queue, workers, repositories, sessions — no free text
chargehand watch                    # self-refreshing status table
chargehand logs <ITEM>              # tail of the session's output — explicitly untrusted
chargehand pause [--repository <name>]   # stop admitting new launches; running sessions continue
chargehand resume [--repository <name>]
chargehand cancel <ITEM>            # stop the session, mark blocked; keep the worktree
chargehand stop <ITEM>              # pause one worker
chargehand continue <ITEM>          # respawn it; the saved conversation resumes
chargehand retry <ITEM>             # new worker for a failed or cancelled one
chargehand discard <ITEM> --yes     # remove session, worktree, branch; refuses with unpushed commits
chargehand clone <ITEM> --mode fresh|full|summary [--prompt TEXT | --prompt-file PATH] [--model M]
                 [--effort E] [--subscribe KIND]...   # a new worker from an existing one
chargehand tick                     # run a tick now, under the tick lock
chargehand serve                    # the daemon; `install` writes the job that runs it
chargehand statusline -- '<command>'   # status line wrapper: records the weekly usage window
chargehand export <FILE>            # this machine's setup in one archive, without secrets
chargehand import <FILE>            # unpack it on another machine; asks for the paths
```

Commands that take an item also take `--role`, which is needed when workers of several
roles hold the item. `--route` stays accepted for `--repository`.

`continue` respawns the session of a paused, blocked or parked worker. The respawn returns
before the listing shows the new process, so `continue` reads the listing again for up to
20 seconds, and answers that no process runs only when none came in that time. A blocked
worker whose session is live and waits for input is not respawned, because it has nothing
to respawn: one that asked a question, or whose turn ended `done` with a question in the
status file, takes the answer as a reply, and one that shows a prompt takes it on its card
(a permission prompt) or in `claude attach`. The refusal says which.

- **One writer of side effects.** A mutating command never calls `claude`, git, or the
  tracker. It records the request in the ledger and waits for the outcome. The daemon is
  the writer: between ticks it looks for new requests about once a second and runs a tick
  at once when one waits, so a command usually returns within a second or two, and after
  the tick in progress when there is one. While the daemon answers on its port the command
  never runs a tick itself. The daemon reports which ledger it serves, so a second
  instance (`CHARGEHAND_STATE_DIR`, `--config`) does not wait on another instance's
  daemon. With no daemon, the command runs a tick itself under the tick lock; a timer job
  from before the daemon is still kick-started first. A `cancel` therefore cannot race a
  launch in progress. Reconciliation runs *before* recorded requests, so a cancel issued
  during a launch acts on a session that is known rather than leaving one that the dying
  tick had already started running unsupervised.
- **The daemon's API.** It binds 127.0.0.1 only, on the `[serve]` port; no setting binds
  another address. `GET /api/health` says that the daemon runs, which ledger it serves and
  how its ticks went. `GET /api/state` is the `status --json` payload with a `daemon`
  object added, read from the ledger, with no tracker call; its sessions are the ones the
  last tick listed, so a request runs no `claude agents`. Reads need no token. Every other
  method must carry `Authorization: Bearer <token>`, where the token is in the file
  `token_file`, which the daemon writes with mode 0600 when it is missing and refuses to
  use when other users can read it. The routes of the sessions take a session's own token
  instead: `/channel/` only that, `/hooks/` that or, from a session launched before
  sessions had their own, the board's. A request whose `Host` header is not
  `127.0.0.1:<port>` or `localhost:<port>` is refused, so a web page that points its own
  name at 127.0.0.1 (DNS rebinding) cannot read the API. No response has a CORS header, so
  a page on another origin cannot read one either. A body is capped at 1 MiB and must come
  with a `Content-Length`. Each request opens its own ledger connection.
  `POST /hooks/<event>` receives the posts of the worker sessions (section 6), and
  `POST /channel/...` the posts of their chargehand mod (section 6); each
  worker in `/api/state` also carries its last hook, its last answer, the reset time of a
  usage limit and its open permission prompts.
- **The board.** `GET /` is one static page with its script and style sheet
  (`templates/board/`), under a content security policy that allows only those two files
  and requests to the daemon itself. The page polls `GET /api/board` about once a second
  and patches its cards in place, keyed by worker, so a hook shows within a second and
  the scroll position of each column stays. That answer is built from the ledger, the
  role files and the usage file only: no tracker call and no session listing per
  request. The top bar shows the weekly usage window with its reset time and level
  (grey when unknown), the launch throttle (working against `max_concurrent`, open
  against `max_open_workers`, pauses), a model-limit badge while a turn that failed on
  one model family's own limit waits for its reset (or for a fixed time when the failure
  gave no reset time), and the daemon's health (last tick, its duration, errors, uptime,
  version). The columns are Needs you (`blocked`), Working (`launching`, `running`),
  Queued (`queued`, with the reason each worker waits: a cap or the usage reserve),
  Parked (`parked`, with its reset time, and the reason a resume waits once the reset has
  passed), Idle (`idle`, `paused`) and Closed (workers that finished in the last 24
  hours). A card shows the role, the item with its link, the branch, the worktree, the
  worker's model and effort (the role's, or the ones chosen for a clone), the last prompt
  (the first prompt before any wake, as the launch built it, else the wake message of the
  last wake, rebuilt from the inbox rows it delivered), the
  last answer, an open permission prompt with Allow, Deny and Always allow while its hook
  waits, or an open question with its options and an answer field (section 6), the last
  report of its session (its state, summary and links), the wakes in the window of the daily wake cap, an approximate token
  count (the transcript's size over four) and the attach command.
- **Board actions.** Every control command has a button, and each button records the
  same request as the command, with `origin` `board` where the CLI records `cli`; the
  tick applies both alike, so the daemon stays the one writer. `status --json` shows the
  origin of each pending request, and the tick's log line for a request names it. The
  API, every call a `POST` with the token and a JSON body:
  `POST /api/workers` starts a worker (`repository`, `role`, an optional `item`, and
  `subscribe`, the event kinds on the item that wake it); `POST /api/workers/<id>/<action>`
  with `pause` (the CLI's `stop`), `continue` and `retry` (each with `force`), `cancel`,
  `discard` (with `force`), `archive` (with `force`), `reply` (`text`), `clone` (see
  below), `attach` (`terminal`) and `answer` (`prompt` and `answer`, for a permission
  prompt, which it answers at once; `prompt` and `text`, for a question, which it answers
  as a reply; see section 6); `POST /api/repositories/<name>/pause` and
  `/resume`, `/archive`
  (with `force`) and `/unarchive`; `POST /api/pause` and `/api/resume` for all
  repositories; `POST /api/token` only checks the token (204 when it is valid) and
  records nothing. An action waits up to three seconds for the tick
  and answers with the request's outcome, or with the request pending;
  `GET /api/requests/<id>` reads the outcome later, without the token, as every read.
  A refusal that force would override (`retry`, `discard`, `archive` over uncommitted
  changes or unpushed commits, or a `discard` whose session did not stop) also carries a
  machine-readable `reason`, kept in the ledger with the request (schema version 13), and
  a `board_message` in the board's words ("its worktree has uncommitted changes; tick
  force and press Discard again"); its `message` stays the command line's text. The card
  of the worker shows the outcome of its last action, "pending", then "done" or "refused"
  with that text, until the next action or a dismiss. The board model makes no git call,
  so the tick records what each worker that ended within the last day, and a live worker
  whose action it refused, holds in its worktree: once the worker ended or the action was
  refused, then at most every ten minutes, because each look runs git twice in the
  worktree. The card's `work_at_risk` carries it, and the confirmation dialog of Discard,
  Retry and Archive says it. A record is dropped once its worker leaves the window.
  The page asks for the token once, keeps it in the browser's `localStorage` for this
  origin, and sends it only in the `Authorization` header, never in a URL. It checks the
  token on Enter, when the field loses focus with a new value, on load and when the
  daemon was restarted (its `started_at` changed) or unreachable, and shows "no token",
  "checking", "token accepted" or "token not valid" next to the field; an action that gets
  a 401 also shows "token not valid", and an accepted token clears its notice.
  A worker started from the board has origin `board`. The one-worker-per-role-and-item
  rule of the start rules does not hold it, but a paused repository and every cap do.
  Its item, when given, becomes a work item through the repository's tracker, which
  knows the item's format; it follows the event kinds you chose on that item, and it
  never writes the queue label. For a start rule it holds its item for those kinds only:
  a start event of another kind is not delivered to it and starts a worker as if it were
  not there. The queue label is the exception, as for any live worker on the item: the
  queue start waits until it ends. Each admission pass names the workers that hold the
  item in the tick report, and a `retry` that puts the item back in the queue names them
  in its answer. The session of a finished one is reported, never
  adopted as a start rule's worker. A role that checks out its item needs an item. Its worktree and branch end in its id
  (`<root>/<role>/<item>-<id>`, `<prefix><role>/<item>-<id>`, or `worker-<id>` without
  an item), so several such workers can work on one item. A `retry` of such a worker
  starts a new one the same way. Workers that share a role and an item are told apart
  by a handle with the worker's id, `role/item#id` (`role/#id` without an item); a
  worker that a start rule created keeps `role/item`. Every command that takes an item
  also takes a handle, and `#id` alone; a shell reads a word that starts with `#` as a
  comment, so quote it: `chargehand stop '#7'`. One rule resolves a target, in order:
  (1) an id handle names the worker with that id; (2) `role/item`, or an item with
  `--role`, is a handle, and when a worker has exactly that handle it wins, the live one
  first, then the newest; (3) otherwise the item decides: its one live worker (in the
  role, when known), or among several the one start-rule worker that the bare item
  names, or with none live the newest recorded. A bare item whose one live worker was
  started by hand, while a start-rule worker on the item has ended, is refused, because a
  notification about that worker carried the bare item. Anything else is refused with
  each candidate as `role/item#id`, and a refusal about a finished worker lists the live
  workers on its item. `cancel` and `discard` name the worker they acted on. Every handle chargehand prints resolves back to its
  worker under this rule: `status --json` and the board show `role/item` for a live
  start-rule worker and the id handle for every other worker, finished ones included,
  and a hint in a refusal uses the bare item only when the rule resolves it to that
  worker.
  A reply is one line of text, cleaned of control sequences and capped at 500
  characters. It enters the worker's inbox as an event of the source `board`, the path
  a comment on its item takes, so delivery wakes the worker with it under the same caps
  and the same usage reserve. No configured source may be named `board`. A worker that
  is blocked by a question gets the reply as its answer: delivery wakes it (section 6,
  "Delivery"). A worker that is paused or parked, or blocked by a permission prompt, a
  dialog or a session that failed or stopped, keeps the reply in its inbox until it is
  idle again, and the answer says what to do first: answer the prompt, or `continue`.
  Attach opens a terminal window that runs the card's attach command (`claude attach
  <session id>`): Terminal.app by default, or iTerm2 chosen on the page, on macOS only.
  The daemon's request thread runs it at once, because the tick can be busy with a launch
  for minutes, and records an `attach` request that is already applied, for the audit
  trail. Only a command of plain words whose last word is a plain identifier is accepted.
  Terminal.app is opened through LaunchServices (`open -a Terminal`) on a script file in a
  private directory under the state directory, which deletes itself and then execs the
  command, so it needs no Automation permission; a file that never ran is removed when the
  next one is written. iTerm2 asks for confirmation before it runs a file it is asked to
  open, so it is opened through `osascript`, with the command as an argument of the
  AppleScript, never part of its source, and needs the Automation permission once. The
  board has no chat: anything that needs a conversation goes through attach.
- **Clone.** A clone is a new worker made from an existing one, for a second opinion, a
  retry with another prompt, or another model. `POST /api/workers/<id>/clone` takes
  `mode` (`fresh`: nothing from the old session; `full`: a copy of its conversation;
  `summary`: a new session that reads the old worker's handoff note as a tool result),
  and optionally `prompt` (a prompt template, at most 16 KiB, cleaned of control
  sequences, with the same placeholders as a role's prompt), `model` (a model name or
  alias that does not start with `-`), `effort` (a level of the role file) and
  `subscribe` (event kinds on the original's item, only when it has one); a bad value is
  refused with 400 before a request is recorded. `chargehand clone <ITEM> --mode ...`
  records the same request row, with origin `cli`. `GET /api/workers/<id>/clone`, a read
  without the token, answers what the card's clone form starts with: the worker's prompt
  template, model and effort (its role's, or for a clone the ones chosen for it), the
  effort levels, the item, `choices` with every mode, `modes` with `full` only while a
  copy is possible and `summary` only while a start from the handoff note is, and
  `full_refused` and `summary_refused` with the reason otherwise. The form has a mode
  choice, the prompt as a text area, the model, the effort and, for a worker with an
  item, the event kinds, empty by default. A card offers Clone while the worker's
  worktree or branch exists: not after garbage collection or an archive, nor while an
  archive of it runs.
  The tick checks the request again and refuses it, before any ledger row or worktree
  exists, when the repository is paused or unavailable, when the original's branch is
  gone, for full mode when the runtime cannot fork, the original has no session, or it
  is launching, running or blocked (a copy would take a turn in progress or an open
  prompt), for summary mode when the runtime's sessions do not load the chargehand mod
  (no tool gives the note), when the original's note is not current and it is
  launching, running or blocked ("clone it in summary mode once it is idle"), when it
  has no note and no wake can reach it, or when the note is not current and the global
  cap leaves no room for the original's turn next to the clone's launch (the waiting
  clone counts as launching); and in every mode when a cap or the usage reserve for new
  workers holds it. A clone has origin `clone`, the original's role and item, and the id
  handle; the one-worker-per-role-and-item rule does not hold it. It gets a new worktree
  and branch (`<item>-<id>`) from the original's current branch (section 5), so the two
  never write the same tree; when the original's worktree has uncommitted changes, the
  answer says that they are not in the clone. By default it follows no event kinds, so it
  costs nothing per comment and only a reply from the board reaches it. Its original,
  mode, prompt, model and effort are columns of its ledger row (schema version 9), so a
  launch resumed after a crash and a `retry` use them; a `retry` of a clone starts a new
  clone of the same original with them, or says why it cannot. A full-mode fork is
  recorded as a `fork` session; a summary clone's session is an `initial` one (section 5
  describes its wait for a current note). A clone's session gets `Edit` and `Write` deny
  rules for its original's worktree in its own settings file, in every mode, and for the
  worktree of every earlier original of a clone of a clone, because a copy of a copy
  names them all; the line before a full-mode prompt names them all too. A deny rule
  holds in every permission mode, so its file tools cannot change the original's tree,
  while its Bash tool is not covered (SECURITY.md). The clone's branch holds the
  original's commits, so its retry, discard and archive refuse while those are not
  pushed, unless forced. In a repository group of one column a clone's card follows its
  original's card, and a clone of a clone follows its own original, when both cards are
  pinned or both are not: a pinned clone of an unpinned card stays with the pinned cards,
  and an unpinned clone of a pinned card stays with the others, so the pinned cards still
  come first. Every clone card carries "clone of <handle>".
- **Board personalisation.** Each column groups its cards by repository, under a heading
  with the repository's name, icon, card count and a collapse toggle. A repository can be
  renamed, given an icon, pinned, collapsed and hidden; a worker can be renamed, given an
  icon and pinned. This is state, not configuration: it lives in the ledger's
  `board_state` table (schema version 6), one row per kind (`repository` or `worker`),
  key (the configured repository name or the worker id) and field, so it survives a
  restart of the daemon. The API: `POST /api/repositories/<name>/board` with any of
  `name`, `icon`, `pinned`, `collapsed` and `hidden`, and `POST /api/workers/<id>/board`
  with `name`, `icon` and `pinned`; `false`, `null` or an empty text drops a setting. It
  needs the token like every `POST`, and it writes the table at once without a request
  row, because it has no effect outside the ledger. A display name is one cleaned line of
  at most 60 characters; it never changes the configured name, the handle or the session
  name, so the CLI, the ledger and the sessions see the same names as before. An icon is
  one emoji or a name from a small bundled set (`star`, `rocket`, `bug`, ...); nothing is
  uploaded. A board pin only sorts: in each column the pinned repositories come first,
  then the others by their latest card, and in each repository the pinned cards come
  first, then the others by their last activity. A collapsed repository shows only its
  pinned cards and the number of cards it folds. The card labels this pin "board pin":
  Claude Code's own pin in agent view keeps a session's process alive, and this one does
  not; the session listing does not report that pin, so the board does not show it. A
  hidden repository's cards leave the board, and the top bar shows the hidden count and a
  filter that shows them again (`GET /api/board?show_hidden=1`). Only the board model
  reads the table: launches, supervision, notifications, `status` and the tick do not,
  so a hidden repository's workers run, are supervised and notify as before.
- **Archives and jobs.** An archive is one zip of what a worker leaves: its worktree (and
  the item checkout of a `checkout = "item"` role), the session transcript as text (and,
  as `transcript-1.txt` and on, oldest first, the transcript of each session that a refresh
  replaced), its ledger row and its sessions as JSON, and its handoff note when one exists.
  It is written
  under the state directory as `archive/<repository>/<role>-<item>-<date>-<time>-<id>.zip`, mode
  0600; the time and the worker id keep two archives apart. The guard runs again when the
  cancel of a live worker has applied, before the zip. The `archive` request is applied
  by the tick like any other: it refuses a worker that is archived, that has an archive
  under way, or whose worktree was already removed; it applies the guard of discard
  (unpushed commits or uncommitted changes refuse it unless forced, and a forced archive
  keeps the files, not the commits); for a live worker it records a `cancel` request with
  the same origin and lets the archive wait for it. The archive itself is a row of the
  `jobs` table (schema version 8, with `workers.archived_at`): a kind, a target, its
  arguments, a state, progress text, a result path, an error and its times. The states are
  `waiting` (for the cancel), `queued`, `running`, `written`, `done` and `failed`. A thread
  of the daemon, not the tick and not a request handler, runs the queued jobs one at a
  time: it writes `<name>.zip.part` with the transcript read from the session's transcript
  file (only a `.jsonl` file under Claude Code's `projects` directory; otherwise
  `claude logs`), reads every member back to check its CRC, syncs the file, and renames it
  to its final name; a file is streamed in chunks, a symbolic link is stored as a link, and
  sockets and pipes are left out. Only then is the job `written`, and the next tick, the
  one writer of git and of sessions, applies the guard again and removes the session, the
  worktree and the branch through garbage collection, and sets `archived_at`. Garbage
  collection, discard and retry leave a worker alone while an archive of it is under way.
  A stop of the daemon during the zip, or a crash, leaves at most the `.part` file and the
  job `running`; the next start of the job thread deletes every `.part` file and puts
  `running` jobs back in the queue, so the job runs again from the start. `archive_repository`
  hides the repository (the `hidden` board setting) and queues an archive of each of its
  workers that still has a worktree; it refuses while the repository has live workers, or
  while any worker holds work that is not pushed, unless forced, which cancels the live
  workers first. `unarchive_repository` only shows the repository again; the zips stay. The
  top bar shows the jobs of the last hour: a running or failed job in full, the others as
  one count per state (waiting, queued, written, archived), so that the archive of a whole
  repository stays readable; each card shows its worker's job and archive time. Restoring
  a worktree from a zip is a manual step (see the README), not a button.
- **Usage readings.** The usage file holds one reading: the weekly window, when it was
  written and its source, `get_usage`, `session` or `statusline`. Each source writes the
  same file, and the newest write is the reading. The daemon asks Claude Code itself every
  `poll_secs` of `[usage]` (300 seconds by default, at least 60, 0 turns it off), on a
  thread of its own, so no tick and no request waits for it: it starts `claude -p
  --input-format stream-json --output-format stream-json --verbose --settings
  '{"disableAllHooks": true}' --strict-mcp-config --no-session-persistence` (no MCP
  servers of yours, no session file) in an empty private directory under the state directory,
  writes one `control_request` line with the subtype `get_usage`, reads lines until the
  `control_response` with that request id, and closes stdin. Claude Code fetches the usage
  with its own sign-in, so chargehand reads no token and no Keychain item and makes
  no network call; the request runs no turn and writes no transcript. The answer's
  `rate_limits.seven_day` has `utilization` (a percentage) and `resets_at` (an ISO time),
  and the daemon writes that window with the source `get_usage`; `five_hour`, the
  per-model windows and `limits` are ignored. This request is experimental in Claude Code
  and can change in any release, so the reader fails closed: an error, a timeout of 10
  seconds (the process is then killed), an answer without the window, or
  `rate_limits_available: false` writes nothing, logs one warning (a debug line while the
  reason stays the same), and leaves the file with the reading it has. `--bare` is no
  option, because it skips the Keychain, where the sign-in is. `doctor` shows the source
  and the age of the last reading, and warns when it is older than three poll intervals.
  The status line wrapper and the mod stay as sources beside it. The chargehand mod posts
  each worker session's rate-limit windows after every turn, and the daemon writes the
  `seven_day` one with the source `session`. Claude Code gives a status line command the subscription's
  `rate_limits`: a `seven_day` window with `used_percentage` and `resets_at`. It sends them
  on subscription plans only, and only after the session's first API response.
  `chargehand statusline -- '<command>'` reads that payload, writes the `seven_day` window
  with the time it wrote it to `usage_file` (atomically, mode 0600), and then runs
  `<command>` through a shell with the same stdin, passing its output and exit code
  through. Other windows in `rate_limits` or in the file are ignored without a message. A
  payload without the `seven_day` window, or with one whose `resets_at` cannot be read,
  leaves the file as it is, and a file that cannot be written changes nothing in the status
  line. chargehand does not edit your settings;
  you wrap your own command in `~/.claude/settings.json`:

  ```json
  "statusLine": {
    "type": "command",
    "command": "chargehand statusline -- '~/.claude/statusline.sh'"
  }
  ```

  Without a status line of your own, leave out `-- '<command>'`; the status line is then
  empty. The daemon reads the file for each `/api/state` and adds a `usage` object: for the
  `seven_day` window its percent, its reset time and a level, `green` below 60 %, `amber`
  from 60 %, `red` from 80 %. A missing or unreadable file, a window whose reset time has
  passed and a window without a reset time are unknown, with the level `grey`; the age of
  the file does not matter. `status --json` carries the same object.
- **Stopping the daemon.** SIGTERM or SIGINT ends it between two tick phases: the phase in
  progress finishes, the rest of that tick is skipped, a job in progress stops and goes
  back to the queue, the server shuts down, and the exit code is 0. The next tick reconciles what the skipped phases left. launchd sends SIGKILL
  when the daemon has not exited after the job's `ExitTimeOut`, 300 seconds; a phase that
  runs longer, such as a slow setup script, is then killed and reconciled as an
  interrupted launch.
- **Untrusted text.** Issue titles and agent output are attacker-influenced, so `status` and
  `watch` omit free text by default; titles and a session's waiting text need
  `--verbose-titles`, output needs `logs`. That covers what a session can put into fields
  that look structured: a pull-request URL it reports is kept only when it is a plain URL,
  and a branch it renamed, which then carries a slug of the issue title, is shown only
  with `--verbose-titles`. Flags cannot be abbreviated, so the rules
  that gate a flag see its full name. Everything printed is stripped of terminal control
  sequences.
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
  sessions get deny rules for all three, and one more for the operator commands of the
  lease tool banksman, which all start with `banksman admin` and which banksman asks every
  caller to deny to agents. The rule matches nothing on a machine without banksman, and
  the other banksman commands stay open, because the repository's scripts use them. They
  also get deny rules for the daemon's port, written for the port in `[serve]` and added
  to an operator's own `deny_rules` too: `Bash(*127.0.0.1:<port>*)`,
  `Bash(*localhost:<port>*)`, and `WebFetch` for both hosts. A `Bash` rule matches command text rather
  than the program behind it, so every pattern leads with a wildcard to cover an absolute
  path and a `sh -c '...'` wrapper alongside the bare name; `probes/deny_rules.py` checks
  each form against a real install. That makes the rules a guardrail against the careless
  path and not a boundary against a deliberate one. Running agents under a separate user
  is the stronger option.

## 8. Sharing devices

Parallel workers on one machine compete for scarce things: a phone on USB, a limited number
of emulators, memory for heavy builds. Sharing them safely is the job of a lease tool, such
as banksman, which the repository's own scripts call. chargehand does not call it, read its
output, or know its version. A machine without a lease tool runs exactly the same.

banksman needs nothing from the caller, so chargehand only has to keep a few properties that
any such tool can rely on:

- **A worker's commands run below its own agent process.** A worker's session is a Claude
  Code process of its own, and Claude Code names it in `CLAUDE_PID`. banksman takes that
  process as the holder, so each worker has its own leases, also with the same holding
  label. A walk up the process tree alone is not enough: the session process has the title
  `claude bg-spare`, and above it is Claude Code's background service, one process that
  hosts every background session of the user. Use a banksman from 2026-10-19 or later,
  which reads `CLAUDE_PID` in that case; an older one makes all background workers one
  holder.
- **The branch names the work item.** A worker starts on `<branch_prefix><role>/<item>`, so
  a tool that reads an issue id from the branch name finds it.
- **Stopping a worker ends its process.** banksman ends a lease when its agent process has
  ended, after a grace period, and takes the resource back at the start of its next command
  or in its own reap job. So a cancel, the watchdog, a discard, or garbage collection frees a
  worker's leases without chargehand releasing anything. The cost: after a stop, a resource
  can stay held for the grace period.
- **Agents cannot reach the tool's operator commands.** The launch deny rules include
  `Bash(*banksman admin*)` (see "Control").

chargehand once ran banksman's `reap` and `status` itself. That made every change of
banksman's output schema a reason to change chargehand, for no gain in safety, because
banksman reaps on its own. Use `banksman watch` to see who holds what.

## 9. Leaving it running

- A logged-in session is required: Claude Code's credentials and tracker keys live in the
  login keychain, and launchd agents run in that session. A locked screen is fine; a logged
  out one is not.
- `install` writes a LaunchAgent with `KeepAlive` and `RunAtLoad` that runs
  `chargehand serve`. launchd starts the daemon when you log in and starts it again when it
  exits, at most once every ten seconds. The label is still `dev.chargehand.tick`, so
  `install --load` unloads a timer job from an older release before it loads the new one,
  and `doctor` warns when the installed job is still that timer. `doctor` also checks that
  the daemon answers on its port and that the token file has mode 0600.
- launchd starts jobs with a minimal `PATH`; everything the tick shells out to must be
  reachable through the `PATH` set in the job definition. `install` copies the `PATH` of the
  shell it runs in, and `doctor` resolves `claude` and `git` through the job's `PATH`
  rather than its own. A repository's setup script inherits the
  same `PATH`.
- The job runs as launchd's `Interactive` process type, which gives it the priority a
  terminal gets. Everything the tick starts inherits the type, and that includes every
  session: `claude --bg` starts the daemon that hosts background sessions when none is
  running, and the daemon keeps the type, also when it restarts itself for an upgrade.
  With the `Background` type, sessions ran at the lowest scheduling priority and CPU-bound
  work took about three times as long. The daemon also hosts the sessions you start by
  hand, so a throttled daemon would slow those too.
- Runs share the machine with you. Keep `max_concurrent` low until the repository's
  scripts lease their devices through a lease tool, because until then nothing stops two
  runs from reaching for the same device or emulator.
- Sessions do not survive a restart. The ledger, the labels and the worktrees do, and the
  next tick parks any run whose session is gone, so a restart costs progress but never
  consistency.
- Archives build up under `archive/` in the state directory; nothing deletes them. They
  are not part of an export.

### Moving a setup to another machine

`chargehand export <file>` writes one zip archive with a `manifest.json` (format version,
chargehand version, time, member list). It holds:

- The machine configuration. Repository paths, `worktree_root`, `usage_file`, and every
  `*_bin` key are written with `~` for the home directory; `token_file` and `usage_file` are
  left out when they have their default value, so the default of the other machine applies.
  The notification hook's command names the script in the archive. Each repository gets an
  explicit `name`, because the board settings are kept by name. Keys that can carry a
  credential (`api_key_command`, and any key whose name ends in `token`, `secret`,
  `password` or `api_key`) are left out and listed in the manifest. Comments and the keys
  export does not rewrite stay as they are: the standard library has no TOML writer, so the
  file is rewritten as text and parsed again.
- The role files in `~/.config/chargehand/roles/`, and the agent files in
  `~/.claude/agents/` that the machine-level roles or the repositories' enabled roles name.
  Repository-level role files travel with their repository.
- The notification hook, when its command runs a file in the home or the configuration
  directory, copied as it is. A program installed elsewhere, such as a system or Homebrew
  binary, stays where it is, and the command keeps naming it. So does a file in the home
  directory that cannot travel as it is: one over 4 MB, one that is not text, or a
  generated shim that contains the home directory's path. Export says why, and import
  lists installing it as a manual step. The archive holds no file larger than 4 MB and at
  most 512 files, the limits import applies.
- The `statusLine` entry of `~/.claude/settings.json` when it runs `chargehand statusline`.
- The board settings of the configured repositories. Worker settings are not exported: they
  are kept by worker id, and a worker id means nothing on another machine.

The archive never holds the daemon's token, a Keychain value, or the rest of the ledger.
Export refuses to write an archive in which any file still contains the home directory or
the token, and names the file.

`chargehand import <file>` checks the archive first: it refuses a format version newer
than it knows, a file name outside the layout above, a symlink, and a member that the
manifest does not list. It then asks for each repository's path and for the worktree root
(the archive's values are the defaults), or takes them from `--repository NAME=PATH` and
`--worktree-root`, or reads one line per question from stdin. It never overwrites a file or
a board setting without `--force`, and it checks all of them before it writes anything. It
writes the configuration, the role and agent files and the hook, restores the board
settings through the board's cleaning rules, and writes a new token. It does not write
`~/.claude/settings.json`, and it does not install the launchd job.

What is left to do by hand goes to `imported-steps.json` next to the configuration:
each credential key that was left out, the tracker's key or sign-in, the status line entry,
`install --load`, the notification method, and each program that the configuration names
by path and the archive does not carry: the notify program, and every `*_bin` key
(`claude_bin`, `git_bin`, a tracker's `gh_bin`) given as a path. Export prints one line for each such program. `doctor` lists each
step; a step it can check (a key that is set again, the status line entry, a loaded job,
an installed program) shows as done once it is.

### The daemon's log

The log is for investigating a missed event, a missed hook, a wrong notification, a
stale worker or a daemon that stopped ticking, without reading the ledger by hand. It is
not a record of what workers did: nothing a session wrote reaches it.

- **One writer, one format.** The daemon writes `~/Library/Logs/chargehand.log` through
  a rotating handler (10 MB, five old files); `install` passes the path as
  `serve --log-file`. The launchd job's stdout and stderr go to `chargehand.launchd.log`,
  because a rotation renames the log file and launchd would go on writing into the renamed
  one. The tick report goes through the same handler, under `chargehand.tick`; a tick run
  by hand still prints its report to stdout, and `tick --json` is unchanged. A formatter
  cuts every message to one line and strips control sequences, so a message that quotes a
  script or a tracker cannot break a line or reach a terminal that tails the file. A
  `newsyslog.d` entry was the other option; it needs root, and `install` runs as the user.
- **Levels.** `log_level` in `[serve]`, read on every tick; `-v` overrides it. `info` is
  the default. Report lines are INFO, except `failed` and `warnings` (WARNING) and `errors`
  (ERROR), so `warning` still shows what went wrong. `debug` adds the HTTP access log, a
  heartbeat per tick and the tick lock waits.
- **What `info` records.** The report fields, `notified` and `routed` included; a
  notification names its detail and the notify command's outcome. One line per poll that
  found new events, with the cursor before and after and the duration. A source reads
  back over its previous poll, so a poll that returned only events the dedupe filter has
  seen counts as finding nothing new, and the `routed` summary leaves it out too. Polls
  that found nothing new are held back per source and written as one line, when a poll
  finds new events or fails, after 15 minutes, and when the daemon stops: an idle daemon writes about four
  lines an hour per source instead of sixty, and still shows that it polls. One line per
  event that passed the dedupe filter: source, kind, item, the dedupe id (up to 80
  characters), the decision (dropped as the account's own marked comment, delivered to
  which workers, not delivered because it predates their start, closed, launched, or why
  the start waits) and the pointer when it is a plain URL. One line per hook post: event,
  the first eight characters of the session id, and the worker, or "no live worker" with
  the cwd; a refused post names its status. One line per accepted label or status write,
  with the status before and after. One line per wake, with the `kind: pointer` lines of
  its message, and the line of a reply from the board, which is the operator's own text.
  A WARNING heartbeat for a tick longer than the interval, or one that began
  more than two intervals after the previous one ended. An idle tick writes no report line.
- **One key.** Every line about a worker starts with its handle, `role/item`.
- **What never appears.** A last answer, a waiting text, a permission prompt's tool
  input, a status file's stop reason, a transcript path, a comment body, and the actor of
  an event, which on Linear is an email address. A test feeds each of them a marker and
  checks that it reaches neither the log nor the output.
- **When the log is not enough.** `events` keeps each routed event with its dedupe id,
  `seen_at` and `start_pending` for 30 days; `inbox` keeps `queued_at` and `delivered_at`
  per worker and event. No `events` row means no poll returned the event; a row without an
  inbox row means no live subscriber matched; an inbox row without `delivered_at` waits for
  a wake.

## 10. Failure modes

| Failure | Behavior |
|---|---|
| Machine reboots | Observed: the ledger, the labels and the worktrees survive, the scheduled job runs a tick by itself, the killed session reads `failed`, and the run is parked as blocked with one worker recorded. `continue` brings the session back but not the work it had running, so resuming is left to you. |
| Tick crashes mid-launch, or a tracker write lands but reports failure | The ledger row came first; the next tick adopts, resumes, or fails the worker loudly. |
| Agent hangs | The watchdog notifies after `max_run_hours` and stops the session after `hard_stop_hours`. |
| Several blocked runs resumed at once | Sessions wake together; `max_open_workers` bounds how many there can be. |
| Tracker unavailable | The tick logs and exits; the next one retries. |
| Control command during a tick | Recorded, then applied by the daemon's next tick, which starts within about a second of the current one ending. |
| Daemon killed or crashed | launchd starts it again; the tick lock was released by the kernel, and the first tick reconciles. |
| Prompt injection through issue text | Mitigated, not eliminated — see `SECURITY.md`. |

## 11. Roadmap

Done:

- Runner: configuration, the `linear` adapter, ledger-first launch,
  reconciliation, label lifecycle, state diffing, the control CLI, the launchd job.
- The resident daemon: the tick on a timer, control requests applied within a second, and
  a loopback API with a token.
- Control surface: `watch`, `logs`, request plumbing through the tick, output sanitizing,
  launch-time deny rules, the assistant skill with its ask rules.
- Notifications, watchdog, garbage collection, the status-file protocol.
- `init`, `doctor`, `install`, and the bundled templates.
- `export` and `import`: a machine's setup carried to another machine without its secrets.
- Devices shared through a lease tool that chargehand never calls, with the deny rule for
  its operator commands.
- Roles and workers: role files with an agent, model, effort and allow rules, start rules,
  several roles per repository, and a ledger of workers and their sessions.
- The runtime interface, with Claude Code as the `claude` runtime behind it and declared
  capabilities.
- The chargehand mod in every worker session: the worker tools from one registry, the wake
  of a live process from its outbox, the lifecycle posts and the usage readings.
- The daemon's own usage reading, from Claude Code's experimental `get_usage` request, so
  the week bar and the reserves need neither the status line wrapper nor a running worker.
- Session refresh: a fresh session for the same worker past a wake or size limit, which
  reads the worker's handoff note through a tool.
- Wake policies: a debounce that makes a burst of events one wake, a notification at the
  daily wake cap, and the global cap on working turns across repositories.

Next:

- Run it unattended for a day against a real tracker.
- Run several workers at once, with repository scripts that lease their devices through
  banksman.
- Event sources (`github`, `linear`, `command`) that start workers and wake idle ones, with
  workers that subscribe to the pull requests and issues they follow.
- Actions on the board: new worker, reply, pause, cancel, discard, clone in fresh,
  full or summary mode; a summary clone starts a new session that reads the old
  worker's handoff note through a tool.
- Packaging, first release.
- Optional: agents under a separate user.

Acceptance for the runner runs against a throwaway repository and a cheap prompt, with fault
injection after every launch step, before any expensive real run. `probes/smoke_route.py`
drives the whole of it against a real tracker and real Claude Code. The reboot drill and the
reboot drill stays manual, because a restart cannot be scripted from the machine under
test.

## 12. Open questions

- Should a run that reports `working` with `status: idle` for long enough be treated as
  stalled rather than healthy? A short window would misread the gap between two tool
  calls; a long one is what the watchdog already does, only at `max_run_hours`.
- A `reply` command. Claude Code 2.1.301 can send a message to a background session from a
  script: after `claude stop`, `claude --resume <full session id> --bg "<message>"` continues
  the same session with its saved launch options. Against a running session, or with the
  short id, it starts a copy instead and says so. The adapter can do this; no command uses it
  yet, so answering a question still means attaching.

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
