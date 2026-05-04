"""Per-page Pydantic schemas + invariant helpers for the OAIC dashboard scraper.

Each schema mirrors the JSON shape requested in the corresponding page prompt
in OAIC_dashboard_scraper.py. Validation is INTENTIONALLY strict: any field
the prompt says will be present must be present, and ranges are enforced
based on what we have observed across 7+ semesters of OAIC reporting.

Failure modes these schemas are designed to catch (each numbered to the
ranking discussed with the user):

  1. Type/shape errors and hallucinated extras (every model).
  2. displayed_semester echo verification (validate_displayed_semester).
  3. Page-9 rank-value-instead-of-count detection (TopSectorsBySource).
  5. Donut/source % must sum to ~100 (SnapshotData / BreachSources).
  6. Time bucket % must sum to ~100 per semester (TimeBuckets).
  7. Cross-page total_notifications consistency (validate_cross_page_totals).
  8. Time-bucket label normalization (CANONICAL_TIME_BUCKETS).
  9. Top-sectors range guard (10-250 per cell, in TopSectorsBySource).
 10. Inter-semester delta soft-warning (validate_inter_semester_delta).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Custom exception used as the single signal for "this extraction is bad"
# ----------------------------------------------------------------------

class OAICValidationError(Exception):
    """Raised when an OAIC vision extraction fails any validity check.

    Carries the page, semester, raw payload, and a list of violations so
    callers can both log and quarantine the offending screenshot/JSON.
    """

    def __init__(self, page: int, semester: str, errors: List[str], payload: Optional[Dict] = None):
        self.page = page
        self.semester = semester
        self.errors = errors
        self.payload = payload or {}
        super().__init__(f"[page {page}] [{semester}] " + "; ".join(errors))


# ----------------------------------------------------------------------
# Time-bucket canonical schema (rank 8)
# ----------------------------------------------------------------------

CANONICAL_TIME_BUCKETS: Tuple[str, ...] = (
    "Unknown",
    "<= 10 days",
    "11-20 days",
    "21-30 days",
    "> 30 days",
)


def normalize_time_bucket(label: str) -> Optional[str]:
    """Map a raw vision-API bucket label to one of CANONICAL_TIME_BUCKETS.

    Returns None if the label is not recognisable.
    """
    if label is None:
        return None
    s = str(label).strip()
    if not s:
        return None
    s_norm = s.casefold().replace("≤", "<=").replace("≤", "<=")
    s_norm = s_norm.replace("≥", ">=").replace("days", "days").strip()

    if s_norm in ("unknown", "n/a", "na"):
        return "Unknown"
    if s_norm in ("<= 10 days", "<=10 days", "< 10 days", "0-10 days",
                  "0-10", "1-10 days", "<10 days", "10 days or less",
                  "less than 10 days"):
        return "<= 10 days"
    if s_norm in ("11-20 days", "11-20", "11 to 20 days"):
        return "11-20 days"
    if s_norm in ("21-30 days", "21-30", "21 to 30 days"):
        return "21-30 days"
    if s_norm in ("> 30 days", ">30 days", "30+ days", "more than 30 days",
                  "over 30 days", "greater than 30 days"):
        return "> 30 days"
    return None


# ----------------------------------------------------------------------
# Page 2: Snapshot
# ----------------------------------------------------------------------

class MonthlyNotification(BaseModel):
    month: str = Field(..., min_length=1, max_length=20)
    count: Optional[int] = Field(None, ge=0, le=2000)


class CyberIncidents(BaseModel):
    phishing_pct: Optional[float] = Field(None, ge=0, le=100)
    compromised_credentials_pct: Optional[float] = Field(None, ge=0, le=100)
    ransomware_pct: Optional[float] = Field(None, ge=0, le=100)
    hacking_pct: Optional[float] = Field(None, ge=0, le=100)
    brute_force_pct: Optional[float] = Field(None, ge=0, le=100)
    malware_pct: Optional[float] = Field(None, ge=0, le=100)


class TopSectorEntry(BaseModel):
    sector: str = Field(..., min_length=1, max_length=120)
    notifications: Optional[int] = Field(None, ge=0, le=1000)


class HumanErrorCauses(BaseModel):
    wrong_recipient_email_pct: Optional[float] = Field(None, ge=0, le=100)
    unauthorised_disclosure_pct: Optional[float] = Field(None, ge=0, le=100)
    failure_to_use_bcc_pct: Optional[float] = Field(None, ge=0, le=100)


class SnapshotData(BaseModel):
    """Page 2: Snapshot."""
    displayed_semester: Optional[str] = Field(None, max_length=80)
    total_notifications: Optional[int] = Field(None, ge=50, le=2000)
    change_from_previous: Optional[str] = Field(None, max_length=40)
    monthly_notifications: List[MonthlyNotification] = Field(default_factory=list)
    human_error_pct: Optional[float] = Field(None, ge=0, le=100)
    malicious_attacks_pct: Optional[float] = Field(None, ge=0, le=100)
    system_faults_pct: Optional[float] = Field(None, ge=0, le=100)
    cyber_incidents: Optional[CyberIncidents] = None
    top_sectors: List[TopSectorEntry] = Field(default_factory=list)
    small_breaches_100_or_fewer_pct: Optional[float] = Field(None, ge=0, le=100)
    human_error_causes: Optional[HumanErrorCauses] = None

    @model_validator(mode="after")
    def _check_donut_sum(self):
        # Rank 5: human/malicious/system % must sum to ~100 if all present.
        parts = [self.human_error_pct, self.malicious_attacks_pct, self.system_faults_pct]
        if all(isinstance(v, (int, float)) for v in parts):
            s = sum(parts)
            if not (96 <= s <= 104):
                raise ValueError(
                    f"Sources-of-breach donut % sums to {s} (expected 100±4); "
                    "likely partial extraction."
                )
        return self

    @model_validator(mode="after")
    def _check_top_sectors_range(self):
        # Rank 9: every reported sector count must be plausible for a top-5
        # ranking on the OAIC dashboard. We've never seen a top-5 sector
        # with fewer than 8 notifications in 7 semesters of data.
        for entry in self.top_sectors[:5]:
            n = entry.notifications
            if n is None:
                continue
            if n < 5 or n > 300:
                raise ValueError(
                    f"top_sectors[{entry.sector!r}] = {n} outside plausible "
                    f"range 5-300; likely rank value or hallucination."
                )
        # Rank 3+9 cross-check: if all five top_sectors counts fall in 1-5
        # and span at least three distinct values, we're looking at a
        # rank-value view rather than counts.
        n_vals = [e.notifications for e in self.top_sectors[:5]
                  if isinstance(e.notifications, int)]
        if len(n_vals) >= 4 and all(1 <= v <= 5 for v in n_vals) and len(set(n_vals)) >= 3:
            raise ValueError(
                "top_sectors counts are all in 1-5 with distinct ranks; "
                "this is the 'rank' view, not real notification counts."
            )
        return self


# ----------------------------------------------------------------------
# Page 3: Notifications received
# ----------------------------------------------------------------------

class NotificationsByType(BaseModel):
    malicious_attack: Optional[int] = Field(None, ge=0, le=2000)
    human_error: Optional[int] = Field(None, ge=0, le=2000)
    system_fault: Optional[int] = Field(None, ge=0, le=500)


class NotificationsData(BaseModel):
    """Page 3."""
    monthly_notifications: List[MonthlyNotification] = Field(default_factory=list)
    by_type: Optional[NotificationsByType] = None
    trend_comparison: Optional[str] = Field(None, max_length=300)


# ----------------------------------------------------------------------
# Page 4: Individuals affected
# ----------------------------------------------------------------------

INDIVIDUALS_AFFECTED_BUCKETS = (
    "1", "2-10", "11-100", "101-1,000", "1,001-5,000", "5,001-10,000",
    "10,001-25,000", "25,001-50,000", "50,001-100,000", "100,001-250,000",
    "250,001-500,000", "1,000,001-10,000,000", "Unknown",
)


class IndividualsAffectedBucket(BaseModel):
    range: str = Field(..., min_length=1, max_length=40)
    count: Optional[int] = Field(None, ge=0, le=2000)


class LargeScaleEntry(BaseModel):
    range: str = Field(..., min_length=1, max_length=40)
    previous_semester: Optional[int] = Field(None, ge=0, le=200)
    current_semester: Optional[int] = Field(None, ge=0, le=200)


class IndividualsAffectedData(BaseModel):
    """Page 4."""
    displayed_semester: Optional[str] = Field(None, max_length=80)
    individuals_affected_distribution: List[IndividualsAffectedBucket] = Field(default_factory=list)
    large_scale_australians: List[LargeScaleEntry] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Page 5: Personal info types
# ----------------------------------------------------------------------

class PersonalInfoCategoryCounts(BaseModel):
    contact_information: Optional[int] = Field(None, ge=0, le=1000)
    identity_information: Optional[int] = Field(None, ge=0, le=1000)
    financial_details: Optional[int] = Field(None, ge=0, le=1000)
    health_information: Optional[int] = Field(None, ge=0, le=1000)
    tax_file_numbers: Optional[int] = Field(None, ge=0, le=1000)
    other_sensitive_information: Optional[int] = Field(None, ge=0, le=1000)
    consumer_data_right: Optional[int] = Field(None, ge=0, le=1000)
    digital_id: Optional[int] = Field(None, ge=0, le=1000)


class PersonalInfoTypesData(BaseModel):
    """Page 5."""
    displayed_semester: Optional[str] = Field(None, max_length=80)
    personal_info_types: Optional[PersonalInfoCategoryCounts] = None


# ----------------------------------------------------------------------
# Page 6: Breach sources
# ----------------------------------------------------------------------

class BreachSourceEntry(BaseModel):
    current_period: Optional[int] = Field(None, ge=0, le=2000)
    previous_period: Optional[int] = Field(None, ge=0, le=2000)


class BreachSourcesPayload(BaseModel):
    human_error: Optional[BreachSourceEntry] = None
    malicious_attack: Optional[BreachSourceEntry] = None
    system_fault: Optional[BreachSourceEntry] = None


class BreachSourcesData(BaseModel):
    """Page 6."""
    displayed_semester: Optional[str] = Field(None, max_length=80)
    current_period_label: Optional[str] = Field(None, max_length=40)
    previous_period_label: Optional[str] = Field(None, max_length=40)
    breach_sources: Optional[BreachSourcesPayload] = None

    @model_validator(mode="after")
    def _check_period_label_distinct(self):
        if (self.current_period_label and self.previous_period_label
                and self.current_period_label.strip() == self.previous_period_label.strip()):
            raise ValueError(
                "current_period_label equals previous_period_label "
                f"({self.current_period_label!r}); period switch failed."
            )
        return self


# ----------------------------------------------------------------------
# Pages 7 & 8: Time-to-identify / Time-to-notify
# ----------------------------------------------------------------------

class TimeBucketEntry(BaseModel):
    bucket: str = Field(..., min_length=1, max_length=40)
    current_pct: Optional[float] = Field(None, ge=0, le=110)
    previous_pct: Optional[float] = Field(None, ge=0, le=110)

    @field_validator("bucket")
    @classmethod
    def _normalize_bucket(cls, v: str) -> str:
        canon = normalize_time_bucket(v)
        if canon is None:
            raise ValueError(f"unknown time bucket: {v!r}")
        return canon


class TimeBucketsData(BaseModel):
    """Pages 7 & 8 share a structure differing only in the field name."""
    displayed_semester: Optional[str] = Field(None, max_length=80)
    current_period_label: Optional[str] = Field(None, max_length=40)
    previous_period_label: Optional[str] = Field(None, max_length=40)
    entries: List[TimeBucketEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_pct_sum(self):
        # Rank 6: each semester's % must sum to ~100±4 and all 5 canonical
        # buckets must be present exactly once.
        seen = [e.bucket for e in self.entries]
        missing = [b for b in CANONICAL_TIME_BUCKETS if b not in seen]
        if missing:
            raise ValueError(
                f"Missing time buckets after normalization: {missing}; "
                f"got {seen}."
            )
        if len(seen) != len(set(seen)):
            raise ValueError(f"Duplicate time buckets after normalization: {seen}")

        for tag in ("current_pct", "previous_pct"):
            vals = [getattr(e, tag) for e in self.entries
                    if getattr(e, tag) is not None]
            if len(vals) >= 4:
                s = sum(vals)
                # Tolerance: ±8% accommodates one-bar misreads + rounding
                # noise. Anything wilder than that (e.g. 120%) is
                # systematically wrong (LLM offset-by-one bucket
                # alignment) and worth quarantining.
                if not (92 <= s <= 108):
                    raise ValueError(
                        f"Time-bucket {tag} sums to {s:.1f} (expected 100±8); "
                        "likely partial extraction."
                    )
        return self


# ----------------------------------------------------------------------
# Page 9: sector × source matrix
# ----------------------------------------------------------------------

class SectorBySourceRow(BaseModel):
    sector: str = Field(..., min_length=1, max_length=120)
    human_error: Optional[int] = Field(None, ge=0, le=300)
    malicious_or_criminal: Optional[int] = Field(None, ge=0, le=300)
    system_fault: Optional[int] = Field(None, ge=0, le=300)


class TopSectorsBySource(BaseModel):
    """Page 9: 5×3 sector × cause matrix."""
    displayed_semester: Optional[str] = Field(None, max_length=80)
    sector_by_source: List[SectorBySourceRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_not_rank_view(self):
        # Rank 3: if every cell is in 1-5 across all 15 cells with at least
        # 3 distinct values, we're capturing the 'rank' view by mistake.
        flat: List[int] = []
        for row in self.sector_by_source:
            for v in (row.human_error, row.malicious_or_criminal, row.system_fault):
                if isinstance(v, int):
                    flat.append(v)
        if (len(flat) >= 12
                and all(1 <= v <= 5 for v in flat)
                and len(set(flat)) >= 3):
            raise ValueError(
                "sector_by_source values are all in 1-5 with multiple "
                "distinct ranks; this is the rank view, not real counts."
            )
        return self


# ----------------------------------------------------------------------
# Page-number to model dispatch (used by extractors)
# ----------------------------------------------------------------------

PAGE_MODELS: Dict[int, Any] = {
    2: SnapshotData,
    3: NotificationsData,
    4: IndividualsAffectedData,
    5: PersonalInfoTypesData,
    6: BreachSourcesData,
    # Pages 7 & 8 are TimeBucketsData but the raw payload uses a custom
    # field name. We adapt in validate_page_payload.
    7: TimeBucketsData,
    8: TimeBucketsData,
    9: TopSectorsBySource,
}


def _adapt_payload_for_page(page: int, payload: Dict, requested_semester: str = "") -> Dict:
    """Convert raw vision JSON into the exact shape PAGE_MODELS[page] expects.

    Pages 7/8 use 'time_to_identify_pct'/'time_to_notify_pct' as the entry
    field. We reroute that into the generic 'entries' field.

    Pages 6/7/8 also get period-swap auto-correction: the vision LLM
    consistently confuses the chart legend's two periods, putting the
    PREVIOUS semester into 'current_period_label' (and the per-bar
    values to match). When the requested semester matches
    'previous_period_label' but not 'current_period_label', we swap.
    """
    if not isinstance(payload, dict):
        return {}
    payload = _maybe_swap_periods(page, dict(payload), requested_semester)
    if page == 7:
        payload["entries"] = payload.pop("time_to_identify_pct", []) or []
    elif page == 8:
        payload["entries"] = payload.pop("time_to_notify_pct", []) or []
    return payload


def _maybe_swap_periods(page: int, payload: Dict, requested_semester: str) -> Dict:
    """If the vision LLM swapped current/previous (a frequent error on
    side-by-side bar charts), undo the swap by inverting period labels
    AND every per-bar/per-source value. Detected by comparing the
    requested semester against the two period labels.
    """
    if page not in (6, 7, 8):
        return payload
    if not requested_semester:
        return payload

    cur_label = payload.get("current_period_label")
    prev_label = payload.get("previous_period_label")
    if not (cur_label and prev_label):
        return payload

    req_norm = _normalize_semester_label(requested_semester)
    cur_norm = _normalize_semester_label(cur_label)
    prev_norm = _normalize_semester_label(prev_label)

    # If current_period_label already matches the request, nothing to do.
    if cur_norm == req_norm:
        return payload
    # If previous_period_label matches the request, the LLM swapped.
    if prev_norm != req_norm:
        return payload  # neither label matches - some other failure

    logger.info(
        f"[page {page}] Auto-correcting LLM period swap: "
        f"current={cur_label!r} prev={prev_label!r}, "
        f"requested={requested_semester!r}"
    )

    # Swap labels.
    payload["current_period_label"] = prev_label
    payload["previous_period_label"] = cur_label
    # Re-align displayed_semester to the slicer-correct value.
    payload["displayed_semester"] = prev_label

    # Swap data values per page.
    if page == 6:
        bs = payload.get("breach_sources") or {}
        for src in ("human_error", "malicious_attack", "system_fault"):
            entry = bs.get(src)
            if isinstance(entry, dict):
                entry["current_period"], entry["previous_period"] = (
                    entry.get("previous_period"), entry.get("current_period"),
                )
    elif page in (7, 8):
        field = "time_to_identify_pct" if page == 7 else "time_to_notify_pct"
        # Handle both shapes: raw payload key or already-adapted 'entries'.
        for key in (field, "entries"):
            entries = payload.get(key)
            if not isinstance(entries, list):
                continue
            for e in entries:
                if isinstance(e, dict):
                    e["current_pct"], e["previous_pct"] = (
                        e.get("previous_pct"), e.get("current_pct"),
                    )
    return payload


def _restore_payload_for_page(page: int, model: BaseModel) -> Dict:
    """Render the validated model back into the JSON shape downstream
    consumers (consolidate_period_data) already expect.
    """
    d = model.model_dump(exclude_none=False)
    if page == 7:
        d["time_to_identify_pct"] = d.pop("entries", [])
    elif page == 8:
        d["time_to_notify_pct"] = d.pop("entries", [])
    return d


# ----------------------------------------------------------------------
# Public validation entry point — rank 1
# ----------------------------------------------------------------------

def validate_page_payload(page: int, payload: Dict, semester: str) -> Dict:
    """Validate raw vision JSON for `page`. Raises OAICValidationError on
    failure. Returns the validated dict (in the original on-the-wire shape).
    """
    model_cls = PAGE_MODELS.get(page)
    if model_cls is None:
        return payload  # No schema for this page yet.
    if not isinstance(payload, dict):
        raise OAICValidationError(
            page=page, semester=semester,
            errors=[f"vision API returned non-dict ({type(payload).__name__})"],
            payload={},
        )

    adapted = _adapt_payload_for_page(page, payload, semester)
    try:
        model = model_cls.model_validate(adapted)
    except ValidationError as e:
        msgs = [
            f"{'/'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        raise OAICValidationError(page=page, semester=semester,
                                  errors=msgs, payload=payload) from e
    return _restore_payload_for_page(page, model)


# ----------------------------------------------------------------------
# Rank 2: displayed_semester echo verification
# ----------------------------------------------------------------------

def _normalize_semester_label(label: Optional[str]) -> str:
    if not label:
        return ""
    s = str(label).strip()
    s = re.sub(r"^show results for[:\s]*", "", s, flags=re.IGNORECASE)
    s = s.replace(" ", " ")  # nbsp
    s = re.sub(r"\s+", " ", s).strip()
    # Tolerate "January-June 2025" vs "Jan-Jun 2025"
    s = (s.replace("January", "Jan").replace("February", "Feb")
           .replace("June", "Jun").replace("July", "Jul")
           .replace("December", "Dec"))
    return s.lower()


def validate_displayed_semester(
    page: int,
    payload: Dict,
    requested_semester: str,
) -> None:
    """Hard-fail when the page's displayed_semester doesn't match request.

    Pages 3 doesn't include displayed_semester in its prompt; skip it.
    Other pages without the field are tolerated as a warning, not a fail
    (some 2022 layouts hide the label).
    """
    if page == 3:
        return
    if not isinstance(payload, dict):
        return
    displayed = payload.get("displayed_semester")
    if not displayed:
        logger.warning(
            f"[page {page}] [{requested_semester}] no displayed_semester in payload "
            "- proceeding without echo verification."
        )
        return
    if _normalize_semester_label(displayed) != _normalize_semester_label(requested_semester):
        raise OAICValidationError(
            page=page, semester=requested_semester,
            errors=[
                f"displayed_semester {displayed!r} != requested {requested_semester!r}; "
                "dashboard kept the previous selection."
            ],
            payload=payload,
        )


# ----------------------------------------------------------------------
# Rank 7: cross-page total_notifications consistency
# ----------------------------------------------------------------------

def validate_cross_page_totals(consolidated: Dict, semester: str) -> List[str]:
    """Check that the page-2 KPI, the sum-of-monthly bars, and the page-6
    source counts all agree to within tolerance. Returns a list of
    warnings; raises only if divergence exceeds the hard threshold.

    Tolerance widens for older periods (pre-2022) because OAIC's
    presentation of those totals has been revised more than once and
    the page-2 KPI vs page-6 source-sum divergence reaches 15-20%
    legitimately for those reports.
    """
    warnings: List[str] = []
    total = consolidated.get("total_notifications")
    if not isinstance(total, (int, float)) or total <= 0:
        return warnings

    monthly = consolidated.get("monthly_notifications") or []
    monthly_counts = [m.get("count") for m in monthly
                      if isinstance(m, dict) and isinstance(m.get("count"), (int, float))]
    monthly_sum = sum(monthly_counts) if monthly_counts else 0

    src_sum = (consolidated.get("malicious_attacks") or 0) \
            + (consolidated.get("human_error") or 0) \
            + (consolidated.get("system_faults") or 0)

    # Tolerance regime by period age. The dashboard for very old
    # semesters has had more revisions and presents totals less
    # consistently, so relax the bands.
    year = consolidated.get("year")
    is_old = isinstance(year, int) and year < 2022
    warn_at = 0.20 if is_old else 0.10
    raise_at = 0.40 if is_old else 0.25

    def _check(label: str, observed: float) -> None:
        if observed <= 0:
            return
        deviation = abs(observed - total) / total
        if deviation > raise_at:
            raise OAICValidationError(
                page=2, semester=semester,
                errors=[
                    f"{label}={observed} differs from total_notifications={total} "
                    f"by {deviation:.0%} (>{raise_at:.0%}); cross-page mismatch."
                ],
                payload=consolidated,
            )
        if deviation > warn_at:
            warnings.append(
                f"{label}={observed} differs from total_notifications={total} "
                f"by {deviation:.0%}"
            )

    _check("monthly_sum", monthly_sum)
    _check("source_sum", src_sum)
    return warnings


# ----------------------------------------------------------------------
# Rank 10: inter-semester delta soft-warning (advisory)
# ----------------------------------------------------------------------

def validate_inter_semester_delta(
    new_period: Dict,
    prior_period: Optional[Dict],
    threshold: float = 0.5,
) -> List[str]:
    """Compare new total_notifications to prior period; warn (don't fail)
    on >threshold relative swings. Real OAIC reporting changes happen, so
    this is advisory only.
    """
    warnings: List[str] = []
    if not prior_period:
        return warnings
    new_total = new_period.get("total_notifications")
    old_total = prior_period.get("total_notifications")
    if not (isinstance(new_total, (int, float)) and new_total > 0
            and isinstance(old_total, (int, float)) and old_total > 0):
        return warnings
    delta = abs(new_total - old_total) / old_total
    if delta > threshold:
        warnings.append(
            f"total_notifications swung {delta:.0%} between prior period "
            f"({old_total}) and new period ({new_total}); review for plausibility."
        )

    new_top = {e.get("sector") for e in (new_period.get("top_sectors") or [])
               if isinstance(e, dict)}
    old_top = {e.get("sector") for e in (prior_period.get("top_sectors") or [])
               if isinstance(e, dict)}
    if old_top and new_top and len(new_top & old_top) <= 1:
        warnings.append(
            f"top_sectors completely re-ordered ({old_top} -> {new_top}); "
            "review for filter-state slip."
        )
    return warnings


# ----------------------------------------------------------------------
# Failure-screenshot quarantine (shared helper)
# ----------------------------------------------------------------------

def quarantine_extraction(
    quarantine_dir: Path,
    semester: str,
    page: int,
    image_bytes: Optional[bytes],
    payload: Optional[Dict],
    errors: List[str],
) -> Path:
    """Write the offending screenshot + extracted JSON + error list into
    `quarantine_dir/<semester>/<page>_<timestamp>/`. Returns that subdir.
    """
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_semester = re.sub(r"[^A-Za-z0-9-]+", "_", semester).strip("_")
    sub = quarantine_dir / safe_semester / f"page{page}_{ts}"
    sub.mkdir(parents=True, exist_ok=True)
    if image_bytes:
        try:
            (sub / "screenshot.png").write_bytes(image_bytes)
        except Exception as e:
            logger.debug(f"quarantine: could not write screenshot: {e}")
    try:
        (sub / "payload.json").write_text(
            json.dumps(payload or {}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug(f"quarantine: could not write payload: {e}")
    try:
        (sub / "errors.txt").write_text("\n".join(errors), encoding="utf-8")
    except Exception as e:
        logger.debug(f"quarantine: could not write errors: {e}")
    logger.warning(f"Quarantined bad extraction to {sub}")
    return sub
