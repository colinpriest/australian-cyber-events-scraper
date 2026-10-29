"""Strip tracking and identity parameters from URLs, in new records and in stored ones.

A newsletter link carries the subscriber with it: ``utm_*`` campaign tags, a
``token`` that is a signed JWT naming the subscriber's email, an ``eid`` mail
id. 24 stored ``RawEvents.source_url`` values (22 Webber, 2 Google) held such a
token. None of it identifies the article, and the token is personal data, so a
record's URL is the article's address without it. Where a URL reaches the
database - ``CyberEventDataV2.add_raw_event``, the Webber list - it is cleaned
by :func:`clean_url`; :func:`scrub_stored_urls` cleans what is already stored.

Nothing here logs or returns a removed value: counts only.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

#: Query parameters that never identify an article: campaign tags, click ids, mail and session ids.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset({
    "token", "mkt_tok", "gclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi",
    "hsctatracking", "oly_enc_id", "oly_anon_id", "vero_id", "sessionid", "session_id", "phpsessid", "jsessionid",
    "email", "emailid", "email_id", "subscriber", "subscriberid", "_ga", "ocid", "campaign",
})
#: A newsletter's mail id and send date (itnews: ``?eid=3&edate=20200227``); an ``eid`` alone is left as it may be an article id.
NEWSLETTER_PAIR = ("eid", "edate")

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)\]\\]+")


def _is_tracking(name: str, names: frozenset) -> bool:
    lowered = name.lower()
    return lowered.startswith(TRACKING_PREFIXES) or lowered in TRACKING_PARAMS or lowered.endswith("token") or (
        lowered in NEWSLETTER_PAIR and all(p in names for p in NEWSLETTER_PAIR))


def clean_url(url: Optional[str]) -> Optional[str]:
    """``url`` without tracking and identity query parameters; unchanged (same string) when it has none."""
    if not url or "?" not in url:
        return url
    parts = urlsplit(url.strip())
    if not parts.query:
        return url
    segments = parts.query.split("&")
    names = frozenset(seg.split("=", 1)[0].lower() for seg in segments if seg)
    kept = [seg for seg in segments if seg and not _is_tracking(seg.split("=", 1)[0], names)]
    # A parameter that carries a link ("redirect=/page?token=...") is cleaned too.
    nested = [(seg.split("=", 1)[0] + "=" + clean_url(seg.split("=", 1)[1])) if "=" in seg and "?" in seg else seg
              for seg in kept]
    if nested == [s for s in segments if s]:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(nested), parts.fragment))


def clean_text(text: Optional[str]) -> Optional[str]:
    """``text`` with every URL in it cleaned (a URL quoted in a page, a description, stored research)."""
    if not text or "http" not in text:
        return text
    return _URL_IN_TEXT.sub(lambda match: clean_url(match.group(0)) or match.group(0), text)


# ---------------------------------------------------------------------------
# What is already stored
# ---------------------------------------------------------------------------

#: ``(table, column, is_text)``: URL columns, and free-text columns that quote URLs. The key is the row id.
STORED_URL_COLUMNS: Tuple[Tuple[str, str, bool], ...] = (
    ("RawEvents", "source_url", False),
    ("RawEvents", "source_metadata", True),
    ("RawEvents", "raw_description", True),
    ("RawEvents", "raw_content", True),
    ("EnrichedEvents", "perplexity_enrichment_data", True),
    ("DeduplicatedEventSources", "source_url", False),
    ("PageRescrapes", "url", False),
    ("CyberDailyRefetches", "url", False),
    ("EnrichmentFactSupport", "source_url", False),
    ("CitedDocuments", "url", False),
    ("CitedDocuments", "final_url", False),
)
_DIRTY = ("%utm\\_%", "%token=%", "%eid=%", "%mc\\_%", "%clid=%", "%mkt\\_tok=%", "%email%", "%session%", "%\\_hs%")


@dataclass
class ScrubReport:
    changed: Dict[str, int] = field(default_factory=dict)    # "Table.column" -> values cleaned
    removed_duplicates: int = 0                              # rows dropped because the cleaned key already existed

    @property
    def total(self) -> int:
        return sum(self.changed.values())

    def summary(self) -> str:
        parts = ", ".join(f"{n} {k}" for k, n in sorted(self.changed.items())) or "none"
        return (f"{self.total} stored value(s) carried tracking or identity parameters ({parts}); "
                f"{self.removed_duplicates} duplicate row(s) dropped")


def scrub_stored_urls(conn: sqlite3.Connection, dry_run: bool = False) -> ScrubReport:
    """Clean every stored URL (and URL quoted in stored text). Idempotent. Commits unless ``dry_run``.

    A row whose cleaned key collides with an existing one (a primary-key
    column) is dropped rather than duplicated. Nothing removed is printed or
    logged; the report holds counts only.
    """
    report = ScrubReport()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, column, is_text in STORED_URL_COLUMNS:
        if table not in tables or column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        where = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in _DIRTY)
        rows = conn.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE '%?%' AND ({where})",
                            _DIRTY).fetchall()
        for rowid, value in rows:
            cleaned = clean_text(value) if is_text else clean_url(value)
            if cleaned == value:
                continue
            report.changed[f"{table}.{column}"] = report.changed.get(f"{table}.{column}", 0) + 1
            if dry_run:
                continue
            updated = conn.execute(f"UPDATE OR IGNORE {table} SET {column} = ? WHERE rowid = ?",
                                   (cleaned, rowid)).rowcount
            if not updated:           # the cleaned key is already a row of its own
                conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                report.removed_duplicates += 1
    if not dry_run:
        conn.commit()
    return report
