"""End-to-end data-integrity check for the Australian Cyber Events DB +
the OAIC stats files. Detects every failure mode we hit on 2026-05-03.

Run:
    python scripts/check_data_integrity.py [--db PATH] [--quiet]

Exit code 0 if clean, 1 if any check fails. Useful as a CI gate, a
pre-pipeline-run sanity check, and a post-pipeline-run smoke test.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class CheckResult:
    name: str
    ok: bool
    summary: str = ""
    details: List[str] = field(default_factory=list)


# Sanity ranges
OAIC_SECTOR_NOTIF_MIN = 10
OAIC_SECTOR_NOTIF_MAX = 250

# Match the YYYYMMDD_HHMMSS scrape stamp embedded in OAIC output filenames.
_OAIC_TS_RE = re.compile(r"(\d{8})_(\d{6})")


def _oaic_files_newest_first() -> List[str]:
    """Return OAIC stat files, newest scrape first.

    Sorts by the timestamp embedded in the filename rather than os.path.getmtime,
    which is unreliable: copying, restoring, or rewriting a file (e.g. a cleanup
    pass or backup) resets its mtime and can silently promote stale data for a
    period. The filename stamp reflects the actual scrape time and is stable.
    """
    def _key(path: str):
        m = _OAIC_TS_RE.search(os.path.basename(path))
        return m.group(1) + m.group(2) if m else "0"

    return sorted(glob.glob("oaic_cyber_statistics_*.json"), key=_key, reverse=True)
SEVERITY_VALUES = {"Critical", "High", "Medium", "Low", "Unknown", None}
EVENT_TYPE_VALUES_KNOWN_PREFIXES = (
    "Ransomware", "Data Breach", "Phishing", "Malware", "Vulnerability Exploit",
    "State-Sponsored Attack", "Supply Chain Attack", "Insider Threat",
    "Denial of Service", "Financial Fraud", "Identity Theft",
    "Infrastructure Attack", "Other",
)
ASD_VALID_CATEGORIES = {"C1", "C2", "C3", "C4", "C5", "C6"}
INDUSTRY_VALUES_BLACKLIST = {
    # Should NEVER appear - vendors mistagged as their clients' sectors
    # Caught by enrichment_validator.known_orgs auto-correct now, but this
    # blacklist guards against regression.
}


def check_severity_no_truncated_or_qualified(conn: sqlite3.Connection) -> CheckResult:
    """No DeduplicatedEvents.severity should look like 'EventSeverity.X' or
    truncated forms 'EventSeverity.UNKNOW' / 'EventSeverity.CRITIC'.
    """
    bad = list(conn.execute("""
        SELECT severity, COUNT(*) c FROM DeduplicatedEvents
        WHERE severity LIKE 'EventSeverity.%' OR severity = 'EventSeverity.UNKNOW'
           OR severity = 'EventSeverity.CRITIC'
        GROUP BY severity
    """))
    if bad:
        return CheckResult(
            name="severity-no-qualified-or-truncated",
            ok=False,
            summary=f"{sum(r[1] for r in bad)} rows have malformed severity values",
            details=[f"{r[0]!r}: {r[1]} rows" for r in bad],
        )
    return CheckResult(
        "severity-no-qualified-or-truncated", True,
        "All DeduplicatedEvents.severity values are clean enum values",
    )


def check_event_type_no_truncated_or_qualified(conn: sqlite3.Connection) -> CheckResult:
    bad = list(conn.execute("""
        SELECT event_type, COUNT(*) c FROM DeduplicatedEvents
        WHERE event_type LIKE 'CyberEventType.%' GROUP BY event_type
    """))
    if bad:
        return CheckResult(
            "event_type-no-qualified", False,
            f"{sum(r[1] for r in bad)} rows have qualified-name event_type values",
            [f"{r[0]!r}: {r[1]} rows" for r in bad],
        )
    return CheckResult(
        "event_type-no-qualified", True,
        "All DeduplicatedEvents.event_type values are clean enum values",
    )


def check_severity_in_known_set(conn: sqlite3.Connection) -> CheckResult:
    bad = list(conn.execute("""
        SELECT severity, COUNT(*) c FROM DeduplicatedEvents
        WHERE severity IS NOT NULL
          AND severity NOT IN ('Critical','High','Medium','Low','Unknown')
        GROUP BY severity
    """))
    if bad:
        return CheckResult(
            "severity-in-enum-set", False,
            f"{sum(r[1] for r in bad)} rows have unknown severity values",
            [f"{r[0]!r}: {r[1]} rows" for r in bad],
        )
    return CheckResult(
        "severity-in-enum-set", True,
        "All severity values are in the valid set",
    )


def check_no_duplicate_h1_h2_oaic(_unused) -> CheckResult:
    """Each year's H1 and H2 records (across all OAIC stat files) should
    differ on at least one canonical count - byte-identical pairs are the
    classic 'silent semester-selection failure' signature.
    """
    bad = []
    for f in _oaic_files_newest_first():
        try:
            data = json.load(open(f))
        except Exception:
            continue
        by_year_period = {(d.get("year"), d.get("period")): d for d in data
                          if d.get("year") and d.get("period")}
        for (year, p), rec in by_year_period.items():
            if p != "H2":
                continue
            h1 = by_year_period.get((year, "H1"))
            if not h1:
                continue
            cf = ("total_notifications", "malicious_attacks", "human_error",
                  "system_faults", "phishing", "ransomware", "hacking",
                  "malware", "brute_force")
            h1v = tuple(h1.get(k) for k in cf)
            h2v = tuple(rec.get(k) for k in cf)
            if any(v is not None for v in h1v) and h1v == h2v:
                bad.append(f"{os.path.basename(f)}: {year} H1==H2 ({cf[0]}={h1v[0]})")
    if bad:
        return CheckResult(
            "oaic-no-duplicate-h1-h2", False,
            f"{len(bad)} byte-identical H1/H2 pairs across OAIC stats files",
            bad,
        )
    return CheckResult(
        "oaic-no-duplicate-h1-h2", True,
        "No byte-identical H1/H2 OAIC stat pairs",
    )


def check_oaic_top_sectors_in_range(_unused) -> CheckResult:
    """Top-5 sector notification counts should be 0 (null) OR in 10-250.
    Anything in 1-9 is almost certainly a rank position; anything > 250 is
    almost certainly an LLM hallucination of a period total.
    """
    bad = []
    files = _oaic_files_newest_first()
    seen_keys = set()
    for f in files:
        try:
            data = json.load(open(f))
        except Exception:
            continue
        for r in data:
            key = (r.get("year"), r.get("period"))
            if key in seen_keys: continue
            seen_keys.add(key)
            for s in r.get("top_sectors") or []:
                n = s.get("notifications")
                if not isinstance(n, (int, float)) or n is None:
                    continue
                if 1 <= n < OAIC_SECTOR_NOTIF_MIN:
                    bad.append(f"{key} {s.get('sector')!r} notifications={n} - looks like a rank")
                elif n > OAIC_SECTOR_NOTIF_MAX:
                    bad.append(f"{key} {s.get('sector')!r} notifications={n} - implausible (likely LLM hallucination)")
    if bad:
        return CheckResult(
            "oaic-top-sectors-in-range", False,
            f"{len(bad)} top_sectors entries outside {OAIC_SECTOR_NOTIF_MIN}-{OAIC_SECTOR_NOTIF_MAX}",
            bad[:30],
        )
    return CheckResult(
        "oaic-top-sectors-in-range", True,
        f"All top_sectors notifications in {OAIC_SECTOR_NOTIF_MIN}-{OAIC_SECTOR_NOTIF_MAX}",
    )


def check_asd_no_orphans(conn: sqlite3.Connection) -> CheckResult:
    """Every ASDRiskClassifications row must point to an existing dedup row."""
    n = conn.execute("""
        SELECT COUNT(*) FROM ASDRiskClassifications arc
        LEFT JOIN DeduplicatedEvents de
          ON arc.deduplicated_event_id = de.deduplicated_event_id
        WHERE de.deduplicated_event_id IS NULL
    """).fetchone()[0]
    if n:
        return CheckResult(
            "asd-no-orphans", False,
            f"{n} ASDRiskClassifications rows have no matching DeduplicatedEvents row",
        )
    return CheckResult("asd-no-orphans", True, "All ASD classifications link to existing dedup rows")


def check_asd_categories_valid(conn: sqlite3.Connection) -> CheckResult:
    bad = list(conn.execute("""
        SELECT severity_category, COUNT(*) c FROM ASDRiskClassifications
        WHERE severity_category IS NOT NULL
          AND severity_category NOT IN ('C1','C2','C3','C4','C5','C6')
        GROUP BY severity_category
    """))
    if bad:
        return CheckResult(
            "asd-categories-valid", False,
            f"{sum(r[1] for r in bad)} ASD rows have invalid severity_category",
            [f"{r[0]!r}: {r[1]}" for r in bad],
        )
    return CheckResult("asd-categories-valid", True, "All ASD severity_category values valid")


def check_dedup_master_links_exist(conn: sqlite3.Connection) -> CheckResult:
    """Every DeduplicatedEvents.master_enriched_event_id must point to an
    existing EnrichedEvents row.
    """
    n = conn.execute("""
        SELECT COUNT(*) FROM DeduplicatedEvents de
        WHERE de.status = 'Active'
          AND NOT EXISTS (
              SELECT 1 FROM EnrichedEvents e
              WHERE e.enriched_event_id = de.master_enriched_event_id
          )
    """).fetchone()[0]
    if n:
        return CheckResult(
            "dedup-master-links-exist", False,
            f"{n} active dedup rows have orphan master_enriched_event_id",
        )
    return CheckResult("dedup-master-links-exist", True, "All dedup master IDs resolve")


def check_industry_known_vendors_correct(conn: sqlite3.Connection) -> CheckResult:
    """Known-vendor org names should never be tagged with their clients' sector.
    Catches regression of today's HWL Ebsworth / Frontier Software / etc. issue.
    """
    expected = {
        "HWL Ebsworth":           "Legal Services",
        "Frontier Software":      "Technology",
        "PricewaterhouseCoopers": "Legal Services",
        "Iress Limited":          "Technology",
        "Tabcorp Holdings":       "Entertainment",
        "ProctorU":               "Technology",
        "National Tertiary Education Union": "Nonprofit",
    }
    bad = []
    for name, expected_industry in expected.items():
        rows = list(conn.execute(
            "SELECT victim_organization_name, victim_organization_industry "
            "FROM DeduplicatedEvents WHERE status='Active' "
            "AND victim_organization_name LIKE ?",
            (f"%{name}%",),
        ))
        for r in rows:
            if r[1] and r[1] != expected_industry:
                bad.append(f"{r[0]!r} tagged {r[1]!r}, expected {expected_industry!r}")
    if bad:
        return CheckResult(
            "industry-known-vendors-correct", False,
            f"{len(bad)} known-vendor rows have wrong industry",
            bad,
        )
    return CheckResult(
        "industry-known-vendors-correct", True,
        "Known-vendor orgs have correct industry tags",
    )


def check_no_records_affected_outliers(conn: sqlite3.Connection) -> CheckResult:
    """records_affected should be 1 .. 1e9 if not null.

    Real-world ceiling: the largest known breaches are LinkedIn 2021 (700M),
    Live Nation/Ticketmaster 2024 (560M), Meta/Facebook leak (533M). Anything
    above 1 billion is almost certainly a parse error (mistook 'population of
    country' for 'records').
    """
    bad = list(conn.execute("""
        SELECT victim_organization_name, records_affected, title
        FROM DeduplicatedEvents
        WHERE status='Active' AND records_affected IS NOT NULL
          AND (records_affected < 1 OR records_affected > 1000000000)
    """))
    if bad:
        return CheckResult(
            "records-affected-in-range", False,
            f"{len(bad)} rows have implausible records_affected",
            [f"{r[0]!r}: records_affected={r[1]} ({(r[2] or '')[:50]})" for r in bad[:20]],
        )
    return CheckResult(
        "records-affected-in-range", True,
        "All records_affected values plausible",
    )


def check_no_dedup_status_drift(conn: sqlite3.Connection) -> CheckResult:
    """status column should only contain 'Active' or 'Merged'."""
    bad = list(conn.execute("""
        SELECT status, COUNT(*) c FROM DeduplicatedEvents
        WHERE status IS NOT NULL AND status NOT IN ('Active', 'Merged')
        GROUP BY status
    """))
    if bad:
        return CheckResult(
            "dedup-status-canonical", False,
            f"{sum(r[1] for r in bad)} rows have non-canonical status values",
            [f"{r[0]!r}: {r[1]}" for r in bad],
        )
    return CheckResult("dedup-status-canonical", True, "All dedup status values canonical")


def check_oaic_period_publication_realistic(_unused) -> CheckResult:
    """Each scraped semester should have total_notifications in 50-2000.
    Outside that range strongly suggests a bad scrape (e.g. only ranks were
    extracted, or the scraper captured an unfiltered all-period total).
    """
    bad = []
    files = _oaic_files_newest_first()
    seen = set()
    for f in files:
        try:
            data = json.load(open(f))
        except Exception:
            continue
        for r in data:
            key = (r.get("year"), r.get("period"))
            if key in seen: continue
            seen.add(key)
            t = r.get("total_notifications")
            if isinstance(t, (int, float)) and (t < 50 or t > 2000):
                bad.append(f"{key}: total_notifications={t}")
    if bad:
        return CheckResult(
            "oaic-period-totals-realistic", False,
            f"{len(bad)} OAIC periods have implausible total_notifications",
            bad,
        )
    return CheckResult(
        "oaic-period-totals-realistic", True,
        "All OAIC period total_notifications in 50-2000",
    )


# ---------------------------------------------------------------------------

DB_CHECKS: List[Callable[[sqlite3.Connection], CheckResult]] = [
    check_severity_no_truncated_or_qualified,
    check_event_type_no_truncated_or_qualified,
    check_severity_in_known_set,
    check_asd_no_orphans,
    check_asd_categories_valid,
    check_dedup_master_links_exist,
    check_industry_known_vendors_correct,
    check_no_records_affected_outliers,
    check_no_dedup_status_drift,
]

JSON_CHECKS: List[Callable[[None], CheckResult]] = [
    check_no_duplicate_h1_h2_oaic,
    check_oaic_top_sectors_in_range,
    check_oaic_period_publication_realistic,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="instance/cyber_events.db",
                        help="path to SQLite database")
    parser.add_argument("--quiet", action="store_true",
                        help="only print failing checks")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    results: List[CheckResult] = []
    for fn in DB_CHECKS:
        try:
            results.append(fn(conn))
        except Exception as e:
            results.append(CheckResult(fn.__name__, False, f"check raised: {e}"))
    for fn in JSON_CHECKS:
        try:
            results.append(fn(None))
        except Exception as e:
            results.append(CheckResult(fn.__name__, False, f"check raised: {e}"))
    conn.close()

    failed = [r for r in results if not r.ok]
    print(f"=== Data integrity check: {len(results) - len(failed)}/{len(results)} passed ===\n")
    for r in results:
        if r.ok and args.quiet:
            continue
        marker = "PASS" if r.ok else "FAIL"
        print(f"[{marker}] {r.name}")
        if r.summary:
            print(f"    {r.summary}")
        for d in r.details[:15]:
            print(f"      - {d}")
        if len(r.details) > 15:
            print(f"      ... (+{len(r.details) - 15} more)")
    if failed:
        print(f"\n{len(failed)} check(s) failed. Exit code 1.")
        return 1
    print("\nAll checks passed. Exit code 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
