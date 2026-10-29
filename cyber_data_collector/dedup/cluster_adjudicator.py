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

**Clusters too big to show whole.** The prompt loses track of indices past
``MAX_CLUSTER_SIZE`` records. Such a cluster used to be skipped (a bucket of
more than twice the limit) or cut into disjoint slices, so two reports of one
supplier incident on opposite sides of a cut were never judged together. It is
now covered by bounded, overlapping neighbourhoods of strongly linked events
whose answers are reconciled (:mod:`cyber_data_collector.dedup.neighbourhoods`),
within an explicit ``call_budget``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field, model_validator

from cyber_data_collector.dedup import adjudication_cache as stored
from cyber_data_collector.dedup import candidate_ranking as ranking
from cyber_data_collector.dedup import excerpts
from cyber_data_collector.dedup import neighbourhoods as nb
from cyber_data_collector.dedup.adjudicator import EventRecord
from cyber_data_collector.dedup.entity_resolution import EntityResolver
from cyber_data_collector.utils.llm_models import chat_params, openai_model

logger = logging.getLogger(__name__)

# Above this many members the prompt gets unwieldy and the model starts losing
# track of indices. Larger clusters are covered by overlapping neighbourhoods (dedup/neighbourhoods.py).
MAX_CLUSTER_SIZE = 20
# A bucket larger than this is too diffuse to be one incident's coverage (a surname, a country). Below it, a
# bucket bigger than MAX_CLUSTER_SIZE is covered by overlapping neighbourhoods instead of being skipped or
# cut into disjoint slices; above it the bucket is counted in ``stats["buckets_too_diffuse"]`` and logged.
MAX_BUCKET_SIZE = 400
#: Bumped when the cluster prompt or what it shows changes (v2: excerpts instead of an opening).
CLUSTER_PROMPT_VERSION = "cluster-partition-v2"


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


def partition_from_incidents(records: Sequence[EventRecord], incidents: Sequence[nb.Incident]) -> ClusterPartition:
    """A full partition of ``records``: each reconciled incident as one group, every other record on its own."""
    index = {r.enriched_event_id: i for i, r in enumerate(records, start=1)}
    groups: List[IncidentGroup] = []
    placed: Set[str] = set()
    for incident in incidents:
        members = [i for i in incident.ids if i in index]
        if len(members) < 2:
            continue
        groups.append(IncidentGroup(
            label=incident.label or "reconciled incident", member_indices=sorted(index[i] for i in members),
            certainty=incident.certainty,
            reasoning=incident.reasoning or "Reconciled from overlapping neighbourhoods."))
        placed.update(members)
    for record in records:
        if record.enriched_event_id not in placed:
            groups.append(IncidentGroup(
                label=f"ungrouped #{index[record.enriched_event_id]}",
                member_indices=[index[record.enriched_event_id]],
                certainty=0.0, reasoning="No neighbourhood placed this record with another."))
    return ClusterPartition(incidents=groups)


class ClusterAdjudicator:
    """Partitions a cluster of records into distinct real-world incidents."""

    def __init__(
        self,
        resolver: Optional[EntityResolver] = None,
        openai_client=None,
        model: Optional[str] = None,
        max_cluster_size: int = MAX_CLUSTER_SIZE,
        cache: Optional["stored.AdjudicationCache"] = None,
        call_budget: Optional[int] = None,
        neighbours: int = nb.NEIGHBOURS_PER_EVENT,
        link_min_certainty: float = nb.LINK_MIN_CERTAINTY,
        blocked: Optional[nb.Blocked] = None,
        max_bucket_size: int = MAX_BUCKET_SIZE,
    ):
        self.resolver = resolver or EntityResolver()
        self._client = openai_client
        self._client_ready = openai_client is not None
        self.model = model or openai_model()
        self.max_cluster_size = max_cluster_size
        # Persisted answers (None: every cluster is asked, as before).
        self.cache = cache
        # Model calls this adjudicator may make (cached answers do not count); None = no bound.
        self.call_budget = call_budget
        self.neighbours = neighbours
        self.link_min_certainty = link_min_certainty
        # ``(a, b) -> reason`` for a pair a ``different`` ruling or label forbids joining.
        self.blocked = blocked
        self.max_bucket_size = max_bucket_size
        self.stats: Dict[str, int] = {
            "clusters": 0, "llm_calls": 0, "llm_failures": 0,
            "invalid_partitions": 0, "oversized": 0, "llm_cached": 0,
            "budget_skipped": 0, "buckets_too_diffuse": 0,
        }
        #: Plan and reconciliation figures of the last oversized cluster / candidate group planned.
        self.last_plan: Dict[str, Any] = {}

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
        dropped. A bucket larger than the adjudication limit is kept - it is
        covered by overlapping neighbourhoods when adjudicated - unless it is
        larger than ``max_bucket_size``, which is too diffuse to be one
        incident's coverage; those are counted and logged, never dropped
        silently.
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
            if len(unique) < 2:
                continue
            if len(unique) > self.max_bucket_size:
                self.stats["buckets_too_diffuse"] += 1
                logger.warning("Cluster bucket of %d events is larger than %d: too diffuse to be one "
                               "incident, not adjudicated", len(unique), self.max_bucket_size)
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
            self.stats["oversized"] += 1
            return self._adjudicate_oversized(records)

        partition = self.answer(records)
        return partition if partition is not None else singleton_partition(size)

    def calls_remaining(self) -> Optional[int]:
        """Model calls left in the budget (None: unbounded)."""
        if self.call_budget is None:
            return None
        return max(self.call_budget - self.stats["llm_calls"], 0)

    def answer(self, records: Sequence[EventRecord]) -> Optional[ClusterPartition]:
        """The valid partition of ``records`` (at most ``max_cluster_size``), or None when there is no answer.

        None means nothing was learned - no API client, a failed call, an invalid partition, or the call budget
        spent - and is *not* the same as an answer that keeps every record apart. A stored answer is returned
        whatever the budget (it costs nothing).
        """
        size = len(records)
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._render_cluster(records)},
        ]
        key = stored.decision_key("cluster", CLUSTER_PROMPT_VERSION, excerpts.EXCERPT_VERSION, self.model, messages)
        partition: Optional[ClusterPartition] = None
        was_cached = False
        if self.cache is not None:
            answer = self.cache.decision(key)
            if answer is not None:
                partition = ClusterPartition.model_validate(answer)
                self.stats["llm_cached"] += 1
                was_cached = True
        if partition is None:
            client = self._get_client()
            if client is None:
                return None
            if self.call_budget is not None and self.stats["llm_calls"] >= self.call_budget:
                self.stats["budget_skipped"] += 1
                return None

            try:
                self.stats["llm_calls"] += 1
                partition = client.chat.completions.create(
                    **chat_params(self.model),
                    response_model=ClusterPartition,
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001 - a failed cluster must not abort a run
                self.stats["llm_failures"] += 1
                logger.warning("Cluster adjudication failed (%s); leaving unchanged", exc)
                return None

        problems = partition.validate_covers(size)
        if problems:
            self.stats["invalid_partitions"] += 1
            logger.warning(
                "Cluster partition invalid (%s); leaving unchanged", "; ".join(problems)
            )
            return None
        if self.cache is not None and not was_cached:
            # Only a valid partition is kept: an invalid one is asked again next run.
            self.cache.store_decision(key, "cluster", CLUSTER_PROMPT_VERSION, excerpts.EXCERPT_VERSION, self.model,
                                      [r.enriched_event_id for r in records], partition.model_dump())
        return partition

    # ------------------------------------------------------------------
    # Clusters too big to show whole
    # ------------------------------------------------------------------

    def adjudicate_plan(
        self,
        plan: nb.Plan,
        records_by_id: Dict[str, EventRecord],
        links: Optional[Dict[nb.Pair, nb.Link]] = None,
        prepare: Optional[Callable[[List[EventRecord]], None]] = None,
        order: Callable[[EventRecord], Any] = lambda r: (r.event_date or "9999", r.enriched_event_id),
    ) -> nb.Reconciliation:
        """Ask each planned neighbourhood for a partition and reconcile the answers.

        Neighbourhoods are asked in plan order (most links settled first). Once the call budget is spent the rest
        are answered only from stored answers; what could not be asked is counted in ``stats["budget_skipped"]``
        and the next run continues. Only valid partitions take part in reconciliation.

        Args:
            plan: From :func:`neighbourhoods.plan`.
            records_by_id: Every record named by the plan.
            links: The evidence links, for choosing each incident's representative.
            prepare: Called with a neighbourhood's records before they are rendered (attach evidence excerpts).
            order: How a neighbourhood's records are ordered in the prompt (the answer cache keys on it).
        """
        answers: List[nb.NeighbourhoodAnswer] = []
        for neighbourhood in plan.neighbourhoods:
            records = sorted((records_by_id[i] for i in neighbourhood.members), key=order)
            if prepare is not None:
                prepare(records)
            partition = self.answer(records)
            if partition is None:
                continue
            groups = tuple(
                nb.GroupAnswer(tuple(ids), group.certainty, group.label, group.reasoning)
                for group, ids in partition.groups_as_ids(records))
            answers.append(nb.NeighbourhoodAnswer(tuple(r.enriched_event_id for r in records), groups))
        result = nb.reconcile(answers, min_certainty=self.link_min_certainty, blocked=self.blocked, links=links)
        self.last_plan = {**plan.stats(), "asked": len(plan.neighbourhoods), "answered": len(answers),
                          **{f"reconciled_{k}": v for k, v in result.stats().items()}}
        return result

    def _adjudicate_oversized(self, records: Sequence[EventRecord]) -> ClusterPartition:
        """Partition a cluster of more than ``max_cluster_size`` records through overlapping neighbourhoods."""
        by_id = {r.enriched_event_id: r for r in records}

        def gap_of(a: str, b: str) -> Optional[int]:
            left, right = by_id[a].parsed_date(), by_id[b].parsed_date()
            return abs((left - right).days) if left and right else None

        corpus = ranking.corpus_from_records(records)
        links = nb.links_within(list(by_id), corpus, gap_of)
        plan = nb.plan(links, max_size=self.max_cluster_size, neighbours=self.neighbours)
        result = self.adjudicate_plan(plan, by_id, links)
        return partition_from_incidents(records, result.incidents)

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
            "grouping them and lower your certainty, rather than splitting.\n"
            "6. A breach of ONE supplier's own systems (a SaaS platform, "
            "software vendor or service provider holding customers' data) "
            "reported by or about several of its customers is ONE incident, "
            "even though each record names a different organisation.\n"
            "7. The SAME ransomware gang, threat actor or campaign hitting "
            "different organisations is NOT one incident, and exploitation of "
            "one product vulnerability on each organisation's own systems is "
            "NOT one incident.\n"
            "8. Organisation fields may hold a description ('Queensland "
            "education sector') rather than a name, and a record may cite a "
            "roundup page while reporting one incident - judge by what the "
            "record says happened.\n"
            "9. Records sharing a source article are strong evidence of one "
            "incident unless the article plainly covers several.\n"
            "10. Use certainty >= 0.9 only when the shared facts (organisation, "
            "attack, data, supplier, article) make it clear.\n"
            "11. An event can contain a stray record about a different "
            "incident. Judge each record by its dominant content and do not let "
            "one stray detail pull two different incidents together."
        )

    @staticmethod
    def _render_cluster(records: Sequence[EventRecord]) -> str:
        lines = [f"{len(records)} records that may describe one or more shared incidents.",
                 "Partition them into distinct real-world incidents.", ""]
        for index, record in enumerate(records, start=1):
            body = record.evidence or (record.summary or record.description or "")[:320]
            entities = ", ".join(record.all_entities()[:4]) or "unknown"
            urls = ", ".join(record.all_urls()[:3]) or "unknown"
            lines.append(
                f"[{index}] title: {record.title or '(untitled)'}\n"
                f"     organisations: {entities}\n"
                f"     sources: {urls}\n"
                f"     date: {record.event_date or 'unknown'}   "
                f"records affected: "
                f"{record.records_affected if record.records_affected is not None else 'unknown'}\n"
                f"     detail: {body or '(none)'}"
            )
        return "\n".join(lines)
