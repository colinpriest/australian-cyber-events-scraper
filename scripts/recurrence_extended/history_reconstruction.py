"""Reconstructing the analysis inputs as known at a date, and checking the frozen cohorts against them.

``--as-known-at DATE`` rewinds the working copy of the snapshot (``cyber_data_collector/dedup/input_history.py``)
before any analysis reads it, so the incident links, victims, dates, size bands and record membership are those
recorded as known by DATE. A row the history does not reach back to has unknown state at DATE: the state recorded
when tracking began stands in, the row is listed in ``InputHistoryUnknown`` and the report counts it (and
``--history-unknown exclude`` drops the victim rows resting on it).

The registered prospective protocol is not changed by this. The two frozen cohorts
(``timing_peak_registration.json``, ``timing_size_adjusted_registration.json``) keep their meaning; a follow-up
evaluation can rerun with ``--as-known-at`` set to the registration date and the same ``--snapshot-id`` to see
what the database then showed, and :func:`registration_check` compares the cohort that reconstruction yields with
the frozen one.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

REGISTRATION_FILES = ("timing_peak_registration.json", "timing_size_adjusted_registration.json")

LIMITATION_WITHOUT_RECONSTRUCTION = (
    "Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, "
    "fields, and classifications. It is not a complete historical database snapshot; retrospective and "
    "prospective interpretations must account for that limitation. Rerun with `--as-known-at DATE` to "
    "reconstruct incident links, dates, victims and size bands from the recorded history of when each became "
    "known (`InputHistoryStatus` says since when that history exists).")

LIMITATION_WITH_RECONSTRUCTION = (
    "Incident links, victims, dates, size bands and record membership were reconstructed from the recorded history "
    "of when each became known (see the section above for which rows came from history and which are unknown at "
    "that date, holding the state recorded when tracking began as a stand-in). Other fields the analyses read (for example the attack type and records-affected "
    "band of an incident, the sector, and the text of the source records) are recorded only where listed there; "
    "the rest are present-day values. Retrospective and prospective interpretations must account for that.")


def derived_cohort(events: pd.DataFrame, cutoff: date) -> dict[str, tuple[str, list[str]]]:
    """``{entity_id: (anchor_date, anchor_ids)}`` as the prospective protocol derives a cohort.

    Incidents dated on or before ``cutoff`` and first discovered on or before it (an unknown discovery date
    cannot establish availability); each organisation is anchored on its latest such incident.
    """
    known = events[events["discovery_date_known"] & (events["event_date"] <= cutoff)] \
        if "discovery_date_known" in events else events.iloc[0:0]
    cohort: dict[str, tuple[str, list[str]]] = {}
    for entity, group in known.groupby("entity_id", sort=True):
        anchor = max(group["event_date"])
        ids = sorted(group.loc[group["event_date"] == anchor, "deduplicated_event_id"].astype(str).tolist())
        cohort[str(entity)] = (anchor.isoformat(), ids)
    return cohort


def registration_check(search_dirs: list[Path], events: pd.DataFrame, as_known_at_date: date) -> dict[str, Any]:
    """Compare the frozen cohorts with the cohort the reconstructed inputs give at the registration date.

    Read-only: the registration files are never written. Only run when ``as_known_at_date`` is the
    registration's own ``snapshot_cutoff`` (otherwise the comparison would mix dates); a different date is
    reported as not applicable. Returns one entry per registration file found, looking in each of
    ``search_dirs`` in turn (the run's own output folder, then the committed extended-results folder).
    """
    out: dict[str, Any] = {}
    for name in REGISTRATION_FILES:
        path = next((Path(d) / name for d in search_dirs if (Path(d) / name).is_file()), None)
        if path is None:
            continue
        registration = json.loads(path.read_text(encoding="utf-8"))
        cutoff = date.fromisoformat(registration.get("snapshot_cutoff") or registration["registered_on"])
        if cutoff != as_known_at_date:
            out[name] = {"status": "not applicable", "registration_snapshot_cutoff": cutoff.isoformat(),
                         "reason": f"--as-known-at is {as_known_at_date.isoformat()}, not the registration's "
                                   f"snapshot cutoff {cutoff.isoformat()}"}
            continue
        frozen = {str(item["entity_id"]): (item["anchor_date"], sorted(item.get("anchor_ids", [])))
                  for item in registration.get("cohort", [])}
        derived = derived_cohort(events, cutoff)
        both = frozen.keys() & derived.keys()
        out[name] = {"status": "compared", "registration_snapshot_cutoff": cutoff.isoformat(),
                     "frozen_organisations": len(frozen), "reconstructed_organisations": len(derived),
                     "in_both": len(both),
                     "same_anchor_date": sum(frozen[e][0] == derived[e][0] for e in both),
                     "same_anchor_events": sum(frozen[e] == derived[e] for e in both),
                     "only_in_frozen": len(frozen.keys() - derived.keys()),
                     "only_in_reconstruction": len(derived.keys() - frozen.keys()),
                     "interpretation": "Entity ids are the representative of each organisation's name variants at "
                                       "the reconstructed date; a difference means the database then showed a "
                                       "different incident list than the cohort froze, not that the cohort changed."}
    return out


def registration_markdown(check: dict[str, Any]) -> str:
    if not check:
        return ""
    lines = ["## Frozen cohorts against the reconstructed inputs", ""]
    for name, item in check.items():
        if item["status"] != "compared":
            lines.append(f"- `{name}`: not compared. {item['reason']}.")
            continue
        lines.append(f"- `{name}` (snapshot cutoff {item['registration_snapshot_cutoff']}): frozen "
                     f"{item['frozen_organisations']} organisations, reconstructed {item['reconstructed_organisations']}; "
                     f"{item['in_both']} in both ({item['same_anchor_date']} with the same anchor date, "
                     f"{item['same_anchor_events']} with the same anchor events); {item['only_in_frozen']} only frozen, "
                     f"{item['only_in_reconstruction']} only reconstructed.")
    lines += ["", "The frozen files are read, never written; the prospective protocol is unchanged.", ""]
    return "\n".join(lines)
