from datetime import date
import json
import sqlite3

import pandas as pd

from scripts.recurrence_extended.linkage_supplier import run


def _database(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
            CREATE TABLE DedupOverrides (left_enriched_event_id TEXT, right_enriched_event_id TEXT, verdict TEXT, active INTEGER);
            CREATE TABLE ModelJudgements (subject_type TEXT, subject_id TEXT, question_id TEXT, value REAL, created_at TEXT);
        ''')
        db.executemany('INSERT INTO EventDeduplicationMap VALUES (?, ?)', [(f'r{i}', f'e{i}') for i in range(1, 6)])
        db.executemany('INSERT INTO DedupOverrides VALUES (?, ?, ?, ?)', [
            ('r1', 'r2', 'same', 1), ('r2', 'r3', 'same', 1),
            ('r1', 'r3', 'different', 1), ('r4', 'r5', 'same', 0),
        ])
        db.executemany('INSERT INTO ModelJudgements VALUES (?, ?, ?, ?, ?)', [
            ('event_pair', 'e1|e3', 'same_incident', 0.98, '2026-01-01'),
            ('event_pair', 'e4|e5', 'same_incident', 0.95, '2026-01-01'),
            ('event_victim', 'e1|B', 'is_recorded_victim', 0.1, '2026-01-01'),
            ('event_victim', 'e1|A', 'is_recorded_victim', 0.1, '2025-01-01'),
            ('event_victim', 'e1|A', 'is_recorded_victim', 0.9, '2026-01-01'),
        ])


def _events():
    return pd.DataFrame([
        ('A', 'e1', '2025-01-01', 'Supplier X'),
        ('B', 'e1', '2025-01-01', 'Supplier X'),
        ('A', 'e2', '2025-03-02', None),
        ('A', 'e3', '2025-06-10', None),
        ('B', 'e4', '2025-04-10', None),
        ('C', 'e5', '2025-02-01', None),
        ('C', 'e6', '2025-07-01', None),
        ('D', 'e7', '2025-08-01', None),
        ('D', 'future', '2026-02-01', None),
    ], columns=['entity_id', 'deduplicated_event_id', 'event_date', 'vendor_organization_name'])


def test_evidence_scenarios_and_shared_components(tmp_path):
    db_path = tmp_path / 'db.sqlite'
    _database(db_path)
    out = tmp_path / 'output'
    result = run(_events(), db_path, date(2026, 1, 1), out, 23, bootstrap=80)
    json.dumps(result, allow_nan=False)
    scenarios = {s['scenario']: s for s in result['scenarios']}
    assert scenarios['baseline']['victim_attributions'] == 8
    assert scenarios['baseline']['incidents'] == 7
    assert scenarios['active_same_overrides']['decisions_applied'] == 1
    assert result['blocked_merges']['active_same_overrides'] == 1
    assert scenarios['active_same_overrides']['incidents'] == 6
    assert scenarios['model_same_probability_ge_0.9']['decisions_applied'] == 1
    assert result['blocked_merges']['model_same_probability_ge_0.9'] == 1
    assert scenarios['model_victim_probability_lt_0.5_excluded']['victim_attributions'] == 7
    assert scenarios['model_victim_probability_lt_0.5_excluded']['incidents'] == 7
    assert result['shared_incidents'] == 1
    assert result['shared_victims'] == 2
    assert result['components'] == 3
    shared = pd.read_csv(out / 'shared_incident_dependencies.csv')
    assert shared.iloc[0]['victim_entity_pairs'] == 1
    assert bool(shared.iloc[0]['supplier_recorded'])
    bootstrap = pd.read_csv(out / 'recurrence_cluster_bootstrap.csv')
    assert set(bootstrap.groupby('resampling_unit').units.first().items()) == {('entity', 4), ('shared_incident_component', 3)}
    second = tmp_path / 'second'
    run(_events(), db_path, date(2026, 1, 1), second, 23, bootstrap=80)
    pd.testing.assert_frame_equal(bootstrap, pd.read_csv(second / 'recurrence_cluster_bootstrap.csv'))
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT count(*) FROM DedupOverrides').fetchone()[0] == 4
        assert db.execute('SELECT count(*) FROM ModelJudgements').fetchone()[0] == 5


def test_absent_evidence_empty_cohort_and_zero_bootstrap(tmp_path):
    db_path = tmp_path / 'empty.sqlite'
    sqlite3.connect(db_path).close()
    result = run(_events().iloc[:0], db_path, date(2026, 1, 1), tmp_path / 'out', 4, bootstrap=0)
    json.dumps(result, allow_nan=False)
    assert result['components'] == 0
    assert all(s['status'] == 'evidence table unavailable' for s in result['scenarios'][1:])
    boot = pd.read_csv(tmp_path / 'out' / 'recurrence_cluster_bootstrap.csv')
    assert boot.valid_replicates.eq(0).all()
    assert boot.estimate.isna().all()
