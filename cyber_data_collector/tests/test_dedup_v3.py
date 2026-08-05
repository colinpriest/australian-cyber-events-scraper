"""Tests for deduplication v3: resolution, adjudication, ledger and backfill.

No network access: the adjudicator is driven with an injected fake client, and
every database test runs against an in-memory SQLite built from the real
column layout.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from cyber_data_collector.dedup import schema
from cyber_data_collector.dedup.adjudicator import (
    Adjudicator,
    EventRecord,
    cosine_similarity,
    date_gap_days,
)
from cyber_data_collector.dedup.backfill import run_backfill
from cyber_data_collector.dedup.entity_resolution import (
    EntityResolver,
    blocking_keys,
    canonical_key,
    name_similarity,
)
from cyber_data_collector.dedup.ledger import DedupLedger, pair_key
from cyber_data_collector.dedup.models import (
    DecidedBy,
    DedupAction,
    LLMPairAdjudication,
    MatchEvidence,
    OverrideVerdict,
    PairVerdict,
)


# --------------------------------------------------------------------------
# Entity resolution
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("MediSecure Ltd", "medisecure"),
    ("MediSecure Limited", "medisecure"),
    ("Qantas Airways Limited", "qantas airways"),
    ("Optus Pty Limited", "optus"),
])
def test_canonical_key_strips_legal_suffixes(raw, expected):
    assert canonical_key(raw) == expected


def test_canonical_key_never_reduces_to_a_generic_fragment():
    """Regression: an earlier draft reduced this to 'co'.

    That key would have merged every unrelated company whose name ends in
    "Co Pty Ltd", which is far worse than missing a merge.
    """
    key = canonical_key("Nissan Motor Co. (Australia) Pty Ltd")
    assert "nissan" in key
    assert key != "co"


def test_canonical_key_handles_pure_suffix_input():
    # Must not return an empty key and collapse unrelated records together.
    assert canonical_key("Pty Ltd") != ""


@pytest.mark.parametrize("left, right", [
    ("MediSecure", "MediSecure Ltd"),
    ("Qantas", "Qantas Airways Limited"),
    ("Latitude Financial Services Limited", "Latitude Group Holdings Limited"),
])
def test_variants_of_one_org_are_candidates(left, right):
    assert EntityResolver().are_candidates(left, right)


@pytest.mark.parametrize("left, right", [
    ("Qantas Airways Limited", "Virgin Australia Airlines"),
    ("Acme Group Holdings Ltd", "Zenith Group Holdings Ltd"),
    ("Nissan Motor Co Pty Ltd", "Brown Motor Co Pty Ltd"),
])
def test_distinct_orgs_are_not_candidates(left, right):
    assert not EntityResolver().are_candidates(left, right)


def test_fit_suppresses_common_tokens_but_keeps_rare_ones():
    """Corpus frequency, not a hand-curated stopword list, decides blocking."""
    corpus = [f"University of Town{i}" for i in range(40)]
    corpus += ["Singtel Optus Pty Limited", "Optus Pty Limited"]
    resolver = EntityResolver()
    resolver.fit(corpus)

    # 'university' is everywhere -> must not bucket unrelated universities.
    assert not resolver.are_candidates("University of Town1", "University of Town2")
    # 'optus' is rare -> variants still find each other.
    assert resolver.are_candidates("Singtel Optus Pty Limited", "Optus Pty Limited")


def test_name_similarity_rewards_containment():
    assert name_similarity("Qantas", "Qantas Airways") > 0.9
    assert name_similarity("Qantas", "Virgin Australia") == 0.0


def test_blocking_keys_exclude_generic_tokens():
    assert "group" not in blocking_keys("Acme Group Holdings Ltd")


# --------------------------------------------------------------------------
# Database fixture
# --------------------------------------------------------------------------

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
CREATE TABLE DeduplicatedEventEntities (
    deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL, source_count INTEGER,
    UNIQUE(deduplicated_event_id, entity_id)
);
CREATE TABLE EntitiesV2 (
    entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT,
    industry TEXT, is_australian BOOLEAN
);
"""


def _add_event(conn, key: str, title: str, url: str, victim: str = "Acme Ltd",
               date: str = "2025-06-01") -> tuple:
    raw_id, enr_id, dedup_id = f"raw-{key}", f"enr-{key}", f"ded-{key}"
    conn.execute(
        "INSERT INTO RawEvents (raw_event_id, source_type, raw_description, "
        "source_url, discovered_at) VALUES (?,?,?,?,?)",
        (raw_id, "Perplexity", f"desc {title}", url, "2025-06-02"),
    )
    conn.execute(
        "INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, "
        "summary, event_date) VALUES (?,?,?,?,?)",
        (enr_id, raw_id, title, f"summary {title}", date),
    )
    conn.execute(
        "INSERT INTO DeduplicatedEvents (deduplicated_event_id, "
        "master_enriched_event_id, title, event_date, victim_organization_name, "
        "status, total_data_sources) VALUES (?,?,?,?,?, 'Active', 0)",
        (dedup_id, enr_id, title, date, victim),
    )
    return raw_id, enr_id, dedup_id


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(BASE_DDL)
    schema.migrate(connection)
    yield connection
    connection.close()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def test_migration_is_idempotent(conn):
    schema.migrate(conn)
    schema.migrate(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"DedupDecisions", "DedupOverrides", "EntityAliases",
            "DedupSnapshots", "DedupCalibration"} <= tables


def test_migration_adds_columns_to_deduplicated_events(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    assert {"dedup_certainty", "dedup_method", "dedup_batch_id",
            "has_human_override"} <= cols


# --------------------------------------------------------------------------
# Backfill - the 2a/2b defects
# --------------------------------------------------------------------------

def test_backfill_creates_lineage_for_singletons(conn):
    """Singletons previously got no EventDeduplicationMap row at all."""
    _add_event(conn, "a", "Acme breach", "https://x/1")
    assert conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap").fetchone()[0] == 0

    report = run_backfill(conn)

    assert report["master_rows_added"] == 1
    assert report["events_without_lineage"] == 0
    row = conn.execute("SELECT * FROM EventDeduplicationMap").fetchone()
    assert row["contribution_type"] == "master"


def test_backfill_populates_sources_and_counts(conn):
    _add_event(conn, "a", "Acme breach", "https://x/1")
    run_backfill(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEventSources").fetchone()[0] == 1
    # total_data_sources must never remain 0 for an event with contributors.
    assert conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEvents WHERE total_data_sources = 0"
    ).fetchone()[0] == 0


def test_backfill_is_idempotent(conn):
    _add_event(conn, "a", "Acme breach", "https://x/1")
    run_backfill(conn)
    second = run_backfill(conn)
    assert second["master_rows_added"] == 0
    assert second["source_rows_added"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEventSources").fetchone()[0] == 1


def test_backfill_dry_run_changes_nothing(conn):
    _add_event(conn, "a", "Acme breach", "https://x/1")
    run_backfill(conn, dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap").fetchone()[0] == 0


# --------------------------------------------------------------------------
# Ledger: reversibility
# --------------------------------------------------------------------------

def test_merge_then_split_restores_separation(conn):
    """A merge must be undoable without re-running the pipeline."""
    _, enr_a, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    _, enr_b, ded_b = _add_event(conn, "b", "Acme incident", "https://x/2")
    run_backfill(conn)
    ledger = DedupLedger(conn)

    ledger.merge_events(ded_a, ded_b, reason="same incident")
    members = conn.execute(
        "SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
        (ded_a,)).fetchone()[0]
    assert members == 2
    assert conn.execute(
        "SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id=?",
        (ded_b,)).fetchone()[0] == "Merged"

    new_id = ledger.split_member(ded_a, enr_b, reason="actually different")

    assert new_id != ded_a
    assert conn.execute(
        "SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
        (ded_a,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
        (new_id,)).fetchone()[0] == 1


def test_split_updates_source_counts(conn):
    _, enr_a, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    _, enr_b, ded_b = _add_event(conn, "b", "Acme incident", "https://x/2")
    run_backfill(conn)
    ledger = DedupLedger(conn)
    ledger.merge_events(ded_a, ded_b, reason="same")
    assert conn.execute(
        "SELECT total_data_sources FROM DeduplicatedEvents WHERE deduplicated_event_id=?",
        (ded_a,)).fetchone()[0] == 2

    ledger.split_member(ded_a, enr_b, reason="undo")
    assert conn.execute(
        "SELECT total_data_sources FROM DeduplicatedEvents WHERE deduplicated_event_id=?",
        (ded_a,)).fetchone()[0] == 1


def test_split_records_an_auditable_decision(conn):
    _, enr_a, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    _, enr_b, ded_b = _add_event(conn, "b", "Acme incident", "https://x/2")
    run_backfill(conn)
    ledger = DedupLedger(conn)
    ledger.merge_events(ded_a, ded_b, reason="same")
    ledger.split_member(ded_a, enr_b, reason="reviewer disagreed")

    decisions = conn.execute(
        "SELECT action, decided_by, reasoning FROM DedupDecisions "
        "WHERE enriched_event_id = ? ORDER BY created_at", (enr_b,)).fetchall()
    assert any(d["action"] == DedupAction.REVERT.value for d in decisions)
    assert any("reviewer disagreed" in (d["reasoning"] or "") for d in decisions)


def test_snapshot_restore_returns_previous_membership(conn):
    _, enr_a, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    _, enr_b, ded_b = _add_event(conn, "b", "Acme incident", "https://x/2")
    run_backfill(conn)
    ledger = DedupLedger(conn)
    ledger.merge_events(ded_a, ded_b, reason="same")

    snapshot_id = ledger.snapshot_event("batch-x", ded_a)
    ledger.split_member(ded_a, enr_b, reason="undo")
    assert conn.execute(
        "SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id=?",
        (ded_a,)).fetchone()[0] == 1

    ledger.restore_snapshot(snapshot_id)
    assert conn.execute(
        "SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id=?",
        (ded_a,)).fetchone()[0] == 2


def test_merge_into_self_is_rejected(conn):
    _, _, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    with pytest.raises(ValueError):
        DedupLedger(conn).merge_events(ded_a, ded_a, reason="nonsense")


def test_split_non_member_is_rejected(conn):
    _, _, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    run_backfill(conn)
    with pytest.raises(ValueError):
        DedupLedger(conn).split_member(ded_a, "enr-nonexistent", reason="x")


# --------------------------------------------------------------------------
# Overrides + learning
# --------------------------------------------------------------------------

def test_override_roundtrip(conn):
    ledger = DedupLedger(conn)
    ledger.add_override("enr-a", "enr-b", OverrideVerdict.DIFFERENT, reason="no")
    overrides = ledger.load_overrides()
    assert overrides[frozenset(("enr-a", "enr-b"))] is False


def test_override_is_order_independent_and_upsertable(conn):
    ledger = DedupLedger(conn)
    ledger.add_override("enr-a", "enr-b", OverrideVerdict.DIFFERENT)
    ledger.add_override("enr-b", "enr-a", OverrideVerdict.SAME)
    assert conn.execute("SELECT COUNT(*) FROM DedupOverrides").fetchone()[0] == 1
    assert ledger.load_overrides()[frozenset(("enr-a", "enr-b"))] is True


def test_override_survives_a_dedup_rebuild(conn):
    """The whole point of keying on enriched ids rather than dedup ids."""
    _, enr_a, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    _, enr_b, ded_b = _add_event(conn, "b", "Acme incident", "https://x/2")
    ledger = DedupLedger(conn)
    ledger.add_override(enr_a, enr_b, OverrideVerdict.DIFFERENT, reason="distinct")

    # Simulate a rebuild: dedup rows are wiped and regenerated with new ids.
    conn.execute("DELETE FROM DeduplicatedEvents")
    conn.execute("DELETE FROM EventDeduplicationMap")
    for key, enr in (("a2", enr_a), ("b2", enr_b)):
        conn.execute(
            "INSERT INTO DeduplicatedEvents (deduplicated_event_id, "
            "master_enriched_event_id, title, status) VALUES (?,?,?, 'Active')",
            (f"ded-{uuid.uuid4()}", enr, "rebuilt"),
        )

    assert ledger.load_overrides()[frozenset((enr_a, enr_b))] is False


def test_learning_creates_alias_for_differently_named_same_events(conn):
    """A human saying 'same' teaches blocking to pair the names next time.

    Entities are attached through EnrichedEventEntities -> EntitiesV2, matching
    the production schema (EnrichedEvents has no victim column).
    """
    _, enr_a, _ = _add_event(conn, "a", "Breach A", "https://x/1", victim="Winterfell Pty Ltd")
    _, enr_b, _ = _add_event(conn, "b", "Breach B", "https://x/2", victim="Stark Holdings Ltd")
    for entity_id, (enriched_id, name) in enumerate(
        ((enr_a, "Winterfell Pty Ltd"), (enr_b, "Stark Holdings Ltd")), start=1
    ):
        conn.execute("INSERT INTO EntitiesV2 (entity_id, entity_name) VALUES (?,?)",
                     (entity_id, name))
        conn.execute(
            "INSERT INTO EnrichedEventEntities (enriched_event_id, entity_id, "
            "relationship_type, confidence_score) VALUES (?,?, 'affected', 0.9)",
            (enriched_id, entity_id),
        )

    ledger = DedupLedger(conn)
    ledger.add_override(enr_a, enr_b, OverrideVerdict.SAME, reason="same incident")
    resolver = EntityResolver(conn)
    assert not resolver.are_candidates("Winterfell Pty Ltd", "Stark Holdings Ltd")

    result = ledger.learn_from_overrides(resolver)

    assert result["aliases_learned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM EntityAliases").fetchone()[0] == 1
    # A freshly loaded resolver now pairs them.
    assert EntityResolver(conn).are_candidates("Winterfell Pty Ltd", "Stark Holdings Ltd")


def test_calibration_tightens_when_humans_reject_merges(conn):
    ledger = DedupLedger(conn)
    for i in range(6):
        ledger.add_override(f"l{i}", f"r{i}", OverrideVerdict.DIFFERENT)
    result = ledger.learn_from_overrides()
    assert result["false_merges"] == 6
    assert result["merge_threshold"] > 0.85


def test_calibration_loosens_when_humans_add_merges(conn):
    ledger = DedupLedger(conn)
    for i in range(6):
        ledger.add_override(f"l{i}", f"r{i}", OverrideVerdict.SAME)
    result = ledger.learn_from_overrides()
    assert result["missed_merges"] == 6
    assert result["merge_threshold"] < 0.85


# --------------------------------------------------------------------------
# Ancestry
# --------------------------------------------------------------------------

def test_ancestry_reports_members_and_decisions(conn):
    _, enr_a, ded_a = _add_event(conn, "a", "Acme breach", "https://x/1")
    _, enr_b, ded_b = _add_event(conn, "b", "Acme incident", "https://x/2")
    run_backfill(conn)
    ledger = DedupLedger(conn)
    ledger.merge_events(ded_a, ded_b, reason="same incident")

    tree = ledger.ancestry(ded_a)
    assert len(tree["members"]) == 2
    assert any("same incident" in (d["reasoning"] or "") for d in tree["decisions"])
    assert {m["source_url"] for m in tree["members"]} == {"https://x/1", "https://x/2"}


# --------------------------------------------------------------------------
# Adjudicator
# --------------------------------------------------------------------------

def _rec(key, title, entity, date="2025-06-01", url=None) -> EventRecord:
    return EventRecord(enriched_event_id=key, title=title, entity_name=entity,
                       event_date=date, source_url=url)


class _FakeClient:
    """Stands in for the instructor-wrapped OpenAI client."""

    def __init__(self, response: LLMPairAdjudication):
        self._response = response
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                outer.last_kwargs = kwargs
                return outer._response

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_human_override_beats_everything(conn):
    verdict_pair = frozenset(("a", "b"))
    adj = Adjudicator(overrides={verdict_pair: False})
    result = adj.adjudicate(_rec("a", "Acme breach", "Acme Ltd"),
                            _rec("b", "Acme breach", "Acme Ltd"))
    assert result.is_same_event is False
    assert result.decided_by == DecidedBy.HUMAN
    assert result.certainty == 1.0


def test_shared_url_is_conclusive():
    adj = Adjudicator()
    result = adj.adjudicate(
        _rec("a", "Totally different wording", "Acme Ltd", url="https://x/1"),
        _rec("b", "Other wording entirely", "Acme Ltd", url="https://x/1"),
    )
    assert result.is_same_event is True
    assert result.decided_by == DecidedBy.RULE
    assert result.certainty >= 0.99


def test_entity_mismatch_rejected_without_llm():
    fake = _FakeClient(LLMPairAdjudication(
        is_same_event=True, certainty=1.0, reasoning="should never be called"))
    adj = Adjudicator(openai_client=fake)
    result = adj.adjudicate(_rec("a", "Breach", "Qantas Airways Limited"),
                            _rec("b", "Breach", "Virgin Australia Airlines"))
    assert result.is_same_event is False
    assert fake.calls == 0, "entity mismatch must short-circuit before any LLM spend"


def test_llm_verdict_is_used_and_carries_reasoning():
    fake = _FakeClient(LLMPairAdjudication(
        is_same_event=True, certainty=0.93,
        reasoning="Both describe the June 2025 Acme ransomware incident.",
        supporting_facts=["same victim", "same month"],
        distinguishing_facts=[],
    ))
    adj = Adjudicator(openai_client=fake)
    result = adj.adjudicate(_rec("a", "Acme hit by ransomware", "Acme Ltd"),
                            _rec("b", "Acme confirms data theft", "Acme Ltd"))
    assert fake.calls == 1
    assert result.is_same_event is True
    assert result.decided_by == DecidedBy.LLM
    assert "June 2025" in result.reasoning
    assert result.evidence.supporting_facts == ["same victim", "same month"]


def test_entity_sets_overlap_even_when_primary_names_differ():
    """Regression: the 'primary' entity is often a regulator, not the victim.

    EnrichedEventEntities tags all 23,388 links 'affected' with no
    victim/regulator distinction, so comparing one name apiece judged two
    reports of the same Optus breach to be different organisations - at 0.90
    confidence. Comparing the whole set fixes that.
    """
    fake = _FakeClient(LLMPairAdjudication(
        is_same_event=True, certainty=0.9, reasoning="same incident"))
    adj = Adjudicator(openai_client=fake)

    left = EventRecord(enriched_event_id="a", title="Optus breach",
                       entity_name="Australian Privacy Commissioner",
                       alt_entities=["Optus Pty Limited"], event_date="2022-09-20")
    right = EventRecord(enriched_event_id="b", title="Optus data breach",
                        entity_name="Federal Court",
                        alt_entities=["Singtel Optus Pty Limited"], event_date="2022-09-22")

    result = adj.adjudicate(left, right)
    assert adj.stats["rejected_early"] == 0, "shared Optus entity must not be missed"
    assert fake.calls == 1
    assert result.is_same_event is True


def test_require_entity_match_false_sends_mismatches_to_the_llm():
    """Reviewing already-grouped events must judge on content, not the label."""
    fake = _FakeClient(LLMPairAdjudication(
        is_same_event=False, certainty=1.0,
        reasoning="Record A is the 2015-2019 White Pages breach; B is the 2022 breach.",
        distinguishing_facts=["different years", "different record counts"],
    ))
    adj = Adjudicator(openai_client=fake, require_entity_match=False)

    result = adj.adjudicate(
        _rec("a", "White Pages privacy breach", "Australian Privacy Commissioner"),
        _rec("b", "2022 Optus data breach", "Optus Pty Limited"),
    )
    assert fake.calls == 1, "entity mismatch must not short-circuit in this mode"
    assert result.is_same_event is False
    assert "White Pages" in result.reasoning


def test_require_entity_match_true_still_short_circuits():
    fake = _FakeClient(LLMPairAdjudication(
        is_same_event=True, certainty=1.0, reasoning="never called"))
    adj = Adjudicator(openai_client=fake, require_entity_match=True)
    adj.adjudicate(_rec("a", "Breach", "Qantas Airways Limited"),
                   _rec("b", "Breach", "Virgin Australia Airlines"))
    assert fake.calls == 0


def test_cosine_similarity_is_clamped_for_identical_vectors():
    """Rounding pushed identical vectors to 1.0000000000000002, which failed
    the [-1, 1] bound on MatchEvidence and aborted the entire review run on the
    first exact duplicate it met.
    """
    vector = [0.1, 0.2, 0.3] * 100
    assert cosine_similarity(vector, vector) <= 1.0
    assert cosine_similarity(vector, [-v for v in vector]) >= -1.0
    # And the value must survive model validation.
    MatchEvidence(embedding_similarity=cosine_similarity(vector, vector))


def test_llm_failure_defaults_to_keeping_events_separate():
    class _Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("api down")

    adj = Adjudicator(openai_client=_Boom())
    result = adj.adjudicate(_rec("a", "Acme breach", "Acme Ltd"),
                            _rec("b", "Acme incident", "Acme Ltd"))
    assert result.is_same_event is False, "a failure must not silently merge"
    assert adj.stats["llm_failures"] == 1


def test_candidate_pairs_are_blocked_not_quadratic():
    records = [_rec(f"e{i}", f"Org{i} breach", f"Org{i} Pty Ltd") for i in range(60)]
    records.append(_rec("dup", "Org7 second report", "Org7 Limited"))
    pairs = Adjudicator().candidate_pairs(records)
    assert len(pairs) < 60  # vs 1,830 for all-pairs
    assert any({l.enriched_event_id, r.enriched_event_id} == {"e7", "dup"}
               for l, r in pairs)


def test_cosine_similarity_edges():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([], [1]) == 0.0
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


def test_date_gap():
    assert date_gap_days(_rec("a", "t", "E", "2025-06-01"),
                         _rec("b", "t", "E", "2025-06-11")) == 10
    assert date_gap_days(_rec("a", "t", "E", None),
                         _rec("b", "t", "E", "2025-06-11")) is None


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

def test_verdict_requires_reasoning():
    with pytest.raises(Exception):
        PairVerdict(is_same_event=True, certainty=0.9, reasoning="   ")


def test_verdict_confidence_flag():
    assert PairVerdict(is_same_event=True, certainty=0.9, reasoning="x").is_confident
    assert not PairVerdict(is_same_event=True, certainty=0.5, reasoning="x").is_confident


def test_evidence_summary_line_is_human_readable():
    evidence = MatchEvidence(
        entity_canonical_left="qantas", entity_canonical_right="qantas",
        entity_match=True, date_delta_days=2, embedding_similarity=0.91,
    )
    line = evidence.summary_line()
    assert "qantas" in line and "2d apart" in line and "0.91" in line


def test_pair_key_is_order_independent():
    assert pair_key("b", "a") == pair_key("a", "b")
