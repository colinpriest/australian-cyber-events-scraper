"""Incident-relevant, diverse excerpts of an event's source text for the adjudicators.

The adjudicators used to be shown the first few hundred characters of an event's
summary and of each member's description. The start of a page is its masthead,
a standfirst or a cookie banner; the facts that tell two incidents apart - which
organisation, what was done, when, how many records - sit further in. Judging
two reports from their openings is how a follow-up article and a second attack
look alike.

:func:`select_excerpts` picks, deterministically and within a character budget,
the passages that carry those facts:

* a passage scores for **naming the organisation**, for **naming the attack**
  (the words of the record's own attack method, attacker and title that say
  more than "breach" or "data"), for **stating a date** and for **stating a
  count**;
* sources are served **round-robin**, best passage first, so one long page
  cannot use the whole budget;
* a passage that **repeats** one already chosen (word overlap) is skipped, so
  a syndicated copy of the same paragraph is not shown twice;
* when nothing in a source scores, its **opening** is used, as before;
* chosen passages are shown **in source order**, so the excerpt still reads.

No word list is involved beyond the shared "generic words" of
:mod:`processing.active_evidence`. Everything that changes what the model sees is
versioned by :data:`EXCERPT_VERSION`, bumped whenever the scoring, the passage
splitting or the defaults change; the adjudication cache keys on the text that
was actually rendered, so a change re-asks only the cases it touches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from cyber_data_collector.processing import active_evidence

#: Bumped when passage splitting, scoring, selection or the defaults below change.
EXCERPT_VERSION = "excerpts-v1"

#: Characters of excerpts shown per event in a pair prompt / a cluster prompt (was 700 / 320 of an opening).
PAIR_EVIDENCE_CHARS = 900
CLUSTER_EVIDENCE_CHARS = 450
#: Longest passage kept whole; longer ones are cut at a word boundary.
MAX_PASSAGE_CHARS = 280
MIN_PASSAGE_CHARS = 40
MIN_PASSAGE_WORDS = 5
#: Passages kept per source before the sources are combined (bounds memory on long pages).
PASSAGES_PER_SOURCE = 6
#: Only this much of a source is read; the incident is reported long before the footer.
MAX_SOURCE_CHARS = 60_000
#: Two passages whose word sets overlap at least this much are one passage.
REDUNDANT_OVERLAP = 0.6
#: Separator between passages in the rendered excerpt.
SEPARATOR = " ... "

SCORE_ORGANISATION = 3
SCORE_ATTACK = 2
SCORE_DATE = 2
SCORE_COUNT = 2

_MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|"
           "august|september|october|november|december")
_DATE = re.compile(
    rf"\b(?:19|20)\d{{2}}\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b|\b(?:{_MONTHS})\s+\d{{1,2}}\b",
    re.IGNORECASE)
# A count: 130,000 / 1.2 million / 600000. A bare four-digit number is a year, not a count.
_COUNT = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?\s*(?:million|billion|thousand|m|k)\b|\b\d{5,}\b",
                    re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Anchors:
    """What to look for in a source: the organisation(s) and the attack."""

    organisations: Tuple[str, ...] = ()
    attack_words: frozenset = frozenset()

    @classmethod
    def for_record(cls, organisations: Iterable[Optional[str]], *attack_texts: Optional[str]) -> "Anchors":
        names = tuple(sorted({n.strip() for n in organisations if n and n.strip()}))
        org_words = {w for n in names for w in _WORD.findall(n.lower())}
        words = set()
        for text in attack_texts:
            words |= active_evidence.distinctive_words(text)
        return cls(names, frozenset(words - org_words))


@dataclass(frozen=True)
class Passage:
    text: str
    score: int
    position: int       # order in its source

    def words(self) -> Set[str]:
        return set(_WORD.findall(self.text.lower()))


def split_passages(text: Optional[str]) -> List[str]:
    """Sentences and lines of ``text`` fit to show: long enough to say something, cut at word boundaries."""
    out: List[str] = []
    for chunk in re.split(r"[\r\n]+", text or ""):
        for sentence in _SENTENCE_END.split(chunk):
            sentence = " ".join(sentence.split())
            while len(sentence) > MAX_PASSAGE_CHARS:
                cut = sentence.rfind(" ", 0, MAX_PASSAGE_CHARS)
                cut = cut if cut > MAX_PASSAGE_CHARS // 2 else MAX_PASSAGE_CHARS
                out.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if len(sentence) < MIN_PASSAGE_CHARS or len(sentence.split()) < MIN_PASSAGE_WORDS:
                continue
            letters = sum(c.isalpha() for c in sentence)
            if letters < 0.6 * len(sentence):      # menus, tables of numbers, link lists
                continue
            out.append(sentence)
    return out


def score_passage(passage: str, anchors: Anchors) -> int:
    """How much a passage says about *this* incident; 0 when it names neither the organisation nor the attack.

    A date or a count alone does not make a passage relevant: a roundup page
    is full of them, about other organisations. They only add weight to a
    passage that is already about the organisation or the attack.
    """
    score = 0
    if active_evidence.names_any_organisation(anchors.organisations, passage):
        score += SCORE_ORGANISATION
    if anchors.attack_words and anchors.attack_words & set(_WORD.findall(passage.lower())):
        score += SCORE_ATTACK
    if score == 0:
        return 0
    if _DATE.search(passage):
        score += SCORE_DATE
    if _COUNT.search(passage):
        score += SCORE_COUNT
    return score


def source_passages(text: Optional[str], anchors: Anchors, limit: int = PASSAGES_PER_SOURCE) -> List[Passage]:
    """The ``limit`` best passages of one source (its opening when none scores), in source order."""
    scored = [Passage(p, score_passage(p, anchors), i) for i, p in enumerate(split_passages(text))]
    if not scored:
        return []
    relevant = [p for p in scored if p.score > 0]
    chosen = sorted(relevant, key=lambda p: (-p.score, p.position))[:limit] if relevant else scored[:limit]
    return sorted(chosen, key=lambda p: p.position)


def _overlap(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def combine(sources: Sequence[Sequence[Passage]], budget: int) -> str:
    """Excerpt of several sources within ``budget`` characters; round-robin, best first, no repeats.

    Sources are served in order of their best passage's score (ties: the order
    given), each contributing its best remaining passage per round, so a
    source with nothing relevant never crowds out one that has. The chosen
    passages are shown by source, then position in the source.
    """
    ranked = [sorted(s, key=lambda p: (-p.score, p.position)) for s in sources]
    order = sorted(range(len(ranked)), key=lambda i: (-(ranked[i][0].score if ranked[i] else -1), i))
    chosen: List[Tuple[int, int, Passage]] = []     # (source index, position, passage)
    seen_words: List[Set[str]] = []
    used = 0
    cursors = [0] * len(ranked)
    progressed = True
    while progressed and used < budget:
        progressed = False
        for index in order:
            passages = ranked[index]
            while cursors[index] < len(passages):
                passage = passages[cursors[index]]
                cursors[index] += 1
                words = passage.words()
                if any(_overlap(words, other) >= REDUNDANT_OVERLAP for other in seen_words):
                    continue
                cost = len(passage.text) + (len(SEPARATOR) if chosen else 0)
                if used + cost > budget:
                    if chosen:
                        continue                  # a shorter passage of this source may still fit
                    # Even the first passage must fit: cut it at a word boundary.
                    passage = Passage(passage.text[:budget].rsplit(" ", 1)[0], passage.score, passage.position)
                    cost = len(passage.text)
                chosen.append((index, passage.position, passage))
                seen_words.append(words)
                used += cost
                progressed = True
                break
    chosen.sort(key=lambda c: (c[0], c[1]))
    return SEPARATOR.join(c[2].text for c in chosen)


def select_excerpts(texts: Sequence[Optional[str]], anchors: Anchors, budget: int) -> str:
    """Incident-relevant, diverse excerpts of ``texts`` (in priority order) within ``budget`` characters."""
    return combine([source_passages(t, anchors) for t in texts if t], budget)


def load_member_excerpts(conn, groups: Mapping[str, Sequence[str]],
                         names_by_group: Mapping[str, Iterable[str]],
                         budget: int = PAIR_EVIDENCE_CHARS) -> Dict[str, str]:
    """``group key -> excerpts`` of the summaries, descriptions and pages of the given member records.

    ``groups`` maps a key (a deduplicated event id, or any label) to the
    ``enriched_event_id`` of the records to read, so the same selection serves
    an event, half of an event or two events read together. A member's stored
    page is used only when it belongs to the record
    (:func:`processing.active_evidence.evidence_page`): another article's text
    would show the model a different incident. ``names_by_group`` are the
    organisations the group is known by; the attack words come from each
    member's own research (method, attacker) - not from titles, whose words
    ("Aussie", "affiliate") match any roundup. Groups with no usable text are absent.
    """
    import json
    import sqlite3 as _sqlite3

    owner: Dict[str, str] = {}
    for key, members in groups.items():
        for member in members:
            owner.setdefault(member, key)
    member_ids = sorted(owner)
    sources: Dict[str, List[List[Passage]]] = {}
    previous = conn.row_factory
    conn.row_factory = _sqlite3.Row
    try:
        for start in range(0, len(member_ids), 400):
            chunk = member_ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT e.enriched_event_id AS eid, e.title, e.summary, e.description, e.attack_method,
                           e.attacking_entity_name AS actor, e.perplexity_enrichment_data AS research,
                           r.raw_content AS page
                    FROM EnrichedEvents e LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
                    WHERE e.enriched_event_id IN ({marks}) {active_evidence.member_condition(conn)}
                    ORDER BY e.enriched_event_id""", chunk)
            for row in rows:
                key = owner[row["eid"]]
                try:
                    research = json.loads(row["research"] or "{}")
                    research = research if isinstance(research, dict) else {}
                except (TypeError, ValueError):
                    research = {}
                organisations = list(names_by_group.get(key, ())) + list(
                    active_evidence.research_names(row["research"]))
                anchors = Anchors.for_record(
                    organisations, research.get("attack_method") or row["attack_method"],
                    research.get("threat_actor") or row["actor"])
                page, _note = active_evidence.evidence_page(row["title"], organisations, row["page"])
                for text in (row["summary"], row["description"], page):
                    passages = source_passages(text, anchors) if text else []
                    if passages:
                        sources.setdefault(key, []).append(passages)
    finally:
        conn.row_factory = previous
    # Sources arrive in member order, so the result does not depend on the query plan.
    return {key: text for key, text in ((k, combine(v, budget)) for k, v in sources.items()) if text}


def load_event_excerpts(conn, names_by_event: Mapping[str, Iterable[str]],
                        budget: int = PAIR_EVIDENCE_CHARS,
                        only: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """``deduplicated_event_id -> excerpts`` of its active member records; ``only`` restricts the events read."""
    wanted = None if only is None else set(only)
    groups: Dict[str, List[str]] = {}
    for did, eid in conn.execute(
            """SELECT m.deduplicated_event_id, m.enriched_event_id FROM EventDeduplicationMap m
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
                                        AND COALESCE(d.status, 'Active') = 'Active'
               ORDER BY m.deduplicated_event_id, m.enriched_event_id"""):
        if wanted is None or did in wanted:
            groups.setdefault(did, []).append(eid)
    return load_member_excerpts(conn, groups, names_by_event, budget)
