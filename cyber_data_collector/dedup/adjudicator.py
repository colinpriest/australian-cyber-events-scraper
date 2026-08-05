"""Candidate generation and pairwise adjudication for deduplication v3.

Three layers, cheapest first, each recording why it decided what it did:

1. **Blocking** (free) - :mod:`entity_resolution` buckets events whose victim
   names could plausibly refer to one organisation. Recall-oriented.
2. **Embeddings** (cheap) - semantic similarity over title+summary ranks the
   candidate pairs and discards the obviously-unrelated ones. This is what
   replaces v2's literal title comparison, which could not tell that "Qantas
   contact centre breach" and "6 million Qantas customers exposed" are one
   incident.
3. **LLM adjudication** (expensive, precise) - GPT-4o rules on the survivors
   and must justify itself in terms a reviewer can check.

Two short-circuits sit in front of all three: an active human override always
wins, and a shared source URL is near-conclusive.

Everything returns a :class:`PairVerdict` carrying certainty *and* evidence,
so nothing is merged on an unexplained number.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field

from cyber_data_collector.dedup.entity_resolution import (
    EntityResolver,
    name_similarity,
)
from cyber_data_collector.dedup.models import (
    DecidedBy,
    LLMPairAdjudication,
    MatchEvidence,
    PairVerdict,
)

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
ADJUDICATION_MODEL = "gpt-4o"

# Pairs below this embedding similarity are not worth an LLM call. Tuned to be
# permissive: a false negative here is a permanently missed merge, whereas a
# false positive only costs one adjudication.
EMBED_CANDIDATE_THRESHOLD = 0.55
# Above this, plus an entity match, we can merge without an LLM call.
EMBED_AUTOMERGE_THRESHOLD = 0.93
# Events more than this far apart are treated as separate incidents unless the
# LLM has strong reasons; OAIC/news coverage of one incident clusters tightly.
MAX_DATE_GAP_DAYS = 45


class EventRecord(BaseModel):
    """Minimal view of an enriched event needed to judge duplication."""

    enriched_event_id: str
    title: str = ""
    summary: Optional[str] = None
    description: Optional[str] = None
    entity_name: Optional[str] = None
    # Every organisation linked to the event. EnrichedEventEntities tags all of
    # them 'affected' with no victim/regulator distinction, so picking a single
    # highest-confidence name frequently returns a regulator ("Australian
    # Privacy Commissioner", "Federal Court") rather than the breached company.
    # Comparing the whole set avoids judging two reports of one incident to be
    # different organisations.
    alt_entities: List[str] = Field(default_factory=list)
    event_date: Optional[str] = None
    source_url: Optional[str] = None
    records_affected: Optional[int] = None

    def all_entities(self) -> List[str]:
        names = [self.entity_name] if self.entity_name else []
        names.extend(n for n in self.alt_entities if n)
        return names

    def embed_text(self) -> str:
        """Text handed to the embedding model."""
        parts = [self.title or ""]
        if self.entity_name:
            parts.append(f"Organisation: {self.entity_name}")
        body = self.summary or self.description or ""
        if body:
            parts.append(body[:600])
        return "\n".join(p for p in parts if p)

    def parsed_date(self) -> Optional[date]:
        if not self.event_date:
            return None
        text = str(self.event_date)[:10]
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity between two dense vectors."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_l = math.sqrt(sum(a * a for a in left))
    norm_r = math.sqrt(sum(b * b for b in right))
    if norm_l == 0.0 or norm_r == 0.0:
        return 0.0
    # Clamp: identical vectors accumulate rounding error and return values
    # marginally above 1.0 (1.0000000000000002), which fails the [-1, 1] bound
    # on MatchEvidence and aborted the whole run on the first exact duplicate.
    return max(-1.0, min(1.0, dot / (norm_l * norm_r)))


def date_gap_days(left: EventRecord, right: EventRecord) -> Optional[int]:
    left_date, right_date = left.parsed_date(), right.parsed_date()
    if left_date is None or right_date is None:
        return None
    return abs((left_date - right_date).days)


class Adjudicator:
    """Decides whether two enriched events describe the same incident.

    Args:
        resolver: Entity resolver, ideally already ``fit`` on the corpus.
        openai_client: Optional pre-built client. Injected in tests so no
            network call happens; when omitted a client is built lazily from
            ``OPENAI_API_KEY`` and the adjudicator degrades to rules plus
            lexical similarity if the key is missing.
        overrides: Mapping of frozenset pair-key -> bool (True == same event),
            normally loaded from ``DedupOverrides``.
    """

    def __init__(
        self,
        resolver: Optional[EntityResolver] = None,
        openai_client=None,
        overrides: Optional[Dict[frozenset, bool]] = None,
        embed_model: str = EMBEDDING_MODEL,
        adjudication_model: str = ADJUDICATION_MODEL,
        merge_threshold: float = 0.85,
        require_entity_match: bool = True,
    ):
        self.resolver = resolver or EntityResolver()
        self._client = openai_client
        self._client_ready = openai_client is not None
        self.overrides = overrides or {}
        self.embed_model = embed_model
        self.adjudication_model = adjudication_model
        self.merge_threshold = merge_threshold
        # When False, a non-matching entity is recorded as evidence but does not
        # by itself decide the pair. Needed when reviewing already-grouped
        # events, where the stored organisation labels are known to be noisy and
        # a hard entity gate would confidently break correct groups.
        self.require_entity_match = require_entity_match
        self._embeddings: Dict[str, List[float]] = {}
        self.stats: Dict[str, int] = {
            "override_hits": 0, "url_matches": 0, "embed_automerges": 0,
            "llm_calls": 0, "llm_failures": 0, "rejected_early": 0,
        }

    # ------------------------------------------------------------------
    # OpenAI plumbing
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client_ready:
            return self._client
        self._client_ready = True
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning(
                "OPENAI_API_KEY not set - adjudication falls back to lexical "
                "similarity only; merges will be more conservative."
            )
            self._client = None
            return None
        try:
            import instructor
            from openai import OpenAI

            self._client = instructor.from_openai(OpenAI(api_key=api_key))
        except ImportError as exc:
            logger.warning("instructor/openai unavailable (%s); rules only", exc)
            self._client = None
        return self._client

    def embed_records(self, records: Sequence[EventRecord], batch_size: int = 128) -> None:
        """Compute and cache embeddings for records that lack one."""
        pending = [r for r in records if r.enriched_event_id not in self._embeddings]
        if not pending:
            return
        client = self._get_client()
        if client is None:
            return

        # instructor wraps the client; embeddings live on the raw client.
        raw = getattr(client, "client", client)
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            try:
                response = raw.embeddings.create(
                    model=self.embed_model,
                    input=[r.embed_text() for r in chunk],
                )
                for record, item in zip(chunk, response.data):
                    self._embeddings[record.enriched_event_id] = item.embedding
            except Exception as exc:  # noqa: BLE001 - degrade, never crash dedup
                logger.warning("Embedding batch failed (%s); continuing without", exc)
                return
        logger.info("Embedded %d event(s)", len(pending))

    def similarity(self, left: EventRecord, right: EventRecord) -> Optional[float]:
        """Embedding cosine similarity, or None when embeddings are absent."""
        vec_l = self._embeddings.get(left.enriched_event_id)
        vec_r = self._embeddings.get(right.enriched_event_id)
        if vec_l is None or vec_r is None:
            return None
        return cosine_similarity(vec_l, vec_r)

    # ------------------------------------------------------------------
    # Candidate generation
    # ------------------------------------------------------------------

    def candidate_pairs(
        self, records: Sequence[EventRecord]
    ) -> List[Tuple[EventRecord, EventRecord]]:
        """Generate pairs worth adjudicating.

        Blocking on entity keys keeps this near-linear instead of comparing all
        N^2 pairs: at 1,000 events that is ~500k comparisons avoided.
        """
        by_id = {r.enriched_event_id: r for r in records}
        self.resolver.fit([r.entity_name for r in records])
        blocks = self.resolver.group_candidates(
            [(r.enriched_event_id, r.entity_name) for r in records]
        )

        seen: Set[frozenset] = set()
        pairs: List[Tuple[EventRecord, EventRecord]] = []
        for member_ids in blocks.values():
            if len(member_ids) < 2:
                continue
            for i, left_id in enumerate(member_ids):
                for right_id in member_ids[i + 1:]:
                    key = frozenset((left_id, right_id))
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append((by_id[left_id], by_id[right_id]))
        logger.info(
            "Blocking produced %d candidate pair(s) from %d event(s)",
            len(pairs), len(records),
        )
        return pairs

    # ------------------------------------------------------------------
    # Adjudication
    # ------------------------------------------------------------------

    def _entities_overlap(self, left: EventRecord, right: EventRecord) -> bool:
        """True when ANY organisation on one side matches any on the other.

        Set-vs-set rather than name-vs-name: a single "primary" entity is often
        a regulator rather than the victim, so comparing one apiece produces
        confident-but-wrong mismatches.
        """
        left_names = left.all_entities() or [None]
        right_names = right.all_entities() or [None]
        for left_name in left_names:
            for right_name in right_names:
                if self.resolver.are_candidates(left_name, right_name):
                    return True
        return False

    def _build_evidence(self, left: EventRecord, right: EventRecord) -> MatchEvidence:
        gap = date_gap_days(left, right)
        shared = []
        if left.source_url and right.source_url and left.source_url == right.source_url:
            shared.append(left.source_url)
        return MatchEvidence(
            entity_left=", ".join(left.all_entities()) or left.entity_name,
            entity_right=", ".join(right.all_entities()) or right.entity_name,
            entity_canonical_left=self.resolver.key_for(left.entity_name),
            entity_canonical_right=self.resolver.key_for(right.entity_name),
            entity_match=self._entities_overlap(left, right),
            date_left=left.event_date,
            date_right=right.event_date,
            date_delta_days=gap,
            title_left=left.title,
            title_right=right.title,
            embedding_similarity=self.similarity(left, right),
            title_similarity=name_similarity(left.title, right.title),
            shared_urls=shared,
        )

    def adjudicate(self, left: EventRecord, right: EventRecord,
                   force_llm: bool = False) -> PairVerdict:
        """Decide whether two events are the same incident, with reasons.

        Args:
            force_llm: Skip the embedding-similarity short-circuits so the pair
                is judged by the LLM. Used when deliberately re-examining
                borderline pairs: cosine similarity is symmetric, so an
                embedding-decided verdict cannot vary with record order and a
                position-swap robustness check on it is vacuous. Human
                overrides and shared-URL matches still short-circuit, since
                neither is a similarity judgement.
        """
        evidence = self._build_evidence(left, right)
        pair_key = frozenset((left.enriched_event_id, right.enriched_event_id))

        # 1. Human override always wins.
        if pair_key in self.overrides:
            self.stats["override_hits"] += 1
            same = self.overrides[pair_key]
            return PairVerdict(
                is_same_event=same,
                certainty=1.0,
                reasoning="Human override recorded for this pair.",
                decided_by=DecidedBy.HUMAN,
                evidence=evidence,
            )

        # 2. Identical source URL - the same article cannot be two incidents.
        if evidence.shared_urls:
            self.stats["url_matches"] += 1
            return PairVerdict(
                is_same_event=True,
                certainty=0.99,
                reasoning=f"Both events cite the same source URL: {evidence.shared_urls[0]}",
                decided_by=DecidedBy.RULE,
                evidence=evidence,
            )

        # 3. Cheap rejections before spending an LLM call.
        if self.require_entity_match and not evidence.entity_match:
            self.stats["rejected_early"] += 1
            return PairVerdict(
                is_same_event=False,
                certainty=0.9,
                reasoning=(
                    f"Victim organisations do not resolve to the same entity "
                    f"({evidence.entity_canonical_left!r} vs "
                    f"{evidence.entity_canonical_right!r})."
                ),
                decided_by=DecidedBy.RULE,
                evidence=evidence,
            )

        similarity = evidence.embedding_similarity
        if (not force_llm and similarity is not None
                and similarity < EMBED_CANDIDATE_THRESHOLD):
            self.stats["rejected_early"] += 1
            return PairVerdict(
                is_same_event=False,
                certainty=0.8,
                reasoning=(
                    f"Same organisation, but the reports are semantically "
                    f"unrelated (embedding similarity {similarity:.2f}); likely "
                    "two different incidents at one organisation."
                ),
                decided_by=DecidedBy.EMBEDDING,
                evidence=evidence,
            )

        # 4. Very high semantic similarity plus entity match - no LLM needed.
        if (not force_llm and similarity is not None
                and similarity >= EMBED_AUTOMERGE_THRESHOLD
                and (evidence.date_delta_days or 0) <= MAX_DATE_GAP_DAYS):
            self.stats["embed_automerges"] += 1
            return PairVerdict(
                is_same_event=True,
                certainty=min(0.95, similarity),
                reasoning=(
                    f"Same organisation and near-identical reporting "
                    f"(embedding similarity {similarity:.2f}, dates "
                    f"{evidence.date_delta_days} day(s) apart)."
                ),
                decided_by=DecidedBy.EMBEDDING,
                evidence=evidence,
            )

        # 5. Ambiguous - ask the LLM.
        return self._llm_adjudicate(left, right, evidence)

    def _llm_adjudicate(
        self, left: EventRecord, right: EventRecord, evidence: MatchEvidence
    ) -> PairVerdict:
        client = self._get_client()
        if client is None:
            # No LLM available: fall back to conservative lexical judgement.
            lexical = evidence.title_similarity or 0.0
            same = lexical >= 0.75 and (evidence.date_delta_days or 999) <= 7
            return PairVerdict(
                is_same_event=same,
                certainty=0.6,
                reasoning=(
                    f"No LLM available; fell back to title similarity "
                    f"{lexical:.2f} and a {evidence.date_delta_days}-day date gap."
                ),
                decided_by=DecidedBy.RULE,
                evidence=evidence,
            )

        prompt = self._render_prompt(left, right, evidence)
        try:
            self.stats["llm_calls"] += 1
            result: LLMPairAdjudication = client.chat.completions.create(
                model=self.adjudication_model,
                response_model=LLMPairAdjudication,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You decide whether two news/notification records "
                            "describe the SAME real-world cyber security "
                            "incident. Two different incidents at the same "
                            "organisation are NOT the same event. Follow-up "
                            "coverage, regulatory action and class actions "
                            "about one incident ARE the same event. Be decisive "
                            "when the evidence is clear and explicitly "
                            "uncertain when it is not."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - never let one pair break a run
            self.stats["llm_failures"] += 1
            logger.warning("LLM adjudication failed (%s); defaulting to separate", exc)
            return PairVerdict(
                is_same_event=False,
                certainty=0.3,
                reasoning=f"LLM adjudication failed ({exc}); kept separate for safety.",
                decided_by=DecidedBy.RULE,
                evidence=evidence,
            )

        evidence.supporting_facts = result.supporting_facts
        evidence.distinguishing_facts = result.distinguishing_facts
        return PairVerdict(
            is_same_event=result.is_same_event,
            certainty=result.certainty,
            reasoning=result.reasoning,
            decided_by=DecidedBy.LLM,
            evidence=evidence,
        )

    @staticmethod
    def _render_prompt(
        left: EventRecord, right: EventRecord, evidence: MatchEvidence
    ) -> str:
        def block(tag: str, rec: EventRecord) -> str:
            body = (rec.summary or rec.description or "")[:700]
            return (
                f"--- RECORD {tag} ---\n"
                f"Title: {rec.title}\n"
                f"Organisation: {rec.entity_name or 'unknown'}\n"
                f"Date: {rec.event_date or 'unknown'}\n"
                f"Records affected: {rec.records_affected if rec.records_affected is not None else 'unknown'}\n"
                f"URL: {rec.source_url or 'unknown'}\n"
                f"Detail: {body or '(none)'}\n"
            )

        computed = (
            f"--- COMPUTED SIGNALS ---\n"
            f"Canonical entity keys: {evidence.entity_canonical_left!r} vs "
            f"{evidence.entity_canonical_right!r}\n"
            f"Date gap (days): {evidence.date_delta_days}\n"
            f"Embedding similarity: "
            f"{evidence.embedding_similarity if evidence.embedding_similarity is not None else 'n/a'}\n"
        )
        return (
            block("A", left) + "\n" + block("B", right) + "\n" + computed +
            "\nDo these two records describe the same real-world incident?"
        )
