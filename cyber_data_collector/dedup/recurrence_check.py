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
from cyber_data_collector.utils.llm_models import chat_params, openai_model

logger = logging.getLogger(__name__)

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
    #: Verified precision of the incident date (``day`` / ``month`` / ``year``) and the interval it
    #: allows (``processing/incident_dates.py``). All None = precision unverified.
    date_precision: Optional[str] = None
    date_earliest: Optional[date] = None
    date_latest: Optional[date] = None

    @property
    def has_verified_interval(self) -> bool:
        return self.date_earliest is not None and self.date_latest is not None

    @property
    def interval(self) -> Optional[Tuple[date, date]]:
        """The dates the incident may have happened on, or None when it is undated.

        A verified month is the whole month, and two events in the same month overlap. An event
        whose precision is unverified has no interval: its stored date is a stated date of unknown
        precision, and is used as that date alone only where ``build_runs`` is told to
        (``unverified_dates='stored'``), never presented as a verified day.
        """
        if self.has_verified_interval:
            return self.date_earliest, self.date_latest
        return None

    def span(self, unverified_dates: str = "stored") -> Optional[Tuple[date, date]]:
        """The interval used to order and chain this event: verified, else (if allowed) the stored date."""
        if self.has_verified_interval:
            return self.date_earliest, self.date_latest
        if unverified_dates == "stored" and self.event_date is not None:
            return self.event_date, self.event_date
        return None

    def gap_to(self, other: "RecurrenceEvent", unverified_dates: str = "stored") -> Optional[int]:
        """Days between this event's interval and ``other``'s: 0 when they overlap.

        None when either has no usable date. Two verified months in the same month, or a month and
        a day inside it, are 0 days apart - the incidents may well be the same day.
        """
        return interval_gap(self.span(unverified_dates), other.span(unverified_dates))


def interval_gap(a: Optional[Tuple[date, date]], b: Optional[Tuple[date, date]]) -> Optional[int]:
    """Days between two inclusive date intervals; 0 if they overlap or touch; None if either is unknown."""
    if a is None or b is None:
        return None
    if a[0] > b[0]:
        a, b = b, a
    return max(0, (b[0] - a[1]).days)


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


def load_recurrence_events(conn, include_undated: bool = False) -> List[RecurrenceEvent]:
    """Every active event, keyed to the organisation it happened to.

    Attribution is by the ``victim`` role rather than
    ``victim_organization_name``, matching the entity dashboard and the
    recurrence analysis: an event with several co-equal victims appears under
    each of them, and an event whose scalar victim was never populated is still
    covered. Undated events are left out (a run is built from gaps) unless
    ``include_undated`` - the duplicate check needs them: a missing date is
    no evidence of a separate incident. Each event carries its verified date
    precision and interval where the database records them (older schemas
    without the columns give unverified).
    """
    present = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    date_columns = ",\n               ".join(
        f"d.{column} AS {column}" if column in present else f"NULL AS {column}"
        for column in ("event_date_precision", "event_date_earliest", "event_date_latest"))
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
               {date_columns},
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
          AND (? OR (d.event_date IS NOT NULL AND d.event_date != ''))
          AND v.entity_name IS NOT NULL
          AND TRIM(v.entity_name) != ''
        ORDER BY v.entity_name COLLATE NOCASE, d.event_date
        """.replace("{date_columns}", date_columns), (int(include_undated),)
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
            date_precision=r["event_date_precision"],
            date_earliest=parse_event_date(r["event_date_earliest"]),
            date_latest=parse_event_date(r["event_date_latest"]),
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


UNVERIFIED_DATE_POLICIES = ("stored", "exclude")


def build_runs(
    events: Sequence[RecurrenceEvent],
    window_days: int = DEFAULT_WINDOW_DAYS,
    unverified_dates: str = "stored",
) -> List[List[RecurrenceEvent]]:
    """Group each organisation's events into runs of short-gap repeats.

    Within one entity, events are ordered by their date *interval* and chained
    while the gap between each event's interval and the run so far is under
    ``window_days`` - the inter-event time, which is what the question is
    about. The gap between intervals is 0 when they overlap: two events in the
    same verified month, or a verified month and a day inside it, may be the
    same day, so they are always candidates; measuring from a month's 1st would
    call them up to 30 days apart (or 59, for a month against the previous
    month's last day), and a gap measured from the 1st of one month to the 30th
    of the next would hide a pair that may have been a day apart.

    Chaining rather than pairing matters: three records six weeks apart each
    form a suspicious pair, and judging them as one run gives an answer that
    cannot contradict itself, where three pairwise verdicts can (A~B, B~C,
    A!~C).

    An event whose date precision is unverified has no interval. With
    ``unverified_dates='stored'`` (the default) its stored date stands in as
    the one date the pipeline holds for it, so that most events are not
    silently dropped from a safety check (on 2026-10-29, 702 of 1,090 active
    events); it is never given a month or a day it was not verified to have,
    and a run reports it (``unverified_in_run``). With ``'exclude'`` such
    events take no part, and an undated event never does.

    Runs are per entity, so the same event appears in a run under each
    organisation it is attributed to. Only runs of two or more are returned;
    isolated events have no repeat to check.
    """
    if unverified_dates not in UNVERIFIED_DATE_POLICIES:
        raise ValueError(f"unverified_dates must be one of {UNVERIFIED_DATE_POLICIES}, not {unverified_dates!r}")
    by_entity: Dict[int, List[RecurrenceEvent]] = {}
    for event in events:
        if event.span(unverified_dates) is None:
            continue
        by_entity.setdefault(event.entity_id, []).append(event)

    runs: List[List[RecurrenceEvent]] = []
    for members in by_entity.values():
        ordered = sorted(members, key=lambda e: (*e.span(unverified_dates), e.dedup_id))
        # De-duplicate the same dedup event appearing twice under one entity.
        seen: set = set()
        unique: List[RecurrenceEvent] = []
        for event in ordered:
            if event.dedup_id in seen:
                continue
            seen.add(event.dedup_id)
            unique.append(event)

        current: List[RecurrenceEvent] = []
        reach: Optional[date] = None      # the latest date any event of the run may have
        for event in unique:
            start, end = event.span(unverified_dates)
            if not current:
                current, reach = [event], end
                continue
            gap = max(0, (start - reach).days)
            if gap < window_days:
                current.append(event)
                reach = max(reach, end)
            else:
                if len(current) > 1:
                    runs.append(current)
                current, reach = [event], end
        if len(current) > 1:
            runs.append(current)
    return runs


def unverified_in_run(run: Sequence[RecurrenceEvent]) -> int:
    """How many events of a run are there on a stored date whose precision is unverified."""
    return sum(1 for event in run if not event.has_verified_interval)


def run_span(run: Sequence[RecurrenceEvent]) -> int:
    """Days from the earliest date the run's first event may have to the latest the last may have."""
    spans = [e.span() for e in run]
    if not spans or any(s is None for s in spans):
        return 0
    return (max(s[1] for s in spans) - min(s[0] for s in spans)).days


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
        model: Optional[str] = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        max_run_size: int = MAX_RUN_SIZE,
    ):
        self._client = openai_client
        self._client_ready = openai_client is not None
        self.model = model or openai_model()
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
                **chat_params(self.model),
                response_model=ClusterPartition,
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

    @staticmethod
    def render_date(event: RecurrenceEvent) -> str:
        """The stored date with what is known about its precision: never a month passed off as a day."""
        if not event.has_verified_interval:
            return f"{event.event_date} (precision unverified)"
        if event.date_earliest == event.date_latest:
            return f"{event.event_date} (exact day)"
        return (f"{event.event_date} ({event.date_precision or 'interval'} precision: any day from "
                f"{event.date_earliest} to {event.date_latest})")

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
            gap = ""
            if previous is not None:
                days = event.gap_to(previous)
                gap = (f" ({days} days after record {index - 1}"
                       + (", the dates' intervals overlap" if days == 0 else "") + ")")
            records = (f"{event.records_affected:,}"
                       if event.records_affected is not None else "unknown")
            detail = (event.summary or "")[:400] or "(none)"
            lines.append(
                f"[{index}] title: {event.title or '(untitled)'}\n"
                f"     stored date: {self.render_date(event)}{gap}\n"
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
        by_date = sorted(members, key=lambda e: (*e.span(), e.dedup_id))
        target, sources = ordered[0], ordered[1:]
        findings.append({
            "entity": run[0].entity_name,
            "entity_id": run[0].entity_id,
            "label": group.label,
            "certainty": group.certainty,
            "reasoning": group.reasoning,
            # Gaps between the events' date intervals (0 where they overlap), not between point dates.
            "max_gap_days": max(
                (a.gap_to(b) for a, b in zip(by_date, by_date[1:])), default=0),
            "span_days": run_span(by_date),
            "unverified_dates": unverified_in_run(members),
            "target": {"id": target.dedup_id, "title": target.title,
                       "date": str(target.event_date), "members": target.members},
            "sources": [{"id": s.dedup_id, "title": s.title,
                         "date": str(s.event_date), "members": s.members}
                        for s in sources],
        })
    return findings
