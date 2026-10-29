"""A calibration holds only for the evidence processing it was measured on.

Every calibrated cut-off (event reality, duplicates, coherence, source gate,
victim attribution, supplier victims) was fixed on labelled cases scored by a
model that saw particular *evidence*: so many characters of each page, only
some records, a page withheld or not, a date described one way. A cut-off
measured on one body of evidence says nothing about another. On 2026-10-28 the
evidence readers changed (``processing/active_evidence.py``: active records
only, pages of other articles withheld) while the cut-offs - activated on the
model and question version alone - went on rejecting and merging.

Each reader therefore carries an **evidence fingerprint**: a hash of a version
constant, bumped by hand whenever the *logic* of what a judgement is shown
changes, and of the *parameters* that shape it (character limits, record caps,
the escalation effort, the shared active-evidence rules). The fingerprint is
recorded in the calibration file next to the model and question version, and
activation requires all three to match (:func:`mismatch`). When they do not,
the step reports only and the log says which part differs.

``scripts/recalibrate.py`` (``dedup_admin.py recalibrate``) re-scores the
labelled cases on current evidence and writes a calibration carrying the new
fingerprint; the refresh runs it when a fingerprint is stale.

Bump rule: change what a reader shows a model and you change its
``EVIDENCE_VERSION``. Changing a parameter that is in the fingerprint needs no
bump. Source hashing was rejected: ``ast`` dumps differ between Python
versions, and a cosmetic edit should not cost a recalibration.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

#: Calibration keys compared at activation (the fingerprint only when the reader defines one).
IDENTITY_KEYS = ("question_version", "model", "evidence")


def fingerprint(reader: str, **parts: Any) -> str:
    """``<reader>:<16 hex>`` - a hash of the reader name and its evidence-shaping parts."""
    blob = json.dumps({"reader": reader, **parts}, sort_keys=True, default=str, ensure_ascii=False)
    return f"{reader}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


def shared_parts() -> Dict[str, Any]:
    """The parts every judgement that reads events, timelines or escalations shares."""
    from cyber_data_collector.dedup import corroboration, org_timeline
    from cyber_data_collector.processing import active_evidence
    from cyber_data_collector.utils import escalation

    return {
        "active_evidence": active_evidence.rules_fingerprint(),
        "timeline": [org_timeline.PROMPT_VERSION, org_timeline.TIMELINE_CHARS, org_timeline.MAX_CITATIONS],
        "corroboration": corroboration.MAX_CITATIONS,
        "escalation_effort": escalation.REASONING_EFFORT,
    }


def mismatch(calibration: Mapping[str, Any], want: Mapping[str, Any]) -> Optional[str]:
    """Why ``calibration`` does not apply to ``want`` (the current identity), or None when it does.

    Compares every key of ``want`` that is one of :data:`IDENTITY_KEYS`. A
    calibration with no recorded ``evidence`` where one is now required
    predates fingerprinting: its evidence cannot be shown to be the current
    one, so it does not apply.
    """
    reasons = []
    for key in IDENTITY_KEYS:
        if key not in want:
            continue
        have = calibration.get(key)
        if key == "evidence" and have is None:
            reasons.append("it records no evidence fingerprint (measured before evidence processing was "
                           f"fingerprinted); current evidence is {want[key]}")
        elif have != want[key]:
            reasons.append(f"{key} was {have!r}, now {want[key]!r}")
    return "; ".join(reasons) if reasons else None


def report_only_message(what: str, calibration: Mapping[str, Any], reason: str) -> str:
    """The log line every step writes when a calibration does not apply (one wording, so it is greppable)."""
    return (f"REPORT ONLY: the {what} calibration does not apply to the current model, question and evidence "
            f"processing ({reason}). Recalibrate on current evidence with "
            "'python scripts/dedup_admin.py recalibrate'; nothing is acted on until then.")
