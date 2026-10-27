"""Evidence-based linkage sensitivity and shared-incident uncertainty.

All scenarios operate on copies of the analysis cohort; the database is read only.
"""
from __future__ import annotations

from contextlib import closing
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

SIZE_GROUPS = ('SMALL', 'MEDIUM', 'LARGE', 'HUGE', 'UNKNOWN')
TOTAL_COLUMNS = ['early_days', 'late_days', 'early_events', 'late_events']


class _Union:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def root(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a, b):
        a, b = self.root(a), self.root(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _prepare(events, censor_date):
    frame = events.copy()
    frame['event_date'] = pd.to_datetime(frame['event_date'], errors='coerce')
    frame = frame.dropna(subset=['entity_id', 'deduplicated_event_id', 'event_date'])
    frame = frame.loc[frame.event_date <= pd.Timestamp(censor_date)].copy()
    frame['entity_id'] = frame.entity_id.astype(str)
    frame['deduplicated_event_id'] = frame.deduplicated_event_id.astype(str)
    size = frame.get('org_size', frame.get('size_estimate', pd.Series(index=frame.index, dtype=object)))
    frame['size_group'] = size.fillna('UNKNOWN').astype(str).str.strip().str.upper()
    frame['size_group'] = frame['size_group'].where(frame['size_group'].isin(SIZE_GROUPS), 'UNKNOWN')
    # Use one current size per entity even if inconsistent values occur on
    # individual event rows; conflicts are deliberately classified UNKNOWN.
    groups = frame.groupby('entity_id')['size_group'].transform(lambda x: x.iloc[0] if x.nunique() == 1 else 'UNKNOWN')
    frame['size_group'] = groups
    return frame.sort_values('event_date').drop_duplicates(['entity_id', 'deduplicated_event_id'])


def _entity_totals(frame, censor_date):
    """Same-day events are one observation, matching the original spell analysis."""
    totals = []
    end = pd.Timestamp(censor_date)
    for entity, group in frame.groupby('entity_id', sort=True):
        dates = sorted(group.event_date.unique())
        early_days = late_days = early_events = late_events = 0
        for i, d in enumerate(dates):
            start = pd.Timestamp(d)
            if start >= end:
                continue
            observed = i + 1 < len(dates)
            stop = pd.Timestamp(dates[i + 1]) if observed else end
            duration = (stop - start).days
            early_days += min(duration, 90)
            late_days += max(0, min(duration - 90, 90))
            early_events += int(observed and duration <= 90)
            late_events += int(observed and 90 < duration <= 180)
        totals.append((entity, group['size_group'].iloc[0], early_days, late_days, early_events, late_events))
    return pd.DataFrame(totals, columns=['entity_id', 'size_group'] + TOTAL_COLUMNS)


def _rates(values):
    early_days, late_days, early_events, late_events = values
    early = float(early_events / early_days * 36525) if early_days else None
    late = float(late_events / late_days * 36525) if late_days else None
    ratio = float(late / early) if early and late is not None else None
    return early, late, ratio


def _summary(frame, censor_date, scenario, decision_count, status):
    totals = _entity_totals(frame, censor_date)
    values = totals[['early_days', 'late_days', 'early_events', 'late_events']].sum().tolist()
    early, late, ratio = _rates(values)
    return {'scenario': scenario, 'status': status, 'decisions_applied': decision_count,
            'victim_attributions': len(frame), 'incidents': frame.deduplicated_event_id.nunique(),
            'entities': frame.entity_id.nunique(), 'repeat_intervals_0_180': int(totals.early_events.sum() + totals.late_events.sum()),
            'days_0_90': int(values[0]), 'days_91_180': int(values[1]),
            'events_0_90': int(values[2]), 'events_91_180': int(values[3]),
            'rate_0_90_per_100_years': early, 'rate_91_180_per_100_years': late, 'rate_ratio': ratio}


def _has_columns(connection, table, required):
    rows = connection.execute('SELECT name FROM sqlite_master WHERE type = ? AND name = ?', ('table', table)).fetchall()
    if not rows:
        return False
    return set(required).issubset({r[1] for r in connection.execute('PRAGMA table_info("' + table + '")')})


def _evidence(db_path, ids):
    # as_uri handles spaces and Windows drive letters; mode=ro prevents creation or writes.
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        overrides_available = (_has_columns(db, 'DedupOverrides', ['left_enriched_event_id', 'right_enriched_event_id', 'verdict', 'active']) and
                               _has_columns(db, 'EventDeduplicationMap', ['enriched_event_id', 'deduplicated_event_id']))
        overrides = []
        if overrides_available:
            overrides = db.execute('''SELECT o.verdict, a.deduplicated_event_id, b.deduplicated_event_id
                FROM DedupOverrides o JOIN EventDeduplicationMap a ON a.enriched_event_id=o.left_enriched_event_id
                JOIN EventDeduplicationMap b ON b.enriched_event_id=o.right_enriched_event_id
                WHERE o.active=1 AND o.verdict IN ('same','different')''').fetchall()
        judgement_available = _has_columns(db, 'ModelJudgements', ['subject_type', 'subject_id', 'question_id', 'value', 'created_at'])
        judgments = []
        if judgement_available:
            judgments = db.execute('''SELECT subject_type, subject_id, question_id, value, created_at, rowid
                FROM ModelJudgements WHERE (subject_type='event_pair' AND question_id='same_incident')
                OR (subject_type='event_victim' AND question_id='is_recorded_victim')
                ORDER BY created_at, rowid''').fetchall()
    different, same, within = set(), set(), set()
    for verdict, a, b in overrides:
        if a is None or b is None:
            continue
        a, b = str(a), str(b)
        if a not in ids or b not in ids:
            continue
        if a == b:
            if verdict == 'different':
                within.add(a)
        elif verdict == 'different':
            different.add(tuple(sorted((a, b))))
        else:
            same.add(tuple(sorted((a, b))))
    latest = {}
    for kind, subject, question, value, _, _ in judgments:
        latest[(kind, subject, question)] = value
    candidates, low_victims, scored_pairs, scored_victims = set(), set(), set(), set()
    for (kind, subject, _), value in latest.items():
        if not isinstance(subject, str) or '|' not in subject:
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score) or not 0 <= score <= 1:
            continue
        a, b = subject.split('|', 1)
        if kind == 'event_pair' and a in ids and b in ids and a != b:
            pair = tuple(sorted((a, b)))
            scored_pairs.add(pair)
            if score >= 0.9:
                candidates.add(pair)
        if kind == 'event_victim' and a in ids:
            scored_victims.add((a, b))
            if score < 0.5:
                low_victims.add((a, b))
    return overrides_available, judgement_available, same, different, within, candidates, low_victims, scored_pairs, scored_victims


def _merge_scenario(frame, pairs, forbidden):
    ids = sorted(frame.deduplicated_event_id.unique())
    uf = _Union(ids)
    applied = blocked = 0
    for a, b in sorted(pairs):
        ra, rb = uf.root(a), uf.root(b)
        if ra == rb:
            continue
        left = {x for x in ids if uf.root(x) == ra}
        right = {x for x in ids if uf.root(x) == rb}
        if any(tuple(sorted((x, y))) in forbidden for x in left for y in right):
            blocked += 1
            continue
        uf.join(a, b)
        applied += 1
    result = frame.copy()
    result['deduplicated_event_id'] = result.deduplicated_event_id.map(uf.root)
    result['event_date'] = result.groupby('deduplicated_event_id').event_date.transform('min')
    return result.sort_values('event_date').drop_duplicates(['entity_id', 'deduplicated_event_id']), applied, blocked


def _shared(frame):
    uf = _Union(sorted(frame.entity_id.unique()))
    rows = []
    for event, group in frame.groupby('deduplicated_event_id', sort=True):
        entities = sorted(group.entity_id.unique())
        if len(entities) < 2:
            continue
        for entity in entities[1:]:
            uf.join(entities[0], entity)
        suppliers = (group['vendor_organization_name'].fillna('').astype(str).str.strip().ne('').any()
                     if 'vendor_organization_name' in group.columns else False)
        rows.append({'deduplicated_event_id': event, 'victim_entities': len(entities),
                     'victim_entity_pairs': len(entities) * (len(entities) - 1) // 2,
                     'supplier_recorded': bool(suppliers)})
    return uf, pd.DataFrame(rows, columns=['deduplicated_event_id', 'victim_entities', 'victim_entity_pairs', 'supplier_recorded'])


def _bootstrap(totals, uf, repetitions, seed):
    columns = TOTAL_COLUMNS
    results = []
    for name in ('entity', 'shared_incident_component'):
        work = totals.copy()
        work['unit'] = work.entity_id if name == 'entity' else work.entity_id.map(uf.root)
        units = work.groupby('unit', sort=True)[columns].sum().to_numpy(dtype=float)
        point = _rates(units.sum(axis=0)) if len(units) else (None, None, None)
        draws = [[], [], []]
        rng = np.random.default_rng(seed)
        for _ in range(repetitions if len(units) else 0):
            sampled = units[rng.integers(0, len(units), size=len(units))].sum(axis=0)
            for target, value in zip(draws, _rates(sampled)):
                if value is not None and math.isfinite(value):
                    target.append(value)
        for metric, value, samples in zip(('rate_0_90_per_100_years', 'rate_91_180_per_100_years', 'rate_ratio'), point, draws):
            low, high = (map(float, np.percentile(samples, [2.5, 97.5])) if samples else (None, None))
            results.append({'resampling_unit': name, 'units': len(units), 'metric': metric,
                            'estimate': value, 'ci_2_5': low, 'ci_97_5': high,
                            'valid_replicates': len(samples), 'requested_replicates': repetitions})
    return pd.DataFrame(results)


def _size_weights(totals):
    """Fixed reference: distribution of unique baseline organisations."""
    counts = totals['size_group'].value_counts()
    return {size: float(counts.get(size, 0) / len(totals)) for size in SIZE_GROUPS} if len(totals) else {}


def _standardized(aggregates, weights):
    """Directly standardize both rates to the same baseline size mix.

    Fail closed if any positively weighted stratum lacks exposure. Zero events
    are valid rates; an undefined ratio or a bootstrap draw with no exposure is
    never silently substituted with an unadjusted estimate.
    """
    if sum(weights.get(s, 0) > 0 for s in SIZE_GROUPS if s != 'UNKNOWN') < 2:
        return None, None, None, 'insufficient known size variation'
    early, late = 0., 0.
    for size, weight in weights.items():
        if not weight:
            continue
        values = aggregates.get(size)
        if values is None or values[0] <= 0 or values[1] <= 0:
            return None, None, None, f'no early or late exposure in {size}'
        early += weight * values[2] / values[0] * 36525
        late += weight * values[3] / values[1] * 36525
    return early, late, late / early if early > 0 else None, 'estimated'


def _standardized_rows(totals, weights, scenario):
    grouped = totals.groupby('size_group')[TOTAL_COLUMNS].sum()
    aggregated = {s: row.to_numpy(dtype=float) for s, row in grouped.iterrows()}
    early, late, ratio, status = _standardized(aggregated, weights)
    result = {'scenario': scenario, 'status': status, 'standardized_rate_0_90': early,
              'standardized_rate_91_180': late, 'standardized_rate_ratio': ratio}
    strata = []
    for size in SIZE_GROUPS:
        group = totals[totals['size_group'] == size]
        values = group[TOTAL_COLUMNS].sum().to_numpy(dtype=float)
        rates = _rates(values)
        strata.append({'scenario': scenario, 'size_group': size,
                       'reference_weight': weights.get(size, 0.), 'entities': len(group),
                       **dict(zip(TOTAL_COLUMNS, values.tolist())),
                       'rate_0_90': rates[0], 'rate_91_180': rates[1], 'rate_ratio': rates[2]})
    return result, strata


def _standardized_bootstrap(totals, uf, weights, repetitions, seed):
    """Resample entities or connected components, retaining size composition.

    The weights stay fixed, so changes in sample size mix do not change the
    estimand. Units spanning multiple sizes are kept intact in component draws.
    """
    output = []
    for unit_name in ('entity', 'shared_incident_component'):
        frame = totals.copy()
        frame['unit'] = frame.entity_id if unit_name == 'entity' else frame.entity_id.map(uf.root)
        units = sorted(frame['unit'].unique())
        grouped = frame.groupby(['unit', 'size_group'])[TOTAL_COLUMNS].sum()
        arr = np.zeros((len(units), len(SIZE_GROUPS), 4), dtype=float)
        for i, unit in enumerate(units):
            for j, size in enumerate(SIZE_GROUPS):
                if (unit, size) in grouped.index:
                    arr[i, j] = grouped.loc[(unit, size)].to_numpy(dtype=float)
        def evaluate(values):
            return _standardized({s: values[j] for j, s in enumerate(SIZE_GROUPS)}, weights)
        point = evaluate(arr.sum(axis=0))
        draws = [[], [], []]
        rng = np.random.default_rng(seed)
        for _ in range(repetitions if units else 0):
            sampled = arr[rng.integers(0, len(units), size=len(units))].sum(axis=0)
            for target, value in zip(draws, evaluate(sampled)[:3]):
                if value is not None and math.isfinite(value):
                    target.append(value)
        for metric, estimate, samples in zip(('rate_0_90', 'rate_91_180', 'rate_ratio'), point[:3], draws):
            low, high = (map(float, np.percentile(samples, [2.5, 97.5])) if samples else (None, None))
            output.append({'resampling_unit': unit_name, 'units': len(units), 'metric': metric,
                           'estimate': estimate, 'ci_2_5': low, 'ci_97_5': high,
                           'valid_replicates': len(samples), 'requested_replicates': repetitions,
                           'status': point[3]})
    return pd.DataFrame(output)


def run(events, db_path, censor_date, out_dir, seed, **kwargs):
    """Write linkage, shared-event, and bootstrap CSVs; return a report section."""
    frame = _prepare(events, censor_date)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    repetitions = int(kwargs.get('bootstrap', 200))
    if repetitions < 0:
        raise ValueError('bootstrap must be non-negative')
    ids = set(frame.deduplicated_event_id)
    overrides_ok, judgments_ok, same, different, within, candidates, low_victims, scored_pairs, scored_victims = _evidence(db_path, ids)
    scenarios = [_summary(frame, censor_date, 'baseline', 0, 'observed cohort')]
    frames = {'baseline': frame}
    blocked_counts = {}
    for name, pairs, available in [('active_same_overrides', same, overrides_ok),
                                   ('model_same_probability_ge_0.9', candidates, judgments_ok and overrides_ok)]:
        if not available:
            pairs = set()
        modified, count, blocked = _merge_scenario(frame, pairs, different)
        blocked_counts[name] = blocked
        status = ('evidence table unavailable' if not available else
                  'no applicable candidate decisions' if not pairs else 'evaluated')
        scenarios.append(_summary(modified, censor_date, name, count, status))
        frames[name] = modified
    keys = list(zip(frame.deduplicated_event_id, frame.entity_id))
    keep = [pair not in low_victims for pair in keys]
    removed = len(frame) - sum(keep)
    status = ('evidence table unavailable' if not judgments_ok else
              'no applicable low-probability attributions' if not removed else 'evaluated')
    scenarios.append(_summary(frame.loc[keep], censor_date, 'model_victim_probability_lt_0.5_excluded', removed, status))
    frames['model_victim_probability_lt_0.5_excluded'] = frame.loc[keep]
    uf, shared = _shared(frame)
    totals = _entity_totals(frame, censor_date)
    cluster = _bootstrap(totals, uf, repetitions, seed)
    weights = _size_weights(totals)
    standardized, strata = [], []
    for name, cohort in frames.items():
        adjusted, rows = _standardized_rows(_entity_totals(cohort, censor_date), weights, name)
        standardized.append(adjusted)
        strata.extend(rows)
    size_bootstrap = _standardized_bootstrap(totals, uf, weights, repetitions, seed)
    scenario_df = pd.DataFrame(scenarios)
    scenario_df.to_csv(out_dir / 'linkage_sensitivity.csv', index=False)
    shared.to_csv(out_dir / 'shared_incident_dependencies.csv', index=False)
    cluster.to_csv(out_dir / 'recurrence_cluster_bootstrap.csv', index=False)
    pd.DataFrame(standardized).to_csv(out_dir / 'linkage_size_standardized.csv', index=False)
    pd.DataFrame(strata).to_csv(out_dir / 'linkage_size_strata.csv', index=False)
    size_bootstrap.to_csv(out_dir / 'recurrence_size_standardized_bootstrap.csv', index=False)
    components = defaultdict(int)
    for entity in uf.parent:
        components[uf.root(entity)] += 1
    largest = max(components.values(), default=0)
    shared_victims = int(frame[frame.deduplicated_event_id.isin(shared.deduplicated_event_id)].entity_id.nunique())
    lines = ['### Linkage and shared-incident sensitivity', '',
             'The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 1–90. Each rate is directly standardised to the same baseline distribution of organisation sizes, so differing size composition between scenarios and time bands is accounted for.', '',
             f"Baseline size weights: {weights}. UNKNOWN is a separate stratum. Organisation size is a current recorded category, not a complete measurement of exposure, target value or reporting visibility.", '',
             '| Scenario | Evidence status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Size-standardised ratio | Adjustment status |',
             '|---|---|---:|---:|---:|---:|---:|---:|---|']
    for s, adjusted in zip(scenarios, standardized):
        ratio = 'n/a' if adjusted['standardized_rate_ratio'] is None else f"{adjusted['standardized_rate_ratio']:.2f}"
        lines.append(f"| {s['scenario']} | {s['status']} | {s['decisions_applied']} | {s['victim_attributions']} | {s['incidents']} | {s['events_0_90']} / {s['days_0_90']} | {s['events_91_180']} / {s['days_91_180']} | {ratio} | {adjusted['status']} |")
    in_cohort_keys = set(keys)
    lines += ['', f"Stored scores cover {len(scored_pairs)} in-cohort incident pairs and {len(scored_victims & in_cohort_keys)} victim attributions. Active different-incident overrides vetoed {blocked_counts['active_same_overrides']} override merges and {blocked_counts['model_same_probability_ge_0.9']} model-candidate merges, including conflicts created transitively. {len(within)} current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.",
              'Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.', '',
              f"{len(shared)} recorded incidents have multiple attributed victims, involving {shared_victims} distinct victims and {int(shared.victim_entity_pairs.sum())} event-level victim pairs; {int(shared.supplier_recorded.sum())} shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has {len(components)} components, largest {largest} organisations.",
              f"The size-standardised bootstrap compares resampling organisations with resampling connected components ({repetitions} requested replicates):", '',
              '| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |',
              '|---|---|---:|---:|---:|']
    def fmt(value):
        return 'n/a' if value is None or pd.isna(value) else f'{value:.2f}'
    for row in size_bootstrap.to_dict('records'):
        lines.append(f"| {row['resampling_unit']} | {row['metric']} | {fmt(row['estimate'])} | {fmt(row['ci_2_5'])}–{fmt(row['ci_97_5'])} | {row['valid_replicates']} |")
    lines += ['', 'Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Bootstrap intervals use fixed baseline weights and retain all sizes within each component; draws lacking exposure in any required stratum are omitted and valid counts are shown. Small cells, zero early events and missing shared incidents limit inference. Stratum-specific rates are in linkage_size_strata.csv; crude results remain separately available for comparison.']
    return {'section': '\n'.join(lines), 'outputs': ['linkage_sensitivity.csv', 'shared_incident_dependencies.csv', 'recurrence_cluster_bootstrap.csv', 'linkage_size_standardized.csv', 'linkage_size_strata.csv', 'recurrence_size_standardized_bootstrap.csv'],
            'scenarios': scenarios, 'shared_incidents': len(shared), 'shared_victims': shared_victims,
            'components': len(components), 'largest_component': largest,
            'within_incident_different_overrides': len(within), 'blocked_merges': blocked_counts,
            'size_reference_weights': weights, 'size_standardized_scenarios': standardized}
