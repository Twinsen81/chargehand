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
  instruction files, its permission rules).

### What is not trusted

- **Issue text.** Titles, descriptions, and comments can be written by anyone who can file
  an issue. They are attacker-controlled input to an agent.
- **Agent output.** Logs and messages produced by a session can echo anything the session
  read.

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
- **Auto permission mode by default.** Sessions run behind Claude Code's safety classifier,
  which is documented as blocking, among others, download-and-execute, data exfiltration,
  force pushes, and destroying files that predate the session, and which falls back to a
  permission prompt when it cannot approve. Treat that list as a description of intent
  rather than a specification: in our own testing the classifier approved deleting a
  git-tracked file that predated the session, and explained itself with "Allowed by auto
  mode classifier". It weighs context we cannot see, and it is not a boundary you can
  reason about precisely. Auto mode reduces risk; it is not a guarantee. `bypassPermissions`
  is an explicit opt-in intended for a dedicated machine, user account, or VM. Repository
  deny rules apply in every mode, and they are the control you can actually predict.
- **No server.** chargehand opens no port. It is steered by a CLI, remotely over SSH. There
  is no token to leak and no browser-borne attack surface (CSRF, DNS rebinding, XSS).
- **Untrusted text stays out of routine output.** `status` and `watch` print identifiers,
  states, timings, and URLs only. Titles need `--verbose`; agent output needs `logs`. All
  output is stripped of terminal control sequences.
- **Driving the CLI from an AI assistant.** Status output then lands in a session that
  holds the operator's permissions. The shipped skill reads only `status --json`, and every
  mutating command sits behind a Claude Code *ask* rule, which prompts in every permission
  mode. `discard` additionally requires `--yes` and refuses when commits are unpushed.
- **Agents cannot steer the runner.** Sessions that chargehand launches get deny rules for
  the chargehand CLI and for stopping, removing, or respawning other sessions.
- **One writer of side effects.** Mutating commands record a request; only the periodic
  tick acts on it. Control actions cannot race a launch in progress.
- **Minimal notifications.** A notification carries the issue identifier, the state, the
  issue URL, and a short reason chargehand itself writes — never question text, agent
  output, issue titles, a status file's `stop_reason`, or a setup script's output —
  because it usually crosses a third-party relay. Stripping terminal escapes would not
  make that text safe to forward; it is kept local instead, in the ledger, and reached
  through `status --verbose` and `logs`.
- **Small supply chain.** No runtime dependencies beyond the Python standard library.

### Known limits

- Sessions run as the operator's macOS user. Deny rules are a guardrail, not a sandbox: a
  session that escapes them can do what the operator can do. Run agents under a separate
  macOS user, or on a machine that holds nothing else, when that matters.
- Prompt injection through issue text is mitigated, not eliminated. Draft pull requests and
  human review remain the final gate; chargehand never merges.
- The machine running chargehand holds tracker credentials and an authenticated Claude Code
  install. Treat it as you would a CI worker with write access.

## Disclosure Policy

We follow coordinated disclosure:

- We aim to acknowledge a report within **3 business days** and to provide an initial
  assessment within **10 business days**.
- We will agree on a disclosure timeline with the reporter, targeting a fix and public
  advisory within **90 days** of the report, sooner for actively exploited issues.
- Fixes are released in a new version, documented in `CHANGELOG.md`, and announced through a
  published GitHub Security Advisory that credits the reporter unless anonymity is
  requested.
