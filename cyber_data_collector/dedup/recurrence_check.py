"""Re-examine every apparent repeat attack that follows another within 90 days.

**The failure mode this exists for.** One breach is covered more than once, and
the coverage is published weeks or months apart: the initial report, then the
company statement, then the OAIC notification, then the class action, then "what
we now know" six weeks later. Each article carries its own publication date, and
if the extraction takes that date as the incident date, one incident becomes two
events dated eight weeks apart. Stored under the same organisation, that reads
as *the organisation was attacked twice in a quarter* - which is not what
happened.

Short-gap pairs are where this is both most likely and most damaging. Most
likely, because reporting lag is measured in weeks, so a spurious pair lands in
exactly this window; most damaging, because the short-elapsed-time band is the
one every recurrence model is estimated from
(``scripts/analyze_recurrent_timing.py``). A false duplicate there does not add
noise, it manufactures the signal: it puts a "repeat event" at a gap of a few
weeks that never occurred.

**Why this pass is not covered by the existing ones.** ``consolidate`` requires
the *same date*, so it never sees a pair eight weeks apart.
``adjudicate-clusters`` blocks on the entity and on rare title tokens, so a
follow-up article with a different headline may never enter the same cluster.
``reconcile-entities`` groups by victim entity but judges an organisation's
entire history at once, where a pair separated by weeks is one comparison among
many. This pass asks the narrow question directly, over the complete set of
short-gap pairs, and it asks it about a *run* rather than a pair so the verdicts
cannot contradict each other.

**Both errors are real.** Organisations genuinely are attacked twice in a
quarter, and merging two real incidents destroys information that cannot be
recovered from the merged row. The adjudicator is therefore given the
distinguishing evidence explicitly - attack method, record count, data types,
source URLs - and merges only above a certainty floor.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup.cluster_adjudicator import (
    ClusterPartition,
    IncidentGroup,
    singleton_partition,
)

logger = logging.getLogger(__name__)

RECURRENCE_MODEL = "gpt-4o"

# The window the user's question is about: a repeat attack arriving less than
# this many days after the previous one is suspicious enough to re-check.
DEFAULT_WINDOW_DAYS = 90

# Above this many events in one run the prompt stops being reliable; oversized
# runs are adjudicated in slices.
MAX_RUN_SIZE = 12


@dataclass
class RecurrenceEvent:
    """One deduplicated event as it appears under an organisation."""

    dedup_id: str
    entity_id: int
    entity_name: str
    title: str
    event_date: Optional[date]
    summary: Optional[str] = None
    records_affected: Optional[int] = None
    attack_method: Optional[str] = None
    event_type: Optional[str] = None
    vendor: Optional[str] = None
    members: int = 1
    source_urls: List[str] = field(default_factory=list)

    def gap_to(self, other: "RecurrenceEvent") -> Optional[int]:
        """Days between this event and ``other``, or None if either is undated."""
        if self.event_date is None or other.event_date is None:
            return None
        return abs((other.event_date - self.event_date).days)


def parse_event_date(value: Any) -> Optional[date]:
    """Lenient date parse; anything unrecognisable is treated as undated."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def load_recurrence_events(conn) -> List[RecurrenceEvent]:
    """Every active event, keyed to the organisation it happened to.

    Attribution is by the ``victim`` role rather than
    ``victim_organization_name``, matching the entity dashboard and the
    recurrence analysis: an event with several co-equal victims appears under
    each of them, and an event whose scalar victim was never populated is still
    covered.
    """
    rows = conn.execute(
        """
        SELECT dee.entity_id                      AS entity_id,
               v.entity_name                      AS entity_name,
               d.deduplicated_event_id            AS dedup_id,
               d.title                            AS title,
               d.event_date                       AS event_date,
               d.records_affected                 AS records_affected,
               d.attack_method                    AS attack_method,
               d.event_type                       AS event_type,
               d.vendor_organization_name         AS vendor,
               COALESCE(NULLIF(TRIM(d.summary), ''),
                        NULLIF(TRIM(d.description), ''),
                        NULLIF(TRIM(e.summary), ''),
                        NULLIF(TRIM(e.description), ''), '') AS summary,
               (SELECT COUNT(*) FROM EventDeduplicationMap m
                 WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS members
        FROM DeduplicatedEventEntities dee
        JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
        JOIN DeduplicatedEvents d
             ON d.deduplicated_event_id = dee.deduplicated_event_id
        LEFT JOIN EnrichedEvents e
             ON e.enriched_event_id = d.master_enriched_event_id
        WHERE dee.relationship_type = 'victim'
          AND COALESCE(d.status, 'Active') = 'Active'
          AND d.event_date IS NOT NULL
          AND v.entity_name IS NOT NULL
          AND TRIM(v.entity_name) != ''
        ORDER BY v.entity_name COLLATE NOCASE, d.event_date
        """
    ).fetchall()

    events: List[RecurrenceEvent] = []
    for r in rows:
        summary = (r["summary"] or "").strip()
        if summary.startswith("%PDF"):
            summary = ""
        events.append(RecurrenceEvent(
            dedup_id=r["dedup_id"],
            entity_id=r["entity_id"],
            entity_name=r["entity_name"],
            title=r["title"] or "",
            event_date=parse_event_date(r["event_date"]),
            summary=summary or None,
            records_affected=r["records_affected"],
            attack_method=r["attack_method"],
            event_type=r["event_type"],
            vendor=r["vendor"],
            members=r["members"] or 1,
        ))
    return events


def attach_source_urls(conn, events: Sequence[RecurrenceEvent],
                       per_event: int = 3) -> None:
    """Fill in a few source URLs per event, in place.

    URLs are strong evidence for this specific question: two records pointing at
    the same article, or at two articles from the same outlet days apart, are
    almost always one incident.
    """
    for event in events:
        rows = conn.execute(
            "SELECT source_url FROM DeduplicatedEventSources "
            "WHERE deduplicated_event_id = ? AND source_url IS NOT NULL LIMIT ?",
            (event.dedup_id, per_event)).fetchall()
        event.source_urls = [r[0] for r in rows if r[0]]


def build_runs(
    events: Sequence[RecurrenceEvent],
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> List[List[RecurrenceEvent]]:
    """Group each organisation's events into runs of short-gap repeats.

    Within one entity, events are sorted by date and chained while each
    *consecutive* gap is under ``window_days`` - the inter-event time, which is
    what the question is about. Chaining rather than pairing matters: three
    records six weeks apart each form a suspicious pair, and judging them as one
    run gives an answer that cannot contradict itself, where three pairwise
    verdicts can (A~B, B~C, A!~C).

    Runs are per entity, so the same event appears in a run under each
    organisation it is attributed to. Only runs of two or more are returned;
    isolated events have no repeat to check.
    """
    by_entity: Dict[int, List[RecurrenceEvent]] = {}
    for event in events:
        if event.event_date is None:
            continue
        by_entity.setdefault(event.entity_id, []).append(event)

    runs: List[List[RecurrenceEvent]] = []
    for members in by_entity.values():
        ordered = sorted(members, key=lambda e: (e.event_date, e.dedup_id))
        # De-duplicate the same dedup event appearing twice under one entity.
        seen: set = set()
        unique: List[RecurrenceEvent] = []
        for event in ordered:
            if event.dedup_id in seen:
                continue
            seen.add(event.dedup_id)
            unique.append(event)

        current: List[RecurrenceEvent] = []
        for event in unique:
            if not current:
                current = [event]
                continue
            gap = (event.event_date - current[-1].event_date).days
            if gap < window_days:
                current.append(event)
            else:
                if len(current) > 1:
                    runs.append(current)
                current = [event]
        if len(current) > 1:
            runs.append(current)
    return runs


def run_span(run: Sequence[RecurrenceEvent]) -> int:
    """Days from the first to the last event of a run."""
    if not run or run[0].event_date is None or run[-1].event_date is None:
        return 0
    return (run[-1].event_date - run[0].event_date).days


SYSTEM_PROMPT = """You decide whether records about one organisation describe ONE security incident or several.

Every record below concerns the same organisation and each follows the previous one by less than {window} days.

The dominant error in this dataset is one incident stored several times because its coverage was published weeks apart. An initial report, the organisation's own statement, the regulator's notification, a class action filing and a "what we now know" follow-up all describe the SAME incident, even though they carry different dates, different headlines and different emphasis. When the stored date is really the publication date, one breach becomes two events a month apart.

The opposite error is also real and is worse to make: organisations genuinely are attacked more than once in a quarter, and merging two real incidents destroys information that cannot be recovered.

Treat as evidence of ONE incident:
- the same number of records or individuals affected, or one figure being a later revision of the other
- the same attack method and the same categories of data
- the same threat actor, the same third-party supplier, or the same root cause
- one record describing the aftermath of the other (investigation, notification, remediation, litigation, parliamentary or regulatory response)
- overlapping source URLs, or coverage from the same outlet a few days apart

Treat as evidence of DIFFERENT incidents:
- different attack vectors (phishing versus ransomware versus a misconfigured server versus an insider)
- different affected populations, systems or business units
- record counts that differ materially and are not revisions of one another
- an explicit statement that this is a second, separate or unrelated incident
- the second incident occurring after the first was contained or remediated

Dates in this dataset are unreliable and are frequently the date of publication rather than the date of the incident. Treat a date difference of a few weeks as weak evidence either way.

Partition the records: every record must appear in exactly one incident, and no record in two. When two records could plausibly be one incident, group them and lower your certainty rather than splitting them."""


class RecurrenceAuditor:
    """Judges whether a run of short-gap repeats is really one incident."""

    def __init__(
        self,
        openai_client=None,
        model: str = RECURRENCE_MODEL,
        window_days: int = DEFAULT_WINDOW_DAYS,
        max_run_size: int = MAX_RUN_SIZE,
    ):
        self._client = openai_client
        self._client_ready = openai_client is not None
        self.model = model
        self.window_days = window_days
        self.max_run_size = max_run_size
        self.stats: Dict[str, int] = {
            "runs": 0, "llm_calls": 0, "llm_failures": 0,
            "invalid_partitions": 0, "chunked": 0,
        }

    def _get_client(self):
        if self._client_ready:
            return self._client
        self._client_ready = True
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set; recurrence audit disabled")
            self._client = None
            return None
        try:
            import instructor
            from openai import OpenAI

            self._client = instructor.from_openai(OpenAI(api_key=api_key))
        except ImportError as exc:  # pragma: no cover - dependency issue
            logger.warning("instructor/openai unavailable (%s)", exc)
            self._client = None
        return self._client

    def audit_run(self, run: Sequence[RecurrenceEvent]) -> ClusterPartition:
        """Partition one run into distinct incidents. Never raises.

        Any failure returns the singleton partition, which changes nothing -
        the safe default when the question could not be answered.
        """
        self.stats["runs"] += 1
        size = len(run)
        if size < 2:
            return singleton_partition(size)
        if size > self.max_run_size:
            self.stats["chunked"] += 1
            return self._audit_chunked(run)

        client = self._get_client()
        if client is None:
            return singleton_partition(size)

        try:
            self.stats["llm_calls"] += 1
            partition: ClusterPartition = client.chat.completions.create(
                model=self.model,
                response_model=ClusterPartition,
                temperature=0.0,
                messages=[
                    {"role": "system",
                     "content": SYSTEM_PROMPT.format(window=self.window_days)},
                    {"role": "user", "content": self.render_run(run)},
                ])
        except Exception as exc:  # noqa: BLE001 - one run must not abort a pass
            self.stats["llm_failures"] += 1
            logger.warning("Recurrence audit failed for %r (%s); leaving unchanged",
                           run[0].entity_name, exc)
            return singleton_partition(size)

        problems = partition.validate_covers(size)
        if problems:
            self.stats["invalid_partitions"] += 1
            logger.warning("Recurrence partition invalid for %r (%s); "
                           "leaving unchanged", run[0].entity_name,
                           "; ".join(problems))
            return singleton_partition(size)
        return partition

    def _audit_chunked(self, run: Sequence[RecurrenceEvent]) -> ClusterPartition:
        """Adjudicate an oversized run in overlapping-free slices, then renumber."""
        incidents: List[IncidentGroup] = []
        for start in range(0, len(run), self.max_run_size):
            chunk = run[start:start + self.max_run_size]
            part = self.audit_run(chunk)
            for group in part.incidents:
                incidents.append(IncidentGroup(
                    label=group.label,
                    member_indices=[start + i for i in group.member_indices],
                    certainty=group.certainty,
                    reasoning=group.reasoning,
                ))
        return ClusterPartition(incidents=incidents)

    def render_run(self, run: Sequence[RecurrenceEvent]) -> str:
        """The prompt body: each record, with the gap from the one before it.

        Gaps are stated explicitly because they are the reason the run is being
        examined, and the model should be able to see that "42 days later" is
        within the range of ordinary reporting lag.
        """
        entity = run[0].entity_name
        lines = [
            f'Organisation: {entity}',
            f'{len(run)} stored events, spanning {run_span(run)} days.',
            "",
        ]
        previous: Optional[RecurrenceEvent] = None
        for index, event in enumerate(run, start=1):
            gap = "" if previous is None else (
                f" ({(event.event_date - previous.event_date).days} days after "
                f"record {index - 1})")
            records = (f"{event.records_affected:,}"
                       if event.records_affected is not None else "unknown")
            detail = (event.summary or "")[:400] or "(none)"
            lines.append(
                f"[{index}] title: {event.title or '(untitled)'}\n"
                f"     stored date: {event.event_date}{gap}\n"
                f"     attack method: {event.attack_method or 'unknown'}   "
                f"type: {event.event_type or 'unknown'}\n"
                f"     records affected: {records}   "
                f"built from {event.members} source record(s)"
                + (f"   via vendor: {event.vendor}" if event.vendor else "")
                + ("\n     sources: " + ", ".join(event.source_urls[:3])
                   if event.source_urls else "")
                + f"\n     detail: {detail}"
            )
            previous = event
        return "\n".join(lines)


def findings_from_partition(
    run: Sequence[RecurrenceEvent],
    partition: ClusterPartition,
) -> List[Dict[str, Any]]:
    """Groups holding more than one record - the suspected false duplicates.

    Returns one finding per group that merges records, ordered so the event with
    the most source records is the merge target: folding a rich event into a
    thin one would discard the better lineage.
    """
    findings: List[Dict[str, Any]] = []
    for group in partition.incidents:
        members = [run[i - 1] for i in group.member_indices
                   if 1 <= i <= len(run)]
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda e: (-e.members, str(e.event_date)))
        by_date = sorted(members, key=lambda e: e.event_date)
        target, sources = ordered[0], ordered[1:]
        findings.append({
            "entity": run[0].entity_name,
            "entity_id": run[0].entity_id,
            "label": group.label,
            "certainty": group.certainty,
            "reasoning": group.reasoning,
            "max_gap_days": max(
                ((b.event_date - a.event_date).days
                 for a, b in zip(by_date, by_date[1:])), default=0),
            "span_days": run_span(by_date),
            "target": {"id": target.dedup_id, "title": target.title,
                       "date": str(target.event_date), "members": target.members},
            "sources": [{"id": s.dedup_id, "title": s.title,
                         "date": str(s.event_date), "members": s.members}
                        for s in sources],
        })
    return findings
