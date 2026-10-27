"""Google results become evidence only if the page itself reports an incident.

Google Custom Search supplies about two thirds of all raw records (4,504 of
6,674 on 2026-10-27), and its results are matched on the search snippet. A
snippet picks up incident words from anywhere on a page - a "Service NSW cyber
incident" link in a site menu, a "cyber-attack" item in the ACT Government's
navigation, a privacy policy's legal clause about data breaches - so help
pages, home pages and policies were accepted. Enrichment then asked Perplexity
about the *organisation*, found its real incident, and attached it to the page:
Service NSW's "Report identity theft" help page became a second Service NSW
"attack" weeks after its real March 2020 breach, and LUSH's privacy policy
became LUSH's breach.

This gate asks Jev whether the page itself - its own text, not its menus,
footers or boilerplate - reports one specific cyber incident. A record below
the **calibrated** cut-off is removed: set inactive before deduplication if it
is new; split out of its event and rejected if it is already in one (an event
left with no other record is rejected outright). Labelled records are ground
truth: ``reports_incident`` is never removed, ``not_incident`` always is.

Removing junk evidence must not lose real incidents. When a removed record's
Perplexity research names a specific incident at an organisation that has no
other active event near that date (the LUSH case), a targeted search looks for
proper reporting of it; if found, the article is queued as a new raw record
(``source_type='Perplexity'``) for the normal scraping and enrichment, so the
incident re-enters the database on real evidence. Every search is stored in
``SourceReplacements`` and never repeated.

The replacement is secured **before** the record is removed: the search runs
first, and if it fails (network, rate limit) a ``pending`` row is stored that
every later ``check_sources --apply`` retries - it does not depend on the
removed record still being active, which it no longer is. A missing date in
the research cannot establish that another event covers the incident, so it
never suppresses the search. With no way to search (no ``PERPLEXITY_API_KEY``
and no search function) ``--apply`` stops before removing anything, and a page
Jev could not judge stops the run instead of being silently kept.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from cyber_data_collector.dedup.event_reality import ROOT
from cyber_data_collector.utils.llm_models import perplexity_model, typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "page_reports_incident"
QUESTION_VERSION = "source-gate-v1"
SOURCE_TYPE = "GoogleSearch"
PAGE_CHARS = 3000
# A page with less stored text than this cannot be judged from its own text
# (the scrape failed, or caught only a headline or cookie wall). 113 of the 543
# records the gate would otherwise have removed on 2026-10-27 were like this;
# they are left alone and not used for calibration.
MIN_PAGE_CHARS = 300
REPLACEMENT_MIN_CONFIDENCE = 0.5
COVERAGE_WINDOW_DAYS = 180

LABELS_PATH = ROOT / "evaluation" / "source_gate_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "source_gate_calibration.json"
SAMPLE_PATH = ROOT / "evaluation" / "source_gate_sample.csv"

INSTRUCTIONS = (
    "This web page was returned by a web search for cyber incidents. Decide whether the page ITSELF "
    "reports one specific cyber incident - a breach, attack, ransomware or extortion claim, account "
    "compromise, data exposure or cyber-caused disruption at a named or clearly described "
    "organisation. Judge from `page_text`, the page's own scraped text, with `title` and `url`; "
    "`search_snippet` is what the search engine matched and often quotes menus or boilerplate. "
    "Incident words in navigation menus, related-link lists, footers, cookie or privacy-policy "
    "clauses, or general advice do NOT make the page an incident report."
)
CRITERIA = {
    "true": ("The page's main content reports a specific incident: a news article, a company, "
             "government or regulator notice or statement, a court or parliamentary record, or a "
             "breach-tracker or leak-site entry about that incident."),
    "false": ("The page is something else: a help, service, product or home page; a privacy policy "
              "or terms; guidance, advice or a scam warning; a list, index, roundup or statistics page "
              "covering many incidents; a profile; or a page that mentions incidents only in menus, "
              "links or boilerplate."),
}

REPLACEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS SourceReplacements (
    enriched_event_id TEXT PRIMARY KEY,
    query             TEXT NOT NULL,
    model             TEXT NOT NULL,
    answer            TEXT,
    citations_json    TEXT,
    raw_event_id      TEXT,          -- the replacement record queued, if any
    created_at        TEXT NOT NULL
);
"""
#: Added after the table was first created; NULL status on older rows = 'searched'.
REPLACEMENT_COLUMNS = (("status", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT"),
                       ("updated_at", "TEXT"))
STATUS_PENDING = "pending"
STATUS_SEARCHED = "searched"


class ReplacementSearchUnavailable(RuntimeError):
    """No way to search for replacement evidence; removal must not go ahead without it."""


def ensure_replacement_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(REPLACEMENT_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(SourceReplacements)")}
    for name, ddl in REPLACEMENT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE SourceReplacements ADD COLUMN {name} {ddl}")
    conn.commit()

REPLACEMENT_PROMPT = """Find credible public reporting - a news article, or a company, government or \
regulator notice - of this specific cyber incident:

Organisation: {org}
Approximate date: {date}
What earlier research said: {summary}

Answer in exactly this format:
FOUND: yes / no
INCIDENT: two or three sentences - who, what happened, when (to the day if reported).
SOURCES: the URLs of the reporting you found, best first, one per line."""


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def google_records(conn: sqlite3.Connection, only_unmapped: bool = False) -> List[Dict[str, Any]]:
    """Active enriched records sourced from Google search, with their event if mapped."""
    rows = conn.execute(
        f"""SELECT e.enriched_event_id, COALESCE(r.raw_title, e.title, ''), COALESCE(r.source_url, ''),
                   COALESCE(r.raw_description, ''),
                   SUBSTR(COALESCE(NULLIF(r.raw_content, ''), ''), 1, {PAGE_CHARS}),
                   (SELECT m.deduplicated_event_id FROM EventDeduplicationMap m
                     WHERE m.enriched_event_id = e.enriched_event_id LIMIT 1)
            FROM EnrichedEvents e JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE e.status = 'Active' AND r.source_type = ?
            ORDER BY e.enriched_event_id""", (SOURCE_TYPE,)).fetchall()
    out = [{"enriched_event_id": r[0], "title": r[1], "url": r[2], "search_snippet": r[3],
            "page_text": r[4] or "(no page text stored)", "deduplicated_event_id": r[5]} for r in rows]
    return [r for r in out if r["deduplicated_event_id"] is None] if only_unmapped else out


def judgeable(record: Dict[str, Any]) -> bool:
    """Is there enough of the page's own text to judge it?"""
    text = record.get("page_text") or ""
    return text != "(no page text stored)" and len(text.strip()) >= MIN_PAGE_CHARS


def page_state(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: record[k] for k in ("title", "url", "search_snippet", "page_text")}


def judge_records(conn: sqlite3.Connection, records: Sequence[Dict[str, Any]], judge=None) -> Dict[str, float]:
    """P(page reports an incident) per enriched record; stored answers are reused."""
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    failures: List[str] = []
    try:
        for i, rec in enumerate(records, 1):
            try:
                out[rec["enriched_event_id"]] = float(judge.ask(
                    "enriched_event", rec["enriched_event_id"], QUESTION_VERSION, page_state(rec), q)[QUESTION_ID].value)
            except Exception as exc:  # noqa: BLE001 - collected, then raised below
                logger.error("Source gate judgement failed for %s: %s", rec["enriched_event_id"], exc)
                failures.append(f"{rec['enriched_event_id']}: {exc}")
            if i % 200 == 0:
                logger.info("Judged %d/%d Google records", i, len(records))
    finally:
        if owned:
            judge.close()
    if failures:
        # Not a silent keep: a page that could not be judged would otherwise
        # stay as evidence unexamined. Answers already given are stored and
        # reused on the rerun.
        raise RuntimeError(f"Source gate: {len(failures)} of {len(records)} Google record(s) could not be judged "
                           f"(first: {failures[0]}); nothing was removed")
    return out


def stored_probabilities(conn: sqlite3.Connection) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sid, value in conn.execute(
            "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
            "AND model_requested = ? ORDER BY created_at, rowid", (QUESTION_ID, QUESTION_VERSION, typesafe_model())):
        out[sid] = float(value)
    return out


class LabelsUnavailable(RuntimeError):
    """The labels that protect records from removal could not be read."""


def labels(path: Path = LABELS_PATH, required: bool = False) -> Dict[str, str]:
    """Labelled records (ground truth: never acted on against their label).

    Args:
        required: Raise :class:`LabelsUnavailable` when the file is missing,
            unreadable or empty instead of returning ``{}``. Removal needs it:
            without the labels nothing protects a labelled real incident.
    """
    try:
        with Path(path).open(encoding="utf-8") as fh:
            out = {r["enriched_event_id"]: r["label"] for r in csv.DictReader(fh)}
    except (OSError, KeyError, csv.Error, UnicodeDecodeError) as exc:
        if required:
            raise LabelsUnavailable(f"Source gate labels {path} could not be read ({exc}): nothing removed") from exc
        logger.error("Source gate labels %s could not be read: %s", path, exc)
        return {}
    if not out and required:
        raise LabelsUnavailable(f"Source gate labels {path} are empty: nothing removed")
    return out


def calibration_identity() -> Dict[str, str]:
    return {"question_version": QUESTION_VERSION, "model": typesafe_model()}


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Source gate calibration %s could not be read (%s): REPORT-ONLY, nothing is removed", path, exc)
        return None
    if not isinstance(cal, dict):
        logger.error("Source gate calibration %s is not a JSON object: REPORT-ONLY, nothing is removed", path)
        return None
    if {k: cal.get(k) for k in ("question_version", "model")} != calibration_identity():
        logger.warning("Source gate calibration is for %s / %s, not %s: report only",
                       cal.get("model"), cal.get("question_version"), calibration_identity())
        return None
    if cal.get("cutoff") is None:
        logger.error("Source gate calibration %s has no cut-off: REPORT-ONLY, nothing is removed", path)
        return None
    return cal


# --- replacement of junk evidence --------------------------------------------

def _research(conn: sqlite3.Connection, enriched_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT perplexity_enrichment_data FROM EnrichedEvents WHERE enriched_event_id = ?",
                       (enriched_id,)).fetchone()
    try:
        data = json.loads((row[0] if row else None) or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _covered(conn: sqlite3.Connection, org: str, when: Optional[date],
             exclude_events: Iterable[str] = ()) -> bool:
    """Does another active event about this organisation lie near this date?

    Names are matched as organisations, not strings: research gives formal
    names ("Medibank Private Limited") where events carry trading names
    ("Medibank"). Exact matching re-searched incidents the database already
    held - 246 searches on the first run (2026-10-27) for 57 emptied events.

    Without a valid date there is no window: a different attack on the same
    organisation years apart would "cover" it. So a missing or malformed date
    is never covered (the search goes ahead) - it used to match any event.

    Args:
        exclude_events: Events that are about to be rejected (every record in
            them is being removed), which therefore cannot cover anything.
    """
    from cyber_data_collector.dedup.entity_resolution import EntityResolver
    from cyber_data_collector.dedup.victim_agreement import denotes

    if when is None:
        logger.warning("No valid incident date for %r: coverage cannot be established, treated as uncovered", org)
        return False
    excluded = set(exclude_events or ())
    resolver = EntityResolver(conn)
    rows = conn.execute(
        """SELECT d.deduplicated_event_id, d.event_date, d.victim_organization_name,
                  (SELECT GROUP_CONCAT(v.entity_name, '|') FROM DeduplicatedEventEntities dee
                     JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
                    WHERE dee.deduplicated_event_id = d.deduplicated_event_id
                      AND dee.relationship_type = 'victim')
           FROM DeduplicatedEvents d
           WHERE d.status = 'Active'""").fetchall()
    for event_id, event_date, scalar, role_names in rows:
        if event_id in excluded:
            continue
        try:
            other = datetime.strptime(str(event_date)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue  # an undated event cannot show the incident is covered
        if abs((other - when).days) > COVERAGE_WINDOW_DAYS:
            continue
        names = [n for n in [scalar, *(role_names or "").split("|")] if n]
        if any(denotes(resolver, org, n) for n in names):
            return True
    return False


def _parse_date(text: str) -> Optional[date]:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _store_replacement(conn: sqlite3.Connection, enriched_id: str, query: str, model: str, *, status: str,
                       answer: Optional[str] = None, citations: Sequence[str] = (), raw_id: Optional[str] = None,
                       error: Optional[str] = None) -> None:
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO SourceReplacements (enriched_event_id, query, model, answer, citations_json, raw_event_id,
                                           created_at, status, attempts, last_error, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT (enriched_event_id) DO UPDATE SET
               query = excluded.query, model = excluded.model, answer = excluded.answer,
               citations_json = excluded.citations_json, raw_event_id = excluded.raw_event_id,
               status = excluded.status, attempts = SourceReplacements.attempts + 1,
               last_error = excluded.last_error, updated_at = excluded.updated_at""",
        (enriched_id, query, model, answer, json.dumps(list(citations)), raw_id, now, status, error, now))
    conn.commit()


def replace_evidence(conn: sqlite3.Connection, enriched_id: str, event_id: Optional[str],
                     search: Optional[Callable[[str, str], Dict[str, Any]]] = None,
                     exclude_events: Iterable[str] = ()) -> Optional[str]:
    """Search for proper reporting of the incident a removed record pointed at; queue it if found.

    Call it *before* removing the record. A search that fails is stored as
    ``pending`` and retried by :func:`retry_pending_replacements` on every
    later run, whether or not the record is still active.

    Args:
        exclude_events: Events about to be rejected, which cannot count as
            covering the incident.

    Returns:
        The queued raw_event_id, or None (nothing to replace, already covered,
        already searched, nothing found, or the search failed and is pending).

    Raises:
        ReplacementSearchUnavailable: a replacement is needed and there is no
            search function and no ``PERPLEXITY_API_KEY``.
    """
    from cyber_data_collector.dedup.corroboration import _perplexity_search

    ensure_replacement_schema(conn)
    previous = conn.execute("SELECT COALESCE(status, ?) FROM SourceReplacements WHERE enriched_event_id = ?",
                            (STATUS_SEARCHED, enriched_id)).fetchone()
    if previous and previous[0] != STATUS_PENDING:
        return None
    research = _research(conn, enriched_id)
    org = str(research.get("formal_entity_name") or "").strip()
    try:
        confidence = float(research.get("overall_confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not org or confidence < REPLACEMENT_MIN_CONFIDENCE:
        return None
    when_text = str(research.get("earliest_event_date") or "")[:10]
    when = _parse_date(when_text)
    # The record's own event is NOT excluded: when a junk record is split out of
    # an event that keeps other evidence, that event still covers the incident
    # (excluding it queued 397 replacements on the first run, mostly redundant).
    # Events about to be rejected are excluded - they cannot cover anything.
    if _covered(conn, org, when, exclude_events):
        return None
    if search is None:
        if not os.getenv("PERPLEXITY_API_KEY"):
            raise ReplacementSearchUnavailable(
                f"Replacement evidence for {enriched_id} ({org}) cannot be searched: PERPLEXITY_API_KEY is not "
                "set. Nothing more is removed until it is.")
        search = _perplexity_search
    model = perplexity_model()
    query = REPLACEMENT_PROMPT.format(org=org, date=when_text or "unknown",
                                      summary=str(research.get("reasoning") or "")[:600])
    try:
        result = search(query, model)
    except Exception as exc:  # noqa: BLE001 - stored as pending, retried every run
        logger.warning("Replacement search failed for %s (queued for retry): %s", enriched_id, exc)
        _store_replacement(conn, enriched_id, query, model, status=STATUS_PENDING, error=str(exc)[:500])
        return None
    answer = result.get("answer") or ""
    citations = [c for c in (result.get("citations") or []) if c]
    raw_id = None
    if answer.strip().upper().startswith("FOUND: YES") and citations:
        raw_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO RawEvents (raw_event_id, source_type, source_event_id, raw_title, raw_description, "
            "raw_content, event_date, source_url, source_metadata, discovered_at, is_processed) "
            "VALUES (?, 'Perplexity', NULL, ?, ?, NULL, ?, ?, ?, ?, 0)",
            (raw_id, f"{org} cyber incident", answer, when.isoformat() if when else None, citations[0],
             json.dumps({"origin": "source-gate replacement search", "replaces": enriched_id,
                         "citations": citations[:8]}), datetime.now().isoformat()))
    _store_replacement(conn, enriched_id, query, model, status=STATUS_SEARCHED, answer=answer, citations=citations,
                       raw_id=raw_id)
    return raw_id


def retry_pending_replacements(conn: sqlite3.Connection, search=None) -> List[Dict[str, str]]:
    """Retry every replacement search that failed earlier; returns those that queued a record."""
    ensure_replacement_schema(conn)
    queued = []
    for (enriched_id,) in conn.execute("SELECT enriched_event_id FROM SourceReplacements WHERE status = ?",
                                       (STATUS_PENDING,)).fetchall():
        raw_id = replace_evidence(conn, enriched_id, None, search=search)
        if raw_id:
            queued.append({"replaces": enriched_id, "raw_event_id": raw_id})
    return queued


def prune_redundant_replacements(conn: sqlite3.Connection) -> List[str]:
    """Drop queued, still-unprocessed replacement records whose incident is already covered.

    Coverage can change after a replacement is queued (later merges, or a
    corrected coverage check), so this re-checks every queued replacement that
    has not been scraped yet and deletes those the data already covers. The
    search itself stays recorded in ``SourceReplacements``.
    """
    ensure_replacement_schema(conn)
    pruned = []
    for enriched_id, raw_id in conn.execute(
            """SELECT s.enriched_event_id, s.raw_event_id FROM SourceReplacements s
               JOIN RawEvents r ON r.raw_event_id = s.raw_event_id
               WHERE s.raw_event_id IS NOT NULL AND COALESCE(r.is_processed, 0) = 0""").fetchall():
        research = _research(conn, enriched_id)
        org = str(research.get("formal_entity_name") or "").strip()
        when = _parse_date(str(research.get("earliest_event_date") or "")[:10])
        if org and _covered(conn, org, when):
            conn.execute("DELETE FROM RawEvents WHERE raw_event_id = ?", (raw_id,))
            conn.execute("UPDATE SourceReplacements SET raw_event_id = NULL WHERE enriched_event_id = ?",
                         (enriched_id,))
            pruned.append(raw_id)
    conn.commit()
    logger.info("Pruned %d queued replacement(s) already covered by active events", len(pruned))
    return pruned


# --- the gate ----------------------------------------------------------------

def _remove(conn: sqlite3.Connection, record: Dict[str, Any], keep_events: frozenset = frozenset()) -> str:
    """Take one junk record out of the data. Returns what was done.

    ``keep_events`` are events labelled real incidents: when the junk page is
    such an event's only record, the record goes but the event stays, waiting
    for its replacement evidence (rejecting it contradicted the event label,
    and check-events then restored it with the junk page)."""
    from cyber_data_collector.dedup.event_reality import reject_events
    from cyber_data_collector.dedup.ledger import DedupLedger

    eid, did = record["enriched_event_id"], record["deduplicated_event_id"]
    now = datetime.now()
    if did is None:
        conn.execute("UPDATE EnrichedEvents SET status = 'Inactive', updated_at = ? WHERE enriched_event_id = ?",
                     (now, eid))
        conn.commit()
        return "deactivated before deduplication"
    members = conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
                           (did,)).fetchone()[0]
    if members <= 1 and did in keep_events:
        conn.execute("UPDATE EnrichedEvents SET status = 'Inactive', updated_at = ? WHERE enriched_event_id = ?",
                     (now, eid))
        conn.commit()
        return "deactivated; event kept (labelled a real incident) for its replacement evidence"
    if members <= 1:
        reject_events(conn, [{"deduplicated_event_id": did}])
        return "event rejected (its only record)"
    new_id = DedupLedger(conn).split_member(did, eid, reason=f"{QUESTION_VERSION}: page does not report an "
                                                             "incident", actor="rule")
    reject_events(conn, [{"deduplicated_event_id": new_id}])
    return "split out of its event and rejected"


def check_sources(conn: sqlite3.Connection, apply: bool = False, only_unmapped: bool = False, judge=None,
                  calibration_path: Path = CALIBRATION_PATH, labels_path: Path = LABELS_PATH,
                  search=None, event_labels_path: Optional[Path] = None) -> Dict[str, Any]:
    """Judge Google records; remove those below the calibrated cut-off and replace lost evidence."""
    # Removal without the labels would drop their protection silently.
    known = labels(labels_path, required=apply)
    records = google_records(conn, only_unmapped=only_unmapped)
    probabilities = judge_records(conn, records, judge=judge)
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    below = [] if cutoff is None else [
        {**r, "p_reports_incident": probabilities[r["enriched_event_id"]], "label": known.get(r["enriched_event_id"])}
        for r in records if r["enriched_event_id"] in probabilities
        and known.get(r["enriched_event_id"]) != "reports_incident"
        and (known.get(r["enriched_event_id"]) == "not_incident"
             or (judgeable(r) and probabilities[r["enriched_event_id"]] < cutoff))]
    removed, replacements, pruned = [], [], []
    if apply and cal:
        if below and search is None and not os.getenv("PERPLEXITY_API_KEY"):
            raise ReplacementSearchUnavailable(
                f"{len(below)} record(s) are below the cut-off but replacement evidence cannot be searched "
                "(PERPLEXITY_API_KEY not set): nothing removed")
        # Searches that failed on an earlier run, whose records are gone.
        replacements.extend(retry_pending_replacements(conn, search=search))
        # Events every one of whose records is being removed will be rejected,
        # so they cannot count as covering an incident.
        below_by_event: Dict[str, int] = {}
        for rec in below:
            if rec["deduplicated_event_id"]:
                below_by_event[rec["deduplicated_event_id"]] = below_by_event.get(rec["deduplicated_event_id"], 0) + 1
        doomed = {did for did, n in below_by_event.items()
                  if n >= conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
                                       (did,)).fetchone()[0]}
        # Secure the replacement (found, or a durable pending search) first,
        # then remove the record.
        from cyber_data_collector.dedup import event_reality
        labelled_real = frozenset(event_reality.protected_events(event_labels_path or event_reality.LABELS_PATH))
        for rec in below:
            raw_id = replace_evidence(conn, rec["enriched_event_id"], rec["deduplicated_event_id"], search=search,
                                      exclude_events=doomed)
            if raw_id:
                replacements.append({"replaces": rec["enriched_event_id"], "raw_event_id": raw_id})
            rec["action"] = _remove(conn, rec, labelled_real)
            removed.append(rec)
        pruned = prune_redundant_replacements(conn)
    return {"records": len(records), "judged": len(probabilities),
            "unjudgeable": sum(1 for r in records if not judgeable(r)), "cutoff": cutoff, "calibrated": cal is not None,
            "below_cutoff": below, "removed": removed, "replacements_queued": replacements,
            "replacements_pruned": pruned}
