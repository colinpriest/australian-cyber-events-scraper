"""Tests for ordinal entity size estimation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup.entity_size import (
    EntitySize,
    EntitySizeResearcher,
    SizeEstimate,
    _derive_band,
    band_from_employees,
    band_from_revenue,
    estimate_sizes,
    normalise_name,
    pending_entities,
    rule_size,
    store_estimate,
)


# ----------------------------------------------------------------------
# Band boundaries
# ----------------------------------------------------------------------

@pytest.mark.parametrize("count,expected", [
    (0, EntitySize.SMALL),
    (19, EntitySize.SMALL),
    (20, EntitySize.MEDIUM),      # ABS small/medium boundary
    (199, EntitySize.MEDIUM),
    (200, EntitySize.LARGE),      # ABS medium/large boundary
    (4_999, EntitySize.LARGE),
    (5_000, EntitySize.HUGE),
    (250_000, EntitySize.HUGE),
])
def test_band_from_employees(count, expected):
    assert band_from_employees(count) == expected


def test_band_from_employees_without_a_figure():
    assert band_from_employees(None) is None


@pytest.mark.parametrize("amount,expected", [
    (9_999_999, EntitySize.SMALL),
    (10_000_000, EntitySize.MEDIUM),
    (100_000_000, EntitySize.LARGE),
    (1_000_000_000, EntitySize.HUGE),
])
def test_band_from_revenue(amount, expected):
    assert band_from_revenue(amount) == expected


def test_size_bands_are_ordinal():
    ranks = [EntitySize.SMALL.rank, EntitySize.MEDIUM.rank,
             EntitySize.LARGE.rank, EntitySize.HUGE.rank]
    assert ranks == sorted(ranks)
    # UNKNOWN sorts last rather than between MEDIUM and LARGE.
    assert EntitySize.UNKNOWN.rank > EntitySize.HUGE.rank


# ----------------------------------------------------------------------
# Rules: what has no size at all
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name,kind", [
    ("Canvas Learning Management System", "product"),
    ("Scattered Spider", "threat_actor"),
    ("Jane Citizen", "person"),
    ("Australian Emails and Addresses", "other"),
])
def test_non_organisations_are_unknown_without_research(name, kind):
    assert rule_size(name, kind) == EntitySize.UNKNOWN


def test_blank_name_is_unknown():
    assert rule_size("   ", None) == EntitySize.UNKNOWN


def test_real_organisations_are_left_for_research():
    # None means "no rule applies" - the researcher must look it up.
    assert rule_size("Medibank Private Limited", "organisation") is None
    assert rule_size("Services Australia", "government_body") is None


def test_collective_nouns_have_no_size():
    """"Australian hospitals" names a group, not an organisation."""
    assert rule_size("Australian hospitals", "organisation") == EntitySize.UNKNOWN


# ----------------------------------------------------------------------
# Reconciling a stated band with the figures reported alongside it
# ----------------------------------------------------------------------

def test_reported_headcount_overrides_a_mislabelled_band():
    estimate = SizeEstimate(size=EntitySize.LARGE, employees=45_000,
                            confidence=0.9, basis="About 45,000 staff.")
    assert _derive_band(estimate) == EntitySize.HUGE


def test_band_is_kept_when_no_figures_were_found():
    estimate = SizeEstimate(size=EntitySize.MEDIUM, confidence=0.6,
                            basis="Mid-sized regional firm.")
    assert _derive_band(estimate) == EntitySize.MEDIUM


def test_unknown_is_never_upgraded_by_a_stray_figure():
    """A figure found while the organisation itself stayed unidentified.

    Several organisations share the name; the caution is the finding.
    """
    estimate = SizeEstimate(size=EntitySize.UNKNOWN, employees=8_000,
                            confidence=0.2,
                            basis="Three companies share this name.")
    assert _derive_band(estimate) == EntitySize.UNKNOWN


def test_revenue_decides_when_no_headcount_is_given():
    estimate = SizeEstimate(size=EntitySize.SMALL, revenue_aud=4_000_000_000.0,
                            confidence=0.8, basis="Revenue A$4bn.")
    assert _derive_band(estimate) == EntitySize.HUGE


# ----------------------------------------------------------------------
# Name normalisation: one lookup per organisation, not per spelling
# ----------------------------------------------------------------------

def test_variant_spellings_share_a_lookup_key():
    keys = {normalise_name(n) for n in
            ["Optus", "Optus Pty Ltd", "Singtel Optus Pty Limited"]}
    # "Singtel Optus" keeps its distinguishing word, so it is not required to
    # collapse; what matters is that the pure suffix variants do.
    assert normalise_name("Optus") == normalise_name("Optus Pty Ltd")
    assert len(keys) < 3


def test_normalisation_keeps_distinguishing_words():
    assert normalise_name("Medibank") != normalise_name("Latitude Financial")


def test_normalisation_never_empties_a_name():
    """A name made only of stopwords must still key to something."""
    assert normalise_name("The Company") != ""


# ----------------------------------------------------------------------
# Database round trip
# ----------------------------------------------------------------------

@pytest.fixture
def conn():
    from cyber_data_collector.dedup import schema

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE EntitiesV2 (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_name VARCHAR(255),
            entity_type VARCHAR(50),
            industry VARCHAR(100),
            turnover VARCHAR(50),
            employee_count INTEGER,
            is_australian BOOLEAN,
            entity_kind TEXT,
            updated_at TIMESTAMP
        );
        CREATE TABLE DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT,
            title TEXT,
            status VARCHAR(20)
        );
        CREATE TABLE DeduplicatedEventEntities (
            deduplicated_event_id TEXT,
            entity_id INTEGER,
            relationship_type VARCHAR(50)
        );
        """
    )
    schema.migrate(connection)
    yield connection
    connection.close()


def _add_entity(conn, name, kind="organisation", industry=None):
    cur = conn.execute(
        "INSERT INTO EntitiesV2 (entity_name, entity_kind, industry, "
        "is_australian) VALUES (?, ?, ?, 1)", (name, kind, industry))
    return cur.lastrowid


def test_migration_adds_the_size_columns(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(EntitiesV2)")}
    assert {"size_estimate", "size_confidence", "size_employees",
            "size_revenue_aud", "size_basis", "size_method", "size_sources",
            "size_estimated_at"} <= columns


def test_migration_is_idempotent(conn):
    from cyber_data_collector.dedup import schema

    schema.migrate(conn)
    schema.migrate(conn)  # must not raise "duplicate column name"


def test_store_and_read_back(conn):
    entity_id = _add_entity(conn, "Medibank Private Limited")
    store_estimate(conn, entity_id, {
        "size": EntitySize.HUGE, "employees": 4_000, "revenue_aud": 7.1e9,
        "confidence": 0.92, "method": "perplexity", "basis": "ASX-100 insurer.",
        "sources": ["https://example.com/annual-report"],
    })
    row = conn.execute("SELECT * FROM EntitiesV2 WHERE entity_id = ?",
                       (entity_id,)).fetchone()
    assert row["size_estimate"] == "HUGE"
    assert row["size_method"] == "perplexity"
    assert json.loads(row["size_sources"]) == ["https://example.com/annual-report"]
    # The legacy column is populated too, so the analyses that already read
    # employee_count start seeing data without being changed.
    assert row["employee_count"] == 4_000


def test_pending_skips_already_estimated(conn):
    done = _add_entity(conn, "Telstra Corporation Limited")
    todo = _add_entity(conn, "Bob's Panel Beating")
    store_estimate(conn, done, {"size": EntitySize.HUGE, "confidence": 0.9,
                                "method": "perplexity", "basis": "x",
                                "sources": []})
    names = {r["entity_name"] for r in pending_entities(conn)}
    assert names == {"Bob's Panel Beating"}
    assert len(pending_entities(conn, refresh=True)) == 2


def test_unavailable_rows_are_retried_without_refresh(conn):
    """`unavailable` records that research could not be reached, not an answer."""
    entity_id = _add_entity(conn, "Some Regional Council")
    store_estimate(conn, entity_id, {
        "size": EntitySize.UNKNOWN, "confidence": 0.0, "method": "unavailable",
        "basis": "No research available.", "sources": []})
    assert [r["entity_id"] for r in pending_entities(conn)] == [entity_id]


def test_human_rulings_are_never_overwritten(conn):
    entity_id = _add_entity(conn, "Corrected By Hand Pty Ltd")
    store_estimate(conn, entity_id, {"size": EntitySize.SMALL, "confidence": 1.0,
                                     "method": "human", "basis": "Checked.",
                                     "sources": []})
    assert pending_entities(conn) == []
    assert pending_entities(conn, refresh=True) == []


def test_linked_only_excludes_orphan_entities(conn):
    linked = _add_entity(conn, "Attacked Org")
    _add_entity(conn, "Never Mentioned Org")
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, "
                 "title, status) VALUES ('d1', 'Breach', 'Active')")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES "
                 "('d1', ?, 'victim')", (linked,))
    names = {r["entity_name"] for r in pending_entities(conn, linked_only=True)}
    assert names == {"Attacked Org"}


# ----------------------------------------------------------------------
# The batch runner, with the network stubbed out
# ----------------------------------------------------------------------

class StubResearcher(EntitySizeResearcher):
    """Records what it was asked about; answers from a fixed table."""

    def __init__(self, answers):
        super().__init__(perplexity_key="stub", openai_client=object())
        self.answers = answers
        self.asked = []

    def estimate(self, name, industry=None, is_australian=None,
                 entity_kind=None, incident_hint=None):
        self.asked.append(name)
        ruled = rule_size(name, entity_kind)
        if ruled is not None:
            return {"size": ruled, "employees": None, "revenue_aud": None,
                    "confidence": 0.9, "method": "rule", "sources": [],
                    "basis": "not an organisation"}
        return self.answers.get(name, {
            "size": EntitySize.UNKNOWN, "confidence": 0.0,
            "method": "unavailable", "basis": "not found", "sources": []})


def test_batch_stores_every_entity(conn):
    _add_entity(conn, "Big Bank Ltd")
    _add_entity(conn, "Tiny Cafe")
    researcher = StubResearcher({
        "Big Bank Ltd": {"size": EntitySize.HUGE, "confidence": 0.9,
                         "method": "perplexity", "basis": "b", "sources": []},
        "Tiny Cafe": {"size": EntitySize.SMALL, "confidence": 0.7,
                      "method": "perplexity", "basis": "c", "sources": []},
    })
    stats = estimate_sizes(conn, pending_entities(conn), researcher=researcher,
                           workers=2)
    assert stats["stored"] == 2
    assert stats["bands"] == {"HUGE": 1, "SMALL": 1}
    bands = dict(conn.execute(
        "SELECT entity_name, size_estimate FROM EntitiesV2"))
    assert bands == {"Big Bank Ltd": "HUGE", "Tiny Cafe": "SMALL"}


def test_one_lookup_serves_every_spelling_variant(conn):
    for name in ["Optus", "Optus Pty Ltd"]:
        _add_entity(conn, name)
    researcher = StubResearcher({
        "Optus": {"size": EntitySize.HUGE, "confidence": 0.95,
                  "method": "perplexity", "basis": "telco", "sources": []},
        "Optus Pty Ltd": {"size": EntitySize.SMALL, "confidence": 0.1,
                          "method": "perplexity", "basis": "wrong", "sources": []},
    })
    stats = estimate_sizes(conn, pending_entities(conn), researcher=researcher,
                           workers=1)
    assert stats["lookups"] == 1
    assert stats["reused"] == 1
    assert len(researcher.asked) == 1
    bands = {r[0] for r in conn.execute("SELECT size_estimate FROM EntitiesV2")}
    assert bands == {"HUGE"}


def test_products_and_attackers_cost_no_lookup(conn):
    _add_entity(conn, "Canvas Learning Management System", kind="product")
    _add_entity(conn, "ALPHV/BlackCat", kind="threat_actor")
    researcher = StubResearcher({})
    stats = estimate_sizes(conn, pending_entities(conn), researcher=researcher,
                           workers=1)
    assert stats["bands"] == {"UNKNOWN": 2}
    methods = {r[0] for r in conn.execute("SELECT size_method FROM EntitiesV2")}
    assert methods == {"rule"}


def test_dry_run_writes_nothing(conn):
    _add_entity(conn, "Big Bank Ltd")
    researcher = StubResearcher({
        "Big Bank Ltd": {"size": EntitySize.HUGE, "confidence": 0.9,
                         "method": "perplexity", "basis": "b", "sources": []}})
    stats = estimate_sizes(conn, pending_entities(conn), researcher=researcher,
                           dry_run=True)
    assert stats["stored"] == 0
    assert conn.execute(
        "SELECT size_estimate FROM EntitiesV2").fetchone()[0] is None


def test_one_failing_entity_does_not_abort_the_batch(conn):
    _add_entity(conn, "Explodes On Lookup")
    _add_entity(conn, "Works Fine Ltd")

    class Exploding(StubResearcher):
        def estimate(self, name, **kwargs):
            if name == "Explodes On Lookup":
                raise RuntimeError("network died")
            return super().estimate(name, **kwargs)

    researcher = Exploding({
        "Works Fine Ltd": {"size": EntitySize.MEDIUM, "confidence": 0.8,
                           "method": "perplexity", "basis": "m", "sources": []}})
    stats = estimate_sizes(conn, pending_entities(conn), researcher=researcher,
                           workers=2)
    assert stats["stored"] == 1
    assert stats["bands"] == {"MEDIUM": 1}


def test_progress_failure_does_not_discard_paid_lookups(conn):
    """A backfill is an hour of paid research; printing must not be able to end it.

    An organisation's researched blurb carried a non-breaking hyphen, which
    Windows' cp1252 console could not encode, and the UnicodeEncodeError killed
    a run holding ~1,100 uncommitted lookups.
    """
    _add_entity(conn, "Awkward Characters Pty Ltd")
    _add_entity(conn, "Second Org Ltd")
    researcher = StubResearcher({
        "Awkward Characters Pty Ltd": {
            "size": EntitySize.MEDIUM, "confidence": 0.8, "method": "perplexity",
            "basis": "Non‑breaking hyphen.", "sources": []},
        "Second Org Ltd": {"size": EntitySize.SMALL, "confidence": 0.6,
                           "method": "perplexity", "basis": "s", "sources": []},
    })

    def exploding_progress(name, result):
        raise UnicodeEncodeError("charmap", "x", 0, 1, "unencodable")

    stats = estimate_sizes(conn, pending_entities(conn), researcher=researcher,
                           workers=1, progress=exploding_progress)
    assert stats["stored"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM EntitiesV2 WHERE size_estimate IS NOT NULL"
    ).fetchone()[0] == 2


def test_progress_is_committed_along_the_way(conn):
    """Work must reach disk as it completes, not only at the very end.

    The commit was guarded by ``stored % 25 == 0``, and because one lookup can
    cover several rows ``stored`` steps over the multiples - so it almost never
    fired and a crash lost everything.
    """
    # Seven spelling variants per organisation, so each lookup stores seven
    # rows at once and `stored` advances 7, 14, 21, 28 ... - never landing on a
    # multiple of 25. That is the shape a real table has, and it is what made
    # the old condition fire only at the very end.
    suffixes = ["", " Pty Ltd", " Limited", " Group", " Holdings", " Co",
                " Corporation"]
    answers = {}
    for i in range(20):
        for suffix in suffixes:
            _add_entity(conn, f"Org Number {i}{suffix}")
        answers[f"Org Number {i}"] = {
            "size": EntitySize.SMALL, "confidence": 0.7,
            "method": "perplexity", "basis": "b", "sources": []}

    class CountingConnection:
        """Delegates everything, tallying commits. sqlite3.Connection.commit
        is read-only, so it cannot be patched in place."""

        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.commits = 0

        def commit(self):
            self.commits += 1
            self._wrapped.commit()

        def __getattr__(self, item):
            return getattr(self._wrapped, item)

    counting = CountingConnection(conn)
    stats = estimate_sizes(counting, pending_entities(conn),
                           researcher=StubResearcher(answers), workers=1)
    assert stats["stored"] == 140
    # 140 rows at a 25-row interval: five interim commits plus the final one.
    # The old condition managed exactly one - the final flush.
    assert counting.commits >= 4


def test_no_perplexity_key_yields_unavailable_not_a_guess(monkeypatch):
    """Without research the answer must be 'not yet known', never a guess."""
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    researcher = EntitySizeResearcher(perplexity_key=None,
                                      openai_client=object())
    result = researcher.estimate("Some Unknown Pty Ltd",
                                 entity_kind="organisation")
    assert result["size"] == EntitySize.UNKNOWN
    assert result["method"] == "unavailable"
    assert result["confidence"] == 0.0
