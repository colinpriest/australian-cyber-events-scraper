"""Data-quality measurements over the database. Every function here only reads.

Each ``measure_*`` returns ``{metric_name: Measurement}``. A column or table a
measurement needs but the database lacks yields ``Measurement.unavailable``
with the reason - never a zero, which would read as "nothing wrong".
"""

from __future__ import annotations

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
                drop_ratio: float, min_median: float) -> List[str]:
    """Months in ``judged`` with zero volume, or below ``drop_ratio`` x the trailing median.

    The trailing window is the ``trailing`` months immediately before, zeros
    included; the ratio test only applies when that median is at least
    ``min_median`` (a fall from 2 to 0.5 is noise, not lost coverage).
    """
    flagged = []
    for month in judged:
        i = months.index(month)
        window = [series.get(m, 0) for m in months[max(0, i - trailing):i]]
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
                      flag_months(table[source], months, judged, trailing, drop_ratio, min_median)]
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
    dated = [p for p in pairs if p["gap_days"] is not None]
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


MEASURES = (
    ("coverage", measure_coverage),
    ("evidence", measure_evidence),
    ("backlog", measure_backlogs),
    ("duplicates", measure_duplicates),
    ("dates", measure_dates),
    ("staleness", measure_staleness),
)
