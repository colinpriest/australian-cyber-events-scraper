"""Data-quality measurements over the database. Every function here only reads.

Each ``measure_*`` returns ``{metric_name: Measurement}``. A column or table a
measurement needs but the database lacks yields ``Measurement.unavailable``
with the reason - never a zero, which would read as "nothing wrong".
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date
from typing import Dict, Iterator, List, Optional, Tuple

from cyber_data_collector.quality.model import Measurement, Thresholds
from cyber_data_collector.reporting.population import incident_filter

Measurements = Dict[str, Measurement]

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
EXAMPLES = 10


def columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone() is not None


@contextmanager
def row_factory(conn: sqlite3.Connection, factory) -> Iterator[sqlite3.Connection]:
    previous = conn.row_factory
    conn.row_factory = factory
    try:
        yield conn
    finally:
        conn.row_factory = previous


def _active_count(conn: sqlite3.Connection) -> int:
    where, params = incident_filter(alias="d")
    return int(conn.execute(f"SELECT COUNT(*) FROM DeduplicatedEvents d WHERE {where}", params).fetchone()[0])


def _share(n: int, total: int) -> Optional[float]:
    return round(n / total, 4) if total else None


# ---------------------------------------------------------------------------
# Source coverage
# ---------------------------------------------------------------------------

def _month_add(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _month_keys(today: date, count: int, extra_before: int) -> List[str]:
    return [f"{y:04d}-{m:02d}" for y, m in
            (_month_add(today.year, today.month, -i) for i in range(count + extra_before - 1, -1, -1))]


def flag_months(series: Dict[str, int], months: List[str], judged: List[str], trailing: int,
                drop_ratio: float, min_median: float, since: Optional[str] = None) -> List[str]:
    """Months in ``judged`` with zero volume, or below ``drop_ratio`` x the trailing median.

    The trailing window is the ``trailing`` months immediately before, zeros
    included; the ratio test only applies when that median is at least
    ``min_median`` (a fall from 2 to 0.5 is noise, not lost coverage).
    Months before ``since`` (``YYYY-MM``) are neither judged nor in a window:
    the source was not collecting monthly then.
    """
    flagged = []
    for month in judged:
        if since and month < since:
            continue
        i = months.index(month)
        window = [series.get(m, 0) for m in months[max(0, i - trailing):i] if not since or m >= since]
        value = series.get(month, 0)
        median = statistics.median(window) if window else 0
        if value == 0:
            flagged.append(f"{month}: 0 (trailing median {median:g})")
        elif median >= min_median and value < drop_ratio * median:
            flagged.append(f"{month}: {value} vs trailing median {median:g}")
    return flagged


def measure_coverage(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """New records and active events per source per month, with sharp falls flagged."""
    n_months = int(thresholds.setting("coverage", "months"))
    trailing = int(thresholds.setting("coverage", "trailing_months"))
    drop_ratio = float(thresholds.setting("coverage", "drop_ratio"))
    min_median = float(thresholds.setting("coverage", "min_trailing_median"))
    monthly_sources = list(thresholds.setting("coverage", "monthly_sources"))
    judge_from: Dict[str, str] = dict(thresholds.setting("coverage", "source_judge_from"))

    months = _month_keys(today, n_months, trailing)
    reported = months[-n_months:]
    current = reported[-1]
    judged = [m for m in reported if m != current]   # the current month is incomplete
    out: Measurements = {}

    raw_cols = columns(conn, "RawEvents")
    if not {"source_type", "discovered_at"} <= raw_cols:
        reason = "RawEvents.source_type / discovered_at missing"
        out["coverage.records_flagged_months"] = Measurement.unavailable(reason)
        out["coverage.events_flagged_months"] = Measurement.unavailable(reason)
        return out

    records: Dict[str, Dict[str, int]] = defaultdict(dict)
    undated = 0
    for source, month, n in conn.execute(
            "SELECT source_type, substr(CAST(discovered_at AS TEXT), 1, 7), COUNT(*) FROM RawEvents "
            "WHERE discovered_at IS NOT NULL GROUP BY 1, 2"):
        if month and re.match(r"^\d{4}-\d{2}$", month):
            records[source or "(none)"][month] = n
        else:
            undated += n
    undated += conn.execute("SELECT COUNT(*) FROM RawEvents WHERE discovered_at IS NULL").fetchone()[0]

    where, params = incident_filter(alias="d")
    events: Dict[str, Dict[str, int]] = defaultdict(dict)
    for source, month, n in conn.execute(
            f"""SELECT r.source_type, substr(d.event_date, 1, 7), COUNT(DISTINCT d.deduplicated_event_id)
                FROM DeduplicatedEvents d
                JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
                WHERE {where} AND d.event_date IS NOT NULL
                GROUP BY 1, 2""", params):
        events[source or "(none)"][month] = n
    totals: Dict[str, int] = dict(conn.execute(
        f"SELECT substr(d.event_date, 1, 7), COUNT(*) FROM DeduplicatedEvents d "
        f"WHERE {where} AND d.event_date IS NOT NULL GROUP BY 1", params).fetchall())

    sources = sorted(set(records) | set(events) | set(monthly_sources))
    for source in sources:
        for month in reported:
            out[f"coverage.records.{source}.{month}"] = Measurement(value=records[source].get(month, 0))
            out[f"coverage.events.{source}.{month}"] = Measurement(value=events[source].get(month, 0))
    for month in reported:
        out[f"coverage.events.all.{month}"] = Measurement(value=totals.get(month, 0))

    for kind, table in (("records", records), ("events", events)):
        flags = []
        for source in monthly_sources:
            flags += [f"{source} {f}" for f in
                      flag_months(table[source], months, judged, trailing, drop_ratio, min_median,
                                  since=judge_from.get(source))]
        what = ("raw records discovered" if kind == "records"
                else "active events with a record from the source, by event month")
        out[f"coverage.{kind}_flagged_months"] = Measurement(
            value=len(flags), examples=flags,
            detail=(f"{what}; {len(monthly_sources)} monthly sources x {len(judged)} complete months "
                    f"({judged[0]}..{judged[-1]}); {current} is incomplete and not judged"
                    + (f"; {undated} raw records have no usable discovered_at" if kind == "records" and undated
                       else "")))
    return out


# ---------------------------------------------------------------------------
# Missing evidence
# ---------------------------------------------------------------------------

def measure_evidence(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """Active events none of whose records has usable page text or Perplexity research."""
    min_chars = int(thresholds.setting("evidence", "min_page_chars"))
    enr = columns(conn, "EnrichedEvents")
    needed = {"perplexity_enrichment_data", "perplexity_validated"}
    if not needed <= enr or "raw_content" not in columns(conn, "RawEvents"):
        reason = f"EnrichedEvents.{'/'.join(sorted(needed))} or RawEvents.raw_content missing"
        return {"evidence.events_without_evidence": Measurement.unavailable(reason),
                "evidence.events_without_evidence_share": Measurement.unavailable(reason)}
    where, params = incident_filter(alias="d")
    rows = conn.execute(f"""
        SELECT d.deduplicated_event_id, d.title,
               COUNT(m.enriched_event_id) AS members,
               MAX(LENGTH(TRIM(COALESCE(r.raw_content, '')))) AS longest_page,
               MAX(CASE WHEN e.perplexity_validated = 1
                         AND TRIM(COALESCE(e.perplexity_enrichment_data, '')) NOT IN ('', '{{}}', 'null')
                        THEN 1 ELSE 0 END) AS researched
        FROM DeduplicatedEvents d
        LEFT JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
        LEFT JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
        WHERE {where}
        GROUP BY d.deduplicated_event_id""", params).fetchall()
    bare = [r for r in rows if (r[3] or 0) < min_chars and not r[4]]
    no_members = sum(1 for r in rows if not r[2])
    detail = (f"{len(bare)} of {len(rows)} active events: no record with >= {min_chars} chars of page text "
              f"and no validated Perplexity research" + (f"; {no_members} have no records at all" if no_members else ""))
    examples = [f"{r[0]}: {(r[1] or '')[:70]}" for r in bare[:EXAMPLES]]
    return {
        "evidence.events_without_evidence": Measurement(value=len(bare), detail=detail, examples=examples),
        "evidence.events_without_evidence_share": Measurement(value=_share(len(bare), len(rows)), detail=detail,
                                                                 examples=examples),
    }


def _evidence_status_unavailable(reason: str) -> Measurements:
    from cyber_data_collector.processing import evidence_recovery as er

    names = ([f"evidence.status.{s}" for s in (*er.STATUSES, "unsupported_share", "partial_share", "stored_stale")]
             + [f"evidence.recovery.{s}" for s in (er.ROUTE_NEEDED, er.ROUTE_PENDING, er.ROUTE_EXHAUSTED)])
    return {name: Measurement.unavailable(reason) for name in names}


def measure_evidence_status(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """Each active event's evidence status (``processing/evidence_recovery.py``), computed afresh.

    ``supported``: a record's own page text and validated research;
    ``partial``: one of them, or only another page's text; ``unsupported``:
    neither, so every field is uncertain. Unsupported events are split by where
    retrieval stands, and ``stored_stale`` counts events whose stored
    ``evidence_status`` no longer matches (the hygiene step has not run since).
    """
    from cyber_data_collector.processing import evidence_recovery as er

    needed = {"perplexity_enrichment_data", "perplexity_validated", "perplexity_status",
              "perplexity_attempt_count", "is_specific_event"}
    missing = needed - columns(conn, "EnrichedEvents")
    if missing or not {"raw_content", "source_metadata"} <= columns(conn, "RawEvents"):
        reason = f"EnrichedEvents.{'/'.join(sorted(missing)) or '...'} or RawEvents.raw_content/source_metadata missing"
        return _evidence_status_unavailable(reason)
    tables = [t for t in ("PageRescrapes", "CyberDailyRefetches", "SourceReplacements") if not has_table(conn, t)]
    if tables:
        # Read-only: the tables the diagnosis joins are created by the hygiene step.
        reason = f"{', '.join(tables)} missing (run dedup_admin.py recover-evidence)"
        return _evidence_status_unavailable(reason)
    events = er.assess_events(conn)
    counts = er.status_counts(events)
    unsupported = [e for e in events if e.status == er.UNSUPPORTED]
    detail = (f"{counts[er.SUPPORTED]} supported, {counts[er.PARTIAL]} partial, {counts[er.UNSUPPORTED]} unsupported "
              f"of {len(events)} active events")
    examples = [f"{e.event_id}: {e.title[:60]} - {e.reason[:160]}" for e in unsupported[:EXAMPLES]]
    out: Measurements = {f"evidence.status.{s}": Measurement(value=counts[s], detail=detail) for s in er.STATUSES}
    out["evidence.status.unsupported_share"] = Measurement(value=_share(counts[er.UNSUPPORTED], len(events)),
                                                            detail=detail, examples=examples)
    out["evidence.status.partial_share"] = Measurement(value=_share(counts[er.PARTIAL], len(events)), detail=detail)
    for state, what in ((er.ROUTE_NEEDED, "retrieval not yet queued (capped per run)"),
                        (er.ROUTE_PENDING, "retrieval queued, waiting"),
                        (er.ROUTE_EXHAUSTED, "every route tried; evidence out of reach")):
        hits = [e for e in unsupported if e.recovery == state]
        out[f"evidence.recovery.{state}"] = Measurement(
            value=len(hits), detail=f"unsupported events: {what}",
            examples=[f"{e.event_id}: {e.reason[:160]}" for e in hits[:EXAMPLES]])
    if er.has_schema(conn):
        stale = [e for e in events if e.stored_status != e.status]
        out["evidence.status.stored_stale"] = Measurement(
            value=len(stale), detail="active events whose stored evidence_status is missing or out of date",
            examples=[f"{e.event_id}: stored {e.stored_status}, now {e.status}" for e in stale[:EXAMPLES]])
    else:
        out["evidence.status.stored_stale"] = Measurement.unavailable(
            "DeduplicatedEvents.evidence_status missing (run dedup_admin.py recover-evidence)")
    return out


# ---------------------------------------------------------------------------
# Retry backlogs
# ---------------------------------------------------------------------------

def measure_backlogs(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """Raw records still waiting to be processed, and enrichments retrying or quarantined."""
    from cyber_data_collector.processing.record_evidence import REJECTION_PREFIX

    stale_days = int(thresholds.setting("backlog", "stale_after_days"))
    out: Measurements = {}
    raw = columns(conn, "RawEvents")
    if "is_processed" in raw:
        # A record whose classification or storage failed is left unprocessed
        # and retried on the next run (pipelines/discovery.py).
        pending = conn.execute(
            "SELECT raw_event_id, source_type, CAST(discovered_at AS TEXT) FROM RawEvents "
            "WHERE COALESCE(is_processed, 0) IN (0, '0', 'false', 'False')").fetchall()
        out["backlog.raw_unprocessed"] = Measurement(
            value=len(pending), detail="raw records not yet processed (new, or failed and awaiting retry)",
            examples=[f"{p[0]} ({p[1]}, found {str(p[2])[:10]})" for p in pending[:EXAMPLES]])
        if "discovered_at" in raw:
            cutoff = date.fromordinal(today.toordinal() - stale_days).isoformat()
            stuck = [p for p in pending if p[2] and str(p[2])[:10] < cutoff]
            undated = sum(1 for p in pending if not p[2])
            out["backlog.raw_unprocessed_stale"] = Measurement(
                value=len(stuck) + undated,
                detail=f"unprocessed records found more than {stale_days} days ago (before {cutoff})"
                       + (f", including {undated} with no discovered_at" if undated else "")
                       + ": a retry that keeps failing",
                examples=[f"{p[0]} ({p[1]}, found {str(p[2])[:10]})" for p in stuck[:EXAMPLES]])
        else:
            out["backlog.raw_unprocessed_stale"] = Measurement.unavailable("RawEvents.discovered_at missing")
    else:
        reason = "RawEvents.is_processed missing"
        out["backlog.raw_unprocessed"] = Measurement.unavailable(reason)
        out["backlog.raw_unprocessed_stale"] = Measurement.unavailable(reason)

    if "processing_error" in raw:
        filtered = conn.execute(
            "SELECT COUNT(*) FROM RawEvents WHERE processing_error = 'Filtered out during processing'").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM RawEvents WHERE processing_error LIKE ?",
                                (REJECTION_PREFIX + "%",)).fetchone()[0]
        other = conn.execute(
            "SELECT processing_error, COUNT(*) FROM RawEvents WHERE processing_error IS NOT NULL "
            "AND processing_error != 'Filtered out during processing' AND processing_error NOT LIKE ? "
            "GROUP BY 1 ORDER BY 2 DESC", (REJECTION_PREFIX + "%",)).fetchall()
        out["backlog.raw_rejected"] = Measurement(
            value=filtered + rejected,
            detail=f"processed and dropped by a filter: {filtered} 'Filtered out during processing', "
                   f"{rejected} '{REJECTION_PREFIX}<stage>'")
        out["backlog.raw_processing_errors"] = Measurement(
            value=sum(n for _, n in other), detail="processing_error values that are not filter rejections",
            examples=[f"{n} x {str(e)[:80]}" for e, n in other[:EXAMPLES]])
    else:
        reason = "RawEvents.processing_error missing"
        out["backlog.raw_rejected"] = Measurement.unavailable(reason)
        out["backlog.raw_processing_errors"] = Measurement.unavailable(reason)

    enr = columns(conn, "EnrichedEvents")
    tracking = {"perplexity_status", "perplexity_attempt_count"}
    if tracking <= enr:
        counts = dict(conn.execute(
            "SELECT COALESCE(perplexity_status, '(never attempted)'), COUNT(*) FROM EnrichedEvents "
            "WHERE COALESCE(status, 'Active') = 'Active' GROUP BY 1").fetchall()) if "status" in enr else dict(
            conn.execute("SELECT COALESCE(perplexity_status, '(never attempted)'), COUNT(*) "
                         "FROM EnrichedEvents GROUP BY 1").fetchall())
        quarantined = conn.execute(
            "SELECT enriched_event_id, title, perplexity_attempt_count FROM EnrichedEvents "
            "WHERE perplexity_status = 'quarantined'").fetchall()
        out["backlog.enrichment_retrying"] = Measurement(
            value=counts.get("retrying", 0), detail=f"Perplexity status counts (active records): {counts}")
        out["backlog.enrichment_quarantined"] = Measurement(
            value=len(quarantined),
            detail="enrichments that failed the maximum number of attempts; release with "
                   "perplexity_backfill_events.py --release-quarantine once the cause is fixed",
            examples=[f"{q[0]}: {(q[1] or '')[:60]} ({q[2]} attempts)" for q in quarantined[:EXAMPLES]])
    else:
        missing = ", ".join(sorted(tracking - enr))
        reason = (f"EnrichedEvents.{missing} missing: the schema predates enrichment retry tracking "
                  "(CyberEventDataV2 adds the columns on its next connection)")
        out["backlog.enrichment_retrying"] = Measurement.unavailable(reason)
        out["backlog.enrichment_quarantined"] = Measurement.unavailable(reason)
    return out


# ---------------------------------------------------------------------------
# Duplicate rates
# ---------------------------------------------------------------------------

def measure_duplicates(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """Same-victim pairs in the short-gap band, identical titles, records shared by events."""
    from cyber_data_collector.dedup.duplicate_judgement import candidate_pairs

    short_gap = int(thresholds.setting("duplicates", "short_gap_days"))
    out: Measurements = {}
    pairs = candidate_pairs(conn)    # every pair of active events sharing a victim (role)
    # Undated pairs are judged too; their gap is GAP_UNKNOWN (text), not a number.
    dated = [p for p in pairs if isinstance(p["gap_days"], int)]
    short = sorted((p for p in dated if p["gap_days"] <= short_gap), key=lambda p: p["gap_days"])
    out["duplicates.same_victim_pairs"] = Measurement(
        value=len(pairs), detail="pairs of active events sharing a victim-role entity")
    out["duplicates.same_victim_pairs_short_gap"] = Measurement(
        value=len(short),
        detail=(f"same-victim pairs dated <= {short_gap} days apart, the band the recurrence analysis "
                f"estimates its early hazard from ({len(pairs) - len(dated)} undated pairs not judged)"),
        examples=[f"{p['organisation']}: {p['gap_days']} days ({p['a']} / {p['b']})" for p in short[:EXAMPLES]])
    out["duplicates.same_victim_pairs_same_day"] = Measurement(
        value=sum(1 for p in dated if p["gap_days"] == 0), detail="same-victim pairs on the same stored date")

    where, params = incident_filter(alias="d")
    titles = conn.execute(
        f"SELECT LOWER(TRIM(d.title)), COUNT(*), GROUP_CONCAT(d.deduplicated_event_id, ', ') "
        f"FROM DeduplicatedEvents d WHERE {where} AND TRIM(COALESCE(d.title, '')) != '' "
        f"GROUP BY 1 HAVING COUNT(*) > 1 ORDER BY 2 DESC", params).fetchall()
    out["duplicates.identical_title_pairs"] = Measurement(
        value=sum(n * (n - 1) // 2 for _, n, _ in titles),
        detail=f"pairs of active events with the same title (case and spacing ignored), in {len(titles)} groups",
        examples=[f"{n} x {t[:70]!r}" for t, n, _ in titles[:EXAMPLES]])

    shared = conn.execute(
        f"""SELECT m.enriched_event_id, COUNT(DISTINCT m.deduplicated_event_id)
            FROM EventDeduplicationMap m JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
            WHERE {where} GROUP BY 1 HAVING COUNT(DISTINCT m.deduplicated_event_id) > 1""", params).fetchall()
    out["duplicates.records_in_multiple_active_events"] = Measurement(
        value=len(shared), detail="records counted as evidence by more than one active event",
        examples=[f"{s[0]} in {s[1]} events" for s in shared[:EXAMPLES]])
    return out


# ---------------------------------------------------------------------------
# Invalid dates
# ---------------------------------------------------------------------------

def measure_dates(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """Future, implausibly early, missing, malformed and placeholder dates on active events."""
    from cyber_data_collector.dedup.date_provenance import precision

    earliest = str(thresholds.setting("dates", "earliest_valid"))
    where, params = incident_filter(alias="d")
    rows = conn.execute(f"SELECT d.deduplicated_event_id, d.event_date, d.title FROM DeduplicatedEvents d "
                        f"WHERE {where}", params).fetchall()
    undated, malformed, future, early, dated = [], [], [], [], []
    for event_id, value, title in rows:
        text = "" if value is None else str(value).strip()
        label = f"{event_id}: {text or '(none)'} {(title or '')[:60]!r}"
        if not text:
            undated.append(label)
            continue
        if not _ISO_DAY.match(text):
            malformed.append(label)
            continue
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            malformed.append(label)
            continue
        dated.append(text[:10])
        if day > today:
            future.append(label)
        if text[:10] < earliest:
            early.append(label)
    precisions = Counter(precision(d) for d in dated)
    first = sum(n for p, n in precisions.items() if p != "day")
    year_only = sum(n for p, n in precisions.items() if p.startswith("year"))
    return {
        "dates.future_active": Measurement(value=len(future), examples=future[:EXAMPLES],
                                           detail=f"active events dated after {today.isoformat()}"),
        "dates.before_earliest_active": Measurement(value=len(early), examples=early[:EXAMPLES],
                                                    detail=f"active events dated before {earliest}"),
        "dates.undated_active": Measurement(value=len(undated), examples=undated[:EXAMPLES],
                                            detail="active events with no date belong to no reporting period"),
        "dates.malformed_active": Measurement(value=len(malformed), examples=malformed[:EXAMPLES],
                                              detail="active event dates that are not YYYY-MM-DD"),
        "dates.first_of_month_share": Measurement(
            value=_share(first, len(dated)),
            detail=f"{first} of {len(dated)} dated active events stored on the 1st of a month "
                   f"(month- or year-only placeholders, or a genuine 1st)"),
        "dates.year_only_share": Measurement(
            value=_share(year_only, len(dated)),
            detail=f"{year_only} of {len(dated)} dated active events stored as 1 January"),
    }


# ---------------------------------------------------------------------------
# Stale classifications
# ---------------------------------------------------------------------------

def _asd_classifier(conn: sqlite3.Connection):
    """An ``ASDRiskClassifier`` that can fingerprint events but makes no API client.

    ``__init__`` needs an API key, opens its own connection and may ALTER the
    table; staleness needs none of that - only the prompt, the model and the
    temperature that ``input_fingerprint`` hashes.
    """
    import importlib

    module = importlib.import_module("scripts.asd_risk_classifier")
    classifier = module.ASDRiskClassifier.__new__(module.ASDRiskClassifier)
    classifier.conn = conn
    classifier.model = module.openai_model()
    classifier._temperature = module.chat_params(classifier.model)["temperature"]
    classifier._prompt_cache = {}
    return classifier


def measure_staleness(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """ASD classifications, entity roles and event facts computed from outdated inputs."""
    out: Measurements = {}
    active = _active_count(conn)
    where, params = incident_filter(alias="d")
    classified = int(conn.execute(
        f"SELECT COUNT(DISTINCT d.deduplicated_event_id) FROM DeduplicatedEvents d "
        f"JOIN ASDRiskClassifications a ON a.deduplicated_event_id = d.deduplicated_event_id WHERE {where}",
        params).fetchone()[0])
    out["staleness.asd_unclassified"] = Measurement(
        value=active - classified, detail=f"{active - classified} of {active} active events have no ASD classification")

    if "input_fingerprint" not in columns(conn, "ASDRiskClassifications"):
        reason = ("ASDRiskClassifications.input_fingerprint missing: the classifications predate input "
                  "fingerprints, so the classifier treats every one of them as stale and will redo "
                  f"them on its next run ({classified} classified active events, {active - classified} unclassified)")
        out["staleness.asd_stale"] = Measurement.unavailable(reason)
        out["staleness.asd_stale_share"] = Measurement.unavailable(reason)
    else:
        with row_factory(conn, sqlite3.Row):
            classifier = _asd_classifier(conn)
            events = classifier.get_events(limit=10 ** 9)
        stale = [e for e in events if e["_is_stale"]]
        detail = (f"{len(stale)} of {len(events)} active events need (re)classification: "
                  f"{active - classified} unclassified, {len(stale) - (active - classified)} classified from "
                  "inputs that have since changed (input_fingerprint)")
        examples = [f"{e['deduplicated_event_id']}: {(e.get('title') or '')[:60]}" for e in stale[:EXAMPLES]]
        out["staleness.asd_stale"] = Measurement(value=len(stale), detail=detail, examples=examples)
        out["staleness.asd_stale_share"] = Measurement(value=_share(len(stale), len(events)), detail=detail)

    if "roles_member_signature" in columns(conn, "DeduplicatedEvents"):
        from cyber_data_collector.dedup.role_maintenance import staleness_report

        with row_factory(conn, sqlite3.Row):
            roles = staleness_report(conn)
        detail = f"entity roles: {roles}"
        out["staleness.roles_outdated"] = Measurement(value=roles["outdated"], detail=detail)
        out["staleness.roles_never_classified"] = Measurement(value=roles["never_classified"], detail=detail)
    else:
        reason = "DeduplicatedEvents.roles_member_signature missing"
        out["staleness.roles_outdated"] = Measurement.unavailable(reason)
        out["staleness.roles_never_classified"] = Measurement.unavailable(reason)

    if "facts_member_signature" not in columns(conn, "DeduplicatedEvents") or not has_table(conn, "EventFactOverrides"):
        # event_facts.stale_events would create these (a write); a quality check only reads.
        out["staleness.facts_stale"] = Measurement.unavailable(
            "DeduplicatedEvents.facts_member_signature / EventFactOverrides missing: the schema predates "
            "fact recomputation (event_facts.ensure_schema adds them on the next deduplication)")
    else:
        from cyber_data_collector.dedup.event_facts import stale_events

        stale = stale_events(conn)
        out["staleness.facts_stale"] = Measurement(
            value=len(stale), detail="active events whose facts were computed for a different membership",
            examples=stale[:EXAMPLES])
    return out


# ---------------------------------------------------------------------------
# Cyber Daily: articles per publication month and backfill completion
# ---------------------------------------------------------------------------

CYBERDAILY_SOURCE = "CyberDaily"
CYBERDAILY_SUMMARY = (
    "coverage.cyberdaily.backfill_months_complete", "coverage.cyberdaily.backfill_months_not_searched",
    "coverage.cyberdaily.backfill_months_retrying", "coverage.cyberdaily.backfill_months_failed",
    "coverage.cyberdaily.short_months", "coverage.cyberdaily.months_seen_not_stored",
    "coverage.cyberdaily.stored_not_judged", "coverage.cyberdaily.stories_incomplete",
    "coverage.cyberdaily.stories_unrecoverable",
)


def _months_between(first: str, last: str) -> List[str]:
    year, month = (int(part) for part in first.split("-"))
    out = []
    while f"{year:04d}-{month:02d}" <= last:
        out.append(f"{year:04d}-{month:02d}")
        year, month = _month_add(year, month, 1)
    return out


def _neighbour_median(series: Dict[str, int], months: List[str], index: int, k: int) -> float:
    window = [series[m] for m in months[max(0, index - k):index] + months[index + 1:index + 1 + k] if m in series]
    return statistics.median(window) if window else 0


def measure_cyberdaily(conn: sqlite3.Connection, thresholds: Thresholds, today: date) -> Measurements:
    """Cyber Daily articles seen / stored / judged per publication month, and backfill completion.

    *Seen*: articles the section feed listed for the month (``SourceMonthDiscovery.items_seen``,
    from its last complete search). *Stored*: distinct Cyber Daily articles held in
    ``RawEvents`` under any source, by publication date. *Judged*: of those, articles with
    a record discovery has finished (classified or rejected). Months from
    ``cyberdaily.backfill_since`` to last month are classed by their search state:
    complete, retrying (the last search failed for a temporary reason and will be
    searched again), failed (a 404/410, or given up after repeated failures) or not
    searched.
    """
    from cyber_data_collector.processing.cyberdaily_refetch import enrichment_ids, is_eligible, is_incomplete
    from cyber_data_collector.processing.fetch_errors import error_type_from_text, is_permanent

    since = str(thresholds.setting("cyberdaily", "backfill_since"))
    k = int(thresholds.setting("cyberdaily", "neighbour_months"))
    drop_ratio = float(thresholds.setting("cyberdaily", "drop_ratio"))
    min_median = float(thresholds.setting("cyberdaily", "min_neighbour_median"))
    tolerance = float(thresholds.setting("cyberdaily", "seen_not_stored_tolerance"))

    raw_cols = columns(conn, "RawEvents")
    if not {"source_url", "is_processed", "raw_content"} <= raw_cols:
        reason = "RawEvents.source_url / is_processed / raw_content missing"
        return {name: Measurement.unavailable(reason) for name in CYBERDAILY_SUMMARY}

    current = f"{today.year:04d}-{today.month:02d}"
    previous = "%04d-%02d" % _month_add(today.year, today.month, -1)
    judged_months = _months_between(since, previous)       # the current month is still being published
    shown_months = _months_between(since, current)

    # --- search state per month ---------------------------------------------
    searches: Dict[str, Dict[str, object]] = {}
    if has_table(conn, "SourceMonthDiscovery"):
        smd_cols = columns(conn, "SourceMonthDiscovery")
        extra = [c for c in ("error_type", "gave_up_at", "items_seen", "failed_attempts", "next_attempt_at")
                 if c in smd_cols]
        cur = conn.execute(
            f"SELECT year, month, status, failures_json{''.join(', ' + c for c in extra)} "
            "FROM SourceMonthDiscovery WHERE source_type = ?", (CYBERDAILY_SOURCE,))
        names = [d[0] for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(names, row))
            searches[f"{record['year']:04d}-{record['month']:02d}"] = record
    processed_months = set()
    if has_table(conn, "MonthProcessed"):
        processed_months = {f"{y:04d}-{m:02d}" for y, m in conn.execute(
            "SELECT year, month FROM MonthProcessed WHERE is_processed")}

    complete, retrying, failed, not_searched = [], [], [], []
    for month in judged_months:
        record = searches.get(month)
        if record is None:
            not_searched.append(month)
            continue
        if record["status"] == "complete":
            complete.append(month)
            continue
        error_type = record.get("error_type")
        if not error_type:
            try:
                messages = json.loads(record.get("failures_json") or "[]")
            except ValueError:
                messages = [record.get("failures_json")]
            error_type = error_type_from_text(" ".join(str(m) for m in messages))
        label = f"{month}: {error_type}, {record.get('failed_attempts') or 1} failed search(es)"
        if record.get("gave_up_at") or is_permanent(error_type):
            failed.append(label + " - given up")
        else:
            retrying.append(label + (f", next after {record['next_attempt_at']}" if record.get("next_attempt_at")
                                     else ", searched again next run"))

    # --- stored and judged articles by publication month ---------------------
    pub_expr = "r.publication_date" if "publication_date" in raw_cols else "NULL"
    from cyber_data_collector.datasources.cyber_daily import article_id

    stored_ids: Dict[str, set] = defaultdict(set)
    judged_ids: Dict[str, set] = defaultdict(set)
    undated = 0
    for url, published, metadata, event_date, source_type, processed in conn.execute(
            f"SELECT r.source_url, {pub_expr}, r.source_metadata, CAST(r.event_date AS TEXT), r.source_type, "
            "r.is_processed FROM RawEvents r WHERE r.source_url LIKE '%cyberdaily.au/%'"):
        aid = article_id(url)
        if not aid:
            continue
        when = str(published or "")
        if not _ISO_DAY.match(when):
            try:
                when = str((json.loads(metadata or "{}") or {}).get("publication_date") or "")
            except (ValueError, AttributeError):
                when = ""
        if not _ISO_DAY.match(when) and source_type == CYBERDAILY_SOURCE:
            when = str(event_date or "")       # Cyber Daily records are dated by publication at discovery
        if not _ISO_DAY.match(when):
            undated += 1
            continue
        stored_ids[when[:7]].add(aid)
        if processed:
            judged_ids[when[:7]].add(aid)
    stored = {m: len(ids) for m, ids in stored_ids.items()}
    judged = {m: len(ids) for m, ids in judged_ids.items()}

    out: Measurements = {}
    for month in shown_months:
        seen = searches.get(month, {}).get("items_seen")
        if isinstance(seen, int):
            out[f"coverage.cyberdaily.seen.{month}"] = Measurement(value=seen)
        out[f"coverage.cyberdaily.stored.{month}"] = Measurement(value=stored.get(month, 0))
        out[f"coverage.cyberdaily.judged.{month}"] = Measurement(value=judged.get(month, 0))

    span = f"{judged_months[0]}..{judged_months[-1]}" if judged_months else "none"
    out["coverage.cyberdaily.backfill_months_complete"] = Measurement(
        value=len(complete), detail=f"{len(complete)} of {len(judged_months)} months ({span}) searched completely")
    unprocessed = [m for m in not_searched if m not in processed_months]
    out["coverage.cyberdaily.backfill_months_not_searched"] = Measurement(
        value=len(not_searched), examples=not_searched[:EXAMPLES],
        detail=(f"months {span} Cyber Daily has never searched; {len(unprocessed)} of them are not processed by "
                "discovery yet (the backfill only searches processed months). Run `python scripts/"
                f"discovery_queue.py backfill --source CyberDaily --since {since}`"))
    out["coverage.cyberdaily.backfill_months_retrying"] = Measurement(
        value=len(retrying), examples=retrying[:EXAMPLES],
        detail="last search lost results for a temporary reason (throttled, network, parse, other); searched "
               "again automatically by the next run after its back-off")
    out["coverage.cyberdaily.backfill_months_failed"] = Measurement(
        value=len(failed), examples=failed[:EXAMPLES],
        detail="feed gone (404/410) or given up after repeated failures: not searched again until "
               "`python scripts/discovery_queue.py release` (or backfill --force)")

    # Short months: a completely searched month well below its completely searched neighbours.
    complete_stored = {m: stored.get(m, 0) for m in complete}
    ordered = [m for m in judged_months if m in complete_stored]
    short = []
    for i, month in enumerate(ordered):
        median = _neighbour_median(complete_stored, ordered, i, k)
        value = complete_stored[month]
        if median >= min_median and (value == 0 or value < drop_ratio * median):
            short.append(f"{month}: {value} stored vs neighbour median {median:g}")
    out["coverage.cyberdaily.short_months"] = Measurement(
        value=len(short), examples=short[:EXAMPLES],
        detail=(f"completely searched months whose stored articles fall below {drop_ratio:g} x the median of up "
                f"to {k} searched months either side (when that median is at least {min_median:g})"
                + (f"; {undated} stored Cyber Daily records have no publication date" if undated else "")))

    gaps = []
    for month in complete:
        seen = searches[month].get("items_seen")
        if isinstance(seen, int) and seen - stored.get(month, 0) > max(2, tolerance * seen):
            gaps.append(f"{month}: feed listed {seen}, {stored.get(month, 0)} stored")
    out["coverage.cyberdaily.months_seen_not_stored"] = Measurement(
        value=len(gaps), examples=gaps[:EXAMPLES],
        detail=f"months where the feed listed more articles than are stored (beyond max(2, {tolerance:.0%}))")

    pending = {m: stored.get(m, 0) - judged.get(m, 0) for m in judged_months if stored.get(m, 0) > judged.get(m, 0)}
    out["coverage.cyberdaily.stored_not_judged"] = Measurement(
        value=sum(pending.values()), examples=[f"{m}: {n}" for m, n in sorted(pending.items())][:EXAMPLES],
        detail="stored articles with no finished record yet (the retry queue drains them)")

    # Incomplete page text, and stories no longer fetched.
    given_up: Dict[str, Tuple[str, Optional[str]]] = {}
    if has_table(conn, "CyberDailyRefetches") and "gave_up_at" in columns(conn, "CyberDailyRefetches"):
        given_up = {rid: (url, etype) for rid, url, etype in conn.execute(
            "SELECT raw_event_id, url, error_type FROM CyberDailyRefetches WHERE gave_up_at IS NOT NULL")}
    incomplete = 0
    active, anything = enrichment_ids(conn) if has_table(conn, "EnrichedEvents") else (set(), set())
    error_col = "r.processing_error" if "processing_error" in raw_cols else "NULL"
    for raw_id, url, content, processed, error in conn.execute(
            f"SELECT r.raw_event_id, r.source_url, r.raw_content, r.is_processed, {error_col} FROM RawEvents r "
            "WHERE r.source_url LIKE '%cyberdaily.au/%'"):
        if (article_id(url) and raw_id not in given_up and is_incomplete(content)
                and is_eligible(raw_id, processed, error, active, anything)):
            incomplete += 1
    out["coverage.cyberdaily.stories_incomplete"] = Measurement(
        value=incomplete,
        detail="stored Cyber Daily stories whose page text is cut off or too short, queued for the re-fetch "
               "(`python scripts/discovery_queue.py refetch-cyberdaily`; a few hundred per run)")
    by_type = Counter(etype or "?" for _, etype in given_up.values())
    out["coverage.cyberdaily.stories_unrecoverable"] = Measurement(
        value=len(given_up), examples=[f"{etype}: {url}" for url, etype in list(given_up.values())[:EXAMPLES]],
        detail=("stories no longer fetched: " + (", ".join(f"{n} {t}" for t, n in sorted(by_type.items()))
                                                 or "none") + " (missing = 404/410 on the site)"))
    return out


MEASURES = (
    ("coverage", measure_coverage),
    ("coverage", measure_cyberdaily),
    ("evidence", measure_evidence),
    ("evidence", measure_evidence_status),
    ("backlog", measure_backlogs),
    ("duplicates", measure_duplicates),
    ("dates", measure_dates),
    ("staleness", measure_staleness),
)
