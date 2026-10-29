"""Webber: a list entry's link is its own, and a date is the incident's only when the text ties it to the breach."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pytest
from bs4 import BeautifulSoup

from cyber_data_collector.datasources import webber_insurance as wi
from cyber_data_collector.models.config import DataSourceConfig
from cyber_data_collector.processing import incident_dates as idt
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils import RateLimiter


# ---------------------------------------------------------------------------
# A day tied to the incident
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    # The reviewer's example: the byline's date must not be taken for the incident's.
    ("Published 1 June 2025. The breach occurred on 2 May 2025.", date(2025, 5, 2)),
    ("Published 1 June 2025", None),
    ("By Hannah Barry\nJanuary 31, 2020 3.11pm\nPerth Mint has been hacked", None),
    ("Last updated 4 June 2025. It was discovered on 28 May 2025.", date(2025, 5, 28)),
    ("The attack on 12 March 2025 hit servers.", date(2025, 3, 12)),
    ("Data was accessed between 1 May 2025 and 3 May 2025 by an unauthorised party.", date(2025, 5, 1)),
    ("Data was accessed between 1 and 3 May 2025 by an unauthorised party.", date(2025, 5, 1)),
    ("Recordings between November 1 and December 21, 2024 may have been stolen.", date(2024, 11, 1)),
    ("On 2 May 2025, the company detected unusual activity on its network.", date(2025, 5, 2)),
    ("On 2 May 2025, the company announced a data breach.", None),            # an announcement, not the incident
    ("The company said on 5 May 2025 that it was investigating.", None),
    ("reported a breach on 2 May 2025", None),                                 # when a report was made
    ("Vertel disclosed on 19 June 2025 that customers were affected.", None),
    ("The incident was detected on 03/05/2025.", date(2025, 5, 3)),            # day first
    ("It occurred on 2 May", None),                                            # no year: not a full date
    ("", None),
])
def test_incident_date_from_text(text, expected):
    found = idt.incident_date_from_text(text)
    assert (found.day if found else None) == expected


def test_a_day_far_outside_the_heading_month_is_background_not_the_incident():
    text = "A similar breach occurred on 2 May 2016. This one happened on 12 May 2025."
    assert idt.incident_date_from_text(text).day == date(2016, 5, 2)                  # no heading: first onset
    assert idt.incident_date_from_text(text, near=date(2025, 6, 1)).day == date(2025, 5, 12)
    assert idt.incident_date_from_text("It occurred on 2 May 2016.", near=date(2025, 6, 1)) is None
    # Listed in June, so a May incident is plausible; one in the following August is not.
    assert idt.incident_date_from_text("It occurred on 2 August 2025.", near=date(2025, 6, 1)) is None


def test_the_day_incident_began_beats_the_day_it_was_found():
    text = "It was discovered on 20 May 2025. The intrusion began on 2 May 2025."
    found = idt.incident_date_from_text(text)
    assert (found.day, found.kind) == (date(2025, 5, 2), "onset")


# ---------------------------------------------------------------------------
# The detail page
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, html: str):
        self.content = html.encode()

    def raise_for_status(self):
        pass


@pytest.fixture
def source():
    return wi.WebberInsuranceDataSource(DataSourceConfig(name="WebberInsurance"), RateLimiter(), {})


def _scrape(monkeypatch, source, html, item=None, section=datetime(2025, 6, 1)):
    monkeypatch.setattr(wi.requests, "get", lambda *a, **k: _Response(html))
    return source._scrape_detail_page("https://news.example/a", section_date=section, item=item)


def test_a_publication_date_is_not_the_incident_date(monkeypatch, source):
    event = _scrape(monkeypatch, source,
                    "<html><h1>Vertel breach</h1><article>Published 1 June 2025. The breach occurred on 2 May 2025."
                    "</article></html>")
    assert event.event_date.date() == date(2025, 5, 2) and event.event_date_precision == "day"
    assert event.event_date_basis == idt.BASIS_STATED_IN_SOURCE


def test_only_a_publication_date_leaves_the_heading_month(monkeypatch, source):
    event = _scrape(monkeypatch, source,
                    "<html><h1>Vertel breach</h1><article>Published 1 June 2025. Vertel is investigating."
                    "</article></html>")
    assert event.event_date.date() == date(2025, 6, 1) and event.event_date_precision == "month"
    assert event.event_date_basis == idt.BASIS_WEBBER_HEADING


def test_a_page_about_something_else_keeps_the_lists_title_so_the_mismatch_shows(monkeypatch, source):
    item = {"text": "Red Cross clients caught up in international data breach", "organisation": "Australian Red Cross"}
    event = _scrape(monkeypatch, source, "<html><h1>Gizmodo</h1><article>Welcome to the site.</article></html>", item)
    assert event.title == item["text"]
    assert event.primary_entity.name == "Australian Red Cross"
    # A page whose heading is about the item keeps its own, fuller title.
    event = _scrape(monkeypatch, source,
                    "<html><h1>Red Cross clients caught up in international breach</h1><article>x</article></html>", item)
    assert event.title == "Red Cross clients caught up in international breach"


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

LIST_HTML = """
<html><body>
<div class="wis-data-breach-item">
  <h3>Vertel &ndash; June 2025</h3>
  <ul><li><a href="https://news.example/vertel-hit">Vertel hit by ransomware</a> | Vertel confirmed an attack.</li>
      <li><a href="https://news.example/quest">Almost 2m impacted, almost 50k credit cards compromised</a></li></ul>
</div>
<div class="wis-data-breach-item">
  <h3>Mastercom &ndash; April 2025</h3>
  <ul><li><a href="https://news.example/mastercom">Mastercom aware of INC Ransom claims</a></li>
      <li><a href="/ultimate-guide-to-cyber">Ultimate guide to cyber insurance</a></li></ul>
</div>
<div class="wpb_wrapper">
  <h3>Optus &#65533; September 2023</h3>
  <ul><li><a href="https://news.example/optus">Optus data breach: what we know</a></li></ul>
  <h3>Optus &ndash; 2019 Update</h3>
  <ul><li><a href="https://news.example/not-in-a-month">Not under a month heading</a></li></ul>
</div>
</body></html>
"""


def test_each_entry_keeps_the_link_of_its_own_heading(source):
    items = source._extract_all_event_links(BeautifulSoup(LIST_HTML, "html.parser"))
    got = [(i["organisation"], i["url"], i["section_date"]) for i in items]
    assert got == [
        ("Vertel", "https://news.example/vertel-hit", datetime(2025, 6, 1)),
        ("Vertel", "https://news.example/quest", datetime(2025, 6, 1)),         # no keyword in its text: still kept
        ("Mastercom", "https://news.example/mastercom", datetime(2025, 4, 1)),
        ("Optus", "https://news.example/optus", datetime(2023, 9, 1)),
    ]
    assert items[0]["summary"].startswith("Vertel hit by ransomware | Vertel confirmed")      # the entry's own text
    assert items[0]["text"] == "Vertel hit by ransomware"


@pytest.mark.parametrize("heading, organisation", [
    ("Vertel – June 2025", "Vertel"),
    ("Quest Apartment Hotels � August 2026", "Quest Apartment Hotels"),
    ("RMS Cloud - October 2026", "RMS Cloud"),
    ("O&G (Obstetrics and Gynaecology) � July 2025", "O&G (Obstetrics and Gynaecology)"),
])
def test_organisation_from_heading(source, heading, organisation):
    assert source._organisation_from_heading(heading) == organisation


# ---------------------------------------------------------------------------
# Stored records
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)

    def add(raw, event_date, basis, precision, content, url="https://news.example/x"):
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, raw_content, event_date, "
                  "event_date_precision, event_date_basis, source_url, discovered_at) "
                  "VALUES (?, 'WebberInsurance', 't', ?, ?, ?, ?, ?, '2026-10-28')",
                  (raw, content, event_date, precision, basis, url))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_date, status, "
                  "is_australian_event, is_specific_event) VALUES (?, ?, 't', ?, 'Active', 1, 1)",
                  (f"e{raw}", raw, event_date))

    # tied: a heading-month record whose article ties a day to the breach
    add("tied", "2025-06-01", idt.BASIS_WEBBER_HEADING, "month", "Published 1 June 2025. The breach occurred on 2 May 2025.")
    # plain: a heading-month record with only a publication date
    add("plain", "2025-06-01", idt.BASIS_WEBBER_HEADING, "month", "Published 1 June 2025. Nothing else.")
    # old: the old parser's first date in the article (a byline) stored as an exact day
    add("old", "2025-06-01", idt.BASIS_STATED_IN_SOURCE, "day", "Published 1 June 2025. Nothing else.",
        url="https://news.example/old")
    # oldtied: the old parser took the byline but the text also ties a day
    add("oldtied", "2025-06-01", idt.BASIS_STATED_IN_SOURCE, "day",
        "Published 1 June 2025. Staff noticed on 20 May 2025 that files were encrypted.", url="https://news.example/oldtied")
    c.commit()
    yield c
    c.close()


def _state(conn, raw):
    return tuple(conn.execute("SELECT event_date, event_date_precision, event_date_basis FROM RawEvents "
                              "WHERE raw_event_id = ?", (raw,)).fetchone())


def test_rederive_ties_days_and_returns_untied_ones_to_the_heading(conn):
    headings = {"https://news.example/old": date(2025, 6, 1), "https://news.example/oldtied": date(2025, 6, 1)}
    dry = idt.rederive_webber_dates(conn, dry_run=True, headings=headings)
    assert (dry.tied, dry.changed, dry.reverted, dry.unresolved) == (1, 1, 1, 0)
    assert _state(conn, "tied")[1] == "month"                                       # a dry run writes nothing

    out = idt.rederive_webber_dates(conn, headings=headings)
    assert (out.tied, out.changed, out.reverted, out.enriched) == (1, 1, 1, 3)
    assert _state(conn, "tied") == ("2025-05-02", "day", idt.BASIS_STATED_IN_SOURCE)
    assert _state(conn, "plain") == ("2025-06-01", "month", idt.BASIS_WEBBER_HEADING)
    assert _state(conn, "old") == ("2025-06-01", "month", idt.BASIS_WEBBER_HEADING)
    assert _state(conn, "oldtied") == ("2025-05-20", "day", idt.BASIS_STATED_IN_SOURCE)
    assert conn.execute("SELECT event_date FROM EnrichedEvents WHERE enriched_event_id = 'etied'").fetchone()[0] == "2025-05-02"

    again = idt.rederive_webber_dates(conn, headings=headings)                       # idempotent
    assert (again.tied, again.changed, again.reverted) == (0, 0, 0)


def test_a_day_the_old_parser_took_stays_until_its_heading_month_is_known(conn):
    out = idt.rederive_webber_dates(conn)                                            # no list: nothing to go back to
    assert out.unresolved == 1 and out.reverted == 0
    assert _state(conn, "old") == ("2025-06-01", "day", idt.BASIS_STATED_IN_SOURCE)
