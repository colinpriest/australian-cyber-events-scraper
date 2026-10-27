"""An organisation's researched cyber-incident history, for judging its events.

Judging events one at a time, or a pair at a time, cannot answer the questions
that decide them: how many incidents did this organisation actually have, and
when? The labellers who calibrated the reality and duplicate filters answered
exactly that by searching - Western Sydney University turned out to have four
separate incidents (2023, August 2024, January 2025, June 2025) - and that is
what the stored evidence lacked.

One Perplexity search per organisation with several events asks for its full
incident history: dated, with what happened and a source per incident,
including supplier breaches that exposed its data. The answer is stored in
``OrganisationTimelines`` and reused - a live search cannot be replayed - and
given to Jev (and to the escalation model) as ``organisation_timeline`` for
reality, duplicate and victim judgements.

**The question (``timeline-v2``).** Version 1 asked one global question with
the two commonest Perplexity names of the organisation's records as aliases,
and failed three ways: an alias was whatever name the records carried, so a
misfiled record put a different organisation into the question ("Australian
Federal Police (also recorded as: Aussie Fluid Power)", "University of Western
Australia (also recorded as: UN Women Australia)") and the timeline then
vouched for the misattribution; multinationals were refused ("too many,
worldwide" for Microsoft); and supplier breaches were missed (UWA's 2020
ProctorU breach). Version 2 accepts an alias only when it is a spelling of the
same name (:func:`is_same_name`); asks about Australia only when stored entity
data says the organisation is not Australian, or when the global question was
refused; gives supplier breaches their own instruction, naming the suppliers
the organisation's events record; and asks for incidents at systems the
organisation operates (the Australian Digital Health Agency runs My Health
Record).

**Keeping timelines current.** A timeline is re-researched whenever a record
about the organisation's events arrives after it was researched, and whenever
the question it was asked differs from the current one (a new prompt version,
a corrected alias, a newly recorded supplier) - :func:`is_stale`. Each
research is a new row, so every version is kept and the latest is used; a new
version changes the input of every judgement that shows it, so the
organisation's events and pairs are re-judged on the next run without any
other bookkeeping. Organisations with no new evidence cost nothing.

**No silent fallback.** When a timeline needs researching and cannot be (no
``PERPLEXITY_API_KEY``, or the search fails), :func:`research` raises: an
out-of-date history standing in for a fresh one would mislead every judgement
that reads it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from cyber_data_collector.utils.llm_models import perplexity_model

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS OrganisationTimelines (
    entity_id      INTEGER NOT NULL,
    entity_name    TEXT NOT NULL,
    query_hash     TEXT NOT NULL,
    query          TEXT NOT NULL,
    model          TEXT NOT NULL,
    answer         TEXT,
    citations_json TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (entity_id, query_hash)
);
CREATE TABLE IF NOT EXISTS OrganisationTimelineFailures (
    entity_id   INTEGER NOT NULL,
    entity_name TEXT NOT NULL,
    query       TEXT NOT NULL,
    model       TEXT NOT NULL,
    error       TEXT NOT NULL,
    failed_at   TEXT NOT NULL
);
"""
TIMELINE_CHARS = 3000
MAX_CITATIONS = 10
MAX_ALIASES = 2
MAX_SUPPLIER_HINTS = 3

PROMPT_VERSION = "timeline-v2"

PROMPT = """List the publicly reported cyber incidents affecting {name}{aliases}{scope}: data breaches, \
ransomware or extortion (including leak-site listings), account or email compromise, DDoS, \
accidental exposure of data, and incidents at systems, services or brands it operates. Use news, \
regulator and company statements, court or parliamentary records and breach trackers. Treat \
incidents as separate only when they are separate attacks or exposures; list follow-up coverage \
of one incident under that incident.

Include supplier and third-party breaches: every breach at a supplier, software vendor, contractor \
or service provider (for example an exam-proctoring, student-records, payroll, mailing, \
file-transfer, cloud or IT provider) that exposed {name}'s data or the data of its customers, \
students or staff, each as its own incident naming the supplier.{suppliers}

Answer as a numbered list, oldest first, one incident per line:
DATE (to the day if reported, else month or year) | WHAT HAPPENED (attacker or method, systems, \
data and people affected) | WHEN AND HOW IT BECAME PUBLIC | SOURCE URL

List every incident your sources support, even if the list may be incomplete. Answer NONE FOUND \
only if you find no incident at all."""

AUSTRALIAN_SCOPE = (" in Australia - only incidents that affected its Australian customers, staff, data or "
                    "operations, including global incidents that exposed Australians' data")
SUPPLIER_HINT = (" Our records link it to incidents at these suppliers; include each only if reporting "
                 "confirms the organisation was affected: {names}.")

# Words that do not tell two organisations apart when comparing spellings of one name.
_NAME_FILLER = {"the", "of", "and", "pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "co",
                "company", "corporation", "corp", "group", "holdings", "australia", "australian", "bv",
                "b", "v", "l", "c"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def organisations_with_several_events(conn: sqlite3.Connection, minimum: int = 2) -> List[Dict[str, Any]]:
    """Organisations that are the victim of at least ``minimum`` active events."""
    rows = conn.execute(
        """SELECT v.entity_id, v.entity_name, COUNT(DISTINCT d.deduplicated_event_id) AS n
           FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.relationship_type = 'victim' AND d.status = 'Active'
           GROUP BY v.entity_id, v.entity_name HAVING n >= ?
           ORDER BY v.entity_name""", (minimum,)).fetchall()
    return [{"entity_id": r[0], "entity_name": r[1], "events": r[2]} for r in rows]


def _name_tokens(name: str) -> frozenset:
    from cyber_data_collector.dedup.entity_merge import _tokens
    from cyber_data_collector.dedup.victim_agreement import strip_legal_suffix
    return frozenset(t for t in _tokens(strip_legal_suffix(name)) if t not in _NAME_FILLER)


def is_same_name(name: Optional[str], other: Optional[str]) -> bool:
    """True when ``other`` is a spelling of ``name`` - not merely a similar organisation.

    Stricter than :func:`victim_agreement.denotes` on purpose: that test
    matches "Aussie Fluid Power" to "Australian Federal Police" (an acronym)
    and "UN Women Australia" to "University of Western Australia", which are
    exactly the misattributions an alias must never carry into a question.
    One name's words must contain the other's (legal forms and "Australia"
    aside), and the shorter must still identify something on its own
    ("Department of Education" does not).
    """
    from cyber_data_collector.dedup.entity_merge import distinctive_core, lines_of_business_conflict

    if not name or not other:
        return False
    a, b = _name_tokens(name), _name_tokens(other)
    if not a or not b or lines_of_business_conflict(name, other):
        return False
    if not (a <= b or b <= a):
        return False
    shorter = name if len(a) <= len(b) else other
    return bool(distinctive_core(shorter))


def _aliases(conn: sqlite3.Connection, entity_id: int, name: str) -> List[str]:
    """Other spellings of the organisation's name in its records' Perplexity research, commonest first."""
    names: Counter = Counter()
    for (pe,) in conn.execute(
            """SELECT e.perplexity_enrichment_data
               FROM DeduplicatedEventEntities dee
               JOIN EventDeduplicationMap m ON m.deduplicated_event_id = dee.deduplicated_event_id
               JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
               WHERE dee.entity_id = ? AND dee.relationship_type = 'victim'""", (entity_id,)):
        try:
            formal = json.loads(pe or "{}").get("formal_entity_name")
        except (TypeError, ValueError, AttributeError):
            formal = None
        formal = str(formal).strip() if formal else ""
        if formal and formal.lower() != name.strip().lower() and is_same_name(name, formal):
            names[formal] += 1
    # Count, then name: a tie must not depend on row order, or the question would flap.
    return [n for n, _ in sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ALIASES]]


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _supplier_hints(conn: sqlite3.Connection, entity_id: int, name: str) -> List[str]:
    """Suppliers recorded as the vendor on the organisation's events (the role pass's vendor)."""
    if "vendor_organization_name" not in _columns(conn, "DeduplicatedEvents"):
        return []
    rows = conn.execute(
        """SELECT DISTINCT d.vendor_organization_name
           FROM DeduplicatedEventEntities dee
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.entity_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'
             AND d.vendor_organization_name IS NOT NULL AND TRIM(d.vendor_organization_name) != ''""",
        (entity_id,)).fetchall()
    return sorted({r[0].strip() for r in rows if not is_same_name(name, r[0])})[:MAX_SUPPLIER_HINTS]


def is_multinational(conn: sqlite3.Connection, entity_id: int) -> bool:
    """Stored entity data says the organisation is not Australian (``EntitiesV2.is_australian = 0``).

    Such an organisation's global history is either refused (Microsoft) or
    dominated by incidents irrelevant to an Australian event, so it is asked
    about Australia only. Unknown (NULL) is not treated as foreign.
    """
    if "is_australian" not in _columns(conn, "EntitiesV2"):
        return False
    row = conn.execute("SELECT is_australian FROM EntitiesV2 WHERE entity_id = ?", (entity_id,)).fetchone()
    return bool(row) and row[0] is not None and int(row[0]) == 0


_DECLINED = re.compile(r"\b(can['’]?t|cannot|unable to|not able to)\b[^.]{0,80}\b(reliably|produce|provide|list)",
                       re.I)


def is_refusal(answer: Optional[str]) -> bool:
    """An answer that lists no incidents because the search declined, not because it found none.

    "NONE FOUND" alone is an answer; "I can't reliably produce an exhaustive
    list ... NONE FOUND" (Westpac, 2026-10-27) is a refusal dressed as one.
    """
    text = (answer or "").strip()
    if parse_timeline(text):
        return False
    return "NONE FOUND" not in text.upper() or bool(_DECLINED.search(text))


def build_query(conn: sqlite3.Connection, entity_id: int, name: str, scoped: Optional[bool] = None) -> str:
    """The current question for an organisation.

    ``scoped`` (Australia only) defaults to :func:`is_multinational`, or to a
    stored refusal of the global question - which keeps the question scoped
    afterwards, so a refused organisation is not re-asked globally every run.
    """
    aliases = _aliases(conn, entity_id, name)
    suppliers = _supplier_hints(conn, entity_id, name)

    def render(is_scoped: bool) -> str:
        return PROMPT.format(
            name=name,
            aliases=f" (also recorded as: {'; '.join(aliases)})" if aliases else "",
            scope=AUSTRALIAN_SCOPE if is_scoped else "",
            suppliers=SUPPLIER_HINT.format(names="; ".join(suppliers)) if suppliers else "")

    if scoped is None:
        ensure_schema(conn)
        unscoped = render(False)
        scoped = is_multinational(conn, entity_id) or any(
            is_refusal(a) for (a,) in conn.execute(
                "SELECT answer FROM OrganisationTimelines WHERE entity_id = ? AND query = ?", (entity_id, unscoped)))
    return render(scoped)


def stored(conn: sqlite3.Connection, entity_id: int) -> Optional[Dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT entity_name, answer, citations_json, model, created_at, query FROM OrganisationTimelines "
        "WHERE entity_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (entity_id,)).fetchone()
    if row is None:
        return None
    return {"organisation": row[0], "timeline": (row[1] or "")[:TIMELINE_CHARS],
            "citations": json.loads(row[2] or "[]")[:MAX_CITATIONS], "model": row[3], "researched_at": row[4],
            "query": row[5], "australia_only": AUSTRALIAN_SCOPE in (row[5] or "")}


def latest_evidence(conn: sqlite3.Connection, entity_id: int) -> Optional[str]:
    """When the newest record about this organisation's events was found."""
    row = conn.execute(
        """SELECT MAX(r.discovered_at)
           FROM DeduplicatedEventEntities dee
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE dee.entity_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'""",
        (entity_id,)).fetchone()
    return row[0] if row and row[0] else None


def is_stale(conn: sqlite3.Connection, entity_id: int, name: Optional[str] = None) -> bool:
    """A timeline is stale when it was asked a different question, or new evidence arrived since.

    A timeline that predates an incident does not list it, and the judgements
    read "matches nothing in the researched history" as evidence against an
    event - so a new real incident at an organisation with an old timeline
    would look unsupported. Refreshing whenever new evidence arrives keeps the
    timeline at least as recent as anything it is used to judge. A changed
    question (a new :data:`PROMPT_VERSION`, a corrected alias, a newly
    recorded supplier) re-researches it too.
    """
    current = stored(conn, entity_id)
    if current is None:
        return True
    if current["query"] != build_query(conn, entity_id, name or current["organisation"]):
        return True
    latest = latest_evidence(conn, entity_id)
    return bool(latest and str(latest) > str(current["researched_at"]))


def limitations(conn: sqlite3.Connection, entity_id: int, timeline: Optional[Dict[str, Any]] = None) -> List[str]:
    """Why a stored timeline may be incomplete; empty when it is current and lists incidents."""
    timeline = timeline or stored(conn, entity_id)
    if timeline is None:
        return []
    reasons = []
    failed = conn.execute(
        # >=: on Windows two timestamps taken milliseconds apart can be equal.
        "SELECT MAX(failed_at) FROM OrganisationTimelineFailures WHERE entity_id = ? AND failed_at >= ?",
        (entity_id, timeline["researched_at"])).fetchone()[0]
    if failed:
        reasons.append(f"refreshing it failed on {failed[:10]}")
    latest = latest_evidence(conn, entity_id)
    if latest and str(latest) > str(timeline["researched_at"]):
        reasons.append(f"records about the organisation have arrived since (latest {str(latest)[:10]})")
    elif timeline["query"] != build_query(conn, entity_id, timeline["organisation"]):
        reasons.append("it was researched with an earlier version of the question")
    if is_refusal(timeline["timeline"]):
        reasons.append("the search declined to list incidents")
    return reasons


def for_judgement(conn: sqlite3.Connection, entity_id: int) -> Optional[Dict[str, Any]]:
    """The timeline as judgements see it: incidents and citations, plus a warning when it may be incomplete.

    A stale, failed or declined history must not read as a complete one: an
    event matching nothing in it is then no evidence against the event. The
    warning is added only when there is one, so a current timeline's input
    (and every stored answer keyed on it) is unchanged.
    """
    timeline = stored(conn, entity_id)
    if timeline is None:
        return None
    out = {"organisation": timeline["organisation"], "incidents": timeline["timeline"],
           "citations": timeline["citations"]}
    reasons = limitations(conn, entity_id, timeline)
    if reasons:
        out["warning"] = (f"This history may be incomplete: {'; '.join(reasons)}. Last successful research "
                          f"{str(timeline['researched_at'])[:10]}. An event that matches nothing in it is NOT "
                          f"evidence against the event.")
    return out


def _search_once(conn: sqlite3.Connection, entity_id: int, name: str, query: str,
                 search: Callable[[str, str], Dict[str, Any]]) -> Optional[str]:
    """Ask one question unless this exact question, model and evidence date is stored; return the answer."""
    model = perplexity_model()
    as_of = latest_evidence(conn, entity_id) or ""
    digest = hashlib.sha256(f"{model}\n{query}\n{as_of}".encode("utf-8")).hexdigest()
    row = conn.execute("SELECT answer FROM OrganisationTimelines WHERE entity_id = ? AND query_hash = ?",
                       (entity_id, digest)).fetchone()
    if row is not None:
        return row[0]
    try:
        result = search(query, model)
    except Exception as exc:
        # Recorded so that every later judgement reading the older timeline is
        # told the history may be incomplete (see for_judgement).
        conn.execute("INSERT INTO OrganisationTimelineFailures VALUES (?, ?, ?, ?, ?, ?)",
                     (entity_id, name, query, model, f"{type(exc).__name__}: {exc}"[:500],
                      datetime.now().isoformat()))
        conn.commit()
        raise RuntimeError(f"Timeline research for {name!r} failed (Perplexity {model}): {exc}") from exc
    conn.execute("INSERT INTO OrganisationTimelines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (entity_id, name, digest, query, model, result.get("answer"),
                  json.dumps(result.get("citations") or []), datetime.now().isoformat()))
    conn.commit()
    return result.get("answer")


def research(conn: sqlite3.Connection, entity_id: int, name: str,
             search: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Research an organisation's timeline when it has none or it is stale; else reuse it.

    Every version is kept (one row per research); the latest is used, and
    because the timeline text is part of every judgement's input, a refreshed
    timeline re-judges the organisation's events and pairs automatically. When
    the global question is declined, it is asked again about Australia only in
    the same call, and both answers are stored.

    Raises ``RuntimeError`` when research is needed and cannot be done (no
    ``PERPLEXITY_API_KEY``, or the search fails).
    """
    from cyber_data_collector.dedup.corroboration import _perplexity_search

    ensure_schema(conn)
    if not is_stale(conn, entity_id, name):
        return stored(conn, entity_id)
    if search is None:
        if not os.getenv("PERPLEXITY_API_KEY"):
            raise RuntimeError(
                f"PERPLEXITY_API_KEY is not set: cannot research the incident timeline of {name!r}")
        search = _perplexity_search
    query = build_query(conn, entity_id, name)
    answer = _search_once(conn, entity_id, name, query, search)
    if is_refusal(answer) and AUSTRALIAN_SCOPE not in query:
        logger.info("Timeline research for %s was declined; asking about Australia only", name)
        _search_once(conn, entity_id, name, build_query(conn, entity_id, name, scoped=True), search)
    return stored(conn, entity_id)


def organisations_to_research(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Organisations with several events, plus any that already have a timeline and are still a victim.

    An organisation can drop to one event (a merge, a rejection) and keep its
    timeline, which the victim and reality judgements still read - so it must
    stay current too.
    """
    ensure_schema(conn)
    orgs = {o["entity_id"]: o for o in organisations_with_several_events(conn)}
    for entity_id, name in conn.execute(
            """SELECT DISTINCT v.entity_id, v.entity_name FROM OrganisationTimelines t
               JOIN EntitiesV2 v ON v.entity_id = t.entity_id
               JOIN DeduplicatedEventEntities dee ON dee.entity_id = v.entity_id AND dee.relationship_type = 'victim'
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE d.status = 'Active'"""):
        orgs.setdefault(entity_id, {"entity_id": entity_id, "entity_name": name, "events": 1})
    return sorted(orgs.values(), key=lambda o: o["entity_name"])


def research_all(conn: sqlite3.Connection, search=None) -> int:
    """Research (or reuse) a timeline for every organisation in :func:`organisations_to_research`."""
    orgs = organisations_to_research(conn)
    done = sum(1 for o in orgs if research(conn, o["entity_id"], o["entity_name"], search=search))
    logger.info("%d of %d organisations have a current timeline", done, len(orgs))
    return done


_ITEM = re.compile(r"^\s*(\d+)[.)]\s*(.+)$")
# An unnumbered line in the requested shape: a dated first field, then at least two more.
_BARE_ITEM = re.compile(r"^\s*(?:[-*•]\s*)?((?:\*\*)?[^|]*\b(?:19|20)\d{2}\b[^|]*\|[^|]+\|.+)$")
_URL = re.compile(r"https?://\S+")


def parse_timeline(text: str) -> List[Dict[str, str]]:
    """Split a researched answer into incidents: date | what | how it became public | source.

    The answer is free text in a requested format, so a continuation line is
    appended to the incident above it rather than dropped, an unnumbered line
    in the requested ``DATE | WHAT | ...`` shape is numbered in order (the
    Commonwealth Bank's answer of 2026-10-27 came without numbers), and an
    answer with no incidents ("NONE FOUND", a refusal) parses to none.
    """
    items: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        m = _ITEM.match(line)
        if m:
            number, body = m.group(1), m.group(2)
        else:
            bare = _BARE_ITEM.match(line)
            if not bare:
                if items and line.strip():
                    items[-1]["what"] += " " + line.strip()
                continue
            number, body = str(len(items) + 1), bare.group(1)
        body = body.replace("**", "").strip()
        parts = [p.strip() for p in body.split("|")]
        urls = _URL.findall(body)
        items.append({
            "number": number,
            "date": parts[0] if len(parts) > 1 else "",
            "what": parts[1] if len(parts) > 1 else body,
            "public": parts[2] if len(parts) > 3 else "",
            "source": urls[-1].rstrip(").,") if urls else "",
        })
    return items


def timeline_for_event(conn: sqlite3.Connection, dedup_id: str) -> Optional[Dict[str, Any]]:
    """The timeline of the event's victim as judgements see it (:func:`for_judgement`), when it has one."""
    ensure_schema(conn)
    row = conn.execute(
        """SELECT t.entity_id FROM DeduplicatedEventEntities dee
           JOIN OrganisationTimelines t ON t.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'
           ORDER BY t.entity_id LIMIT 1""", (dedup_id,)).fetchone()
    return for_judgement(conn, row[0]) if row else None
