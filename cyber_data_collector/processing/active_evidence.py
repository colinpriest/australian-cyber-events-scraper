"""Which member records, and which stored pages, may drive an event's decisions.

Two rules every evidence reader shares, so no judgement reads a record or a
page another one would refuse:

* **Active members only.** An event's mapped records include ones made
  ``Inactive`` (a rejected junk page, a record split out); they stay in the
  tables for audit but are not evidence. The fact recomputation already read
  only active members while the event-reality reader read all of them.
  :func:`member_condition` is the one SQL condition for it.
* **The page must belong to the record.** A stored page can be another
  article: the "Mackay Sugar ransomware attack" record held a breach-list page
  about the OpenAI agent's Medicare incident, and the AI question duly called
  Mackay Sugar's ransomware an AI attack. :func:`page_identity` checks the page
  names the record's organisation (or, without one, the distinctive words of
  its title); a page that does not is withheld from the evidence - title,
  snippet and research still count - and the withholding is stated. A page
  that cannot be checked (no name, no distinctive title words) is kept.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterable, Optional, Sequence, Tuple

#: Words that say what happened, not to whom - no help in telling articles apart.
_GENERIC = {
    "about", "access", "account", "accounts", "after", "against", "alert", "alleged", "allegedly", "amid",
    "attack", "attacked", "attackers", "attacks", "australia", "australian", "australians", "bank", "banks",
    "breach", "breached", "breaches", "business", "claims", "company", "confirms", "council", "customer",
    "customers", "cyber", "cyberattack", "cybersecurity", "data", "department", "details", "disclosed",
    "exposed", "files", "following", "from", "government", "group", "hack", "hacked", "hackers", "health",
    "impacted", "incident", "information", "investigating", "leak", "leaked", "limited", "listing", "major",
    "national", "network", "news", "over", "personal", "privacy", "ransomware", "records", "report",
    "reported", "reports", "says", "security", "service", "services", "stolen", "suffers", "system",
    "systems", "than", "that", "their", "this", "threat", "update", "users", "victim", "what", "with",
    "after", "into", "university", "school", "hospital", "state", "federal", "south", "north", "west",
    "east", "western", "eastern", "queensland", "victoria", "victorian", "wales", "tasmania", "exclusive",
}


def member_condition(conn: sqlite3.Connection, alias: str = "e") -> str:
    """SQL condition (``AND ...``) keeping active member records; empty on a schema without status."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(EnrichedEvents)")}
    return f"AND COALESCE({alias}.status, 'Active') = 'Active'" if "status" in columns else ""


def _tokens(text: Optional[str], min_len: int = 4) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= min_len and w not in _GENERIC}


_LEGAL = re.compile(r"\b(pty|ltd|limited|inc|incorporated|llc|plc|corp|corporation|co|the)\b\.?", re.I)


def _normal(text: Optional[str]) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _LEGAL.sub(" ", (text or "").lower())))


def _names_page(name: str, page_words: set, page_normal: str) -> bool:
    """The page names the organisation: its whole name, or its first distinctive word (the brand)."""
    whole = _normal(name)
    if whole and f" {whole} " in f" {page_normal} ":
        return True
    brand = next((w for w in re.findall(r"[a-z0-9]+", _normal(name)) if len(w) >= 3 and w not in _GENERIC), None)
    return bool(brand) and brand in page_words


def research_names(perplexity_json: Optional[str]) -> Sequence[str]:
    """The organisation Perplexity named for a record, if any."""
    try:
        data = json.loads(perplexity_json or "{}")
    except (TypeError, ValueError):
        return ()
    name = data.get("formal_entity_name") if isinstance(data, dict) else None
    return (name,) if isinstance(name, str) and name.strip() else ()


def page_identity(title: Optional[str], names: Iterable[Optional[str]],
                  page_text: Optional[str]) -> Tuple[Optional[bool], str]:
    """``(belongs, reason)``: True, False (another article) or None (cannot tell).

    The page belongs when it names one of ``names`` (the whole name without
    its legal suffix, or its first distinctive word - "NAB", "Uber"), or -
    with no usable name - two of the title's distinctive words (one, when the
    title has only one).
    """
    page = _tokens(page_text)
    if not page:
        return None, "no page text"
    page_words = set(re.findall(r"[a-z0-9]+", (page_text or "").lower()))
    page_normal = _normal(page_text)
    usable = [n for n in names if n and _normal(n)]
    if usable:
        if any(_names_page(n, page_words, page_normal) for n in usable):
            return True, "page names the record's organisation"
        return False, "page does not name the record's organisation"
    wanted = _tokens(title)
    if not wanted:
        return None, "nothing distinctive to check the page against"
    needed = min(2, len(wanted))
    if len(wanted & page) >= needed:
        return True, "page carries the title's distinctive words"
    return False, "page carries none of the title's distinctive words"


def shares_distinctive_words(*texts: Optional[str]) -> bool:
    """Do the first text and any other share a distinctive word (one that says whom or what, not that it was a breach)?"""
    first, *rest = texts
    wanted = _tokens(first)
    return any(wanted & _tokens(other) for other in rest)


def evidence_page(title: Optional[str], names: Iterable[Optional[str]], page_text: Optional[str]) -> Tuple[str, Optional[str]]:
    """The page text to show a judgement, and a note when it was withheld."""
    belongs, reason = page_identity(title, names, page_text)
    if belongs is False:
        return "", f"stored page withheld: it appears to be another article ({reason})"
    return page_text or "", None
