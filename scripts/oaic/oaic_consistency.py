"""Internal-consistency rules for one OAIC semester record.

Every ingester (data.gov.au, Power BI, PDF), the published dataset
(``oaic_dataset.py``), the cleanup/validation command and the dashboard apply
the same rules, so a figure that contradicts another figure of the same period
is never published:

* :func:`find_contradictions` lists every contradiction in a record. Each one
  names the figures it involves (``fields``); a contradiction that involves the
  period total cannot be resolved by dropping a breakdown, so it withholds the
  whole period (``fields == WHOLE_PERIOD``).
* :func:`screen_record` returns a copy with the involved figures withheld (set
  to None) and listed under ``withheld``, or None when the whole period must be
  withheld. Withheld figures are shown as a note, never silently dropped.

Rules here describe contradictions, not plausibility guesses: a part larger
than its whole, parts that sum past their whole, a complete set of parts that
does not sum to its whole, a percentage outside 0-100.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

WHOLE_PERIOD: Tuple[str, ...] = ("*",)

SOURCE_COUNT_FIELDS = (
    "malicious_attacks", "human_error", "system_faults",
    "other_sources", "currently_unknown_sources",
)
METHOD_FIELDS = (
    "phishing", "ransomware", "hacking", "brute_force", "malware", "compromised_credentials",
)
DONUT_PCT_FIELDS = ("human_error_pct", "malicious_attacks_pct", "system_faults_pct")

# Rounding slack for percentages that are each rounded to a whole number.
PCT_SUM_TOLERANCE = 3.0

# Power BI records (source == "dashboard") scaled per-method percentages - which
# partition cyber incidents - by the malicious-attack count, overstating every
# method count (~1.6x where both bases are published). Records that carry
# ``attack_method_basis == "cyber_incidents"`` were computed on the right base.
POWER_BI_SOURCE = "dashboard"


@dataclass(frozen=True)
class Contradiction:
    """One internal contradiction in a period record."""
    check: str
    message: str
    fields: Tuple[str, ...]

    @property
    def whole_period(self) -> bool:
        return self.fields == WHOLE_PERIOD

    def as_dict(self) -> Dict[str, Any]:
        return {"check": self.check, "message": self.message,
                "fields": "whole period" if self.whole_period else list(self.fields)}


def period_label(record: Dict[str, Any]) -> str:
    """``"2025 H2"`` style label (the dataset's period key)."""
    return f"{record.get('year')} {record.get('period')}"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _list_counts(items: Any, key: str) -> Tuple[List[float], bool]:
    """Numeric ``key`` values of a list of dicts, and whether any were malformed."""
    if items is None:
        return [], False
    if not isinstance(items, list):
        return [], True
    out, malformed = [], False
    for item in items:
        if not isinstance(item, dict):
            malformed = True
            continue
        v = item.get(key)
        if v is None:
            continue
        n = _num(v)
        if n is None or n < 0:
            malformed = True
            continue
        out.append(n)
    return out, malformed


def find_contradictions(record: Dict[str, Any]) -> List[Contradiction]:
    """Every internal contradiction in one OAIC semester record."""
    found: List[Contradiction] = []

    def add(check: str, message: str, fields: Sequence[str]) -> None:
        found.append(Contradiction(check, message, tuple(fields)))

    year, period = record.get("year"), record.get("period")
    if not isinstance(year, int) or period not in ("H1", "H2"):
        add("period", f"malformed period (year={year!r}, period={period!r})", WHOLE_PERIOD)
        return found

    total = _num(record.get("total_notifications"))
    if total is None or total <= 0:
        add("total", f"total_notifications missing or not positive "
                     f"({record.get('total_notifications')!r})", WHOLE_PERIOD)
        return found

    # Source of breach: parts of the total.
    sources = {f: _num(record.get(f)) for f in SOURCE_COUNT_FIELDS}
    for f, v in sources.items():
        if record.get(f) is not None and (v is None or v < 0):
            add("source-split", f"{f}={record.get(f)!r} is not a count", WHOLE_PERIOD)
        elif v is not None and v > total:
            add("source-split", f"{f}={v:g} exceeds total_notifications={total:g}", WHOLE_PERIOD)
    present = {f: v for f, v in sources.items() if v is not None and v >= 0}
    source_sum = sum(present.values())
    if present and source_sum > total:
        add("source-split", f"source counts sum to {source_sum:g}, more than "
                            f"total_notifications={total:g}", WHOLE_PERIOD)
    elif len(present) == len(SOURCE_COUNT_FIELDS) and source_sum != total:
        add("source-split", f"the complete source split sums to {source_sum:g}, not "
                            f"total_notifications={total:g}", WHOLE_PERIOD)

    # Percentages.
    for f in DONUT_PCT_FIELDS + ("cyber_incidents_percentage",):
        v = record.get(f)
        if v is not None and (_num(v) is None or not 0 <= _num(v) <= 100):
            add("percentage", f"{f}={v!r} is not a percentage", (f,))
    donut = [_num(record.get(f)) for f in DONUT_PCT_FIELDS]
    if all(v is not None for v in donut) and sum(donut) > 100 + PCT_SUM_TOLERANCE:
        add("percentage", f"source percentages sum to {sum(donut):g}%", DONUT_PCT_FIELDS)

    # Cyber incidents: a subset of malicious attacks, which is a subset of the total.
    cyber = _num(record.get("cyber_incidents_total"))
    malicious = sources.get("malicious_attacks")
    cyber_fields = ("cyber_incidents_total", "cyber_incidents_percentage")
    if cyber is not None:
        if cyber < 0 or cyber > total:
            add("cyber-incidents", f"cyber_incidents_total={cyber:g} is outside 0..total "
                                   f"({total:g})", cyber_fields)
        elif malicious is not None and cyber > malicious:
            add("cyber-incidents", f"cyber_incidents_total={cyber:g} exceeds "
                                   f"malicious_attacks={malicious:g} (cyber incidents are a "
                                   "subset of malicious attacks)",
                cyber_fields + ("malicious_attacks",))
    narrow = _num(record.get("cyber_incident_only"))
    if narrow is not None and malicious is not None and narrow > malicious:
        add("cyber-incidents", f"cyber_incident_only={narrow:g} exceeds "
                               f"malicious_attacks={malicious:g}",
            ("cyber_incident_only", "malicious_attacks"))

    # Attack methods: a partition of cyber incidents.
    methods = {f: _num(record.get(f)) for f in METHOD_FIELDS}
    bad_method = [f for f in METHOD_FIELDS
                  if record.get(f) is not None and (methods[f] is None or methods[f] < 0
                                                    or methods[f] > total)]
    if bad_method:
        add("attack-methods", f"{', '.join(bad_method)} not a count within the period total",
            METHOD_FIELDS)
    else:
        method_sum = sum(v for v in methods.values() if v is not None)
        base_field, base = next(
            ((f, _num(record.get(f))) for f in ("cyber_incident_only", "cyber_incidents_total")
             if _num(record.get(f)) is not None),
            ("total_notifications", total))
        if method_sum > base:
            fields = METHOD_FIELDS + ((base_field,) if base_field != "total_notifications" else ())
            if base_field == "cyber_incidents_total":
                fields += ("cyber_incidents_percentage",)
            add("attack-methods", f"attack-method counts sum to {method_sum:g}, more than "
                                  f"{base_field}={base:g}", fields)

    # Monthly notifications: parts of the semester.
    monthly, monthly_bad = _list_counts(record.get("monthly_notifications"), "count")
    if monthly_bad:
        add("monthly", "monthly_notifications has malformed entries", ("monthly_notifications",))
    elif monthly:
        start, end = record.get("start_month"), record.get("end_month")
        months_in_period = (end - start + 1) if isinstance(start, int) and isinstance(end, int) else 6
        if sum(monthly) > total:
            add("monthly", f"monthly counts sum to {sum(monthly):g}, more than "
                           f"total_notifications={total:g}", WHOLE_PERIOD)
        elif len(monthly) == months_in_period and sum(monthly) != total:
            add("monthly", f"all {len(monthly)} monthly counts sum to {sum(monthly):g}, not "
                           f"total_notifications={total:g}", WHOLE_PERIOD)

    # Top sectors: each a part of the total, and together too.
    sectors, sectors_bad = _list_counts(record.get("top_sectors"), "notifications")
    if sectors_bad:
        add("top-sectors", "top_sectors has malformed entries", ("top_sectors",))
    elif sectors and (max(sectors) > total or sum(sectors) > total):
        add("top-sectors", f"top-sector counts (max {max(sectors):g}, sum {sum(sectors):g}) "
                           f"exceed total_notifications={total:g}", ("top_sectors",))

    # Individuals-affected distribution: parts of the total.
    dist, dist_bad = _list_counts(record.get("individuals_affected_distribution"), "count")
    if dist_bad:
        add("individuals-affected", "individuals_affected_distribution has malformed entries",
            ("individuals_affected_distribution",))
    elif dist and sum(dist) > total:
        add("individuals-affected", f"individuals-affected buckets sum to {sum(dist):g}, more "
                                    f"than total_notifications={total:g}",
            ("individuals_affected_distribution",))

    # Personal information types: each a count of notifications.
    pit = record.get("personal_info_types")
    if pit is not None:
        if not isinstance(pit, dict):
            add("personal-info", "personal_info_types is not a mapping", ("personal_info_types",))
        else:
            bad = [k for k, v in pit.items()
                   if v is not None and (_num(v) is None or _num(v) < 0 or _num(v) > total)]
            if bad:
                add("personal-info", f"personal_info_types {', '.join(bad)} not a count within "
                                     f"the period total", ("personal_info_types",))

    # Time-to-identify / notify percentages: a distribution over buckets.
    for f in ("time_to_identify_pct", "time_to_notify_pct", "time_to_identify", "time_to_notify"):
        pcts, bad = _list_counts(record.get(f), "current_pct")
        if bad or any(p > 100 for p in pcts):
            add("time-buckets", f"{f} has malformed percentages", (f,))
        elif pcts and sum(pcts) > 100 + PCT_SUM_TOLERANCE:
            add("time-buckets", f"{f} percentages sum to {sum(pcts):g}%", (f,))

    return found


def known_defects(record: Dict[str, Any]) -> List[Contradiction]:
    """Figures known to be computed wrongly by an earlier scraper version."""
    out: List[Contradiction] = []
    if (record.get("source") == POWER_BI_SOURCE
            and record.get("attack_method_basis") != "cyber_incidents"
            and any(record.get(f) is not None for f in METHOD_FIELDS)):
        out.append(Contradiction(
            "attack-method-base",
            "Power BI per-method counts were computed as percentages of malicious attacks, "
            "but the percentages partition cyber incidents (counts overstated)",
            METHOD_FIELDS))
    return out


def screen_record(record: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[Contradiction]]:
    """Withhold every figure involved in a contradiction or a known defect.

    Returns ``(screened_copy, issues)``. ``screened_copy`` is None when the whole
    period is withheld. Otherwise involved fields are set to None and listed in
    ``screened_copy["withheld"]`` as ``{"fields": [...], "reason": ...}`` so the
    dashboard can say what it is not showing and why.
    """
    issues = find_contradictions(record) + known_defects(record)
    if any(c.whole_period for c in issues):
        return None, issues
    out = copy.deepcopy(record)
    withheld = list(out.get("withheld") or [])
    for c in issues:
        for f in c.fields:
            out[f] = None
        withheld.append({"fields": list(c.fields), "reason": c.message, "check": c.check})
    if withheld:
        out["withheld"] = withheld
    return out, issues
