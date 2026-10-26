# The command source

The `command` source connects chargehand to a system that has no built-in source: Jira,
GitLab, YouTrack, a spreadsheet, a cron job. You write an executable that prints events as
JSON lines, and chargehand runs it on every tick.

This contract is a stable, public surface. A change to it that breaks an existing command
is a breaking change, as for the CLI and the configuration files (see
[CONTRIBUTING.md](../CONTRIBUTING.md)).

There is no plugin framework. chargehand does not import your code and does not load entry
points. Each command runs as a subprocess, with a timeout and without a shell, and its
output is checked against the closed shapes below. Everything a command prints is
untrusted, as the text from any other source is.

## Configuration

A `[[source]]` table in the machine configuration:

```toml
[[source]]
name = "jira"                           # qualifies event kinds: jira.comment
type = "command"
poll = "jira-events"                    # required
mark = "jira-mark {ref} {status}"       # optional
item_open = "jira-open {ref}"           # optional
account = "me@example.com"              # optional, default ""
repositories = ["app"]                  # optional, default []
timeout_secs = 30                       # optional, default 30
```

| Option | Meaning |
|---|---|
| `poll` | The command that prints events. |
| `mark` | The command that shows a worker's state on the item, for a system with labels or states. |
| `item_open` | The command that says whether an item is still open, for garbage collection. Without it, an item stays open. |
| `account` | The name your workers post as, written the way your `poll` command prints `actor`. A comment by this actor that your command reports as `marked` wakes nobody. |
| `repositories` | The configured repositories that this source's start events may start workers in. |
| `timeout_secs` | How long each command may run. A command that runs longer is killed together with its child processes. |

Each command is an argument template. chargehand splits it into arguments first and
substitutes the variables after that, so a value with spaces or shell characters stays one
argument. Nothing is run through a shell; use `sh -c '...'` yourself if you need one, and
quote with care. The variables are `{source}` (the source's name) in every command, `{item}`
and `{ref}` in `mark` and `item_open`, and `{status}` in `mark`. An unknown variable is a
configuration error.

A relative program is looked up in the directory of the machine configuration first, then
on `PATH`. The commands run in that directory, with chargehand's environment, so a
credential can come from an environment variable or from a file next to the
configuration.

## poll

chargehand writes the cursor that the previous poll printed to the command's stdin, without
a newline. The first poll gets an empty stdin. The command prints one JSON object per line
and exits 0:

```json
{"kind": "comment", "item": "APP-12", "actor": "ana@example.com", "occurred_at": "2026-10-27T09:12:00Z", "dedupe_id": "comment:88213", "pointer": "https://jira.example.com/browse/APP-12?focusedCommentId=88213"}
{"kind": "assigned", "item": "APP-14", "ref": "10442", "url": "https://jira.example.com/browse/APP-14", "actor": "", "occurred_at": 1793095200, "dedupe_id": "assigned:10442:3", "pointer": "https://jira.example.com/browse/APP-14"}
{"cursor": "2026-10-27T09:15:00Z"}
```

The last line is the cursor: an object with the one key `cursor`, a string or `null`.
chargehand stores it after it has routed the events and gives it back on the next poll. Its
content is yours. When the last line is not a valid cursor line, the poll keeps the old
cursor, and the events are routed anyway.

Every other line is an event. These are all its fields; a line with any other field is
skipped:

| Field | Required | Meaning |
|---|---|---|
| `kind` | yes | What happened, as a bare lower-case name (`comment`, `assigned`). A role's start rule or subscription names it bare, or qualified by the source's name (`jira.assigned`). |
| `item` | yes | The work item's key. It becomes a worker's item, its worktree and branch name, and its session name, so it is 1 to 64 characters of letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `actor` | yes | Who did it, as text; may be empty. Used only for the authorship filter. |
| `occurred_at` | yes | When it happened: seconds since the epoch, or an ISO 8601 time with a time zone. |
| `dedupe_id` | yes | A non-empty id, unique within this source for this event. chargehand drops an event whose id it has seen before, so a poll may report the same event again. |
| `pointer` | yes | Where the worker looks: a URL or an identifier. A wake lists only pointers that are plain http(s) URLs or plain identifiers. |
| `source` | no | The source's name. An event that names another source is skipped. |
| `ref` | no | The item in the system's own terms, given to `mark` and `item_open` as `{ref}`. Defaults to `item`. |
| `url` | no | The item's web page, given to the launch prompt as `{item_url}`. Must be a plain http(s) URL. |
| `repository` | no | Which of the source's `repositories` a start event belongs to. Without it, a source with exactly one repository uses that one, and a source with several starts nothing on the event. |
| `marked` | no | `true` when the event is a comment that carries chargehand's authorship marker, `<!-- chargehand -->`. Default `false`. |

An event never has a body. Read a comment's text in your command at most to find the
marker, and do not print it.

A line that is not JSON, not an object, too long, or does not fit the table is logged and
skipped. It never stops the poll. Empty lines are ignored.

Limits: one line is at most 64 KiB, and one poll prints at most 1 MiB. A poll that prints
more fails; return the rest on the next poll, from your cursor. A poll that exits with any
code other than 0 fails, and its first 300 characters of stderr go into the error. A failed
poll keeps its cursor, so its events arrive on a later tick.

## mark

`mark` gets the item and the state as arguments, for example `jira-mark 10442 running`.
`{status}` is one of `queued`, `running`, `blocked` and `done`. The exit code says what
happened:

| Exit code | Meaning |
|---|---|
| 0 | The write landed. |
| 3 | The write may or may not have landed, for example a timeout after the request was sent. chargehand leaves it for reconciliation and does not assume either outcome. |
| any other | The write failed. |

Verify the write in the command, by reading the item again, before you exit 0.

The tick changes labels only for a worker that a repository's queue label started. A
`[[source]]` starts its workers by start rules, so today chargehand does not call the
`mark` command of a command source; the contract is fixed now so that a later version can.

## item_open

`item_open` prints one JSON object and exits 0:

```json
{"open": false}
```

`false` means the item is closed, merged or gone, and a worker that was started for it
ends. Any other output, or an exit code other than 0, is an error, not a closed item.

## Example

A poll command in shell that reports a file of new events:

```sh
#!/bin/sh
cursor=$(cat)                 # the line number read up to, empty on the first poll
total=$(wc -l < "$HOME/events.jsonl" | tr -d ' ')
sed -n "$(( ${cursor:-0} + 1 )),${total}p" "$HOME/events.jsonl"
printf '{"cursor": "%s"}\n' "$total"
```

With a role that starts on its kind:

```toml
[[start]]
on = "jira.assigned"
subscribe = ["comment"]
```

an `assigned` event starts a worker on the item, and a later `comment` event on the same
item wakes it.
