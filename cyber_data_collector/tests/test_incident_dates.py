"""Incident dates as intervals with verified precision; Webber headings are months, not the 15th."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from cyber_data_collector.processing import incident_dates as idt
from cyber_data_collector.storage.schema_init import initialize_database


@pytest.mark.parametrize("value, precision, expected", [
    ("2025-06-01", "month", ("2025-06-01", "2025-06-30")),
    ("2024-02-01", "month", ("2024-02-01", "2024-02-29")),
    ("2023-01-01", "year", ("2023-01-01", "2023-12-31")),
    ("2025-06-19", "day", ("2025-06-19", "2025-06-19")),
    ("2025-06-01", None, (None, None)),                 # unverified: no interval is claimed
])
def test_intervals(value, precision, expected):
    assert idt.interval(value, precision) == expected


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)

    def add(raw, dedup, source, raw_date, enriched_date, event_date, precision=None, published=None):
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, event_date, event_date_precision, "
                  "publication_date, publication_date_source, source_url, discovered_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, 'https://x', '2026-10-28')",
                  (raw, source, raw_date, precision, published, "source" if published else None))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_date, status, "
                  "is_australian_event, is_specific_event, perplexity_enrichment_data) "
                  "VALUES (?, ?, 't', ?, 'Active', 1, 1, ?)",
                  (f"e{raw}", raw, enriched_date, json.dumps({"date_precision": "day"})))
        c.execute("INSERT OR IGNORE INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, "
                  "title, event_date, status, is_australian_event, is_specific_event) "
                  "VALUES (?, ?, 't', ?, 'Active', 1, 1)", (dedup, f"e{raw}", event_date))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, "
                  "deduplicated_event_id) VALUES (?, ?, ?, ?)", (f"m{raw}", raw, f"e{raw}", dedup))

    # w1: a Webber record on the invented 15th, its heading month stored as its publication date.
    add("w1", "dw", "WebberInsurance", "2025-06-15", "2025-06-15", "2025-06-15", published="2025-06-15")
    # c1: a citation-checked day from Perplexity research.
    add("c1", "dc", "GoogleSearch", None, "2024-03-12", "2024-03-12")
    c.execute("INSERT INTO EnrichmentFactSupport (enriched_event_id, fact, status, checked_at) "
              "VALUES ('ec1', 'date', 'supported', '2026-10-28')")
    # u1: a 1st of the month nothing verifies.
    add("u1", "du", "GoogleSearch", "2023-05-01", "2023-05-01", "2023-05-01")
    c.commit()
    yield c
    c.close()


def test_webber_records_leave_the_15th_for_their_heading_month(conn):
    out = idt.repair_webber_dates(conn)
    assert (out.raw, out.enriched, out.events, out.publication_cleared) == (1, 1, 1, 1)
    raw = conn.execute("SELECT event_date, event_date_precision, event_date_basis, publication_date "
                       "FROM RawEvents WHERE raw_event_id = 'w1'").fetchone()
    assert tuple(raw) == ("2025-06-01", "month", idt.BASIS_WEBBER_HEADING, None)
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = 'dw'"
                        ).fetchone()[0] == "2025-06-01"
    assert idt.repair_webber_dates(conn).raw == 0                                 # idempotent


def test_only_verified_precision_is_stored_and_unverified_dates_get_no_interval(conn):
    idt.repair_webber_dates(conn)
    report = idt.refresh_event_precision(conn)
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT deduplicated_event_id, event_date_precision, event_date_earliest, event_date_latest "
        "FROM DeduplicatedEvents")}
    assert rows["dw"] == ("month", "2025-06-01", "2025-06-30")
    assert rows["dc"] == ("day", "2024-03-12", "2024-03-12")
    assert rows["du"] == (None, None, None)
    assert report.verified == {"month": 1, "day": 1} and report.unverified == 1


def test_webber_detail_page_prefers_the_day_tied_to_the_breach_and_keeps_the_link_date_as_publication(monkeypatch):
    from cyber_data_collector.datasources import webber_insurance as wi
    from cyber_data_collector.models.config import DataSourceConfig
    from cyber_data_collector.utils import RateLimiter

    html = b"<html><body><h1>Vertel - data breach</h1><article>Vertel said the breach occurred on 19 June 2025 and ...</article></body></html>"

    class Response:
        content = html

        def raise_for_status(self):
            pass

    monkeypatch.setattr(wi.requests, "get", lambda *a, **k: Response())
    source = wi.WebberInsuranceDataSource(DataSourceConfig(name="WebberInsurance"), RateLimiter(), {})
    url = "https://www.webberinsurance.com.au/data-breaches-list?campaign=20_06_2025#vertel"
    event = source._scrape_detail_page(url, section_date=datetime(2025, 6, 1))
    assert event.event_date.date().isoformat() == "2025-06-19" and event.event_date_precision == "day"
    assert event.data_sources[0].publication_date.date().isoformat() == "2025-06-20"

    monkeypatch.setattr(Response, "content", b"<html><body><h1>Vertel</h1><article>No date here.</article></body></html>")
    event = source._scrape_detail_page(url, section_date=datetime(2025, 6, 1))
    assert event.event_date.date().isoformat() == "2025-06-01" and event.event_date_precision == "month"
    assert source._parse_section_header_date("Vertel – June 2025") == datetime(2025, 6, 1)


# ---------------------------------------------------------------------------
# Compatible month and day evidence reconcile to the narrower interval
# ---------------------------------------------------------------------------

from datetime import date


def _ev(precision, value):
    start, end = idt.interval(value, precision)
    return (precision, start, end)


def test_a_month_and_a_day_inside_it_reconcile_to_the_day():
    evidence = [_ev("month", "2025-06-01"), _ev("day", "2025-06-12"), _ev("year", "2025-01-01")]
    assert idt.reconcile_precision(date(2025, 6, 12), evidence) == ("day", "verified")


def test_order_of_the_sources_does_not_matter():
    evidence = [_ev("day", "2025-06-12"), _ev("month", "2025-06-01")]
    assert idt.reconcile_precision(date(2025, 6, 12), evidence) == ("day", "verified")
    assert idt.reconcile_precision(date(2025, 6, 12), list(reversed(evidence))) == ("day", "verified")


def test_a_more_exact_day_elsewhere_in_the_month_does_not_move_the_stored_date():
    """The stored 1st sits in the month; a compatible day elsewhere in it keeps the month interval."""
    evidence = [_ev("month", "2022-05-01"), _ev("day", "2022-05-15")]
    assert idt.reconcile_precision(date(2022, 5, 1), evidence) == ("month", "verified")


def test_evidence_for_another_month_is_a_conflict():
    evidence = [_ev("month", "2025-06-01"), _ev("day", "2025-07-02")]
    assert idt.reconcile_precision(date(2025, 6, 1), evidence) == (None, "conflicting")
    assert idt.reconcile_precision(date(2025, 7, 2), evidence) == (None, "conflicting")


def test_verified_evidence_that_excludes_the_event_date_leaves_no_precision():
    assert idt.reconcile_precision(date(2012, 1, 1), [_ev("month", "2021-12-01")]) == (None, "conflicting")


def test_no_evidence_is_unverified_not_conflicting():
    assert idt.reconcile_precision(date(2025, 6, 1), []) == (None, "unverified")


def test_refresh_reconciles_a_month_record_and_a_day_record_of_one_event(conn):
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, event_date, event_date_precision, source_url, "
                 "discovered_at) VALUES ('m2', 'WebberInsurance', '2024-03-01', 'month', 'https://x', '2026-10-28')")
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_date, status, "
                 "is_australian_event, is_specific_event, perplexity_enrichment_data) "
                 "VALUES ('em2', 'm2', 't', '2024-03-12', 'Active', 1, 1, NULL)")
    conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                 "VALUES ('mm2', 'm2', 'em2', 'dc')")
    conn.commit()
    report = idt.refresh_event_precision(conn)
    row = conn.execute("SELECT event_date_precision, event_date_earliest, event_date_latest FROM DeduplicatedEvents "
                       "WHERE deduplicated_event_id = 'dc'").fetchone()
    assert tuple(row) == ("day", "2024-03-12", "2024-03-12")      # the month record did not make it a conflict
    assert "dc" not in report.conflicting and report.reconciled == 1


def test_refresh_reports_incompatible_sources_as_conflicting_with_no_interval(conn):
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, event_date, event_date_precision, source_url, "
                 "discovered_at) VALUES ('m3', 'WebberInsurance', '2024-07-01', 'month', 'https://x', '2026-10-28')")
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_date, status, "
                 "is_australian_event, is_specific_event, perplexity_enrichment_data) "
                 "VALUES ('em3', 'm3', 't', '2024-07-01', 'Active', 1, 1, NULL)")
    conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                 "VALUES ('mm3', 'm3', 'em3', 'dc')")
    conn.commit()
    report = idt.refresh_event_precision(conn)
    row = conn.execute("SELECT event_date_precision, event_date_earliest, event_date_latest FROM DeduplicatedEvents "
                       "WHERE deduplicated_event_id = 'dc'").fetchone()
    assert tuple(row) == (None, None, None)
    assert "dc" in report.conflicting
