"""Canonical entity naming and candidate blocking for deduplication.

The v2 pipeline compared victim organisation names close to literally, so
"Optus Pty Limited", "Optus Mobile Pty Limited" and "Singtel Optus Pty Limited"
were three organisations and one incident was stored three times. The database
holds 64 entity stems with multiple legal-name variants, which is the largest
single cause of missed merges.

**Design stance.** An earlier draft of this module tried to solve entity
identity with aggressive rewriting - stripping corporate parents and "noise"
tokens until two variants matched. That is the brittle approach this rewrite
exists to avoid, and it failed badly in testing: "Nissan Motor Co. (Australia)
Pty Ltd" reduced to the key ``co``, which would merge every unrelated company
whose name ends in "Co Pty Ltd".

So identity is *not* decided here. This module does two narrow, safe jobs:

* ``canonical_key`` - conservative normalisation (case, punctuation, legal
  suffixes). It never reduces a name to a generic fragment.
* ``blocking_keys`` - deliberately over-generates candidate keys so that
  related variants collide and get *considered*. Recall is the goal; precision
  is the adjudicator's job.

Learned aliases from ``EntityAliases`` override both, so a human correction is
always honoured.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

# Legal-form suffixes, longest-first so "pty ltd" is consumed before "ltd".
LEGAL_SUFFIXES: Tuple[str, ...] = (
    "proprietary limited", "pty limited", "pty ltd",
    "limited liability company", "public limited company",
    "incorporated", "corporation", "company",
    "limited", "ltd", "llc", "plc", "inc", "gmbh", "nv", "ag", "sa", "srl",
)

# Words too generic to identify an organisation on their own. Used ONLY to
# reject a degenerate blocking key - never to rewrite the canonical key.
GENERIC_TOKENS: Set[str] = {
    "the", "of", "and", "for", "australia", "australian", "aust", "au",
    "group", "holdings", "services", "service", "solutions", "systems",
    "co", "motor", "financial", "national", "international", "global",
    "pty", "ltd", "limited", "inc", "corporation", "company", "department",
    "government", "state", "new", "south", "north", "west", "east",
}

_PUNCT_RE = re.compile(r"[^\w\s&]+", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def normalize_name(name: Optional[str]) -> str:
    """Lower-case, de-accent, expand '&', and strip punctuation."""
    if not name:
        return ""
    text = _strip_accents(str(name)).lower()
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def canonical_key(name: Optional[str]) -> str:
    """Conservative canonical form: normalised, minus trailing legal suffixes.

    Deliberately does NOT strip corporate parents or generic words. Over-eager
    stripping produced degenerate keys that merged unrelated organisations;
    variant matching is handled by :func:`blocking_keys` plus adjudication
    instead.
    """
    text = normalize_name(name)
    if not text:
        return ""

    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            if text.endswith(" " + suffix):
                candidate = text[: -(len(suffix) + 1)].strip()
                # Never strip a name down to nothing.
                if candidate:
                    text = candidate
                    changed = True
                    break
    return text


def significant_tokens(name: Optional[str]) -> List[str]:
    """Tokens of the canonical key that carry identifying information."""
    return [t for t in canonical_key(name).split() if t not in GENERIC_TOKENS]


def blocking_keys(
    name: Optional[str],
    distinctive: Optional[Set[str]] = None,
) -> Set[str]:
    """Candidate keys for recall-oriented blocking.

    Two entities become *candidates* if they share any blocking key. Emitting
    several keys per name lets "Qantas" and "Qantas Airways Limited" collide on
    ``qantas``, and "Optus Pty Limited" and "Singtel Optus Pty Limited" collide
    on ``optus`` - without either name being rewritten.

    Args:
        name: Raw entity name.
        distinctive: Tokens considered rare enough to block on alone, normally
            supplied by :meth:`EntityResolver.fit` from corpus frequencies.
            When omitted, only the static ``GENERIC_TOKENS`` list guards
            against over-blocking, which is weaker: shared-but-common words
            like "university" or "department" will block together.
    """
    keys: Set[str] = set()
    key = canonical_key(name)
    if not key:
        return keys

    tokens = key.split()
    sig = significant_tokens(name)

    if key not in GENERIC_TOKENS:
        keys.add(key)
    if sig:
        keys.add(" ".join(sig))
        if len(sig) >= 2:
            keys.add(" ".join(sig[:2]))
        # Single-token keys drive most of the recall (Singtel Optus -> optus),
        # but only for tokens rare enough not to bucket half the corpus.
        for token in sig:
            if distinctive is None:
                if token == sig[0]:
                    keys.add(token)
            elif token in distinctive:
                keys.add(token)
    # Acronym for long multi-word names ("australian clinical labs" -> acl)
    if len(tokens) >= 3:
        acronym = "".join(t[0] for t in tokens if t not in GENERIC_TOKENS)
        if len(acronym) >= 3:
            keys.add(acronym)

    return {k for k in keys if k and k not in GENERIC_TOKENS}


def name_similarity(left: Optional[str], right: Optional[str]) -> float:
    """0..1 similarity between two entity names.

    Combines Jaccard overlap with containment, so a short name fully contained
    in a longer one ("qantas" in "qantas airways") scores highly rather than
    being penalised for the length difference.
    """
    left_tokens = set(significant_tokens(left))
    right_tokens = set(significant_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    if not intersection:
        return 0.0
    jaccard = len(intersection) / len(left_tokens | right_tokens)
    containment = len(intersection) / min(len(left_tokens), len(right_tokens))
    return max(jaccard, containment * 0.95)


#: Alias sources trusted to fold without asking: reviewed by a person, or
#: confirmed by Perplexity (entity_alias_verification). ``rule`` aliases were
#: made by name structure alone and are verified before they are trusted.
TRUSTED_ALIAS_SOURCES = ("human", "llm")


def stored_aliases(conn: sqlite3.Connection, sources: Sequence[str] = TRUSTED_ALIAS_SOURCES) -> Dict[str, str]:
    """``{raw_name: canonical_name}`` from ``EntityAliases`` for the given sources.

    An alias whose pair is listed in ``EntityAntiAliases`` (:mod:`anti_aliases`) is left out: the listing says
    the two are different entities, and a person cannot list a pair a ``human`` alias joins.
    """
    from cyber_data_collector.dedup.anti_aliases import is_anti_alias, pair_set

    marks = ", ".join("?" for _ in sources)
    try:
        rows = conn.execute(f"SELECT raw_name, canonical_name FROM EntityAliases WHERE source IN ({marks})",
                            tuple(sources)).fetchall()
    except sqlite3.Error:
        return {}
    anti = pair_set(conn)
    return {raw: canonical for raw, canonical in rows if not is_anti_alias(anti, raw, canonical)}


def canonical_entity_name(conn: sqlite3.Connection, name: Optional[str],
                          context: Optional[str] = None) -> Optional[str]:
    """The organisation's canonical name when ``name`` is a stored alias of it, else ``name``.

    Looked up before an entity is found or created, so a record naming
    "Medicare" files under the organisation already known instead of
    recreating the variant. Global aliases (``EntityAliases``): exact
    (case-insensitive) first, then normalised (punctuation, '&', accents). An
    acronym alias is contextual (``dedup/contextual_aliases.py``) and applies
    only when ``context`` - the record's or event's text - names exactly one
    organisation it may mean; without context the name is kept as written.
    """
    if not name or not name.strip():
        return name
    aliases = stored_aliases(conn, (*TRUSTED_ALIAS_SOURCES, "rule", "learned"))
    lowered = {raw.lower(): canonical for raw, canonical in aliases.items()}
    hit = lowered.get(name.strip().lower())
    if hit is None and aliases:
        wanted = normalize_name(name)
        hit = next((canonical for raw, canonical in aliases.items() if normalize_name(raw) == wanted), None)
    if hit is None and context:
        from cyber_data_collector.dedup.contextual_aliases import resolve_in_context

        try:
            hit = resolve_in_context(conn, name, context)
        except sqlite3.Error:
            hit = None
    return hit or name


class EntityResolver:
    """Resolves entity names to keys and candidate blocks, with learned aliases.

    Aliases in the ``EntityAliases`` table take precedence over the rule-based
    key, so human corrections always win and persist across rebuilds.
    """

    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self._aliases: Dict[str, str] = {}
        self._canonical_names: Dict[str, str] = {}
        self._distinctive: Optional[Set[str]] = None
        if conn is not None:
            self.load_aliases(conn)

    def fit(self, names: Iterable[Optional[str]], max_share: float = 0.005,
            min_absolute: int = 3) -> Set[str]:
        """Learn which single tokens are rare enough to block on.

        Blocking on a token that half the corpus shares ("university",
        "department", "nsw") buckets thousands of unrelated organisations
        together and makes candidate generation useless. Rather than curate a
        stopword list by hand - the brittle approach - we measure it: a token
        is distinctive when it appears in at most ``max_share`` of entities
        (floored at ``min_absolute`` so small corpora still work).

        Returns the distinctive-token set, and stores it for later calls.
        """
        doc_freq: Dict[str, int] = {}
        total = 0
        for name in names:
            total += 1
            for token in set(significant_tokens(name)):
                doc_freq[token] = doc_freq.get(token, 0) + 1

        ceiling = max(min_absolute, int(total * max_share))
        self._distinctive = {t for t, n in doc_freq.items() if n <= ceiling}
        logger.info(
            "Entity blocking fitted on %d name(s): %d/%d tokens distinctive "
            "(<= %d occurrences)",
            total, len(self._distinctive), len(doc_freq), ceiling,
        )
        return self._distinctive

    def load_aliases(self, conn: sqlite3.Connection) -> int:
        try:
            rows = conn.execute(
                "SELECT raw_name, canonical_name, canonical_key FROM EntityAliases"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug("EntityAliases unavailable (%s); using rules only", exc)
            return 0

        from cyber_data_collector.dedup.anti_aliases import is_anti_alias, pair_set
        anti = pair_set(conn)
        rows = [r for r in rows if not is_anti_alias(anti, r[0], r[1])]
        for raw_name, canonical_name, key in rows:
            self._aliases[normalize_name(raw_name)] = key
            self._canonical_names[key] = canonical_name
        if rows:
            logger.info("Loaded %d entity alias(es)", len(rows))
        return len(rows)

    def key_for(self, name: Optional[str]) -> str:
        """Canonical key, honouring learned aliases."""
        if not name:
            return ""
        alias = self._aliases.get(normalize_name(name))
        return alias if alias else canonical_key(name)

    def blocks_for(self, name: Optional[str]) -> Set[str]:
        """Blocking keys, including any learned alias key."""
        keys = blocking_keys(name, distinctive=self._distinctive)
        alias = self._aliases.get(normalize_name(name))
        if alias:
            keys.add(alias)
        return keys

    def display_name(self, key: str) -> str:
        return self._canonical_names.get(key, key)

    # Two names this similar are treated as candidates regardless of blocking.
    # Containment scoring already gives 0.95 for "Optus" vs "Singtel Optus",
    # while genuinely different organisations that merely share a common word
    # ("University of Melbourne" vs "University of Sydney") score below 0.5.
    SIMILARITY_CANDIDATE_THRESHOLD = 0.9

    def are_candidates(self, left: Optional[str], right: Optional[str]) -> bool:
        """True when two names are worth adjudicating.

        Blocking-key overlap is the primary test, but it depends on ``fit``
        having marked the shared token distinctive. Falling back to name
        similarity means an unfitted resolver still pairs obvious variants
        instead of silently missing them.
        """
        if self.key_for(left) and self.key_for(left) == self.key_for(right):
            return True
        if self.blocks_for(left) & self.blocks_for(right):
            return True
        return name_similarity(left, right) >= self.SIMILARITY_CANDIDATE_THRESHOLD

    # Words that qualify an organisation without changing which one it is:
    # legal forms, corporate parents and geographic scoping.
    CORPORATE_QUALIFIERS: Set[str] = {
        "pty", "ltd", "limited", "inc", "incorporated", "corporation", "corp",
        "plc", "llc", "co", "group", "holdings", "australia", "australian",
        "international", "global", "singtel", "telecom", "telecommunications",
    }

    def same_organisation(self, left: Optional[str], right: Optional[str]) -> bool:
        """Strict test: do these names denote the SAME organisation?

        Distinct from :meth:`are_candidates`, which is deliberately loose
        because its job is recall for blocking. Used where precision matters -
        counting how many records mention an organisation - because the loose
        test folds "University of Technology Sydney" into "University of
        Sydney" (they share the distinctive token "sydney"), which made a wrong
        victim look well supported.

        Two names match when their significant tokens are equal, or when one is
        a subset of the other and the extra tokens are only corporate
        qualifiers ("Optus" vs "Singtel Optus Pty Limited" - yes;
        "University of Sydney" vs "University of Technology Sydney" - no,
        because "technology" distinguishes them).
        """
        if not left or not right:
            return False
        if self.key_for(left) and self.key_for(left) == self.key_for(right):
            return True

        left_tokens = set(significant_tokens(left))
        right_tokens = set(significant_tokens(right))
        if not left_tokens or not right_tokens:
            return False
        if left_tokens == right_tokens:
            return True

        smaller, larger = sorted((left_tokens, right_tokens), key=len)
        if not smaller <= larger:
            return False
        if (larger - smaller) <= self.CORPORATE_QUALIFIERS:
            return True
        # A single distinctive brand word identifies the organisation on its
        # own, so anything qualifying it is still that organisation: "Qantas"
        # vs "Qantas Airways Limited". This does NOT apply to "University of
        # Sydney" vs "University of Technology Sydney", where the shorter name
        # is two tokens and shares the generic "university".
        if len(smaller) == 1:
            token = next(iter(smaller))
            if self._distinctive is None or token in self._distinctive:
                return True
        return False

    def group_candidates(
        self, named_items: Sequence[Tuple[str, Optional[str]]]
    ) -> Dict[str, List[str]]:
        """Bucket ``(item_id, entity_name)`` pairs by every blocking key.

        An item appears in every bucket it qualifies for, so downstream
        candidate generation sees all plausible pairings.
        """
        blocks: Dict[str, List[str]] = {}
        for item_id, entity_name in named_items:
            for key in self.blocks_for(entity_name) or {""}:
                if key:
                    blocks.setdefault(key, []).append(item_id)
        return blocks

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    def learn_alias(
        self,
        conn: sqlite3.Connection,
        raw_name: str,
        canonical_name: str,
        source: str = "learned",
        confidence: float = 1.0,
    ) -> None:
        """Bind ``raw_name`` to ``canonical_name`` for all future runs.

        Called when a human override rules that two differently-named records
        describe the same incident.
        """
        key = canonical_key(canonical_name)
        conn.execute(
            """
            INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key,
                                       source, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(raw_name) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                canonical_key  = excluded.canonical_key,
                source         = excluded.source,
                confidence     = excluded.confidence,
                -- dated when the alias last changed, so an --as-known-at reconstruction never
                -- passes a later decision off as known at an earlier date
                created_at     = CASE WHEN EntityAliases.canonical_name IS excluded.canonical_name
                                       AND EntityAliases.source IS excluded.source
                                      THEN EntityAliases.created_at ELSE excluded.created_at END
            """,
            (raw_name, canonical_name, key, source, confidence, utc_now_iso()),
        )
        self._aliases[normalize_name(raw_name)] = key
        self._canonical_names[key] = canonical_name
        logger.info("Learned alias %r -> %r (key=%r)", raw_name, canonical_name, key)
