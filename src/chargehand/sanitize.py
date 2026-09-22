"""Strip anything from untrusted text that could act on the terminal it is printed to.

Tracker titles and agent output are attacker-influenced. Printing them raw lets an
issue title move the cursor, rewrite earlier lines, set the window title, or drive
a terminal's clipboard and hyperlink escapes. Everything chargehand prints that did
not originate in chargehand goes through :func:`clean`.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

# CSI/SS2/SS3/DCS/OSC/PM/APC/SOS and the two-character escapes, plus bare C1 bytes.
_ANSI = re.compile(
    r"""
    \x1b \[ [0-?]* [ -/]* [@-~]        # CSI ... final
  | \x1b \] .*? (?: \x07 | \x1b \\ )   # OSC ... BEL or ST
  | \x1b [PX^_] .*? (?: \x1b \\ | \x07 )  # DCS/SOS/PM/APC ... ST
  | \x1b [@-Z\\-_]                     # two-character escape
  | \x1b .                             # any other escape introducer
  | [\x80-\x9f]                        # bare C1 controls
    """,
    re.VERBOSE | re.DOTALL,
)

# Everything below 0x20 except tab and newline, plus DEL.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Bidirectional overrides and isolates: they can visually reorder a line.
_BIDI = re.compile(r"[‪-‮⁦-⁩]")

_REPLACEMENT = "�"


def clean(text: str, *, keep_newlines: bool = True) -> str:
    """Return *text* with terminal control sequences and direction overrides removed."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _ANSI.sub("", text)
    text = _BIDI.sub("", text)
    text = _CONTROL.sub("", text)
    if not keep_newlines:
        text = text.replace("\n", " ").replace("\t", " ")
    return text


def one_line(text: str, *, limit: int = 120) -> str:
    """A single-line, length-capped rendering, for tables."""
    cleaned = " ".join(clean(text, keep_newlines=False).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def safe_url(value: object, *, limit: int = 500) -> str | None:
    """Return *value* only if it really is an ordinary http(s) URL.

    `startswith("http")` accepts "http and then whatever the agent felt like writing",
    which is how agent-authored text reached output documented as carrying none.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > limit:
        return None
    if candidate != clean(candidate, keep_newlines=False) or any(
        character.isspace() for character in candidate
    ):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return candidate


def is_safe_identifier(value: str) -> bool:
    """Identifiers reach argv, branch names, and file paths, so keep them boring."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value))


def scrub_identifier(value: str) -> str:
    """Best-effort identifier for display when a tracker returns something odd."""
    cleaned = clean(value, keep_newlines=False).strip()
    if is_safe_identifier(cleaned):
        return cleaned
    return _REPLACEMENT if not cleaned else re.sub(r"[^A-Za-z0-9._-]", _REPLACEMENT, cleaned)[:64]
