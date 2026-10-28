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
- **No server.** chargehand opens no port. It is steered by a CLI on the machine it runs on. There
  is no token to leak and no browser-borne attack surface (CSRF, DNS rebinding, XSS).
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
  and for the
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
- **One writer of side effects.** Mutating commands record a request; only the periodic
  tick acts on it. Control actions cannot race a launch in progress.
- **Minimal notifications.** A notification carries the issue identifier, the state, the
  issue URL, and a short reason chargehand itself writes — never question text, agent
  output, issue titles, a status file's `stop_reason`, or a setup script's output —
  because it usually crosses a third-party relay. Stripping terminal escapes would not
  make that text safe to forward; it is kept local instead, in the ledger, and reached
  through `status --verbose-titles` and `logs`.
- **Small supply chain.** No runtime dependencies beyond the Python standard library.

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
