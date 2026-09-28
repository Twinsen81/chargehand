# Probes

Scripts that check a claim about an external tool against the real thing. They are not
part of the package and are not installed with it: each one starts a real Claude Code
session, so it costs usage and cannot run in the test suite, which never needs a real
`claude`.

Run one after upgrading Claude Code, or when changing what the claim depends on.

| Probe | Claim it checks | Last run |
|---|---|---|
| `deny_rules.py` | The deny rules passed at launch refuse the control plane, in every spelling a session might reach it through. | Claude Code 2.1.280: all ten attempts refused, control ran. |
| `assistant_rules.py` | The bundled assistant settings, loaded as they ship, gate every mutating command and both untrusted reads in auto and bypassPermissions mode, in the spellings an assistant produces by accident, while `status --json` stays silent; and the skill answers a status question without a prompt and takes a request to change a run no further than its refused command. | Claude Code 2.1.282: 55 checks, none failed. |
| `smoke_route.py` | The runner behaves against a real tracker and real Claude Code the way the test suite says it behaves against the fakes: launch, fault injection at every step, ambiguous tracker writes, the control commands, and what a session reports at the end of a turn. | Claude Code 2.1.280, Linear: 116 checks, none failed. |

The parts of a probe that can be checked without a real install - the battery of cases,
the model of the matcher, the verdict logic - are covered by `tests/test_deny_rules.py`,
`tests/test_assistant_rules.py` and `tests/test_smoke_route.py`, so neither a rule list nor a
fault plan can narrow silently between probe runs.

## `assistant_rules.py`

```bash
python3 probes/assistant_rules.py                       # everything, a few minutes
python3 probes/assistant_rules.py --only conversation   # the skill in conversation
python3 probes/assistant_rules.py --mode auto --json    # one mode, as JSON
```

Its turns ignore your own Claude Code settings, skills and MCP servers, so it checks the
shipped files even on a machine where you have installed them.

## `smoke_route.py`

It needs a tracker team it may write to, and an API key in the login keychain:

```bash
python3 probes/smoke_route.py --team ABC                  # everything
python3 probes/smoke_route.py --team ABC --only happy      # one section
python3 probes/smoke_route.py --team ABC --keep --json     # leave the evidence behind
```

It creates its own issues, assigned to whoever owns the API key, under labels that are
deliberately not the default ones, and trashes them afterwards. The runner it drives is
a throwaway instance: configuration, ledger, logs, scratch repository and worktrees all
live under one directory (`.context/smoke` by default), reached through the
`CHARGEHAND_*` path overrides, so a machine with a real chargehand installed is not
touched. Sessions it started are stopped and removed on the way out, scoped by working
directory so one interrupted mid-launch is caught too.

Two things it cannot do, and one to know about:

- **It does not cover the reboot drill or the SSH leg.** Both need a second machine or a
  restart, so they stay manual.
- **It waits for each issue to become answerable by the runner's own queue query before
  ticking.** A tracker takes a fraction of a second to index a newly labelled issue, and
  a probe that drives it faster than a human can click would otherwise measure the
  tracker rather than the runner.
- **A section failure is worth reading twice.** The transcript it writes next to the
  sandbox keeps the tick report and the proxy's call log for the checks that have them,
  which is usually enough to tell a runner defect from a timing artefact.

