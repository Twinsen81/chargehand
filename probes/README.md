# Probes

Scripts that check a claim about an external tool against the real thing. They are not
part of the package and are not installed with it: each one starts a real Claude Code
session, so it costs usage and cannot run in the test suite, which never needs a real
`claude`.

Run one after upgrading Claude Code, or when changing what the claim depends on.

| Probe | Claim it checks | Last run |
|---|---|---|
| `deny_rules.py` | The deny rules passed at launch refuse the control plane, in every spelling a session might reach it through. | Claude Code 2.1.278: all ten attempts refused, control ran. |

The parts of a probe that can be checked without a real install - the battery of cases,
the model of the matcher, the verdict logic - are covered by `tests/test_deny_rules.py`,
so a rule list cannot narrow silently between probe runs.

