"""Tests for entity-role staleness detection.

Roles are assigned per event and depend on which records the event contains,
so any change of membership invalidates them. Staleness is *derived* from a
signature of the members rather than set by a flag, because a flag only works
while every current and future mutation path remembers to raise it - and this
pipeline has several (merge, split, consolidate, legacy review, backfill).
"""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import schema
from cyber_data_collector.dedup.ledger import DedupLedger
from cyber_data_collector.dedup.role_maintenance import (
    DeferredRoleRefresher,
    invalidate,
    invalidate_all,
    mark_classified,
    member_signature,
    signature_for_members,
    stale_event_ids,
    staleness_report,
)

BASE_DDL = """
CREATE TABLE RawEvents (
    raw_event_id TEXT PRIMARY KEY,
    source_type TEXT, raw_title TEXT, raw_description TEXT, raw_content TEXT,
    event_date DATE, source_url TEXT, discovered_at TIMESTAMP
);
CREATE TABLE EnrichedEvents (
    enriched_event_id TEXT PRIMARY KEY,
    raw_event_id TEXT, title TEXT, description TEXT, summary TEXT,
    event_type TEXT, severity TEXT, event_date DATE, records_affected BIGINT
);
CREATE TABLE DeduplicatedEvents (
    deduplicated_event_id TEXT PRIMARY KEY,
    master_enriched_event_id TEXT NOT NULL,
    title TEXT, description TEXT, summary TEXT, event_type TEXT, severity TEXT,
    event_date DATE, records_affected BIGINT,
    is_australian_event BOOLEAN DEFAULT 1, is_specific_event BOOLEAN DEFAULT 1,
    confidence_score REAL, status TEXT DEFAULT 'Active',
    created_at TIMESTAMP, updated_at TIMESTAMP,
    victim_organization_name TEXT, victim_organization_industry TEXT,
    total_data_sources INTEGER DEFAULT 0
);
CREATE TABLE DeduplicatedEventSources (
    deduplicated_event_id TEXT, source_url TEXT, source_type TEXT,
    credibility_score REAL, content_snippet TEXT, discovered_at TIMESTAMP
);
CREATE TABLE EventDeduplicationMap (
    map_id TEXT PRIMARY KEY, raw_event_id TEXT, enriched_event_id TEXT,
    deduplicated_event_id TEXT, contribution_type TEXT,
    similarity_score REAL, data_source_weight REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE EnrichedEventEntities (
    enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL
);
CREATE TABLE EntitiesV2 (
    entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT,
    industry TEXT, is_australian BOOLEAN
);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(BASE_DDL)
    schema.migrate(c)
    yield c
    c.close()


def _add_event(conn, key, title="Event", date="2025-06-01"):
    raw, enr, ded = f"raw-{key}", f"enr-{key}", f"ded-{key}"
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_url) VALUES (?,?)",
                 (raw, f"https://x/{key}"))
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, "
                 "title, event_date) VALUES (?,?,?,?)", (enr, raw, title, date))
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, "
                 "master_enriched_event_id, title, event_date, status) "
                 "VALUES (?,?,?,?, 'Active')", (ded, enr, title, date))
    conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, "
                 "enriched_event_id, deduplicated_event_id, contribution_type) "
                 "VALUES (?,?,?,?, 'master')", (f"map-{key}", raw, enr, ded))
    return ded, enr


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------

def test_signature_is_order_independent():
    """Re-storing the same members in a different order is not a change."""
    assert signature_for_members(["a", "b", "c"]) == signature_for_members(["c", "a", "b"])


def test_signature_changes_with_membership():
    assert signature_for_members(["a", "b"]) != signature_for_members(["a", "b", "c"])


def test_signature_of_empty_membership_is_stable():
    assert signature_for_members([]) == signature_for_members([])


def test_signature_ignores_blank_ids():
    assert signature_for_members(["a", None, ""]) == signature_for_members(["a"])


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

def test_unclassified_event_is_stale(conn):
    ded, _ = _add_event(conn, "a")
    assert ded in stale_event_ids(conn)


def test_unclassified_can_be_excluded(conn):
    ded, _ = _add_event(conn, "a")
    assert ded not in stale_event_ids(conn, include_unclassified=False)


def test_marking_classified_clears_staleness(conn):
    ded, _ = _add_event(conn, "a")
    mark_classified(conn, ded)
    assert ded not in stale_event_ids(conn)


def test_merge_invalidates_roles(conn):
    """The capability this module exists for."""
    target, _ = _add_event(conn, "a", "Acme breach")
    source, _ = _add_event(conn, "b", "Acme incident")
    mark_classified(conn, target)
    mark_classified(conn, source)
    assert stale_event_ids(conn, include_unclassified=False) == []

    DedupLedger(conn).merge_events(target, source, reason="same incident")

    assert target in stale_event_ids(conn), \
        "folding members in must invalidate the roles decided for the old set"


def test_split_invalidates_roles(conn):
    target, _ = _add_event(conn, "a", "Acme breach")
    source, moved = _add_event(conn, "b", "Acme incident")
    ledger = DedupLedger(conn)
    ledger.merge_events(target, source, reason="same")
    mark_classified(conn, target)
    assert target not in stale_event_ids(conn)

    ledger.split_member(target, moved, reason="actually different")

    assert target in stale_event_ids(conn)


def test_unaffected_events_stay_current(conn):
    """Invalidation must be targeted, not global."""
    target, _ = _add_event(conn, "a")
    source, _ = _add_event(conn, "b")
    untouched, _ = _add_event(conn, "c")
    for e in (target, source, untouched):
        mark_classified(conn, e)

    DedupLedger(conn).merge_events(target, source, reason="same")

    stale = stale_event_ids(conn, include_unclassified=False)
    assert target in stale
    assert untouched not in stale


def test_reclassifying_after_a_merge_clears_it(conn):
    target, _ = _add_event(conn, "a")
    source, _ = _add_event(conn, "b")
    mark_classified(conn, target)
    DedupLedger(conn).merge_events(target, source, reason="same")
    assert target in stale_event_ids(conn)

    mark_classified(conn, target)

    assert target not in stale_event_ids(conn, include_unclassified=False)


def test_member_signature_matches_stored_after_marking(conn):
    ded, _ = _add_event(conn, "a")
    stored = mark_classified(conn, ded)
    assert stored == member_signature(conn, ded)


# --------------------------------------------------------------------------
# Reporting and manual invalidation
# --------------------------------------------------------------------------

def test_staleness_report_counts_each_state(conn):
    current, _ = _add_event(conn, "a")
    outdated, _ = _add_event(conn, "b")
    extra, _ = _add_event(conn, "c")
    _add_event(conn, "d")  # never classified

    mark_classified(conn, current)
    mark_classified(conn, outdated)
    mark_classified(conn, extra)
    DedupLedger(conn).merge_events(outdated, extra, reason="same")

    report = staleness_report(conn)
    assert report["outdated"] == 1
    assert report["never_classified"] == 1
    assert report["active"] == report["current"] + report["outdated"] + report["never_classified"]


def test_merged_away_events_are_not_reported(conn):
    """Only active events matter; a merged-away row is not pending work."""
    target, _ = _add_event(conn, "a")
    source, _ = _add_event(conn, "b")
    mark_classified(conn, target)
    mark_classified(conn, source)
    DedupLedger(conn).merge_events(target, source, reason="same")

    assert source not in stale_event_ids(conn)
    assert staleness_report(conn)["active"] == 1


def test_invalidate_forces_a_single_event_stale(conn):
    ded, _ = _add_event(conn, "a")
    mark_classified(conn, ded)
    invalidate(conn, ded)
    assert ded in stale_event_ids(conn)


def test_invalidate_all_marks_every_active_event(conn):
    """Used when the classifier itself changes, not when membership does."""
    for key in ("a", "b", "c"):
        ded, _ = _add_event(conn, key)
        mark_classified(conn, ded)
    assert stale_event_ids(conn, include_unclassified=False) == []

    invalidate_all(conn)

    assert len(stale_event_ids(conn)) == 3


# --------------------------------------------------------------------------
# Automatic refresh on merge / split
# --------------------------------------------------------------------------

class _RecordingRefresher:
    """Stands in for DeferredRoleRefresher without touching an LLM."""

    def __init__(self):
        self.seen = []

    def __call__(self, dedup_id):
        self.seen.append(dedup_id)


def test_merge_schedules_a_refresh(conn):
    """A merge must schedule its own refresh - no follow-up command."""
    target, _ = _add_event(conn, "a")
    source, _ = _add_event(conn, "b")
    refresher = _RecordingRefresher()

    DedupLedger(conn, role_refresher=refresher).merge_events(
        target, source, reason="same incident")

    assert refresher.seen == [target]


def test_split_schedules_both_sides(conn):
    """A split changes two events: one loses a record, one is created."""
    target, _ = _add_event(conn, "a")
    source, moved = _add_event(conn, "b")
    ledger = DedupLedger(conn)
    ledger.merge_events(target, source, reason="same")

    refresher = _RecordingRefresher()
    ledger.role_refresher = refresher
    new_id = ledger.split_member(target, moved, reason="different")

    assert target in refresher.seen
    assert new_id in refresher.seen


def test_no_refresher_is_harmless(conn):
    """Opting out must not break the mutation itself."""
    target, _ = _add_event(conn, "a")
    source, _ = _add_event(conn, "b")
    DedupLedger(conn, role_refresher=None).merge_events(
        target, source, reason="same")
    assert conn.execute(
        "SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id=?",
        (target,)).fetchone()[0] == 2


def test_a_failing_refresher_never_breaks_the_merge(conn):
    """The merge is the user's intent; refreshing is a follow-on convenience."""
    def boom(_):
        raise RuntimeError("classifier exploded")

    target, _ = _add_event(conn, "a")
    source, _ = _add_event(conn, "b")
    DedupLedger(conn, role_refresher=boom).merge_events(
        target, source, reason="same")

    assert conn.execute(
        "SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id=?",
        (source,)).fetchone()[0] == "Merged"


def test_deferred_refresher_deduplicates_chained_merges(conn):
    """Consolidating a cluster folds B, C, D into A; A is classified once."""
    target, _ = _add_event(conn, "a")
    others = [_add_event(conn, k)[0] for k in ("b", "c", "d")]
    refresher = DeferredRoleRefresher()
    ledger = DedupLedger(conn, role_refresher=refresher)

    for source in others:
        ledger.merge_events(target, source, reason="same cluster")

    assert refresher.stats["scheduled"] == 3
    assert refresher.pending == [target], "one entry, not one per merge"


def test_deferred_refresher_skips_events_merged_away_later(conn):
    """An event folded into another after being scheduled needs no refresh."""
    first, _ = _add_event(conn, "a")
    second, _ = _add_event(conn, "b")
    third, _ = _add_event(conn, "c")
    refresher = DeferredRoleRefresher()
    ledger = DedupLedger(conn, role_refresher=refresher)

    ledger.merge_events(first, second, reason="same")   # schedules `first`
    ledger.merge_events(third, first, reason="chain")   # `first` merged away

    stats = refresher.flush(conn)
    assert stats["skipped"] >= 1
    assert stats["failed"] == 0


# --------------------------------------------------------------------------
# Splitting the record that defines the event
# --------------------------------------------------------------------------

def test_splitting_the_master_promotes_a_replacement(conn):
    """The master id is the identity key, so it cannot simply be removed."""
    # Distinct titles so the split takes the new-event path rather than
    # reattaching to an existing event with the same title and date.
    target, master = _add_event(conn, "a", title="Acme breach")
    source, other = _add_event(conn, "b", title="Acme follow-up", date="2025-07-02")
    ledger = DedupLedger(conn)
    ledger.merge_events(target, source, reason="same")

    new_id = ledger.split_member(target, master, reason="wrongly merged")

    remaining = conn.execute(
        "SELECT master_enriched_event_id FROM DeduplicatedEvents "
        "WHERE deduplicated_event_id = ?", (target,)).fetchone()[0]
    assert remaining == other, "a surviving member must take over as master"
    assert new_id != target


def test_splitting_a_sole_member_is_refused(conn):
    """Removing the only record would leave an event describing nothing."""
    target, master = _add_event(conn, "a")
    with pytest.raises(ValueError, match="only member"):
        DedupLedger(conn).split_member(target, master, reason="nonsense")
