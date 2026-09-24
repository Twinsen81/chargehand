# Contributing to chargehand

Thanks for your interest in improving chargehand. This guide covers setup, tests, the
project layout, coding standards, the sign-off requirement, and the pull-request process.
By contributing you agree your work is licensed under the project's
[Apache License 2.0](LICENSE).

## Prerequisites

- Python 3.11 or newer.
- macOS for anything that touches launchd or the Keychain. The test suite itself is
  platform-independent and runs on Linux too.

## Setup and tests

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest -q
```

Tests must not need a real Claude Code install, a real tracker, or network access. Anything
that shells out goes through a small seam that the tests replace with a fake `claude`
binary or a fake tracker. Crash-safety is tested by fault injection: interrupt a launch
after each step and assert that the next tick adopts, resumes, or fails loudly.

## Project layout

- `src/chargehand/` — the package. `cli.py` is the entry point.
- `tests/` — pytest suite.
- `docs/DESIGN.md` — architecture and behavior. Read it before proposing structural changes.
- `DECISIONS.md` — resolved design decisions and how to change them.

As the implementation lands, modules follow the split described in `docs/DESIGN.md`: the
tick, the ledger, one module that knows the Claude Code CLI, tracker adapters, the pool
interface, and notifications.

## Coding standards

- **No runtime dependencies.** The standard library only. A tool that launches privileged
  agents should have the smallest possible supply-chain surface. Test-only dependencies are
  fine.
- **One module per external tool.** Only one module may know the Claude Code command line,
  and only tracker adapters may know a tracker's API. Everything else works on plain data.
- **Side effects have one writer.** Mutating control commands record a request and let the
  tick apply it. Do not add a second code path that calls `claude`, git, or a tracker.
- **Treat tracker text and agent output as untrusted.** Never put it into a prompt, never
  print it without stripping terminal control sequences, and keep it out of default output.
- Type hints on public functions. Comments explain why, not what.

## Developer Certificate of Origin (DCO)

All commits must be signed off. chargehand uses the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): by adding a
`Signed-off-by` line you certify that you wrote the contribution or otherwise have the
right to submit it under the project's open-source license.

Sign off automatically when committing:

```bash
git commit -s -m "Your message"
```

This appends a trailer like:

```
Signed-off-by: Your Name <you@example.com>
```

Use your real name and an email address you can be reached at. Every commit in a pull
request must carry a sign-off line.

## Pull request process

1. Fork and branch from `main`. Keep changes focused; one logical change per PR.
2. Add or update tests for your change.
3. Update [`CHANGELOG.md`](CHANGELOG.md) under `## [Unreleased]`.
4. Update `docs/DESIGN.md`, `DECISIONS.md`, or `SECURITY.md` when behavior, a decision, or
   the threat model changes.
5. Make sure `pytest -q` passes.
6. Open the PR, fill in the template, and confirm every commit is signed off. CI must be
   green before review.

## Compatibility expectations

chargehand follows [Semantic Versioning](https://semver.org/). Before `1.0.0` anything may
change between minor versions. The surfaces that will be treated as public API are the CLI
(commands, flags, `status --json` output), the configuration files, the status-file
protocol, and the command-adapter JSON contract.

## Code of Conduct

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).
