"""Cut-off selection for the event-reality filter."""

from __future__ import annotations

import pytest

from scripts.calibrate_event_reality import choose_cutoff, extend_stratum, packet, wilson


def _rows(spec):
    """spec: [(p, not_incident, weight)]"""
    return [{"p": p, "not_incident": n, "weight": w} for p, n, w in spec]


def test_largest_cutoff_meeting_precision_is_chosen():
    rows = _rows([(0.01, True, 1), (0.02, True, 1), (0.03, True, 1), (0.10, True, 1),
                  (0.20, False, 1), (0.30, True, 1), (0.90, False, 20)])
    chosen, _ = choose_cutoff(rows, precision=0.95, min_rejections=1)
    # Rejecting below 0.2 is all correct; reaching 0.2 lets a real event in (4/5 = 0.8).
    assert 0.10 < chosen["cutoff"] <= 0.10 + 1e-5
    assert chosen["correct_labelled"] == 4 and chosen["weighted_precision"] == 1.0


def test_weights_count_in_precision():
    # One real event from a heavily weighted stratum outweighs several right rejections.
    rows = _rows([(0.01, True, 1), (0.02, True, 1), (0.03, False, 20)])
    chosen, table = choose_cutoff(rows, precision=0.95, min_rejections=1)
    assert chosen["cutoff"] < 0.03
    at_all = [t for t in table if t["rejected_labelled"] == 3][0]
    assert at_all["weighted_precision"] == pytest.approx(2 / 22)


def test_no_cutoff_when_precision_is_never_met():
    rows = _rows([(0.01, False, 1), (0.02, True, 1)])
    chosen, _ = choose_cutoff(rows, precision=0.95, min_rejections=1)
    assert chosen is None


def test_minimum_rejections_guards_against_a_lucky_few():
    rows = _rows([(0.01, True, 1), (0.02, True, 1)])
    chosen, _ = choose_cutoff(rows, precision=0.95, min_rejections=10)
    assert chosen is None


def test_wilson_interval():
    lo, hi = wilson(19, 20)
    assert lo == pytest.approx(0.7639, abs=1e-4) and hi == pytest.approx(0.9911, abs=1e-4)


# ----------------------------------------------------------------------
# Extending a sampled stratum: census below a final-score split, existing sample above
# ----------------------------------------------------------------------

def _sample(b_ids, band_size):
    rows = [{"deduplicated_event_id": "a1", "stratum": "A", "weight": "1.0"}]
    w = round(band_size / len(b_ids), 4)
    rows += [{"deduplicated_event_id": d, "stratum": "B", "weight": str(w)} for d in b_ids]
    rows += [{"deduplicated_event_id": "c1", "stratum": "C", "weight": "20.0"}]
    return rows


def test_extend_splits_a_stratum_into_a_census_and_the_existing_sample():
    band = [f"b{i}" for i in range(10)]
    final = {d: (0.1 if i < 4 else 0.9) for i, d in enumerate(band)}     # b0-b3 below the split
    rows, counts = extend_stratum(_sample(["b0", "b5", "b6"], 10), "B", band, final, 0.5, "new")
    by_id = {r["deduplicated_event_id"]: r for r in rows}
    # Every low member is in, at weight 1; the one already sampled is not marked new.
    low = [r for r in rows if r["stratum"] == "B-low"]
    assert sorted(r["deduplicated_event_id"] for r in low) == ["b0", "b1", "b2", "b3"]
    assert all(float(r["weight"]) == 1.0 for r in low)
    assert by_id["b0"]["added"] == "" and by_id["b1"]["added"] == "new"
    # The high part keeps only the sampled members, weighted up to its 6 events.
    high = [r for r in rows if r["stratum"] == "B-high"]
    assert sorted(r["deduplicated_event_id"] for r in high) == ["b5", "b6"]
    assert all(float(r["weight"]) == 3.0 for r in high)
    # The design still accounts for the whole band, and other strata are untouched.
    assert sum(float(r["weight"]) for r in rows if r["stratum"].startswith("B-")) == pytest.approx(10)
    assert by_id["a1"]["stratum"] == "A" and by_id["c1"]["weight"] == "20.0"
    assert counts == {"band": 10, "low": 4, "low_new": 3, "high": 6, "high_sampled": 2}


@pytest.mark.parametrize("case, message", [
    ("unscored", "no final score"),
    ("wrong_band", "imply a band"),
    ("no_high_sample", "nothing to stand for it"),
    ("split_twice", "split already"),
])
def test_extend_refuses_what_it_cannot_weight(case, message):
    band = [f"b{i}" for i in range(10)]
    final = {d: (0.1 if i < 4 else 0.9) for i, d in enumerate(band)}
    sample = _sample(["b0", "b5"], 10)
    if case == "unscored":
        del final["b7"]
    elif case == "wrong_band":
        band = band[:8]
        final = {d: final[d] for d in band}
    elif case == "no_high_sample":
        sample = _sample(["b0", "b1"], 10)
    elif case == "split_twice":
        sample, _ = extend_stratum(sample, "B", band, final, 0.5, "new")
    with pytest.raises(ValueError, match=message):
        extend_stratum(sample, "B", band, final, 0.5, "new")


def test_packet_carries_evidence_and_no_score(tmp_path):
    import sqlite3

    from cyber_data_collector.dedup import event_reality as er

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, title TEXT, event_date TEXT,
            victim_organization_name TEXT, description TEXT, summary TEXT, status TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT, raw_event_id TEXT, title TEXT,
            perplexity_enrichment_data TEXT, status TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT, source_url TEXT, raw_content TEXT, raw_description TEXT);
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT);
        INSERT INTO DeduplicatedEvents VALUES ('d1', 'Acme data breach', '2025-01-02', 'Acme',
            'Acme disclosed a breach', NULL, 'Active');
        INSERT INTO EventDeduplicationMap VALUES ('d1', 'e1');
        INSERT INTO EnrichedEvents VALUES ('e1', 'r1', 'Acme data breach',
            '{"formal_entity_name": "Acme", "overall_confidence": 0.9}', 'Active');
        INSERT INTO RawEvents VALUES ('r1', 'https://example.com/acme', 'Acme confirmed a data breach.', NULL);
    """)
    out = packet(conn, "d1")
    assert out["deduplicated_event_id"] == "d1" and out["event"]["victim"] == "Acme"
    assert out["records"][0]["page_text"].startswith("Acme confirmed")
    assert out["definition"].startswith(er.INSTRUCTIONS)
    text = str(out).lower()
    assert "probab" not in text and "score" not in text
