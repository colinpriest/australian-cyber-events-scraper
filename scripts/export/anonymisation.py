"""Shared rules for anonymised export columns.

An anonymised column must never carry the original identifying text. When
anonymisation fails - an LLM error, an empty or unusable answer, an answer that
still names the victim, or no anonymiser at all - the cell gets a placeholder
that says so, and the export counts the failure. The original text is never a
fallback.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

ANONYMISATION_FAILED = "[anonymisation failed]"
NOT_ANONYMISED = "[not anonymised]"

# Names shorter than this are too generic to test for ("AU", "ACT").
_MIN_NAME_LENGTH = 4
# A title shorter than this is too generic to count as a verbatim leak.
_MIN_TITLE_LENGTH = 15


class AnonymisationError(RuntimeError):
    """Anonymisation did not produce safe text."""


def failure_placeholder(reason: str) -> str:
    """The cell text for a failed anonymisation (never the original text)."""
    reason = (reason or "").strip().replace("\n", " ")
    return f"{ANONYMISATION_FAILED} {reason[:200]}".strip() if reason else ANONYMISATION_FAILED


def identifying_leak(text: str, names: Iterable[Optional[str]],
                     title: Optional[str] = None) -> Optional[str]:
    """Why ``text`` is not anonymous, or None.

    Checks the identifiers the export knows for certain: the victim and
    attacker names (whole-word, case-insensitive) and the event title verbatim.
    It cannot prove text anonymous; it catches the failures that matter most.
    The returned reason never contains the identifier itself.
    """
    if not text:
        return None
    for name in names:
        name = (name or "").strip()
        if len(name) < _MIN_NAME_LENGTH or name.lower() in {"unknown", "none", "null", "n/a"}:
            continue
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE):
            # Never quote the name: this reason is written beside the placeholder.
            return "output still contains a known victim or attacker name"
    title = (title or "").strip()
    if len(title) >= _MIN_TITLE_LENGTH and title.lower() in text.lower():
        return "output still contains the event title"
    return None
