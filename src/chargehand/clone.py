"""The clone request: a new worker made from an existing one.

A clone starts from its original's current branch in a worktree of its own, so the two
never write the same tree. In ``fresh`` mode it gets nothing from the original's session;
in ``full`` mode its session starts as a copy of the original's conversation. The operator
may give it another prompt template, model and effort, and chooses the event kinds it
follows on the original's item; by default it follows none, so only a reply reaches it.

The board, the command line and the tick check a request with :func:`clone_args`, so all
three agree on what is valid, and the board and the command line record the same row.
The prompt is operator text from a holder of the daemon's token or from the command
line: it may reach the session's first prompt, it is cleaned of control sequences and
capped, and no log line carries it, only its length.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from chargehand import config as config_mod
from chargehand import ledger as ledger_mod
from chargehand import roles as roles_mod
from chargehand import sanitize
from chargehand.errors import ConfigError

# The request kind.
CLONE = "clone"

# A prompt template above this many bytes is refused rather than cut.
PROMPT_LIMIT = 16 * 1024
SUBSCRIBE_LIMIT = 10
# A model name goes into argv, so it never starts with a dash.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,99}")


class CloneArgsError(ValueError):
    """A clone request that is not valid; the message is chargehand's own text."""


def clone_args(raw: Mapping[str, object]) -> dict[str, object]:
    """The request's arguments, checked and cleaned, with the unset ones left out.

    ``mode`` is required. ``prompt``, ``model`` and ``effort`` replace the role's when
    given. ``subscribe`` lists the event kinds the clone follows on the original's item.
    Raises :class:`CloneArgsError`.
    """
    mode = raw.get("mode")
    if mode not in ledger_mod.CLONE_MODES:
        raise CloneArgsError(f"mode must be one of {', '.join(ledger_mod.CLONE_MODES)}")
    args: dict[str, object] = {"mode": mode}
    prompt = clean_prompt(raw.get("prompt"))
    if prompt is not None:
        args["prompt"] = prompt
    model = raw.get("model")
    if model is not None and model != "":
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise CloneArgsError(
                "model must be a model name or alias of letters, digits, '.', '-', '_' and "
                "brackets, such as sonnet"
            )
        args["model"] = model
    effort = raw.get("effort")
    if effort is not None and effort != "":
        if effort not in roles_mod.EFFORT_LEVELS:
            raise CloneArgsError(f"effort must be one of {', '.join(roles_mod.EFFORT_LEVELS)}")
        args["effort"] = effort
    kinds = raw.get("subscribe")
    if kinds is not None:
        if not isinstance(kinds, list) or len(kinds) > SUBSCRIBE_LIMIT or not all(
            isinstance(kind, str) and sanitize.is_event_kind(kind) for kind in kinds
        ):
            raise CloneArgsError("subscribe must be a list of event kinds, such as [\"comment\"]")
        if kinds:
            args["subscribe"] = list(dict.fromkeys(kinds))
    return args


def clean_prompt(value: object) -> str | None:
    """The prompt template without control sequences, or None for the role's own."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise CloneArgsError("prompt must be text")
    if len(value.encode("utf-8")) > PROMPT_LIMIT:
        raise CloneArgsError(f"the prompt is longer than {PROMPT_LIMIT // 1024} KiB")
    prompt = sanitize.clean(value).strip()
    if not prompt:
        return None
    try:
        config_mod.validate_template(prompt, where="the clone prompt")
    except ConfigError as exc:
        raise CloneArgsError(str(exc)) from None
    return prompt


__all__ = ["CLONE", "CloneArgsError", "PROMPT_LIMIT", "clean_prompt", "clone_args"]
