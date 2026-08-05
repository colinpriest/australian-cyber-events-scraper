"""Tests for title selection and the immutable-identity migration.

Titles are display metadata. Two things must hold: a merged event gets the best
available headline rather than whatever its master happened to carry, and
nothing in the schema treats a title as identity - otherwise titles cannot be
corrected and splits collide.
"""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import schema
from cyber_data_collector.dedup.title_selection import (
    clean_title,
    derive_title,
    score_title,
    victim_tokens,
)


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("  Qantas   cyber incident  ", "Qantas cyber incident"),
    ("Qantas data breach confirmed - iTnews", "Qantas data breach confirmed"),
    ("Medibank cyber attack update | ABC News", "Medibank cyber attack update"),
])
def test_clean_title_strips_noise(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_keeps_short_headlines_intact():
    # Stripping here would leave nothing useful, so the text stays as-is.
    assert clean_title("Optus - Telco") == "Optus - Telco"


def test_clean_title_handles_empty():
    assert clean_title(None) == ""
    assert clean_title("   ") == ""


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def test_placeholder_scores_below_everything():
    tokens = victim_tokens("HWL Ebsworth Lawyers")
    assert score_title("Untitled Event", tokens) < score_title("HWL Ebsworth data breach", tokens)


def test_naming_the_victim_scores_higher():
    tokens = victim_tokens("Qantas Airways Limited")
    named = score_title("Qantas confirms cyber incident affecting customers", tokens)
    unnamed = score_title("Scattered Spider Ransomware Attacks", tokens)
    assert named > unnamed


def test_masthead_only_titles_are_penalised():
    tokens = victim_tokens("Some Company Pty Ltd")
    assert score_title("Cyber Daily", tokens) < score_title(
        "Some Company confirms ransomware breach", tokens)


def test_incident_label_gets_a_head_start():
    tokens = victim_tokens("HWL Ebsworth Lawyers")
    label = score_title("HWL Ebsworth ransomware breach (April 2023)", tokens, True)
    plain = score_title("HWL Ebsworth ransomware breach (April 2023)", tokens, False)
    assert label > plain


def test_shouty_titles_are_penalised():
    tokens = victim_tokens("Qantas Airways Limited")
    assert score_title("QANTAS CYBER INCIDENT REPORTED TODAY", tokens) < \
           score_title("Qantas cyber incident reported today", tokens)


def test_victim_tokens_drop_generic_words():
    assert victim_tokens("Qantas Airways Limited") == {"qantas", "airways"}
    assert "australia" not in victim_tokens("Beyond Bank Australia Limited")


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------

def test_incident_label_wins_over_placeholder_master():
    """The exact HWL Ebsworth case: 43 records titled 'Untitled Event'."""
    result = derive_title(
        ["Untitled Event", "OAIC opens investigation into HWL Ebsworth"],
        victim="HWL Ebsworth Lawyers",
        incident_label="HWL Ebsworth ransomware breach (April 2023)",
        current_title="Untitled Event",
    )
    assert result == "HWL Ebsworth ransomware breach (April 2023)"


def test_best_member_title_wins_when_no_label():
    """The Qantas case: master headline never names the victim."""
    result = derive_title(
        ["Scattered Spider Ransomware Attacks",
         "Qantas confirms cyber incident impacting customer data",
         "Cyber Attack News - 7NEWS"],
        victim="Qantas Airways Limited",
        current_title="Scattered Spider Ransomware Attacks",
    )
    assert result == "Qantas confirms cyber incident impacting customer data"


def test_a_good_current_title_is_kept():
    result = derive_title(
        ["Cyber Daily", "iTnews"],
        victim="Genea Pty Ltd",
        current_title="Genea IVF Clinic data breach",
    )
    assert result == "Genea IVF Clinic data breach"


def test_derive_title_never_returns_empty_when_input_exists():
    assert derive_title(["Something happened"], victim=None) != ""


def test_derive_title_handles_all_empty_members():
    assert derive_title([None, ""], victim=None, current_title="Fallback title") \
        == "Fallback title"


# --------------------------------------------------------------------------
# Immutable identity
# --------------------------------------------------------------------------

DDL = """
CREATE TABLE DeduplicatedEvents (
    deduplicated_event_id TEXT PRIMARY KEY,
    master_enriched_event_id TEXT NOT NULL,
    title TEXT, event_date DATE, status TEXT DEFAULT 'Active',
    victim_organization_name TEXT, total_data_sources INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX idx_dedup_unique_event
    ON DeduplicatedEvents(title, event_date) WHERE status = 'Active';
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    yield c
    c.close()


def _add(c, dedup_id, master, title, date="2023-04-26"):
    c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, "
              "master_enriched_event_id, title, event_date, status) "
              "VALUES (?,?,?,?, 'Active')", (dedup_id, master, title, date))


def test_title_date_uniqueness_is_removed(conn):
    """Two distinct incidents may legitimately share a title and a date."""
    _add(conn, "d1", "e1", "Untitled Event")
    conn.commit()  # keep d1 - the rollback below must only undo the failed insert
    with pytest.raises(sqlite3.IntegrityError):
        _add(conn, "d2", "e2", "Untitled Event")
    conn.rollback()

    schema.migrate(conn)

    _add(conn, "d2", "e2", "Untitled Event")  # must now be allowed
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents").fetchone()[0] == 2


def test_identity_is_enforced_on_the_immutable_key(conn):
    schema.migrate(conn)
    _add(conn, "d1", "e1", "First title")
    with pytest.raises(sqlite3.IntegrityError):
        _add(conn, "d2", "e1", "A different title", date="2024-01-01")


def test_titles_are_freely_rewritable_after_migration(conn):
    schema.migrate(conn)
    _add(conn, "d1", "e1", "Untitled Event")
    _add(conn, "d2", "e2", "Another event", date="2023-04-26")
    # Renaming d2 onto d1s exact title+date must not raise.
    conn.execute("UPDATE DeduplicatedEvents SET title = 'Untitled Event' "
                 "WHERE deduplicated_event_id = 'd2'")
    assert conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEvents WHERE title = 'Untitled Event'"
    ).fetchone()[0] == 2


def test_migration_refuses_when_immutable_key_is_not_unique(conn):
    """Never drop the old guard without a valid replacement."""
    _add(conn, "d1", "shared", "Title A", date="2023-01-01")
    _add(conn, "d2", "shared", "Title B", date="2023-01-02")

    schema.migrate(conn)

    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_dedup_unique_master" not in indexes
    assert "idx_dedup_unique_event" in indexes, "old guard must remain"
