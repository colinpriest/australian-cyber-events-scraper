"""Aliases that hold only where the record's context says so.

Perplexity confirms that two names can mean one organisation - "AV" is used
for Ambulance Victoria, "AP" for Advance Press - and until 2026-10-29 every
confirmed alias applied everywhere: the variant's entity was folded into the
canonical one and every link moved with it. An acronym is not that kind of
name. "AP" is also Associated Press and Australia Post, "UC" the University
of Canberra and of California, "ACS" the Australian Computer Society and the
American Chemical Society. A record where "AP" meant Associated Press was
then filed under Advance Press.

So aliases have a scope:

* **global** - the variant can only mean the organisation: a spelling, case,
  punctuation or legal-suffix variant, or a longer form that contains the name
  ("National Australia Bank (NAB)" -> "NAB"). Kept in ``EntityAliases`` and
  applied everywhere, as before. Reviewed (``human``) aliases stay global.
* **contextual** - the variant is an acronym or initialism
  (:func:`is_contextual_variant`). Kept in ``ContextualEntityAliases``, which
  can hold several organisations for one variant, and applied to one record
  (or one event) at a time:

  1. the record's own text names exactly one candidate organisation in full
     (or by one of its global, non-acronym aliases) - resolved, no model;
  2. otherwise Jev reads the text around the mention and chooses among the
     candidates or "another organisation / not clear"; a candidate at or above
     :data:`RESOLVE_AT` resolves it;
  3. an answer below that is re-judged by the strong OpenAI model with
     reasoning (``utils/escalation.py``); its distribution decides on the same
     bar, and a failed escalation leaves the mention unresolved and is
     reported - never guessed;
  4. anything else stays unresolved, and **nothing moves on an unsure
     answer**: a link stays where it is unless the context resolves it to an
     organisation, or a model is confident the acronym means a different
     organisation from every candidate ("AFP" credited as a photo agency in a
     report filed under the Australian Federal Police).

:func:`resolve_links` applies this to stored links, both ways: a link on the
variant's entity moves to the organisation the context resolves to, and a link
the old entity-wide fold put on the organisation moves back to the variant
when the record mentions only the variant and the context does not resolve it.
Every decision is recorded in ``EntityMentionResolutions``; every model answer
in ``ModelJudgements``, reused until the text, question or model changes.

:data:`RESOLVE_AT` is not calibrated on labelled mentions; it is a high bar
chosen so that an unresolved acronym (a separate entity, possibly a missed
duplicate) is the likelier error than a wrong organisation.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup.entity_resolution import canonical_key, normalize_name
from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

QUESTION_ID = "alias_mention"
QUESTION_VERSION = "alias-mention-v2"
ESCALATION_QUESTION_ID = "alias_mention_escalated"
ESCALATION_VERSION = "alias-mention-escalation-v2"
#: Probability a candidate needs to resolve a mention (uncalibrated; see module docstring).
RESOLVE_AT = 0.9
#: "A different organisation" and "the text does not say" are separate answers:
#: only the first may move a link off an organisation (v1 joined them, and an
#: unclear "PWDA Responds to NDIS Data Breach" left People with Disability Australia).
DIFFERENT = "different"
UNCLEAR = "unclear"
#: Characters of text shown to the model around the mentions.
EXCERPT_CHARS = 6000
WINDOW = 700

METHOD_NAMED = "named in the text"
METHOD_JEV = "jev"
METHOD_ESCALATED = "escalated"
METHOD_UNRESOLVED = "unresolved"
METHOD_FAILED = "escalation failed"
METHOD_NEEDS_MODEL = "needs a model (not asked)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ContextualEntityAliases (
    raw_name       TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    source         TEXT NOT NULL,          -- llm / rule / learned
    created_at     TEXT NOT NULL,
    PRIMARY KEY (raw_name, canonical_name)
);
CREATE TABLE IF NOT EXISTS EntityMentionResolutions (
    link_table     TEXT NOT NULL,          -- EnrichedEventEntities / DeduplicatedEventEntities
    subject_id     TEXT NOT NULL,          -- enriched_event_id / deduplicated_event_id
    raw_name       TEXT NOT NULL,          -- the contextual variant
    canonical_name TEXT,                   -- what it resolved to; NULL = unresolved
    method         TEXT NOT NULL,
    probability    REAL,
    moved_from     TEXT,                   -- entity name the link was on, when it moved
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (link_table, subject_id, raw_name)
);
"""

_ACRONYM_TOKEN = re.compile(r"^[A-Z0-9][A-Z0-9&.'\-]{0,6}$")
_PAREN = re.compile(r"\s*\([^)]*\)")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def is_contextual_variant(raw: str, canonical: str) -> bool:
    """True when ``raw`` is an acronym or initialism that other organisations could share.

    Every token upper case (``AP``, ``UC``, ``IPC NSW``, ``M&S``), at most 8
    letters and digits in all. A variant that is the canonical name up to case,
    punctuation or legal suffix (``NVIDIA`` / ``Nvidia``) is a spelling, not an
    acronym.
    """
    raw = (raw or "").strip()
    if not raw or canonical_key(raw) == canonical_key(canonical):
        return False
    core = normalize_name(_PAREN.sub("", canonical or ""))
    if core and f" {core} " in f" {normalize_name(raw)} ":
        return False        # the organisation's own name plus a qualifier ("JBS S.A.", "US CISA")
    tokens = raw.split()
    if not tokens or not all(_ACRONYM_TOKEN.match(t) for t in tokens):
        return False
    letters = sum(ch.isalnum() for ch in raw)
    return letters <= 8 and any(ch.isalpha() for ch in raw)


def migrate_contextual_aliases(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Move model- and rule-made acronym aliases out of the global table. Idempotent.

    Reviewed aliases stay global - ``human`` rows and every pair in
    ``EXPLICIT_ALIASES`` (some were first stored by the model): a person
    decided they hold everywhere.
    """
    from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES

    ensure_schema(conn)
    moved = []
    for raw, canonical, source in conn.execute(
            "SELECT raw_name, canonical_name, source FROM EntityAliases WHERE source != 'human'").fetchall():
        if EXPLICIT_ALIASES.get(raw) == canonical or not is_contextual_variant(raw, canonical):
            continue
        add_contextual_alias(conn, raw, canonical, source)
        conn.execute("DELETE FROM EntityAliases WHERE raw_name = ?", (raw,))
        moved.append((raw, canonical))
    # A full name folded into an acronym is one organisation ("National Australia Bank" -> "NAB") - until a second
    # name folds into the same acronym. "Top Education Group" -> "TEG" and Ticketek's parent "TEG Pty Ltd" -> "TEG"
    # filed both organisations under one entity. Then the expansions become the acronym's contextual candidates;
    # the acronym's own name with a qualifier ("TEG Pty Ltd") stays global.
    into: Dict[str, List[Tuple[str, str]]] = {}
    for raw, canonical, source in conn.execute(
            "SELECT raw_name, canonical_name, source FROM EntityAliases WHERE source != 'human'").fetchall():
        into.setdefault(canonical, []).append((raw, source))
    for acronym, raws in sorted(into.items()):
        if len(raws) < 2:
            continue
        for raw, source in raws:
            if EXPLICIT_ALIASES.get(raw) == acronym or not is_contextual_variant(acronym, raw):
                continue
            add_contextual_alias(conn, acronym, raw, source)
            conn.execute("DELETE FROM EntityAliases WHERE raw_name = ?", (raw,))
            moved.append((raw, acronym))
    return moved


def add_contextual_alias(conn: sqlite3.Connection, raw: str, canonical: str, source: str = "llm") -> None:
    ensure_schema(conn)
    conn.execute("INSERT OR IGNORE INTO ContextualEntityAliases VALUES (?, ?, ?, ?)",
                 (raw, canonical, source, utc_now_iso()))


def contextual_aliases(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """``{variant: [candidate organisations]}``."""
    ensure_schema(conn)
    out: Dict[str, List[str]] = {}
    for raw, canonical in conn.execute(
            "SELECT raw_name, canonical_name FROM ContextualEntityAliases ORDER BY raw_name, canonical_name"):
        out.setdefault(raw, []).append(canonical)
    return out


# ---------------------------------------------------------------------------
# Reading the context
# ---------------------------------------------------------------------------

def search_names(conn: sqlite3.Connection, canonical: str) -> List[str]:
    """Normalised full names that identify ``canonical`` in text: itself and its
    global aliases, without bracketed parts, never an acronym."""
    from cyber_data_collector.dedup.entity_resolution import stored_aliases

    names = {canonical, *(raw for raw, c in stored_aliases(conn, ("human", "llm", "rule", "learned")).items()
                          if c == canonical)}
    out = set()
    for name in names:
        bare = _PAREN.sub("", name).strip()
        for candidate in (bare, name):
            if candidate and not is_contextual_variant(candidate, "\0") and len(normalize_name(candidate)) >= 5:
                out.add(normalize_name(candidate))
    return sorted(out)


def names_in(text: str, names: Iterable[str]) -> bool:
    padded = f" {normalize_name(text)} "
    return any(f" {name} " in padded for name in names if name)


def mention_pattern(raw: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(raw) + r"(?![A-Za-z0-9])")


def mentions(text: str, raw: str) -> bool:
    return bool(mention_pattern(raw).search(text or ""))


_SEGMENT_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def locally_named(raw: str, text: str, names: Iterable[str]) -> bool:
    """True when some mention of ``raw`` sits in the same sentence or line as one of the full ``names``.

    "Australian Federal Police (AFP) investigated" and "the AFP said the Australian Federal Police..."
    connect the mention to the organisation; a full name elsewhere in the article does not. Without this a
    photo credit ("Photograph: AFP") in an article that names the Australian Federal Police in another
    sentence was resolved to the police. The names are normalised full names (:func:`search_names`).
    """
    names = [n for n in names if n]
    pattern = mention_pattern(raw)
    for segment in _SEGMENT_BREAK.split(text or ""):
        if pattern.search(segment) and names_in(segment, names):
            return True
    return False


def excerpt(text: str, raw: str, limit: int = EXCERPT_CHARS) -> str:
    """The text around each mention of ``raw``, up to ``limit`` characters; the start when none."""
    spans: List[Tuple[int, int]] = []
    for match in mention_pattern(raw).finditer(text or ""):
        start, end = max(0, match.start() - WINDOW), min(len(text), match.end() + WINDOW)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
    if not spans:
        return (text or "")[:limit]
    return " ... ".join(text[a:b] for a, b in spans)[:limit]


def record_text(conn: sqlite3.Connection, enriched_event_id: str) -> Tuple[str, str]:
    """(title, text) of one record: title, description, summary, page and research."""
    enriched = {r[1] for r in conn.execute("PRAGMA table_info(EnrichedEvents)")}
    raw = {r[1] for r in conn.execute("PRAGMA table_info(RawEvents)")}
    wanted = [("e", "title", enriched), ("e", "description", enriched), ("e", "summary", enriched),
              ("r", "raw_title", raw), ("r", "raw_description", raw), ("r", "raw_content", raw),
              ("e", "perplexity_enrichment_data", enriched)]
    select = ", ".join(f"COALESCE({a}.{c}, '')" if c in cols else "''" for a, c, cols in wanted)
    row = conn.execute(
        f"""SELECT {select}
            FROM EnrichedEvents e LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE e.enriched_event_id = ?""", (enriched_event_id,)).fetchone()
    if row is None:
        return "", ""
    return row[0], "\n".join(part for part in row if part)


def event_text(conn: sqlite3.Connection, dedup_id: str) -> Tuple[str, str]:
    """(title, text) of an event: its own title and description and every active member's text."""
    from cyber_data_collector.processing.active_evidence import member_condition

    columns = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    description = "COALESCE(description, '')" if "description" in columns else "''"
    head = conn.execute(f"SELECT COALESCE(title, ''), {description} FROM DeduplicatedEvents "
                        "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone() or ("", "")
    parts = [head[0], head[1]]
    for (enriched_id,) in conn.execute(
            f"""SELECT e.enriched_event_id FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ? {member_condition(conn)} ORDER BY e.enriched_event_id""",
            (dedup_id,)):
        parts.append(record_text(conn, enriched_id)[1])
    return head[0], "\n".join(p for p in parts if p)


def resolve_in_context(conn: sqlite3.Connection, name: str, context: Optional[str]) -> Optional[str]:
    """The organisation a contextual variant means when ``context`` names exactly one candidate.

    No model is asked; used at ingest, where an unresolved variant is simply
    stored as written and :func:`resolve_links` decides later.
    """
    if not context:
        return None
    candidates = contextual_aliases(conn).get(name.strip())
    if not candidates:
        return None
    named = [c for c in candidates if locally_named(name.strip(), context, search_names(conn, c))]
    return named[0] if len(named) == 1 else None


# ---------------------------------------------------------------------------
# Deciding a mention
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    canonical: Optional[str]
    method: str
    probability: Optional[float] = None
    failed: bool = False
    #: Confident that the acronym means none of the candidates.
    different: bool = False


def _options(raw: str, candidates: Sequence[str]) -> Dict[str, str]:
    options = {f"candidate_{i}": f'"{raw}" in this text refers to {name}.' for i, name in enumerate(candidates, 1)}
    options[DIFFERENT] = (f'"{raw}" in this text refers to a different organisation from all of these '
                          "(the text shows it is something else).")
    options[UNCLEAR] = (f'The text does not make clear which organisation "{raw}" refers to.')
    return options


def mention_question(raw: str, candidates: Sequence[str]):
    from typesafe_sdk import Choice

    return Choice(
        instructions=(f'Which organisation does the name "{raw}" refer to in `text`? It is short for several '
                      "organisations. Decide from what the text says about it - what it does, where it is, "
                      "how it is described - not from which organisation is best known. Choose the "
                      "candidate only when the text makes it clear."),
        criteria=_options(raw, candidates))


class MentionResolver:
    """Resolves contextual variants in a text: the text first, then Jev, then escalation."""

    def __init__(self, conn: sqlite3.Connection, judge: Any = None, escalation_client: Any = None,
                 use_models: bool = True):
        self.conn = conn
        self.aliases = contextual_aliases(conn)
        self._names: Dict[str, List[str]] = {}
        self.use_models = use_models
        self.escalation_client = escalation_client
        self._judge = judge

    @property
    def judge(self):
        if self._judge is None:
            from cyber_data_collector.utils.jev import JevJudge, require

            require()
            self._judge = JevJudge(self.conn)
        return self._judge

    def names_for(self, canonical: str) -> List[str]:
        if canonical not in self._names:
            self._names[canonical] = search_names(self.conn, canonical)
        return self._names[canonical]

    def named(self, raw: str, text: str) -> List[str]:
        """The candidates the text connects to the mention: named in full in the mention's own sentence."""
        return [c for c in self.aliases.get(raw, []) if locally_named(raw, text, self.names_for(c))]

    def resolve(self, raw: str, title: str, text: str, subject: str) -> Resolution:
        candidates = self.aliases.get(raw, [])
        if not candidates:
            return Resolution(None, METHOD_UNRESOLVED)
        named = self.named(raw, text)
        if len(named) == 1:
            return Resolution(named[0], METHOD_NAMED, 1.0)
        if not self.use_models:
            return Resolution(None, METHOD_NEEDS_MODEL)
        state = {"mention": raw, "record_title": title, "text": excerpt(text, raw)}
        from cyber_data_collector.dedup.entity_profiles import candidate_profiles

        profiles = candidate_profiles(self.conn, list(candidates))     # what each candidate is: evidence, not a matcher
        if profiles:
            state["candidate_profiles"] = profiles
        options = _options(raw, candidates)
        answer = self.judge.ask("entity_mention", subject, QUESTION_VERSION, state,
                                {QUESTION_ID: mention_question(raw, candidates)})[QUESTION_ID]
        probs = {k: float(v) for k, v in (answer.probabilities or {}).items()}
        top, p = max(probs.items(), key=lambda kv: kv[1]) if probs else (UNCLEAR, 0.0)
        if p >= RESOLVE_AT and top != UNCLEAR:
            return self._settled(candidates, top, p, METHOD_JEV)
        from cyber_data_collector.utils.escalation import escalate_choice

        escalated = escalate_choice(
            self.conn, "entity_mention", subject, ESCALATION_QUESTION_ID, ESCALATION_VERSION, state,
            f'Which organisation does the name "{raw}" refer to in the evidence\'s `text`? It is short for '
            "several organisations; decide from what the text says about it, not from which is best known.",
            options, client=self.escalation_client)
        if escalated is None:
            return Resolution(None, METHOD_FAILED, None, failed=True)
        top, p = max(escalated.items(), key=lambda kv: kv[1])
        if p >= RESOLVE_AT and top != UNCLEAR:
            return self._settled(candidates, top, p, METHOD_ESCALATED)
        return Resolution(None, METHOD_UNRESOLVED, p)

    @staticmethod
    def _settled(candidates: Sequence[str], option: str, p: float, method: str) -> Resolution:
        if option == DIFFERENT:
            return Resolution(None, method, p, different=True)
        return Resolution(candidates[int(option.split("_")[1]) - 1], method, p)


# ---------------------------------------------------------------------------
# Applying to stored links
# ---------------------------------------------------------------------------

LINKS = (("EnrichedEventEntities", "enriched_event_id"), ("DeduplicatedEventEntities", "deduplicated_event_id"))


@dataclass
class LinkReport:
    checked: int = 0
    kept: int = 0
    moved_to_organisation: List[Tuple[str, str, str, str]] = field(default_factory=list)   # table, subject, from, to
    moved_to_variant: List[Tuple[str, str, str, str]] = field(default_factory=list)
    unresolved: int = 0
    needs_model: int = 0          # without models (a dry run): mentions only a model could place
    failed: List[Tuple[str, str, str]] = field(default_factory=list)                       # table, subject, variant
    victims_renamed: int = 0

    def summary(self) -> str:
        return (f"{self.checked} link(s) checked: {len(self.moved_to_organisation)} moved to the organisation "
                f"the context names, {len(self.moved_to_variant)} returned to the name the record used, "
                f"{self.kept} kept, {self.unresolved} variant link(s) left unresolved, "
                f"{len(self.failed)} undecided (escalation failed); {self.victims_renamed} event victim name(s) "
                "updated" + (f"; {self.needs_model} would be asked of Jev" if self.needs_model else ""))


def _entity_id(conn: sqlite3.Connection, name: str, create: bool) -> Optional[int]:
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (name,)).fetchone()
    if row:
        return row[0]
    if not create:
        return None
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, created_at, updated_at) VALUES (?, ?, ?)",
                        (name, utc_now_iso(), utc_now_iso())).lastrowid


def _active_links(conn: sqlite3.Connection, table: str, column: str, entity_id: int) -> List[Tuple[str, str]]:
    status_table = "EnrichedEvents" if table == "EnrichedEventEntities" else "DeduplicatedEvents"
    return conn.execute(
        f"""SELECT l.{column}, COALESCE(l.relationship_type, '') FROM {table} l
            JOIN {status_table} s ON s.{column} = l.{column}
            WHERE l.entity_id = ? AND COALESCE(s.status, 'Active') = 'Active' ORDER BY l.{column}""",
        (entity_id,)).fetchall()


def _move(conn: sqlite3.Connection, table: str, column: str, subject: str, from_id: int, to_id: int) -> None:
    conn.execute(f"UPDATE OR IGNORE {table} SET entity_id = ? WHERE {column} = ? AND entity_id = ?",
                 (to_id, subject, from_id))
    conn.execute(f"DELETE FROM {table} WHERE {column} = ? AND entity_id = ?", (subject, from_id))


def _record(conn: sqlite3.Connection, table: str, subject: str, raw: str, resolution: Resolution,
            moved_from: Optional[str]) -> None:
    conn.execute("INSERT OR REPLACE INTO EntityMentionResolutions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (table, subject, raw, resolution.canonical, resolution.method, resolution.probability,
                  moved_from, utc_now_iso()))


def _events_of(conn: sqlite3.Connection, table: str, subject: str) -> List[str]:
    if table == "DeduplicatedEventEntities":
        return [subject]
    return [r[0] for r in conn.execute(
        "SELECT m.deduplicated_event_id FROM EventDeduplicationMap m JOIN DeduplicatedEvents d "
        "ON d.deduplicated_event_id = m.deduplicated_event_id WHERE m.enriched_event_id = ? "
        "AND d.status = 'Active'", (subject,))]


def _victim_entity_names(conn: sqlite3.Connection, dedup_id: str) -> Set[str]:
    from cyber_data_collector.processing.active_evidence import member_condition

    rows = conn.execute(
        f"""SELECT v.entity_name FROM DeduplicatedEventEntities l JOIN EntitiesV2 v ON v.entity_id = l.entity_id
            WHERE l.deduplicated_event_id = ? AND l.relationship_type = 'victim'
            UNION
            SELECT v.entity_name FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            JOIN EnrichedEventEntities l ON l.enriched_event_id = e.enriched_event_id
            JOIN EntitiesV2 v ON v.entity_id = l.entity_id
            WHERE m.deduplicated_event_id = ? AND l.relationship_type = 'victim' {member_condition(conn)}""",
        (dedup_id, dedup_id)).fetchall()
    return {r[0] for r in rows}


def _fix_victim_names(conn: sqlite3.Connection, moves: Iterable[Tuple[str, str, str, str]]) -> int:
    """An event whose victim name was a moved entity, and that no longer links a victim by that
    name, takes the name the victim link now has."""
    renamed = 0
    for table, subject, old, new in moves:
        for dedup_id in _events_of(conn, table, subject):
            row = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents "
                               "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
            if not row or row[0] != old:
                continue
            victims = _victim_entity_names(conn, dedup_id)
            if old not in victims and new in victims:
                conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ? "
                             "WHERE deduplicated_event_id = ?", (new, dedup_id))
                renamed += 1
    return renamed


def resolve_links(conn: sqlite3.Connection, resolver: Optional[MentionResolver] = None,
                  dry_run: bool = False) -> LinkReport:
    """Put every link of a contextual variant, and of the organisations it may mean, where the
    record's context says it belongs. Commits per variant unless ``dry_run``.

    For each variant ``v`` with candidate organisations ``C``:

    * a link on entity ``v`` moves to the organisation the context resolves it
      to; unresolved, it stays;
    * a link on an organisation ``c`` in ``C`` whose text does not name ``c``
      but mentions ``v`` was (or could have been) put there by the old
      entity-wide fold: it stays when the context resolves ``v`` to ``c`` and
      returns to entity ``v`` otherwise.
    """
    ensure_schema(conn)
    resolver = resolver or MentionResolver(conn)
    report = LinkReport()
    for raw, candidates in sorted(resolver.aliases.items()):
        moves: List[Tuple[str, str, str, str]] = []
        decided: Set[Tuple[str, str]] = set()      # each record (or event) decided once per variant
        variant_id = _entity_id(conn, raw, create=False)
        for table, column in LINKS:
            reader = record_text if table == "EnrichedEventEntities" else event_text
            if variant_id is not None:
                for subject, _role in _active_links(conn, table, column, variant_id):
                    decided.add((table, subject))
                    report.checked += 1
                    title, text = reader(conn, subject)
                    resolution = resolver.resolve(raw, title, text, f"{table}:{subject}:{raw}")
                    if resolution.failed:
                        report.failed.append((table, subject, raw))
                        continue
                    if resolution.method == METHOD_NEEDS_MODEL:
                        report.needs_model += 1
                        continue
                    if resolution.canonical is None:
                        report.unresolved += 1
                        if not dry_run:
                            _record(conn, table, subject, raw, resolution, None)
                        continue
                    moves.append((table, subject, raw, resolution.canonical))
                    report.moved_to_organisation.append((table, subject, raw, resolution.canonical))
                    if not dry_run:
                        _move(conn, table, column, subject, variant_id,
                              _entity_id(conn, resolution.canonical, create=True))
                        _record(conn, table, subject, raw, resolution, raw)
            for canonical in candidates:
                canonical_id = _entity_id(conn, canonical, create=False)
                if canonical_id is None:
                    continue
                for subject, _role in _active_links(conn, table, column, canonical_id):
                    if (table, subject) in decided:
                        continue
                    decided.add((table, subject))
                    title, text = reader(conn, subject)
                    if names_in(text, resolver.names_for(canonical)) or not mentions(text, raw):
                        continue        # names the organisation, or the link did not come from the variant
                    report.checked += 1
                    resolution = resolver.resolve(raw, title, text, f"{table}:{subject}:{raw}")
                    if resolution.failed:
                        report.failed.append((table, subject, raw))
                        continue
                    if resolution.method == METHOD_NEEDS_MODEL:
                        report.needs_model += 1
                        continue
                    if resolution.canonical == canonical or (resolution.canonical is None
                                                              and not resolution.different):
                        # Resolved to this organisation, or unsure: an unsure answer moves nothing.
                        report.kept += 1
                        report.unresolved += resolution.canonical is None
                        if not dry_run:
                            _record(conn, table, subject, raw, resolution, None)
                        continue
                    target = resolution.canonical or raw
                    moves.append((table, subject, canonical, target))
                    report.moved_to_variant.append((table, subject, canonical, target))
                    if not dry_run:
                        _move(conn, table, column, subject, canonical_id, _entity_id(conn, target, create=True))
                        _record(conn, table, subject, raw, resolution, canonical)
        if not dry_run:
            report.victims_renamed += _fix_victim_names(conn, moves)
            conn.commit()
    return report
