"""Run every quality check, record the run, and decide whether outputs may be published.

``publication_allowed(conn)`` is the call the pipeline makes before writing a
dashboard. Measuring only reads; recording a run writes two tables:

``QualityRuns``    one row per run: when, against which thresholds, the verdict
``QualityMetrics`` one row per metric per run: value, threshold, status

so every metric has a history and a trend is one query away::

    SELECT r.created_at, m.value, m.status FROM QualityMetrics m
    JOIN QualityRuns r USING (run_id) WHERE m.metric = 'dates.first_of_month_share'
    ORDER BY r.created_at;
"""

from __future__ import annotations

import html
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cyber_data_collector.quality.integrity import measure_integrity
from cyber_data_collector.quality.metrics import MEASURES
from cyber_data_collector.quality.model import (
    FAIL, INFO, NOT_MEASURED, PASS, WARN, Measurement, Metric, Thresholds, evaluate, gate,
    load_thresholds, unproduced_rules,
)
from cyber_data_collector.quality.regression import measure_labels, measure_synthetic

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS QualityRuns (
    run_id              TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    db_path             TEXT,
    thresholds_version  TEXT NOT NULL,
    thresholds_sha256   TEXT NOT NULL,
    publication_allowed INTEGER NOT NULL,
    blocking_failures   INTEGER NOT NULL,
    failed              INTEGER NOT NULL,
    warned              INTEGER NOT NULL,
    not_measured        INTEGER NOT NULL,
    reasons_json        TEXT NOT NULL,
    snapshot_id         TEXT,
    snapshot_sha256     TEXT
);
CREATE TABLE IF NOT EXISTS QualityMetrics (
    run_id        TEXT NOT NULL REFERENCES QualityRuns(run_id),
    metric        TEXT NOT NULL,
    metric_group  TEXT NOT NULL,
    value         REAL,
    threshold     TEXT,
    status        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    examples_json TEXT,
    PRIMARY KEY (run_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_quality_metrics_metric ON QualityMetrics(metric);
"""

#: Columns added after the table was first created (the snapshot the run measured; NULL = the live database).
RUN_COLUMNS = (("snapshot_id", "TEXT"), ("snapshot_sha256", "TEXT"))

ALL_MEASURES = MEASURES + (
    ("integrity", measure_integrity),
    ("labels", measure_labels),
    ("synthetic", measure_synthetic),
)


class QualityStoreError(RuntimeError):
    """A quality run could not be recorded."""


@dataclass
class QualityReport:
    run_id: str
    created_at: str
    db_path: str
    thresholds: Thresholds
    metrics: List[Metric]
    allowed: bool
    reasons: List[str]
    previous: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Recorded runs, newest first, with the TREND_METRICS values: ``[{"created_at", metric: value...}]``.
    history: List[Dict[str, Any]] = field(default_factory=list)
    #: The retained database snapshot this run measured (``cyber_data_collector.publication.snapshot``).
    snapshot_id: Optional[str] = None
    snapshot_sha256: Optional[str] = None

    def count(self, status: str) -> int:
        return sum(1 for m in self.metrics if m.status == status)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id, "created_at": self.created_at, "db_path": self.db_path,
            "snapshot_id": self.snapshot_id, "snapshot_sha256": self.snapshot_sha256,
            "thresholds": {"path": self.thresholds.path, "version": self.thresholds.version,
                           "sha256": self.thresholds.sha256},
            "publication_allowed": self.allowed, "reasons": self.reasons,
            "counts": {s: self.count(s) for s in (PASS, FAIL, WARN, NOT_MEASURED, INFO)},
            "metrics": [m.to_dict() for m in self.metrics],
            "previous_run": self.previous,
            "history": self.history,
        }


def _db_path(conn: sqlite3.Connection) -> str:
    row = next(iter(conn.execute("PRAGMA database_list")), None)
    return (row[2] if row else "") or ":memory:"


def run_quality_checks(conn: sqlite3.Connection, thresholds: Optional[Thresholds] = None,
                       today: Optional[date] = None, evaluation_dir: Optional[Path] = None,
                       snapshot: Optional[Any] = None, db_path: Optional[str] = None) -> QualityReport:
    """Measure everything and judge it. Reads only; nothing is written.

    ``snapshot`` (a ``publication.snapshot.Snapshot``) says the connection reads a retained copy: the
    report names its id and hash, and ``db_path`` (the live database it was taken from) is what the
    report shows as its database.

    A measuring function that raises is recorded as ``<group>.error``
    (not measured), and every rule it would have satisfied is reported as
    "configured but not produced" - a crash can only close the gate.
    """
    thresholds = thresholds or load_thresholds()
    today = today or date.today()
    metrics: List[Metric] = []
    for group, measure in ALL_MEASURES:
        try:
            if group == "labels" and evaluation_dir is not None:
                found = measure_labels(conn, thresholds, today, evaluation_dir=evaluation_dir)
            else:
                found = measure(conn, thresholds, today)
        except Exception as exc:  # noqa: BLE001 - reported, never passed
            logger.exception("Quality measure %s failed", group)
            name = f"{group}.error"
            if any(m.name == name for m in metrics):      # a second measure in the same group
                name = f"{group}.{measure.__name__}.error"
            found = {name: Measurement.unavailable(f"{type(exc).__name__}: {exc}")}
        for name, measurement in found.items():
            metrics.append(evaluate(name, group, measurement, thresholds))
    metrics.extend(unproduced_rules(metrics, thresholds))
    allowed, reasons = gate(metrics)
    report = QualityReport(run_id=f"qr-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}",
                           created_at=datetime.now().isoformat(timespec="seconds"),
                           db_path=db_path or _db_path(conn), thresholds=thresholds, metrics=metrics,
                           allowed=allowed, reasons=reasons,
                           snapshot_id=getattr(snapshot, "snapshot_id", None),
                           snapshot_sha256=getattr(snapshot, "sha256", None))
    report.previous = previous_values(conn)
    report.history = metric_history(conn, TREND_METRICS + ROUTE_METRICS)
    return report


#: Shown run by run in the report's trend table.
TREND_METRICS = ("evidence.status.supported", "evidence.status.partial", "evidence.status.unsupported",
                 "evidence.status.unsupported_share", "evidence.recovery.pending", "evidence.recovery.exhausted")
TREND_RUNS = 12
#: Evidence-recovery route progress (``recovery.route.<route>.<field>``), shown as one cell per route per run.
ROUTE_NAMES = ("cyberdaily_refetch", "page_rescrape", "research", "label_report", "replacement_search")
ROUTE_FIELDS = ("queued", "succeeded", "failing", "exhausted", "oldest_queued_days")
ROUTE_METRICS = tuple(f"recovery.route.{route}.{field_}" for route in ROUTE_NAMES for field_ in ROUTE_FIELDS)


def metric_history(conn: sqlite3.Connection, metrics: Tuple[str, ...] = TREND_METRICS,
                   runs: int = TREND_RUNS) -> List[Dict[str, Any]]:
    """The last ``runs`` recorded runs (newest first) with these metrics' values."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='QualityRuns'").fetchone():
        return []
    out = []
    for run_id, created_at in conn.execute("SELECT run_id, created_at FROM QualityRuns "
                                           "ORDER BY created_at DESC, run_id DESC LIMIT ?", (runs,)).fetchall():
        marks = ",".join("?" * len(metrics))
        values = dict(conn.execute(f"SELECT metric, value FROM QualityMetrics WHERE run_id = ? AND metric IN ({marks})",
                                   (run_id, *metrics)).fetchall())
        out.append({"created_at": created_at, **values})
    return out


def _route_cell(values: Dict[str, Any], route: str) -> str:
    got = {f: values.get(f"recovery.route.{route}.{f}") for f in ROUTE_FIELDS}
    if all(v is None for v in got.values()):
        return "-"
    age = got["oldest_queued_days"]
    return (f"{_fmt(got['queued'])} queued, {_fmt(got['succeeded'])} ok, {_fmt(got['failing'])} failing, "
            f"{_fmt(got['exhausted'])} exhausted" + (f", oldest {_fmt(age)} d" if age else ""))


def _route_trend_table(report: "QualityReport") -> Optional[Tuple[str, List[str], List[List[str]]]]:
    """Per-route recovery progress, this run and the recorded ones before it."""
    current = {m.name: m.value for m in report.metrics if m.name in ROUTE_METRICS}
    if not current:
        return None
    header = ["run", *ROUTE_NAMES]
    rows = [[f"{report.created_at} (this run)"] + [_route_cell(current, r) for r in ROUTE_NAMES]]
    rows += [[str(h["created_at"])] + [_route_cell(h, r) for r in ROUTE_NAMES] for h in report.history
             if any(h.get(n) is not None for n in ROUTE_METRICS)]
    return "Evidence recovery routes trend (queued / succeeded / failing / exhausted, oldest queued item)", header, rows


def _trend_tables(report: "QualityReport") -> List[Tuple[str, List[str], List[List[str]]]]:
    return [t for t in (_trend_table(report), _route_trend_table(report)) if t]


def _trend_table(report: "QualityReport") -> Optional[Tuple[str, List[str], List[List[str]]]]:
    """This run and the recorded ones before it, for TREND_METRICS (None when this run has none of them)."""
    current = {m.name: m.value for m in report.metrics if m.name in TREND_METRICS}
    if not current:
        return None
    header = ["run"] + [name.split(".", 1)[1] for name in TREND_METRICS]
    rows = [[f"{report.created_at} (this run)"] + [_fmt(current.get(n)) for n in TREND_METRICS]]
    rows += [[str(h["created_at"])] + [_fmt(h.get(n)) for n in TREND_METRICS] for h in report.history
             if any(h.get(n) is not None for n in TREND_METRICS)]
    return "Evidence status trend", header, rows


def previous_values(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """The most recent recorded run's value and status for every metric (empty before the first)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='QualityRuns'").fetchone():
        return {}
    last = conn.execute("SELECT run_id, created_at FROM QualityRuns ORDER BY created_at DESC, run_id DESC "
                        "LIMIT 1").fetchone()
    if last is None:
        return {}
    return {metric: {"value": value, "status": status, "run_id": last[0], "created_at": last[1]}
            for metric, value, status in conn.execute(
                "SELECT metric, value, status FROM QualityMetrics WHERE run_id = ?", (last[0],))}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    present = {row[1] for row in conn.execute("PRAGMA table_info(QualityRuns)")}
    for name, ddl in RUN_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE QualityRuns ADD COLUMN {name} {ddl}")


def store_report(conn: sqlite3.Connection, report: QualityReport) -> str:
    """Record a run and its metrics in one transaction; raises ``QualityStoreError`` on failure."""
    try:
        ensure_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO QualityRuns (run_id, created_at, db_path, thresholds_version, thresholds_sha256, "
                "publication_allowed, blocking_failures, failed, warned, not_measured, reasons_json, "
                "snapshot_id, snapshot_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report.run_id, report.created_at, report.db_path, report.thresholds.version,
                 report.thresholds.sha256, int(report.allowed),
                 sum(1 for m in report.metrics if m.blocking and m.status in (FAIL, NOT_MEASURED)),
                 report.count(FAIL), report.count(WARN), report.count(NOT_MEASURED), json.dumps(report.reasons),
                 report.snapshot_id, report.snapshot_sha256))
            conn.executemany(
                "INSERT INTO QualityMetrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(report.run_id, m.name, m.group, m.value, m.threshold, m.status, int(m.blocking), m.detail,
                  json.dumps(m.examples)) for m in report.metrics])
    except sqlite3.Error as exc:
        raise QualityStoreError(f"could not record quality run {report.run_id}: {exc}") from exc
    return report.run_id


def publication_allowed(conn: sqlite3.Connection, record: bool = True,
                        report_dir: Optional[Path] = None,
                        thresholds_path: Optional[Path] = None) -> Tuple[bool, List[str]]:
    """Whether the data in ``conn`` may be published, and if not, why.

    Runs every quality check. With ``record`` (the default) the run is stored
    in ``QualityRuns`` / ``QualityMetrics`` on ``conn``; a run that cannot be
    stored raises ``QualityStoreError`` rather than publishing unrecorded. With
    ``report_dir`` the JSON, Markdown and HTML reports are written there too.

    Returns:
        ``(allowed, reasons)`` - one reason per blocking metric that failed or
        could not be measured.
    """
    report = run_quality_checks(conn, load_thresholds(thresholds_path))
    if record:
        store_report(conn, report)
    if report_dir is not None:
        write_outputs(report, Path(report_dir))
    for reason in report.reasons:
        logger.error("Publication blocked: %s", reason)
    return report.allowed, report.reasons


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

GROUP_ORDER = ("integrity", "labels", "synthetic", "dates", "staleness", "duplicates", "evidence",
               "backlog", "coverage")
STATUS_ORDER = {FAIL: 0, NOT_MEASURED: 1, WARN: 2, PASS: 3, INFO: 4}


CYBERDAILY_SERIES = ("seen", "stored", "judged")


def _is_series(metric: Metric) -> bool:
    return metric.name.startswith(("coverage.records.", "coverage.events.")
                                  + tuple(f"coverage.cyberdaily.{kind}." for kind in CYBERDAILY_SERIES))


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _sorted(metrics: List[Metric]) -> List[Metric]:
    return sorted(metrics, key=lambda m: (GROUP_ORDER.index(m.group) if m.group in GROUP_ORDER else 99,
                                          STATUS_ORDER.get(m.status, 9), m.name))


def _coverage_tables(report: QualityReport) -> List[Tuple[str, List[str], List[List[str]]]]:
    tables = []
    for kind, title in (("records", "New raw records per source (by discovery month)"),
                        ("events", "Active events per source (by event month)")):
        series: Dict[str, Dict[str, float]] = {}
        for m in report.metrics:
            prefix = f"coverage.{kind}."
            if m.name.startswith(prefix):
                source, month = m.name[len(prefix):].rsplit(".", 1)
                series.setdefault(source, {})[month] = m.value
        if not series:
            continue
        months = sorted({mo for s in series.values() for mo in s})
        rows = [[source] + [_fmt(series[source].get(mo)) for mo in months] for source in sorted(series)]
        tables.append((title, ["source"] + months, rows))
    per_month: Dict[str, Dict[str, float]] = {}
    for m in report.metrics:
        for kind in CYBERDAILY_SERIES:
            prefix = f"coverage.cyberdaily.{kind}."
            if m.name.startswith(prefix):
                per_month.setdefault(m.name[len(prefix):], {})[kind] = m.value
    if per_month:
        rows = [[month] + [_fmt(per_month[month].get(kind)) for kind in CYBERDAILY_SERIES]
                for month in sorted(per_month, reverse=True)]
        tables.append(("Cyber Daily articles per publication month (seen in the feed / stored / judged)",
                       ["month", *CYBERDAILY_SERIES], rows))
    return tables


def render_markdown(report: QualityReport) -> str:
    verdict = "ALLOWED" if report.allowed else "BLOCKED"
    lines = [f"# Data quality report - publication {verdict}", "",
             f"- Run: `{report.run_id}` at {report.created_at}",
             f"- Database: `{report.db_path}`",
             f"- Snapshot: `{report.snapshot_id}` (sha-256 {report.snapshot_sha256[:12]}; "
             f"reproduce with `--snapshot-id {report.snapshot_id}`)" if report.snapshot_id else
             "- Snapshot: none - measured the live database directly",
             f"- Thresholds: version {report.thresholds.version} (`{report.thresholds.path}`, "
             f"sha256 {report.thresholds.sha256[:12]})",
             "- Metrics: " + ", ".join(f"{report.count(s)} {s}" for s in (FAIL, NOT_MEASURED, WARN, PASS, INFO)),
             ""]
    if report.reasons:
        lines += ["## Why publication is blocked", ""] + [f"- {r}" for r in report.reasons] + [""]
    previous = report.previous
    lines += ["## Checks", "",
              "| Status | Blocking | Metric | Value | Previous | Threshold | Detail |",
              "|---|---|---|---|---|---|---|"]
    for m in _sorted([m for m in report.metrics if not _is_series(m)]):
        prev = previous.get(m.name)
        detail = m.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {m.status} | {'yes' if m.blocking else ''} | `{m.name}` | {_fmt(m.value)} | "
                     f"{_fmt(prev['value']) if prev else ''} | {m.threshold} | {detail} |")
    lines.append("")
    flagged = [m for m in _sorted(report.metrics) if m.examples and m.status in (FAIL, WARN)]
    if flagged:
        lines += ["## Examples", ""]
        for m in flagged:
            lines.append(f"### `{m.name}` ({m.status})")
            lines += [f"- {str(e).replace(chr(10), ' ')}" for e in m.examples] + [""]
    for title, header, rows in _trend_tables(report) + _coverage_tables(report):
        lines += [f"## {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(r) + " |" for r in rows] + [""]
    return "\n".join(lines)


def render_html(report: QualityReport) -> str:
    """Self-contained page; every database-derived string is escaped."""
    e = html.escape
    verdict = "ALLOWED" if report.allowed else "BLOCKED"
    colours = {FAIL: "#b42318", NOT_MEASURED: "#b54708", WARN: "#a15c07", PASS: "#067647", INFO: "#475467"}
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width, initial-scale=1'>",
             f"<title>Data quality {e(verdict.lower())}</title><style>",
             "body{font:14px/1.45 system-ui,sans-serif;margin:16px;color:#101828;background:#fff}",
             "table{border-collapse:collapse;width:100%;margin:8px 0 24px}td,th{border:1px solid #d0d5dd;"
             "padding:4px 6px;text-align:left;vertical-align:top}th{background:#f2f4f7}",
             ".wrap{overflow-x:auto}code{font-size:12px}.s{font-weight:600}",
             "</style></head><body>",
             f"<h1>Data quality report - publication {e(verdict)}</h1>",
             f"<p>Run <code>{e(report.run_id)}</code> at {e(report.created_at)}<br>Database "
             f"<code>{e(report.db_path)}</code><br>Snapshot "
             + (f"<code>{e(report.snapshot_id)}</code>" if report.snapshot_id else "none (live database)")
             + f"<br>Thresholds version {e(report.thresholds.version)}</p>"]
    if report.reasons:
        parts.append("<h2>Why publication is blocked</h2><ul>")
        parts += [f"<li>{e(r)}</li>" for r in report.reasons]
        parts.append("</ul>")
    parts.append("<h2>Checks</h2><div class='wrap'><table><tr><th>Status</th><th>Blocking</th><th>Metric</th>"
                 "<th>Value</th><th>Previous</th><th>Threshold</th><th>Detail</th></tr>")
    for m in _sorted([m for m in report.metrics if not _is_series(m)]):
        prev = report.previous.get(m.name)
        examples = "".join(f"<br>&bull; {e(str(x))}" for x in m.examples[:10]) if m.status in (FAIL, WARN) else ""
        parts.append(f"<tr><td class='s' style='color:{colours.get(m.status, '#000')}'>{e(m.status)}</td>"
                     f"<td>{'yes' if m.blocking else ''}</td><td><code>{e(m.name)}</code></td>"
                     f"<td>{e(_fmt(m.value))}</td><td>{e(_fmt(prev['value'])) if prev else ''}</td>"
                     f"<td>{e(m.threshold)}</td><td>{e(m.detail)}{examples}</td></tr>")
    parts.append("</table></div>")
    for title, header, rows in _trend_tables(report) + _coverage_tables(report):
        parts.append(f"<h2>{e(title)}</h2><div class='wrap'><table><tr>"
                     + "".join(f"<th>{e(h)}</th>" for h in header) + "</tr>")
        parts += ["<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in rows]
        parts.append("</table></div>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_outputs(report: QualityReport, out_dir: Path) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / "quality_report.json", "markdown": out_dir / "quality_report.md",
             "html": out_dir / "quality_report.html"}
    paths["json"].write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    paths["html"].write_text(render_html(report), encoding="utf-8")
    return paths
