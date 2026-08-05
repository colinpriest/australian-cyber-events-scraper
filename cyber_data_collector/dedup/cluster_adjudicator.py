"""Cluster-level adjudication: partition a group of records into incidents.

**Why this exists.** Pairwise adjudication has a structural blind spot. When one
breach generates a long tail of coverage - the incident report, the regulator
statement, the class action, "government concludes formal response" - each
article emphasises something different. Asked about any two of them in
isolation, an LLM keeps answering "different focus, therefore different
incident", and it does so *consistently*, so re-checking pairs does not help.
Observed on the live database: HWL Ebsworth fragmented into 19 separate events,
all dated 2023-04-26, with every pairwise re-check upholding the split.

Showing the whole cluster at once removes the blind spot. The model can see
that six records are follow-up coverage of one breach and two others are a
different incident entirely, because it is looking at them together. It also
returns a *partition*, which is internally consistent by construction - N
pairwise verdicts can contradict each other (A~B, B~C, A!~C), a partition
cannot.

Cost also improves: one call per cluster instead of N(N-1)/2 pairwise calls.
A 13-member cluster goes from 78 calls to 1.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field, model_validator

from cyber_data_collector.dedup.adjudicator import EventRecord
from cyber_data_collector.dedup.entity_resolution import EntityResolver

logger = logging.getLogger(__name__)

CLUSTER_MODEL = "gpt-4o"
# Above this many members the prompt gets unwieldy and the model starts losing
# track of indices. Larger clusters are chunked.
MAX_CLUSTER_SIZE = 20


class IncidentGroup(BaseModel):
    """One real-world incident and the records that describe it."""

    label: str = Field(
        description="Short human-readable name for the incident, e.g. "
                    "'HWL Ebsworth ransomware breach (April 2023)'."
    )
    member_indices: List[int] = Field(
        description="1-based indices of every record describing THIS incident."
    )
    certainty: float = Field(
        ge=0.0, le=1.0,
        description="0.0-1.0 confidence that these records are one incident.",
    )
    reasoning: str = Field(
        description="One or two sentences naming the decisive shared facts."
    )


class ClusterPartition(BaseModel):
    """A complete partition of the cluster into distinct incidents."""

    incidents: List[IncidentGroup] = Field(
        description="Every distinct incident. Each input record must appear in "
                    "exactly one incident."
    )

    @model_validator(mode="after")
    def _non_empty(self):
        if not self.incidents:
            raise ValueError("partition must contain at least one incident")
        for group in self.incidents:
            if not group.member_indices:
                raise ValueError(f"incident {group.label!r} has no members")
        return self

    def validate_covers(self, size: int) -> List[str]:
        """Return the ways this partition fails to cover 1..size exactly once.

        A partition that drops or double-assigns a record would silently lose
        or duplicate an event, so this is checked before anything is applied.
        """
        problems: List[str] = []
        seen: Dict[int, int] = {}
        for group in self.incidents:
            for index in group.member_indices:
                if not 1 <= index <= size:
                    problems.append(f"index {index} out of range 1..{size}")
                    continue
                seen[index] = seen.get(index, 0) + 1

        missing = sorted(set(range(1, size + 1)) - set(seen))
        if missing:
            problems.append(f"records not assigned to any incident: {missing}")
        duplicated = sorted(i for i, n in seen.items() if n > 1)
        if duplicated:
            problems.append(f"records assigned to more than one incident: {duplicated}")
        return problems

    def groups_as_ids(self, records: Sequence[EventRecord]) -> List[Tuple[IncidentGroup, List[str]]]:
        """Map each incident group to the record ids it contains."""
        out: List[Tuple[IncidentGroup, List[str]]] = []
        for group in self.incidents:
            ids = [
                records[i - 1].enriched_event_id
                for i in group.member_indices
                if 1 <= i <= len(records)
            ]
            if ids:
                out.append((group, ids))
        return out


def singleton_partition(size: int, label: str = "ungrouped") -> ClusterPartition:
    """Fallback partition placing every record in its own incident.

    Used when adjudication is unavailable or invalid: it changes nothing when
    applied, which is the safe default.
    """
    return ClusterPartition(incidents=[
        IncidentGroup(
            label=f"{label} #{i}",
            member_indices=[i],
            certainty=0.0,
            reasoning="No cluster adjudication available; left unchanged.",
        )
        for i in range(1, size + 1)
    ])


class ClusterAdjudicator:
    """Partitions a cluster of records into distinct real-world incidents."""

    def __init__(
        self,
        resolver: Optional[EntityResolver] = None,
        openai_client=None,
        model: str = CLUSTER_MODEL,
        max_cluster_size: int = MAX_CLUSTER_SIZE,
    ):
        self.resolver = resolver or EntityResolver()
        self._client = openai_client
        self._client_ready = openai_client is not None
        self.model = model
        self.max_cluster_size = max_cluster_size
        self.stats: Dict[str, int] = {
            "clusters": 0, "llm_calls": 0, "llm_failures": 0,
            "invalid_partitions": 0, "chunked": 0,
        }

    def _get_client(self):
        if self._client_ready:
            return self._client
        self._client_ready = True
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set; cluster adjudication disabled")
            self._client = None
            return None
        try:
            import instructor
            from openai import OpenAI

            self._client = instructor.from_openai(OpenAI(api_key=api_key))
        except ImportError as exc:
            logger.warning("instructor/openai unavailable (%s)", exc)
            self._client = None
        return self._client

    # ------------------------------------------------------------------
    # Cluster construction
    # ------------------------------------------------------------------

    def build_clusters(
        self, records: Sequence[EventRecord]
    ) -> List[List[EventRecord]]:
        """Group records that could plausibly describe the same incident.

        Deliberately date-agnostic: the point is to catch coverage spread over
        several dates, which the date-keyed Rule 1 consolidation cannot.

        Clusters are **buckets, not a transitive closure**. An earlier version
        unioned records that shared any distinctive title token, which chained
        807 of 845 events into a single cluster - A shares a word with B, B with
        C, and the whole corpus collapses. Each bucket here is independent:

        * one bucket per entity blocking key;
        * one bucket per *rare* title token (entity labels are unreliable - the
          OAIC is stored as the victim of the HWL Ebsworth breach - but every
          one of those records says "Ebsworth" in its title).

        Rarity is measured from corpus frequency, so "data" or "breach" never
        form a bucket. Buckets that are duplicates or subsets of another are
        dropped, and anything larger than twice the adjudication limit is
        discarded as too diffuse to be one incident.
        """
        self.resolver.fit([r.entity_name for r in records])

        freq: Dict[str, int] = {}
        for record in records:
            for token in self._title_tokens(record.title):
                freq[token] = freq.get(token, 0) + 1
        # A distinctive incident name legitimately appears in a dozen or more
        # titles ("Ebsworth" is in 11 of the 13 HWL records), so an aggressive
        # rarity cut-off silently prevents the very cluster this exists to
        # build. The earlier 807-event blow-up came from union-find
        # transitivity, not from the cut-off, and buckets are independent now -
        # so this can be generous, with the bucket-size cap below as the guard.
        ceiling = max(3, int(len(records) * 0.03))

        buckets: Dict[str, List[EventRecord]] = {}

        # Entity buckets.
        entity_reps: List[EventRecord] = []
        for record in records:
            for rep in entity_reps:
                if self.resolver.are_candidates(rep.entity_name, record.entity_name):
                    buckets.setdefault(f"entity:{rep.enriched_event_id}", [rep]).append(record)
                    break
            else:
                entity_reps.append(record)

        # Rare-title-token buckets.
        for record in records:
            for token in self._title_tokens(record.title):
                if freq.get(token, 0) <= ceiling:
                    buckets.setdefault(f"token:{token}", []).append(record)

        seen: Set[frozenset] = set()
        clusters: List[List[EventRecord]] = []
        for members in sorted(buckets.values(), key=len, reverse=True):
            unique = list({m.enriched_event_id: m for m in members}.values())
            if len(unique) < 2 or len(unique) > self.max_cluster_size * 2:
                continue
            key = frozenset(m.enriched_event_id for m in unique)
            if key in seen or any(key <= existing for existing in seen):
                continue
            seen.add(key)
            clusters.append(unique)
        return clusters

    @staticmethod
    def _title_tokens(title: Optional[str]) -> Set[str]:
        """Lower-cased word tokens of a title, ignoring short filler words."""
        import re as _re

        if not title:
            return set()
        return {t for t in _re.findall(r"[a-z0-9]+", title.lower()) if len(t) >= 4}

    # ------------------------------------------------------------------
    # Adjudication
    # ------------------------------------------------------------------

    def adjudicate_cluster(self, records: Sequence[EventRecord]) -> ClusterPartition:
        """Partition one cluster. Never raises; falls back to singletons."""
        self.stats["clusters"] += 1
        size = len(records)
        if size < 2:
            return singleton_partition(size)

        if size > self.max_cluster_size:
            self.stats["chunked"] += 1
            return self._adjudicate_chunked(records)

        client = self._get_client()
        if client is None:
            return singleton_partition(size)

        try:
            self.stats["llm_calls"] += 1
            partition: ClusterPartition = client.chat.completions.create(
                model=self.model,
                response_model=ClusterPartition,
                messages=[
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._render_cluster(records)},
                ],
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - a failed cluster must not abort a run
            self.stats["llm_failures"] += 1
            logger.warning("Cluster adjudication failed (%s); leaving unchanged", exc)
            return singleton_partition(size)

        problems = partition.validate_covers(size)
        if problems:
            self.stats["invalid_partitions"] += 1
            logger.warning(
                "Cluster partition invalid (%s); leaving unchanged", "; ".join(problems)
            )
            return singleton_partition(size)
        return partition

    def _adjudicate_chunked(self, records: Sequence[EventRecord]) -> ClusterPartition:
        """Adjudicate an oversized cluster in slices, then renumber.

        Chunking loses some cross-chunk context, so it is a fallback for very
        large clusters rather than the normal path.
        """
        incidents: List[IncidentGroup] = []
        for start in range(0, len(records), self.max_cluster_size):
            chunk = records[start:start + self.max_cluster_size]
            part = self.adjudicate_cluster(chunk)
            for group in part.incidents:
                incidents.append(IncidentGroup(
                    label=group.label,
                    member_indices=[start + i for i in group.member_indices],
                    certainty=group.certainty,
                    reasoning=group.reasoning,
                ))
        return ClusterPartition(incidents=incidents)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You group news and notification records by the REAL-WORLD SECURITY "
            "INCIDENT they describe.\n\n"
            "Rules:\n"
            "1. Records covering ONE incident belong together even when they "
            "emphasise different aspects. Initial reports, company statements, "
            "regulator investigations, class actions, parliamentary responses "
            "and 'X weeks later' follow-ups about the same breach are all the "
            "SAME incident.\n"
            "2. Two genuinely different incidents at the same organisation are "
            "DIFFERENT, even if reported on similar dates. Look for a different "
            "attack vector, a different victim population, or a different "
            "record count.\n"
            "3. Reported dates are unreliable and often wrong by months or "
            "years. Treat a date difference as weak evidence; the nature of "
            "the incident is much stronger evidence.\n"
            "4. Every record must be placed in exactly one incident. Never omit "
            "a record and never place one in two incidents.\n"
            "5. When two records could plausibly be one incident, prefer "
            "grouping them and lower your certainty, rather than splitting."
        )

    @staticmethod
    def _render_cluster(records: Sequence[EventRecord]) -> str:
        lines = [f"{len(records)} records, possibly describing the same organisation.",
                 "Partition them into distinct real-world incidents.", ""]
        for index, record in enumerate(records, start=1):
            body = (record.summary or record.description or "")[:320]
            entities = ", ".join(record.all_entities()[:4]) or "unknown"
            lines.append(
                f"[{index}] title: {record.title or '(untitled)'}\n"
                f"     organisations: {entities}\n"
                f"     date: {record.event_date or 'unknown'}   "
                f"records affected: "
                f"{record.records_affected if record.records_affected is not None else 'unknown'}\n"
                f"     detail: {body or '(none)'}"
            )
        return "\n".join(lines)
