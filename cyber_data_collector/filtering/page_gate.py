"""Discovery filter: does this page itself report a specific cyber incident?

Replaces the retired Random Forest filter (``rf_event_filter``), which failed
acceptance - on organisation-separated labels it dropped real incidents about
as often as junk.

The question is the calibrated source gate's (``dedup/source_gate.py``,
``page_reports_incident`` / ``source-gate-v1``): Jev is shown the page's own
scraped text, title, URL and search snippet - the same state, built the same
way - so its probability means what it meant when the cut-off was calibrated.
Answers are stored in ``ModelJudgements`` (subject ``raw_event``) and reused.

How a record is decided:

* **Before scraping nothing is dropped** on URL or title alone.
* A page with less than ``MIN_PAGE_CHARS`` of stored text is not judged (it
  cannot be judged from its own text) and is kept.
* A judged page below the calibrated cut-off is **rejected only for a source
  the calibration covers** - ``GoogleSearch``, where the cut-off was measured
  (precision 0.95 on 100 labelled removals), plus any source an evaluation in
  ``evaluation/page_gate_source_evaluation.json`` accepted for the same model,
  question version and cut-off. For every other source the score is recorded
  and the record kept (*would drop*, report-only).
* The calibration must match the current model and question version
  (``source_gate.active_calibration``); otherwise the gate is report-only for
  every source and says so prominently.

There is **no silent fallback**. The gate needs Jev: with no TypeSafe key or
SDK, :meth:`DiscoveryPageGate.require_available` raises
:class:`PageGateUnavailable`; a Jev error that survives the retries raises it
too, and the caller leaves the record in the retry queue as a temporary
failure. The only non-enforcing modes are explicit: ``mode='report-only'``
(``DISCOVERY_PAGE_GATE_MODE=report-only``) and a calibration identity
mismatch, both logged as warnings at every run.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cyber_data_collector.dedup import source_gate
from cyber_data_collector.processing.record_evidence import STAGE_PAGE_GATE  # noqa: F401 - re-exported
from cyber_data_collector.utils.api_budget import is_budget_refusal

logger = logging.getLogger(__name__)

SUBJECT_TYPE = "raw_event"
MODE_ENFORCE = "enforce"
MODE_REPORT_ONLY = "report-only"
MODES = (MODE_ENFORCE, MODE_REPORT_ONLY)
MODE_ENV = "DISCOVERY_PAGE_GATE_MODE"

#: Sources the source-gate calibration was measured on.
CALIBRATED_SOURCES = frozenset({source_gate.SOURCE_TYPE})
EVALUATION_PATH = source_gate.ROOT / "evaluation" / "page_gate_source_evaluation.json"

#: Jev calls per page before the error is treated as an outage.
MAX_JEV_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0


class PageGateUnavailable(RuntimeError):
    """Jev cannot be reached or is not configured; discovery must not go on without it."""


@dataclass(frozen=True)
class GateDecision:
    """One page's outcome."""

    raw_event_id: str
    source_type: str
    probability: Optional[float]   # None when the page was not judged
    judged: bool
    below_cutoff: bool              # would be dropped by an enforcing gate
    dropped: bool                   # actually rejected
    reason: str


def page_state(row: Dict[str, Any]) -> Dict[str, str]:
    """The state Jev is shown, built exactly as the source gate builds it."""
    page_text = (row.get("raw_content") or "")[:source_gate.PAGE_CHARS]
    return {
        "title": row.get("raw_title") or "",
        "url": row.get("source_url") or "",
        "search_snippet": row.get("raw_description") or "",
        "page_text": page_text or "(no page text stored)",
    }


def judgeable(state: Dict[str, str]) -> bool:
    return source_gate.judgeable({"page_text": state["page_text"]})


def accepted_extra_sources(calibration: Optional[Dict[str, Any]],
                           path: Path = EVALUATION_PATH) -> frozenset:
    """Sources beyond the calibrated one that an evaluation accepted for this exact gate."""
    if calibration is None:
        return frozenset()
    try:
        evaluation = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    identity = source_gate.calibration_identity()
    if {k: evaluation.get(k) for k in identity} != identity or evaluation.get("cutoff") != calibration.get("cutoff"):
        logger.warning("Page gate source evaluation is for %s / %s at cut-off %s, not the active gate: "
                       "only %s enforced", evaluation.get("model"), evaluation.get("question_version"),
                       evaluation.get("cutoff"), ", ".join(sorted(CALIBRATED_SOURCES)))
        return frozenset()
    return frozenset(name for name, verdict in (evaluation.get("sources") or {}).items()
                     if isinstance(verdict, dict) and verdict.get("accepted") is True)


class DiscoveryPageGate:
    """Judge scraped pages and reject non-incident pages where that is calibrated.

    Args:
        conn: Database the Jev answers are stored in.
        judge: A ``JevJudge`` (or test double); created on first use when omitted.
        mode: ``'enforce'`` (default) or ``'report-only'``; ``None`` reads
            ``DISCOVERY_PAGE_GATE_MODE``.
        calibration_path / evaluation_path: Override for tests.
        sleep: Delay function between Jev retries (tests pass a no-op).
    """

    def __init__(self, conn: Any, judge: Any = None, mode: Optional[str] = None,
                 calibration_path: Path = source_gate.CALIBRATION_PATH,
                 evaluation_path: Path = EVALUATION_PATH,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        mode = (mode or os.getenv(MODE_ENV) or MODE_ENFORCE).strip().lower()
        if mode not in MODES:
            raise ValueError(f"{MODE_ENV} must be one of {', '.join(MODES)}, not {mode!r}")
        self.conn = conn
        self.mode = mode
        self._judge = judge
        self._sleep = sleep
        self.calibration = source_gate.active_calibration(calibration_path)
        self.cutoff: Optional[float] = self.calibration["cutoff"] if self.calibration else None
        self.enforced_sources = (CALIBRATED_SOURCES | accepted_extra_sources(self.calibration, evaluation_path)
                                 if self.calibration else frozenset())
        self.stats: Counter = Counter()
        self.by_source: Dict[str, Counter] = {}
        if self.mode == MODE_REPORT_ONLY:
            logger.warning("[PAGE_GATE] REPORT-ONLY by explicit choice (mode report-only, or %s=report-only): pages below the "
                           "cut-off are recorded and kept", MODE_ENV)
        elif self.calibration is None:
            logger.warning("[PAGE_GATE] REPORT-ONLY: no source-gate calibration matches %s - nothing is dropped "
                           "until the gate is recalibrated", source_gate.calibration_identity())
        else:
            logger.info("[PAGE_GATE] Enforcing cut-off %.3f for %s; other sources report-only",
                        self.cutoff, ", ".join(sorted(self.enforced_sources)))

    # --- availability ------------------------------------------------------

    def require_available(self) -> None:
        """Raise :class:`PageGateUnavailable` unless Jev can be asked."""
        if self._judge is not None:
            return
        from cyber_data_collector.utils import jev
        if not jev.available():
            raise PageGateUnavailable(
                "The discovery page gate needs Jev (TypeSafe): set TYPESAFE_API_KEY and install typesafe-sdk. "
                "Discovery does not run without it - records stay queued for the next run.")

    def _get_judge(self):
        if self._judge is None:
            self.require_available()
            from cyber_data_collector.utils.jev import JevJudge
            self._judge = JevJudge(self.conn)
        return self._judge

    def close(self) -> None:
        if self._judge is not None and hasattr(self._judge, "close"):
            self._judge.close()

    # --- deciding ----------------------------------------------------------

    def is_enforcing(self, source_type: str) -> bool:
        return self.mode == MODE_ENFORCE and self.cutoff is not None and source_type in self.enforced_sources

    def _ask(self, raw_event_id: str, state: Dict[str, str]) -> float:
        question = {source_gate.QUESTION_ID: source_gate.question()}
        last: Optional[BaseException] = None
        for attempt in range(1, MAX_JEV_ATTEMPTS + 1):
            try:
                answer = self._get_judge().ask(SUBJECT_TYPE, raw_event_id, source_gate.QUESTION_VERSION,
                                               state, question)
                return float(answer[source_gate.QUESTION_ID].value)
            except PageGateUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if is_budget_refusal(exc) or attempt == MAX_JEV_ATTEMPTS:
                    break
                logger.warning("[PAGE_GATE] Jev error for %s (attempt %d/%d): %s", raw_event_id, attempt,
                               MAX_JEV_ATTEMPTS, exc)
                self._sleep(RETRY_DELAY_SECONDS * attempt)
        raise PageGateUnavailable(f"Jev could not judge {raw_event_id}: {last}") from last

    def decide(self, row: Dict[str, Any]) -> GateDecision:
        """Judge one ``RawEvents`` row (needs raw_event_id, source_type, raw_title,
        raw_description, raw_content, source_url).

        Raises:
            PageGateUnavailable: Jev is not configured or kept failing.
        """
        raw_id = row["raw_event_id"]
        source = row.get("source_type") or "Unknown"
        counts = self.by_source.setdefault(source, Counter())
        state = page_state(row)
        if not judgeable(state):
            self.stats["unjudgeable"] += 1
            counts["unjudgeable"] += 1
            return GateDecision(raw_id, source, None, False, False, False,
                                f"under {source_gate.MIN_PAGE_CHARS} characters of page text - not judged, kept")
        probability = self._ask(raw_id, state)
        self.stats["judged"] += 1
        counts["judged"] += 1
        below = self.cutoff is not None and probability < self.cutoff
        dropped = below and self.is_enforcing(source)
        if dropped:
            self.stats["dropped"] += 1
            counts["dropped"] += 1
            reason = (f"page does not itself report an incident: P={probability:.3f} < calibrated cut-off "
                      f"{self.cutoff:.3f} ({source_gate.QUESTION_VERSION})")
        elif below:
            self.stats["would_drop"] += 1
            counts["would_drop"] += 1
            reason = f"P={probability:.3f} below cut-off {self.cutoff:.3f}; kept ({self._why_report_only(source)})"
        else:
            self.stats["kept"] += 1
            counts["kept"] += 1
            reason = (f"P={probability:.3f}" + (f" >= cut-off {self.cutoff:.3f}" if self.cutoff is not None
                                                else " (no active calibration)"))
        return GateDecision(raw_id, source, probability, True, below, dropped, reason)

    def _why_report_only(self, source: str) -> str:
        if self.mode == MODE_REPORT_ONLY:
            return "report-only by explicit choice"
        if self.cutoff is None:
            return "no matching calibration"
        return f"cut-off not calibrated for {source}"

    # --- reporting ---------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        return {"mode": self.mode, "cutoff": self.cutoff, "enforced_sources": sorted(self.enforced_sources),
                **{k: self.stats.get(k, 0) for k in ("judged", "unjudgeable", "kept", "would_drop", "dropped")},
                "by_source": {s: dict(c) for s, c in sorted(self.by_source.items())}}

    def summary_lines(self) -> List[str]:
        s = self.statistics()
        lines = [f"mode={s['mode']} cut-off={s['cutoff']} enforced for: {', '.join(s['enforced_sources']) or 'none'}",
                 f"judged {s['judged']}, not judgeable {s['unjudgeable']}, kept {s['kept']}, "
                 f"would drop (report-only) {s['would_drop']}, dropped {s['dropped']}"]
        for source, counts in s["by_source"].items():
            lines.append(f"  {source}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return lines

