from datetime import date
import json
import sqlite3

import pandas as pd

from scripts.analyze_recurrent_timing import build_spells
from scripts import analyze_recurrence_extensions as runner


def test_censoring_does_not_use_future_incident():
    common = dict(entity_id='a', entity_name='A', title='event', industry='unknown',
                  entity_kind='organisation', org_size='UNKNOWN', org_size_score=0,
                  org_size_unknown=1, size_confidence=0, employee_size='unknown',
                  turnover_size='unknown', entity_kind_group='organisation',
                  sector_proxy='other', records_affected=None, records_bucket='unknown')
    events = pd.DataFrame([dict(common, event_date=date(2024, 1, 1), deduplicated_event_id='1'),
                           dict(common, event_date=date(2024, 5, 1), deduplicated_event_id='2')])
    spells, _ = build_spells(events, date(2024, 3, 1))
    row = spells.iloc[0]
    assert (row.event, row.duration_days, row.next_event_ids) == (0, 60, '')
    spells, _ = build_spells(events, date(2024, 5, 1))
    assert (spells.iloc[0].event, spells.iloc[0].next_event_ids) == (1, '2')


def test_observation_cutoff_filters_late_discovery_and_incident_dates():
    events = pd.DataFrame({'event_date': ['2024-01-01', '2024-01-02', '2024-02-01', '2024-04-01'],
                           'first_discovered_at': ['2024-01-03', '2024-04-01', None, None]})
    retained, info = runner.observed_by(events, date(2024, 3, 1))
    assert len(retained) == 2
    assert retained.discovery_date_known.tolist() == [True, False]
    assert info['excluded_first_discovered_after_cutoff'] == 1
    assert info['excluded_after_incident_cutoff'] == 1
    assert info['retained_unknown_discovery_date'] == 1


def test_snapshot_includes_committed_wal_changes(tmp_path):
    source = tmp_path / 'source.db'
    db = sqlite3.connect(source)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE sample (value INTEGER)')
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('INSERT INTO sample VALUES (42)')
        db.commit()
        snapshot = tmp_path / 'snapshot.db'
        runner.snapshot_database(source, snapshot)
        with sqlite3.connect(snapshot) as copy:
            assert copy.execute('SELECT value FROM sample').fetchone() == (42,)
        assert db.execute('SELECT value FROM sample').fetchone() == (42,)
    finally:
        db.close()


def test_runner_uses_one_snapshot_and_combines_reports(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE RawEvents (raw_event_id TEXT, discovered_at TEXT)')
        db.execute('CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, raw_event_id TEXT)')
        db.execute("INSERT INTO RawEvents VALUES ('r1', '2024-01-03')")
        db.execute("INSERT INTO EventDeduplicationMap VALUES ('e1', 'r1')")
    monkeypatch.setattr(runner, 'load_victim_events', lambda _: pd.DataFrame([
        {'deduplicated_event_id': 'e1', 'event_date': date(2024, 1, 1)},
        {'deduplicated_event_id': 'unknown', 'event_date': date(2024, 2, 1)}]))
    calls = []
    def fake_run(events, snapshot, end, out_dir, seed, **kwargs):
        calls.append((snapshot, len(events), seed, kwargs))
        assert snapshot.is_file()
        return {'section': '## Test section', 'count': len(events)}
    for module in (runner.timing_precision, runner.linkage_supplier, runner.sequence_bias):
        monkeypatch.setattr(module, 'run', fake_run)
    out = tmp_path / 'out'
    summary = runner.run_analysis(source, out, date(2024, 3, 1), bootstrap=3, date_draws=2, seed=7)
    assert len(calls) == 3 and len({item[0] for item in calls}) == 1
    assert all(item[1:] == (2, 7, {'bootstrap': 3, 'date_draws': 2, 'draws': 2}) for item in calls)
    assert summary['observation_coverage']['retained_unknown_discovery_date'] == 1
    assert (out / 'extended_recurrence_report.md').read_text(encoding='utf-8').count('## Test section') == 3
    assert json.loads((out / 'extended_analysis_summary.json').read_text(encoding='utf-8'))['seed'] == 7
