# Security Policy

chargehand starts coding agents on your machine in response to issue-tracker activity and
lets them run unattended. That is a sensitive thing to automate, so the security posture is
part of the design, not an afterthought. This document describes what is supported, how to
report a vulnerability, and the threat model.

## Supported Versions

chargehand is pre-1.0 and under active development. Until `1.0.0`, only the latest
published version receives security fixes.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security problems.**

Report privately through **GitHub Security Advisories**:

1. Go to <https://github.com/Twinsen81/chargehand/security/advisories/new>.
2. Describe the issue, affected versions, and a reproduction if you have one.
3. We will acknowledge the report and coordinate a fix and disclosure with you.

Do **not** include working exploit payloads, tokens, or private repository content in any
public location.

## Threat Model

### What is trusted

- The operator: the person who installs chargehand, owns the machine, and owns the tracker
  credentials it uses.
- The repository's own configuration and instructions (its chargehand config, its agent
  instruction files, its permission rules), as the base branch the operator configured
  holds them.

### What is not trusted

- **Issue text.** Titles, descriptions, and comments can be written by anyone who can file
  an issue. They are attacker-controlled input to an agent.
- **Agent output.** Logs and messages produced by a session can echo anything the session
  read.
- **The head of a pull request under review.** Its author controls every file in it,
  including `.claude/settings.json` (hooks and permission rules), `.mcp.json`, `CLAUDE.md`
  and `.claude/agents/`. A review request needs no triage rights: a code-owners rule can
  ask the operator to review a pull request from any fork. Claude Code loads those files
  from the working directory of a session it trusts, and a hook runs as the operator's
  user before the model reads anything.
- **Event fields.** An event's actor and pointer come from the source. An event has no
  body: a source reads a comment only to find the authorship marker, and the text does
  not leave the source. The marker filter is a convenience against workers waking each
  other, not a defense: anyone can quote the marker, and it only drops comments by the
  signed-in account.
- **Posts of the chargehand mod.** The mod runs in the worker's session, so what it posts
  (tool arguments, the last answer, the rate limits) is session output. The daemon cleans
  and caps it like a hook post, and it changes a worker only through the hook transitions.
  The worker tools keep what they are given as untrusted text: a question and its options,
  a report's summary and links (only plain http(s) URLs, and only a pull request link
  becomes the card's link), and the handoff note. The board renders them as text, no log
  line or notification carries them, and no prompt that chargehand writes includes them.
  `read_handoff` gives the note back only as a tool result, to a session of the worker
  whose token the post carries, so a fresh session after a refresh reads it as session
  data, not as a prompt.
  A report of `done` or `failed` can end the worker whose token the post carries, as its
  status file could. A timer from `request_wake` is a wake with
  chargehand's own pointer, under the same caps as any wake. The answer to a question is
  the operator's line, sent as a reply.
- **Command source output.** The commands of a `command` source are the operator's own,
  but what they print comes from the system they poll. chargehand runs them without a
  shell, with a timeout and a cap on the output, and checks every line against a closed
  event shape: an item key must be a safe path name and a URL a plain http(s) URL. A
  line that does not fit is skipped. The log names the reason and, for an unknown field,
  the field names, sanitized and cut to 80 characters; it does not contain the line's
  values.

### Defenses

- **The human gate.** A run starts only for an issue that is assigned to the operator and
  carries the queue label. On a public tracker, only people with triage rights can do
  either. chargehand never picks up unassigned or unlabelled work.
- **Issue text never enters the launch prompt.** The prompt carries only the issue
  identifier and URL; the agent fetches the issue itself. Where a tracker builds its
  URLs from the issue title — Linear appends a slug — the adapter shortens the URL to
  its identifier form first, so the title does not ride along into the prompt or into
  a notification. Claude Code's auto-mode
  classifier trusts user messages and does not see tool results, so this keeps
  attacker-controlled text on the untrusted side of that boundary.
- **A pull request head is never a session's workspace.** A role with `checkout = "item"`,
  such as the shipped `pr-reviewer`, starts its session in a fresh checkout of the base
  branch, the only directory chargehand marks as a trusted workspace. Its agent file must
  be committed there or be in `~/.claude/agents`; the launch checks that checkout, not
  the main one. The pull request head goes into a second, detached checkout outside that
  directory, under `<worktree_root>/.items/`, which the role's setup step fills and the
  launch prompt names. The session gets a read permission for it and nothing else: it is
  not passed as an added directory, because Claude Code loads skills from one.
- **The setup step does not run the head's code.** The role's setup command runs in the
  head checkout, at the launch and again before each wake, so that the checkout follows
  the pull request head. Its program is taken from the base checkout, the main checkout
  or an absolute `PATH` entry, never from the head: a relative program that is in neither
  checkout is refused. Before a wake, the base checkout is left out as well, because the
  session has worked in it since the launch and a program there may be the session's.
  Git runs with hooks and the fsmonitor turned off for the whole step (`core.hooksPath`
  and `core.fsmonitor` through git's environment configuration), and so does every git
  call chargehand itself makes (`-c` on the command line): a repository with a relative
  `core.hooksPath`, as husky sets it, or a `core.fsmonitor` set to a program, would
  otherwise run that program when git checks the head out or reads the worktree. A wake
  whose step fails waits.
- **A changed workspace is not resumed.** Before each wake, and before `continue`, of such
  a worker, chargehand checks that its working directory is still on the worker's own
  branch and that the files Claude Code loads from it (`.claude/` and `CLAUDE.md` and
  `CLAUDE.local.md` at any depth, `.mcp.json`) are as they were when the session started,
  after the setup step. Tracked, untracked and ignored files count by their content, a
  symlink by its target path and, for a file, that file's content, and the `.claude` entry
  itself counts, so that a symlink in its place is seen. Otherwise a wake stops the idle
  session instead of resuming it, pauses the worker with its events kept and sends
  `wake-fault` to the hook, and `continue` is refused. It also refuses either checkout
  when it is no longer a plain directory that git lists, from the main checkout, as the
  worktree it was: a symlink, a `.git` that points somewhere other than the git directory
  the main checkout recorded for it, or an item checkout that is not detached at its own
  path. The main checkout holds those records out of the session's reach, and the `.git`
  file is read as data, never followed by running git, so a redirected link is seen
  rather than acted on. The check runs again after the refresh step, so a step that moved
  the head into the session's own worktree shows as a worktree off its branch before the
  resume. An operator who has looked at the
  worktree accepts a file change with `chargehand continue <item> --force`, which records
  the files as they are now; a worktree that left its branch, or whose item checkout is
  gone or redirected, is not accepted, and `chargehand discard` removes the worker.

  What this does not cover: the session itself. During a turn it can run any command the
  classifier or a prompt lets through, including git commands in the head checkout,
  which then run the head's hooks under a relative `core.hooksPath`, and a file it copies
  into its working directory can be read in that same turn; one it copies into `~/.claude`
  is loaded by every session on the machine and is not checked. The shipped agent file tells
  the reviewer to treat the head as data, never to run it, never to check anything out,
  and its allow rules leave out `gh api`, `gh pr checkout`, merge, close and approve.
  Those are guardrails, not a boundary. A session that gains approval to write outside
  its own working directory is out of scope: one that can write the item checkout, the
  main checkout or `~/.claude` can already alter the operator's shell profile or a
  LaunchAgent, so the checks above defend against a change the session made inside its
  own worktree and against an accident, not against a session that already holds that
  power. Git filters that the operator configured, such as
  Git LFS, still run on the files the head selects through `.gitattributes`, and a
  session that the operator resumes by hand, outside chargehand, is not checked.
- **A wake carries kinds and pointers only.** The message that wakes a worker is a fixed
  line that chargehand writes, followed by one line per event with its kind and its
  pointer. A pointer that is not a plain http(s) URL or a plain identifier is left out. A
  kind comes from a source, so it must match `[a-z][a-z0-9_]{0,63}`, or the line names
  `event` instead. No actor name, title or body is added, so the wake crosses the same
  boundary as the launch prompt.
- **Auto permission mode by default.** Sessions run behind Claude Code's safety classifier,
  which is documented as blocking, among others, download-and-execute, data exfiltration,
  force pushes, and destroying files that predate the session, and which falls back to a
  permission prompt when it cannot approve. Treat that list as a description of intent
  rather than a specification: in our own testing the classifier approved deleting a
  git-tracked file that predated the session, and explained itself with "Allowed by auto
  mode classifier". It weighs context we cannot see, and it is not a boundary you can
  reason about precisely. Auto mode reduces risk; it is not a guarantee. `bypassPermissions`
  is an explicit opt-in intended for a dedicated machine, user account, or VM. Deny rules
  apply in every mode, including `bypassPermissions`, and unlike the classifier they are
  deterministic - but see below for what they are deterministic *about*.
- **A loopback daemon with a token.** The daemon (`chargehand serve`) listens on
  127.0.0.1 only, on the `[serve]` port, and no setting binds another address. Reads
  (`GET`) need no token and return what `status --json` returns: identifiers, states,
  timings and URLs. Every other method needs `Authorization: Bearer <token>`, compared in
  constant time. The token is in `token_file` (next to `config.toml` by default), which
  the daemon creates with mode 0600 and refuses to use when it is not a regular file of
  the operator's with mode 0600; `doctor` checks the same. A request whose `Host` header
  is not `127.0.0.1:<port>` or `localhost:<port>` is refused, which stops DNS rebinding,
  and no response carries a CORS header, so a web page on another origin cannot read a
  response. A cross-origin page can still send a request without reading the answer,
  which is why every mutating method needs the token. Request bodies are capped at 1 MiB.
- **A token per session.** Each launch and each fork writes a new token for its session,
  in a file of mode 0600 in a directory of mode 0700 under the state directory
  (`worker-tokens/`). The session's hooks and its chargehand mod send that token, and the
  daemon maps the token, not the session id or the working directory that a post names,
  to the worker, comparing in constant time. A session never gets the board's token: its
  settings hold its own token, and the mod's environment names its own token file. The
  channel routes take only a worker's token, and the board's routes only the board's
  token. A token ends with its worker: the daemon refuses the token of a worker that ended,
  a new launch in the same worktree replaces it, and the file is deleted when the session
  is removed. A tool call acts as the token's worker, whatever session id it names. A
  hook, a hello, a lifecycle or usage post, or an outbox poll from a session that is not
  the worker's current one, such as a copy that a resume started in the same worktree
  with the same settings, is ignored: the post changes nothing, and the poll gets no
  message. Every launch's settings deny `Read` and `Edit` (which covers Write) on the
  board's token file and on the whole token directory, a launch without hooks too, so a
  session launched earlier is also denied the token files of sessions launched after it; a
  launch with hooks also denies them on every session settings file. The mod still reads
  its own token file, because a `Read` deny rule does not bind the mod's own file reads
  (observed with Claude Code 2.1.322).
- **Session hooks.** Launched sessions post their start, the start and end of each turn,
  failures, notifications and permission prompts to `POST /hooks/<event>` on that port,
  with the session's token in the `Authorization` header; a post without a valid token
  changes nothing. A session launched before sessions had their own token posts with the board's
  token, which `/hooks` still accepts, and the daemon logs that once per session. The token
  is never on a command line, where other users of the machine could read it with `ps`:
  the settings of a launch with hooks are in a file of mode 0600 in a directory of mode
  0700 under the state directory, and the `SessionStart` command hook names the token
  file, not the token, and runs its script with `python -I`, so nothing in the worktree
  is imported. The settings file is deleted when its session is removed. A post moves
  only the live worker whose token it carries. The daemon answers every post but a
  permission prompt with an empty object, so it never keeps a turn running, never
  blocks a prompt and never adds text to the model's context. The turn-start post
  (`UserPromptSubmit`) carries the prompt, which the daemon never reads or stores. The
  payload is untrusted: the last answer, the tool input and the other fields are cleaned
  of terminal control sequences and capped when stored, never reach a notification or a
  prompt, and
  `GET /api/state` returns them as JSON data for the page to escape. `status --json`
  names the last hook event and the reset time, but no payload text.
- **Permission decisions come from the operator only.** The daemon holds a session's
  permission hook open and answers it with allow or deny only after the operator clicks
  Allow, Deny or Always allow on the board, which is a `POST` with the token. Nothing in a
  hook payload, a tracker or a session's output can answer a prompt. Always allow records
  a rule for that one worker in the ledger: the same tool with the same input, matched by
  a SHA-256 hash of the whole input as the session sent it, so an input that differs
  after the display cap or in a control character does not match. The one key left out
  is the `description` of a Bash call, which the model words anew for each call and which
  changes nothing the command does. The rule never changes
  a role, a repository or Claude Code's settings, and it ends with the worker. A hook the
  daemon holds is answered with no decision, so the session shows its own dialog, when
  the hook's timeout comes near (the role's `permission_timeout_secs`, 600 seconds by
  default), when the worker leaves `blocked` because the operator answered in the session,
  and when the daemon stops; after a restart a prompt can only be answered in the session.
  The first answer wins: the ledger row changes only while it is open and held, so a
  second click, a late click and a click after a timeout change nothing. Before it takes
  a click, the daemon checks in a fresh session listing that the session still shows a
  permission dialog; after an answer in the session the click is refused and the card
  is cancelled, so the ledger never records a board decision that the session did not
  apply. Only the short time between that listing and the answer is left open.
- **The board.** `GET /` serves one static page, its script and its style sheet,
  all from the package; nothing else is served as a file, and the page loads nothing
  from another origin. They are sent with `Content-Security-Policy: default-src 'none';
  script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri
  'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`, so no inline script or
  style runs and no other site can frame the page. The page reads `GET /api/board`. Every
  text field in that answer is cleaned of control sequences and direction overrides on
  the server, `<`, `>` and `&` are escaped in its JSON, and the page sets text with
  `textContent` only, never as markup. A link is made only from a plain http(s) URL, and
  opens with `rel="noopener noreferrer"`. The board shows session text (the last prompt,
  the last answer, a permission prompt's tool name and input) that `status` leaves out, so it is
  for the operator's own browser on the machine.
- **Board actions hold the token.** Each action is a `POST` with the bearer token, like
  any other mutating call, and records the same control request as the CLI, which the
  tick applies; no action handler calls `claude`, git or a tracker, except attach (below).
  The operator pastes the token into the page once; the page keeps it in the browser's
  `localStorage` for `http://127.0.0.1:<port>` and sends it only in the `Authorization`
  header. It is never in a URL, in `GET /`, or in any answer. Anything that can run
  script in that origin, or read that browser profile, can therefore read the token, so
  use the board in your own browser profile; "Forget token" removes it. The page checks
  the token with `POST /api/token`, which passes the same `Host` and token checks as
  every action and then answers 204: it records nothing and tells a caller nothing that
  a refused action does not tell already. Another origin cannot read the token, and the
  `Host` check and the missing CORS headers still apply. The token is the same one every
  client uses: a board action can do what any CLI command can, and no more.
- **Operator text from the board.** A reply is trusted operator text: it reaches the
  session's next turn as written. It is one line, cleaned of control sequences and
  direction overrides, capped at 500 characters, and carried in the inbox under the
  source name `board`, which no configured source may take, so no event from a source
  can pass as a reply. The request row keeps the line, so it is in the ledger like the
  other request arguments.
- **The prompt of a clone.** The clone form sends a prompt template that the operator
  edited: trusted operator text from a holder of the token, or from the command line, as
  a role file is. It may reach the clone's first prompt; tracker text never does. It is
  cleaned of control sequences and direction overrides, refused above 16 KiB, and checked
  for the role prompt's placeholders, so `{title}` and `{body}` are refused as in a role
  file. A model name that starts with `-` is refused, because it goes into the command
  line. The request row and the clone's ledger row keep the prompt; log lines name only
  its length. `GET /api/workers/<id>/clone` needs no token, as every read, and shows the
  role's prompt template, which the card already shows filled in.
- **A clone and its original's worktree.** Two workers never work in one worktree, but a
  clone in full mode starts with a copy of the original's conversation, which names the
  original's worktree, and a fresh clone can find it too. A line before a full-mode
  clone's prompt says that this worktree belongs to another worker, and the clone's own
  settings file denies `Edit(//<original worktree>/**)` and `Write(//<original
  worktree>/**)`, as given and resolved. A clone of a clone gets the same line and rules for
  the worktree of every earlier original, because a copy of a copy names them all. A deny rule holds in every permission mode,
  `auto` and `bypassPermissions` included, so the file tools (Edit, Write and the other
  file-editing tools) cannot change the original's files. The Bash tool is not covered: a
  rule matches the text of a command, not the files it changes, so under `auto` or
  `bypassPermissions` a shell command can still write there; a role in `manual` mode asks
  before such a command.
- **Board settings.** Names, icons, pins and the collapsed and hidden repositories are
  written by `POST /api/repositories/<name>/board` and `POST /api/workers/<id>/board`,
  with the token, straight into the ledger's `board_state` table; they reach no session,
  no prompt, no notification and no log line, and only the board model reads them. A name
  is operator text, cleaned to one line and refused above 60 characters. An icon is
  accepted only as one short emoji (one symbol, or symbols joined by the zero-width
  joiner, each with an optional variation selector and skin tone; a flag of two regional
  indicators or of tag characters; or a keycap after `#`, `*` or a digit; no letters, no
  markup) or a name from a bundled set, so nothing is uploaded or fetched. The model cleans both again when it reads them, because an
  import writes the same table. Hiding a repository only filters the board: its workers
  are still supervised and still notify, so hiding cannot silence a blocked worker.
- **Attach runs a terminal on this machine.** `POST /api/workers/<id>/attach` opens an
  iTerm2 or Terminal.app window that runs the worker's attach command. The terminal is one
  of those two names; the command must be plain words and its last word, the session id,
  a plain identifier, so a shell that runs it finds no metacharacter. For Terminal.app the
  daemon writes the command into a script file and runs `/usr/bin/open -a Terminal
  <file>`, through LaunchServices, so it needs no Automation permission. The file is in
  `attach/` under the state directory (mode 0700, created anew if missing, refused if it is
  a symbolic link), has a random name and mode 0700, is created with `O_EXCL` and
  `O_NOFOLLOW`, and holds only `rm -f -- "$0"` and `exec <command>`: it deletes itself before
  it runs the command, and a file that never ran is removed when the next one is written,
  after five minutes. The daemon's process sets no quarantine attribute, so macOS shows no
  dialog for the file. For iTerm2, which asks before it runs any file it opens, the daemon
  runs `/usr/bin/osascript`; the command reaches the AppleScript as an argument, never
  inside the script's source, so no text in it can change the script. macOS asks once
  whether the daemon's process may control iTerm2 (Privacy & Security, Automation). The
  board embeds no chat: a conversation with a session happens in that terminal.
- **Untrusted text stays out of routine output.** `status` and `watch` print identifiers,
  states, timings, and URLs only. Titles and a session's waiting text need
  `--verbose-titles`; agent output needs `logs`. A pull-request URL a session reports is
  kept only when it is a plain URL, and a branch a session renamed after the tracker's
  convention, which carries a slug of the title, is shown only with `--verbose-titles`.
  All output is stripped of terminal control sequences.
- **Driving the CLI from an AI assistant.** Status output then lands in a session that
  holds the operator's permissions. The shipped skill reads with `status --json`, which by
  default carries identifiers, states, timings and URLs only, and the shipped settings let
  only that read run without a prompt. Everything else sits behind a Claude Code *ask*
  rule, which prompts in every permission mode, auto and `bypassPermissions` included:
  every mutating command, and `logs` and `--verbose-titles`, which bring issue and agent
  text into the session. `discard` additionally requires `--yes`, which approving the
  prompt does not replace, and refuses when commits are unpushed. An ask rule makes Claude
  Code ask whatever runs the session. A hook, a script or an app that answers permission
  prompts automatically leaves no person to see the prompt, and then the rule protects
  nothing.
- **Agents are discouraged from steering the runner.** Sessions that chargehand launches
  get deny rules for the chargehand CLI, for stopping, killing, removing or respawning
  other sessions, for the operator commands of the lease tool banksman (`banksman admin`),
  for the daemon's port (`127.0.0.1:<port>` and `localhost:<port>` in a command, and
  `WebFetch` to either host), and for the
  flags that would start a fresh session without these rules. Read that as a guardrail
  and not as a boundary. A Claude Code `Bash` rule matches the command text the model
  writes, not the program it ends up running, which its documentation states plainly:
  such a rule "isn't a security boundary around the program". chargehand's patterns
  therefore lead with a wildcard so that an absolute path to the binary and a
  `sh -c '...'` wrapper are covered as well as the bare name, which closes the forms a
  session reaches by accident. It does not close the forms a session reaches on purpose:
  text assembled at runtime, a name split across quotes, a copy of the binary under
  another name, or a script that calls the API directly all still get through, and no
  list of patterns can change that. The boundary is the operating system, not the rule
  list; see the first known limit below.
- **One writer of side effects.** Mutating commands record a request; only the daemon's
  tick acts on it, under the tick lock. Control actions cannot race a launch in progress.
- **Minimal notifications.** A notification carries the issue identifier, the state, the
  issue URL, and a short reason chargehand itself writes — never question text, agent
  output, issue titles, a status file's `stop_reason`, or a setup script's output —
  because it usually crosses a third-party relay. Stripping terminal escapes would not
  make that text safe to forward; it is kept local instead, in the ledger, and reached
  through `status --verbose-titles` and `logs`.
- **Export carries no secret.** `chargehand export` never puts the token in the archive
  and never reads the Keychain. It leaves out every configuration key that can carry a
  credential, and it refuses to write an archive in which a file still contains the home
  directory or the token. `chargehand import` treats the archive as input it checks: it
  refuses a newer format, a file name outside the archive's layout, a symlink and a file
  the manifest does not list; it writes no file and no board setting that exists without
  `--force`; it cleans board names and icons as the board does; and it writes a new token
  rather than taking one from the archive. The archive still holds the notification hook
  and the role files, which run as the operator: import only an archive you made. The hook
  is copied as it is, so a credential written into it, such as a private push topic, is
  in the archive too; export says so when it copies a hook.
- **Worker archives stay private.** An archive of a worker holds its worktree and its
  session transcript, which can contain anything the session read, so the zip has mode
  0600 in a directory of mode 0700 under the state directory, and its content never
  reaches a log line, a notification or the board. A symbolic link in the worktree is
  stored as a link, so a link to a file elsewhere does not copy that file into the zip. The
  transcript path comes from a hook post, so only a regular `.jsonl` file under Claude
  Code's `projects` directory is read. The settings file of a session, which holds the
  daemon's token, is not in the worktree and so not in the archive. The worktree is
  removed only after the zip is complete and has passed its check, and only by the tick,
  with the guard of discard applied again.
- **Small supply chain.** No runtime dependencies beyond the Python standard library.

- **The chargehand mod does only what it says.** Every worker session loads it, so what it
  may do is checked in the test suite against its source, and against `claude plugin
  validate` where Claude Code is installed: it hooks the session start, the turn end, the
  session end and the calls of its own tools; it reads two environment variables, the
  token file and the session's id, working directory and usage; it posts to the daemon,
  registers tools and submits a prompt. It never answers a permission prompt, never
  writes a file or the environment and runs no process. The only text it submits is the
  outbox message the daemon handed it, which is chargehand's wake message of kinds and
  pointers. It posts only to the port that the launch settings name, with the session's own token
  from the token file that they name; the settings name the file, not the token.
- **The usage request holds no credential.** To read the weekly usage window, the daemon
  sends Claude Code's `get_usage` control request to a short `claude -p` process. Claude
  Code fetches the usage with its own sign-in; chargehand reads no token and no Keychain
  item and makes no network call. The process runs in an empty private directory under
  the state directory, with all hooks and your MCP servers turned off and no session
  file, runs no turn, and is killed after a timeout. Only the percentage and the reset time of its answer are kept.

### Known limits

- Sessions run as the operator's macOS user. Deny rules are a guardrail, not a sandbox: a
  session that escapes them can do what the operator can do. Run agents under a separate
  macOS user, or on a machine that holds nothing else, when that matters.
- Nothing stops a session from reaching the control plane if it sets out to. The deny
  rules are verified rather than assumed - `probes/deny_rules.py` runs each form against a
  real Claude Code install and reports which were refused - but what they are verified to
  do is refuse a list of spellings. Treat a run that is misbehaving as able to cancel its
  siblings, and reach for a separate user account when that is not acceptable.
- Prompt injection through issue text is mitigated, not eliminated. Draft pull requests and
  human review remain the final gate; chargehand never merges.
- The machine running chargehand holds tracker credentials and an authenticated Claude Code
  install. Treat it as you would a CI worker with write access.
- A session runs as the operator's user, so it can read the daemon's token file and the
  ledger. The deny rules for the port are a guardrail against the careless path; a session
  that reads the token and reaches the port another way can send what the CLI can.
- The token per session is a guardrail of the same kind, not a wall. It stops the careless
  path and a prompt-injected call that names another worker's session id: such a call acts
  as the worker whose token it carries. A process of the same user can still read another
  session's token file or settings file with a shell command, which the deny rules do not
  cover. With
  another worker's token, a session can post as that worker's hooks and mod: move its
  state and its label, take its outbox message or call a tool for it, but run no command
  and answer no prompt. Sessions launched before sessions had their own token still post
  their hooks with the board's token until their next launch. The launch settings put the
  session's own token file path in its environment, where every command of the session
  can read it.
- A session can read any credential that the runner or a notification hook reads from the
  login keychain, because it runs as the same user. Give a hook a credential that protects
  nothing else, such as the password of a mailbox that exists only to send alerts.

## Disclosure Policy

We follow coordinated disclosure:

- We aim to acknowledge a report within **3 business days** and to provide an initial
  assessment within **10 business days**.
- We will agree on a disclosure timeline with the reporter, targeting a fix and public
  advisory within **90 days** of the report, sooner for actively exploited issues.
- Fixes are released in a new version, documented in `CHANGELOG.md`, and announced through a
  published GitHub Security Advisory that credits the reporter unless anonymity is
  requested.
