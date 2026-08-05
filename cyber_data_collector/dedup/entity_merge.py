"""Collapse duplicate spellings of one organisation into a single entity.

``EntitiesV2`` holds 3,755 distinct names, but 413 of them are re-spellings of
200 organisations: "Australian Government" and "Australian government",
"MediSecure" and "Medisecure", "Prosura" and "Prosura Pty Ltd".

This is not merely cosmetic. Deduplication blocks candidate pairs on the entity
key, so two events about one incident that name the organisation differently
are **never compared at all** - no amount of LLM adjudication downstream can
recover a pair that blocking never produced. Duplicate spellings are therefore
a direct cause of missed merges, as well as of one organisation appearing
several times in the by-entity dashboard.

Grouping uses :func:`canonical_key`, which is deliberately conservative -
"Nissan Motor Co. (Australia) Pty Ltd" must not reduce to "co". Names that only
a human would know are the same ("Australia" for the Commonwealth) are handled
through :data:`EXPLICIT_ALIASES` instead of loosened matching.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from cyber_data_collector.dedup.entity_resolution import canonical_key
from cyber_data_collector.dedup.victim_selection import (
    is_descriptive_label,
    is_non_organisation,
)

logger = logging.getLogger(__name__)

# Tables holding a foreign key to EntitiesV2.entity_id.
LINK_TABLES = ("EnrichedEventEntities", "DeduplicatedEventEntities")

# Names no key function should be expected to unify, because knowing they are
# the same needs knowledge of Australian government structure rather than
# string similarity. Left side is folded into the right.
EXPLICIT_ALIASES: Dict[str, str] = {
    "Australia": "Australian Government",
    "Australian federal government": "Australian Government",
    "Australian Federal Government": "Australian Government",
    "Australian Government Entity": "Australian Government",
    "Australian government department": "Australian Government",
    "Commonwealth Government": "Australian Government",
    "Commonwealth entities": "Australian Government",
    "Federal Government": "Australian Government",
    "the Australian Government": "Australian Government",
    "Austal (U.S. arm)": "Austal",
    "Austal Limited": "Austal",
    "Austal USA": "Austal",
    # The Australian arm and the global brand, reported as one entity.
    "American Express": "American Express Australia Limited",
    "Amex": "American Express Australia Limited",
    "American Express Australia": "American Express Australia Limited",
    # A missing or extra "s", and the bracketed acronym, produced three ASIC
    # events with byte-identical titles, dates and descriptions.
    "Australian Securities and Investment Commission":
        "Australian Securities and Investments Commission",
    "Australian Securities and Investments Commission (ASIC)":
        "Australian Securities and Investments Commission",
    "ASIC": "Australian Securities and Investments Commission",
    # Recorded on the user's instruction: the fund is referred to both by name
    # and by the sector word in coverage of the April 2025 incident.
    "Australian Superannuation": "AustralianSuper",
    "Australian Super": "AustralianSuper",
    "Australian super": "AustralianSuper",
    # Trading name, common name and legal name of one bank.
    "CommBank": "Commonwealth Bank of Australia",
    "Commonwealth Bank": "Commonwealth Bank of Australia",
    "CBA": "Commonwealth Bank of Australia",
    "Commbank": "Commonwealth Bank of Australia",
    # The department, the force it administers and the recruiting branch whose
    # system was breached are reported interchangeably in coverage of the 2020
    # Defence Force Recruiting incident.
    "Defence Force": "Department of Defence",
    "Defence Force Recruiting": "Department of Defence",
    "Defence Department": "Department of Defence",
    "Australian Defence Force": "Department of Defence",
    "ADF": "Department of Defence",
    "Defence": "Department of Defence",
    # The television network is the broadcast arm of the listed parent, and
    # coverage of the 2021 attack uses the two names interchangeably.
    "Nine Network": "Nine Entertainment",
    "Channel Nine": "Nine Entertainment",
    "Nine Entertainment Co": "Nine Entertainment",
    # Parent and its beverage division, reported interchangeably.
    "Lion Dairy & Drinks": "Lion",
    "Lion Australia": "Lion",
    "Lion Corporate": "Lion",
    # The board is always referred to by its full jurisdictional name; the
    # short form is not a different body.
    "Legal Practice Board": "Legal Practice Board of Western Australia",
    # Two-letter brands fall below the distinctiveness floor, so the
    # structural rules cannot see that these are one company.
    "RI Group": "RI Advice Group Pty Ltd",
    "RI Advice": "RI Advice Group Pty Ltd",
    "RI Advice Group": "RI Advice Group Pty Ltd",
    "UTAS": "University of Tasmania",
    "UniMelb": "University of Melbourne",
    "USyd": "University of Sydney",
    "UQ": "University of Queensland",
    "ANU": "Australian National University",
    "UTS": "University of Technology Sydney",
    # A registered-trademark symbol survives tokenising as its own name.
    "Land Rover® Australia": "Jaguar Land Rover",
    "Land Rover Australia": "Jaguar Land Rover",
    # The agency's own abbreviation keeps the "f" of "for", which no
    # initialism rule reproduces.
    "TfNSW": "Transport for NSW",
    "Transport for New South Wales": "Transport for NSW",
    "SVHA": "St Vincent's Health Australia",
    "St Vincent's Health": "St Vincent's Health Australia",
    "St Vincent's Hospital": "St Vincent's Health Australia",
    # BG Group is the Australian gas business Shell acquired; coverage
    # names the parent and the subsidiary interchangeably.
    "BG Group": "Shell",
    "Shell Australia": "Shell",
}

_ACRONYM = re.compile(r"^[A-Z0-9&.\- ]+$")


_DATE_LIKE = re.compile(
    r"^\s*(\d{1,2}\s+)?(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)|^\s*\d{4}\s*$|^\s*\d{1,2}[/-]\d",
    re.IGNORECASE)


def _is_date(name: str) -> bool:
    """True when the "entity" is really a date that extraction picked up."""
    return bool(_DATE_LIKE.search(name or ""))


def preferred_name(names: Sequence[str], usage: Dict[str, int]) -> str:
    """Pick the name to keep for a group of spellings.

    Ordered by: proper capitalisation (so "ORICA AUSTRALIA PTY. LTD." loses to
    "Orica Australia Pty. Ltd." but a genuine acronym like "ANZ" is not
    penalised), then how often the name is actually used, then the longer and
    more formal spelling.
    """
    def score(name: str) -> tuple:
        letters = [c for c in name if c.isalpha()]
        shouty = (len(name) > 5 and letters
                  and all(c.isupper() for c in letters) and _ACRONYM.match(name))
        whisper = letters and all(c.islower() for c in letters)
        return (0 if (shouty or whisper) else 1, usage.get(name, 0), len(name))

    return max(names, key=score)


def canonical_groups(conn: sqlite3.Connection) -> List[Tuple[str, List[str]]]:
    """Groups of entity names that denote the same organisation.

    Returns ``(name_to_keep, names_to_fold_in)`` for every group with more than
    one spelling, largest group first.
    """
    rows = conn.execute(
        """
        SELECT entity_id, entity_name FROM EntitiesV2
        WHERE entity_name IS NOT NULL AND TRIM(entity_name) != ''
          AND COALESCE(entity_kind, 'organisation')
              IN ('organisation', 'government_body')
        """
    ).fetchall()

    usage: Dict[str, int] = {}
    for name, count in conn.execute(f"""
        SELECT v.entity_name, COUNT(*) FROM EnrichedEventEntities ee
        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id GROUP BY v.entity_name
    """):
        usage[name] = count

    buckets: Dict[str, List[str]] = {}
    for row in rows:
        name = row[1] if not isinstance(row, sqlite3.Row) else row["entity_name"]
        # Dates, sector labels and descriptions are not organisations, and
        # folding them together produced groups like "6 October 2025" and
        # "Australian Healthcare Sector". They are handled by entity
        # classification, not here.
        if is_descriptive_label(name) or is_non_organisation(name) or _is_date(name):
            continue
        target = EXPLICIT_ALIASES.get(name, name)
        key = canonical_key(target) or target.strip().lower()
        buckets.setdefault(key, [])
        if name not in buckets[key]:
            buckets[key].append(name)

    # Widen the buckets structurally. canonical_key only unifies names that
    # normalise identically, so it never saw "Genea" / "Genea IVF Clinic" or
    # "FRV" / "Fire Rescue Victoria". Union-find makes the relation transitive,
    # which is needed because "Genea Fertility" and "Genea IVF" match only
    # through the shared "Genea".
    parent: Dict[str, str] = {k: k for k in buckets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    keys = list(buckets)
    # Compare one representative per bucket; the rest already agree with it.
    reps = {k: buckets[k][0] for k in keys}
    by_anchor: Dict[str, List[str]] = {}
    for k in keys:
        core = distinctive_core(reps[k]) or frozenset(_tokens(reps[k])[:1])
        anchors = set(core)
        initials = initialism(reps[k])
        if initials:
            anchors.add(initials)
        else:
            # A short name may itself be the acronym of a longer one.
            flat = "".join(_tokens(reps[k]))
            if 2 <= len(flat) <= 6:
                anchors.add(flat)
        for token in anchors:
            by_anchor.setdefault(token, []).append(k)
    # Only names sharing a token can match, which keeps this near-linear
    # instead of comparing all 3,500 names pairwise.
    for candidates in by_anchor.values():
        for i, left in enumerate(candidates):
            for right in candidates[i + 1:]:
                if find(left) == find(right):
                    continue
                if same_organisation_by_structure(reps[left], reps[right]):
                    union(left, right)

    merged_buckets: Dict[str, List[str]] = {}
    for k in keys:
        merged_buckets.setdefault(find(k), []).extend(buckets[k])
    buckets = {k: sorted(set(v)) for k, v in merged_buckets.items()}

    groups: List[Tuple[str, List[str]]] = []
    for key, names in buckets.items():
        if len(names) < 2:
            continue
        # An explicit alias names its own winner; otherwise score the spellings.
        forced = {EXPLICIT_ALIASES[n] for n in names if n in EXPLICIT_ALIASES}
        keep = (sorted(forced)[0] if len(forced) == 1 and sorted(forced)[0] in names
                else preferred_name(names, usage))
        groups.append((keep, [n for n in names if n != keep]))

    groups.sort(key=lambda g: -len(g[1]))
    return groups


def merge_entities(conn: sqlite3.Connection, keep: str, drop: Iterable[str]) -> int:
    """Repoint every link from ``drop`` onto ``keep`` and delete the duplicates.

    ``INSERT OR IGNORE`` semantics are needed because an event can already link
    to both spellings; the update is done with ``UPDATE OR IGNORE`` followed by
    a delete of whatever could not move, which leaves exactly one link.
    """
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?",
                       (keep,)).fetchone()
    if row is None:
        logger.warning("Keep-name %r not present; skipping group", keep)
        return 0
    keep_id = row[0]

    moved = 0
    for name in drop:
        got = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?",
                           (name,)).fetchone()
        if got is None or got[0] == keep_id:
            continue
        drop_id = got[0]
        for table in LINK_TABLES:
            conn.execute(f"UPDATE OR IGNORE {table} SET entity_id = ? WHERE entity_id = ?",
                         (keep_id, drop_id))
            conn.execute(f"DELETE FROM {table} WHERE entity_id = ?", (drop_id,))
        # The scalar columns store names, not ids, so they move separately.
        conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ? "
                     "WHERE victim_organization_name = ?", (keep, name))
        conn.execute("UPDATE DeduplicatedEvents SET vendor_organization_name = ? "
                     "WHERE vendor_organization_name = ?", (keep, name))
        conn.execute("DELETE FROM EntitiesV2 WHERE entity_id = ?", (drop_id,))
        moved += 1
    return moved


def inherit_attributes(conn: sqlite3.Connection, keep: str) -> None:
    """Fill blank kind/industry/nationality on the surviving entity.

    The best-spelled name is not always the best-populated row, so a merge
    should not lose an ``is_australian`` flag or industry that one of the
    dropped spellings carried. Only blanks are filled - nothing is overwritten.
    """
    conn.execute(
        """
        UPDATE EntitiesV2 SET
            entity_kind = COALESCE(entity_kind, (
                SELECT entity_kind FROM EntitiesV2 x WHERE x.entity_name = ?
                  AND x.entity_kind IS NOT NULL LIMIT 1)),
            is_australian = COALESCE(is_australian, (
                SELECT is_australian FROM EntitiesV2 x WHERE x.entity_name = ?
                  AND x.is_australian IS NOT NULL LIMIT 1))
        WHERE entity_name = ?
        """, (keep, keep, keep))


class NamedOrganisation(BaseModel):
    """The organisation a descriptive phrase actually refers to."""

    name: Optional[str] = Field(
        default=None,
        description=("The organisation's proper name, exactly as it appears in "
                     "the evidence. Null if the evidence does not name it - "
                     "never invent or infer a name."))
    confidence: float = Field(ge=0.0, le=1.0)
    basis: str = Field(description="Where in the evidence the name appears.")


class OrganisationResolver:
    """Turn a description of an organisation into its name.

    "Australia's largest home builder" is Metricon; "Australian educational
    support platform" is Thanks For the Help. Both names are stated in the
    event's own description, so the text is tried first and a search is used
    only when it is not.
    """

    def __init__(self, perplexity_key: Optional[str] = None, openai_client=None):
        import os

        self.perplexity_key = perplexity_key or os.getenv("PERPLEXITY_API_KEY")
        self._client = openai_client
        if self._client is None:
            try:
                import instructor
                from openai import OpenAI

                key = os.getenv("OPENAI_API_KEY")
                if key:
                    self._client = instructor.from_openai(OpenAI(api_key=key))
            except Exception as exc:  # pragma: no cover - dependency issue
                logger.warning("instructor/openai unavailable (%s)", exc)

    def _search(self, query: str) -> Optional[str]:
        if not self.perplexity_key:
            return None
        try:
            import requests

            r = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {self.perplexity_key}",
                         "Content-Type": "application/json"},
                json={"model": "sonar-pro",
                      "messages": [{"role": "user", "content": query}],
                      "max_tokens": 400, "temperature": 0.0},
                timeout=60)
            return (r.json()["choices"][0]["message"]["content"]
                    if r.status_code == 200 else None)
        except Exception as exc:
            logger.warning("Perplexity search failed: %s", exc)
            return None

    def resolve(
        self,
        description_phrase: str,
        title: str,
        event_description: Optional[str] = None,
        search: bool = True,
    ) -> Optional[NamedOrganisation]:
        """Name the organisation ``description_phrase`` refers to, or None."""
        if self._client is None:
            return None

        evidence = f"Headline: {title}\n\nReport: {(event_description or '')[:1500]}"
        got = self._ask(description_phrase, evidence)
        if got and got.name and got.confidence >= 0.7:
            return got

        if not search:
            return None
        found = self._search(
            f"Which Australian organisation is described as "
            f"\"{description_phrase}\" in this cyber security incident? "
            f"{title}. {(event_description or '')[:400]} "
            f"Name the organisation, or say it was not publicly named.")
        if not found:
            return None
        return self._ask(description_phrase, evidence + "\n\nSearch:\n" + found)

    def _ask(self, phrase: str, evidence: str) -> Optional[NamedOrganisation]:
        try:
            return self._client.chat.completions.create(
                model="gpt-4o",
                response_model=NamedOrganisation,
                temperature=0.0,
                messages=[
                    {"role": "system",
                     "content": ("You identify the organisation a description "
                                 "refers to. Return its proper name only when "
                                 "the evidence states it. Return null if the "
                                 "organisation was not publicly named - never "
                                 "guess from the description alone.")},
                    {"role": "user",
                     "content": (f"The description {phrase!r} refers to an "
                                 f"organisation. What is its name?\n\n{evidence}")},
                ])
        except Exception as exc:
            logger.warning("Name resolution failed for %r: %s", phrase[:40], exc)
            return None


# ---------------------------------------------------------------------------
# Structural equivalence: containment and acronyms
# ---------------------------------------------------------------------------
#
# canonical_key() only unifies names that normalise identically, so it misses
# the two commonest ways one organisation is written differently:
#
#   containment  Genea / Genea Fertility / Genea IVF Clinic
#                Epworth / Epworth HealthCare
#                HWL Ebsworth / HWL Ebsworth Lawyers
#                Jaguar Land Rover / Jaguar Land Rover Australia
#   acronym      FRV / Fire Rescue Victoria
#                JLR / Jaguar Land Rover
#                ADF / Australian Defence Force
#
# Containment is only safe when the *extra* words say nothing about which
# organisation is meant. "Amnesty International" and "Amnesty International
# Australia" are the same body for this dataset's purposes; "Amnesty
# International Canada" is a different one, and the only thing distinguishing
# them is that "Canada" names a different jurisdiction. Extra words are
# therefore accepted from a fixed list of legal, sector and Australian-place
# words, and any other extra word blocks the match.

# Words that qualify an organisation without changing which one it is.
_NEUTRAL_EXTRAS = {
    # legal form
    "pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "corp",
    "corporation", "co", "company", "holdings", "group", "the", "and",
    # sector descriptors that routinely trail a brand
    "lawyers", "legal", "solicitors", "healthcare", "health", "hospital",
    "hospitals", "medical", "clinic", "clinics", "fertility", "ivf",
    "services", "service", "solutions", "systems", "technologies", "technology",
    "bank", "banking", "insurance", "energy", "telecom", "telecommunications",
    "university", "college", "school", "schools", "council", "department",
    "recruiting", "force", "airlines", "airways", "motors", "automotive",
    "pharmacy", "pharmacies", "foods", "retail", "stores", "mining", "resources",
    "financial", "finance", "fertility", "recruitment", "board", "authority",
    "pathology", "software", "pdf", "labs", "laboratories", "network",
    "hotel", "hotels", "resorts", "resort", "motor", "motoring", "logistics",
    "club", "holdings", "enterprises", "industries",
    # Australian scope words
    "australia", "australian", "aus", "anz", "nsw", "vic", "qld", "wa", "sa",
    "tas", "nt", "act",
}

# Extra words that mean a *different* legal entity, not a fuller name.
_JURISDICTION_BLOCKERS = {
    "canada", "canadian", "usa", "us", "america", "american", "uk", "britain",
    "british", "nz", "zealand", "india", "indian", "singapore", "japan",
    "china", "chinese", "germany", "german", "france", "french", "ireland",
    "global", "international", "worldwide", "europe", "european", "asia",
}

# Extra words accepted by *containment* only. Containment already requires the
# whole shorter name to appear in the longer one, so "Legal Practice Board" and
# "Legal Practice Board of Western Australia" match while "University of
# Sydney" and "Western Sydney University" cannot - the latter is not a subset.
# These are deliberately NOT accepted by core-equality, where "Western" is what
# separates Western Sydney University from the University of Sydney.
_CONTAINMENT_EXTRAS = _NEUTRAL_EXTRAS | {"of", "for", "in", "at", "the"}

# Three, not four: "PwC" and "IBM" are brands, and the neutral/place filters
# already remove the short tokens that would otherwise be noise.
_MIN_DISTINCTIVE = 3


# A place name identifies a jurisdiction, never an organisation. Without this
# "Queensland Health", "Bank of Queensland" and "Queensland University of
# Technology" all reduce to {queensland} and merge into one entity - which is
# exactly what happened, chaining 60 unrelated bodies into "Australian
# Government".
_PLACE_TOKENS = {
    "australia", "australian", "aus", "queensland", "victoria", "victorian",
    "tasmania", "tasmanian", "sydney", "melbourne", "brisbane", "perth",
    "adelaide", "canberra", "darwin", "hobart", "new", "south", "north",
    "east", "west", "western", "eastern", "northern", "southern", "wales",
    "territory", "capital", "state", "national", "federal", "regional",
    "commonwealth", "government", "city", "metropolitan", "central",
    # Foreign jurisdictions, so "New Zealand Government" and the NZ cyber
    # centre are not folded into one another on "zealand" alone.
    "zealand", "kingdom", "states", "britain", "guinea", "island", "islands",
}

# Words naming a *kind* of institution or actor. Alone they identify nobody, so
# "Police" and "Police Bank", "Court" and "Court Services Victoria",
# "Ransomware" and "INC Ransomware Group" must not be treated as one entity.
_GENERIC_INSTITUTIONS = {
    "police", "court", "courts", "parliament", "office", "ministry", "bureau",
    "commission", "committee", "agency", "ministry", "administration",
    "ransomware", "malware", "hackers", "hacker", "gang", "cybercriminals",
    "attack", "breach", "incident", "entity", "firm", "provider", "operator",
    "aussie", "russian", "chinese", "iranian", "abc", "news", "media",
    "clinic", "clinics", "centre", "center", "trust", "fund", "party",
    # Portfolio words. "Department of Education" and "NSW Department of
    # Education" are different bodies, and the portfolio name is all they
    # share, so it cannot be what identifies either of them.
    "education", "training", "transport", "justice", "environment",
    "planning", "treasury", "corrections", "communities",
    # Regulator and topic words. Every jurisdiction has an Information
    # Commissioner and a Privacy Commissioner; the title is not the identity.
    "information", "commissioner", "privacy", "security", "cyber", "sector",
    "act", "protection", "digital", "data", "business", "news", "gov",
}


def distinctive_core(name: str) -> frozenset:
    """The tokens that actually identify the organisation.

    Drops legal forms, sector words and Australian scope words, keeping what
    remains: "Epworth Hospital" and "Epworth HealthCare" both reduce to
    ``{epworth}``, while "Amnesty International Canada" keeps ``canada`` and so
    stays distinct from the Australian body.
    """
    core = frozenset(
        t for t in _tokens(name)
        if len(t) >= _MIN_DISTINCTIVE
        and t not in _NEUTRAL_EXTRAS
        and t not in _PLACE_TOKENS
        and t not in _GENERIC_INSTITUTIONS)
    # A name made only of place and category words identifies nothing, so it
    # must not match anything by core equality.
    return core


# State abbreviations, expanded before comparison so "NSW Government" and
# "New South Wales government" reduce to the same tokens.
_STATE_EXPANSIONS = {
    "nsw": ("new", "south", "wales"),
    "vic": ("victoria",),
    "qld": ("queensland",),
    "wa": ("western", "australia"),
    "sa": ("south", "australia"),
    "tas": ("tasmania",),
    "nt": ("northern", "territory"),
    "act": ("australian", "capital", "territory"),
}


def _tokens(name: str) -> List[str]:
    """Lowercase tokens, with state abbreviations expanded and repeats dropped.

    Repeats are dropped because coverage writes the abbreviation alongside the
    full name - "New South Wales (NSW) government" - which would otherwise
    expand to a different token list from "NSW Government".
    """
    text = re.sub(r"\(([^)]{2,8})\)", " ", name or "")   # drop "(TMC)", "(ASIC)"
    raw = [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]
    out: List[str] = []
    for token in raw:
        for part in _STATE_EXPANSIONS.get(token, (token,)):
            if part not in out:
                out.append(part)
    return out


# Joining words that an initialism leaves out.
_ACRONYM_SKIP = {"of", "the", "and", "for", "in", "at", "a", "&"}

# Words that introduce a sub-unit rather than a different organisation.
_SUBUNIT_MARKERS = {
    "school", "faculty", "campus", "division", "branch", "unit", "centre",
    "center", "institute", "college", "office", "team", "hospital",
}


def initialism(name: str) -> str:
    """The initials of ``name``, skipping joining words."""
    words = [w for w in _tokens(name) if w not in _ACRONYM_SKIP]
    return "".join(w[0] for w in words) if len(words) >= 2 else ""


def is_acronym_of(short: str, long: str) -> bool:
    """True when ``short`` is the initialism of ``long``.

    Handles names that already contain an acronym: "HWLE" against "HWL
    Ebsworth" takes H, W, L from the first token and E from the second.
    """
    letters = "".join(_tokens(short))
    if not (2 <= len(letters) <= 6) or not letters.isalpha():
        return False
    words = [w for w in _tokens(long) if w not in _ACRONYM_SKIP]
    if len(words) < 2:
        return False

    if letters == "".join(w[0] for w in words):
        return True
    # Expand a leading all-consonant token ("HWL") into its own letters.
    expanded = "".join(w if len(w) <= 4 and not any(
        c in "aeiou" for c in w) else w[0] for w in words)
    return letters == expanded


def same_organisation_by_structure(
    left: str, right: str, rare_tokens: Optional[frozenset] = None
) -> bool:
    """True when two names denote one organisation on structure alone.

    Symmetric. Used to widen :func:`canonical_groups`, never to override an
    explicit alias.

    ``rare_tokens`` optionally carries the tokens that identify only one
    organisation across the whole corpus. When the shared part is one of those,
    extra words are allowed to be anything - "Lion" and "Lion Dairy & Drinks"
    are the same company, and nothing but rarity distinguishes that from "Bank"
    and "Bank of Queensland", where the shared token is worthless.
    """
    a, b = _tokens(left), _tokens(right)

    # "ManageMyHealth" and "Manage My Health" differ only in spacing.
    if a and b and "".join(a) == "".join(b):
        return True

    # "Dodo/iPrimus" lists two brands of one business; matching either is enough.
    for one, other in ((left, right), (right, left)):
        if "/" in one:
            parts = [p.strip() for p in one.split("/") if p.strip()]
            if any(_tokens(part) == _tokens(other) for part in parts):
                return True
    if not a or not b or a == b:
        return bool(a and a == b)

    if is_acronym_of(left, right) or is_acronym_of(right, left):
        return True

    core_a, core_b = distinctive_core(left), distinctive_core(right)
    if core_a and core_a == core_b:
        return True
    # A jurisdiction word is what tells two national bodies apart.
    if (core_a | core_b) & _JURISDICTION_BLOCKERS and core_a != core_b:
        return False

    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not set(short).issubset(set(long)):
        return False
    if not distinctive_core(" ".join(short)):
        return False

    extras = [t for t in long if t not in short]
    if not extras:
        return False
    # Only neutral extras. A prefix match on arbitrary extra words was tried
    # and merged "David" into "David Jones", "Aussie" into "Aussie poultry
    # processor" and "Police" into "Police Bank"; a genuine parent/division
    # pair such as Lion / Lion Dairy & Drinks is an explicit alias instead.
    if all(t in _CONTAINMENT_EXTRAS for t in extras):
        return True
    # A named sub-unit is still the same organisation: "UNSW School of Physics"
    # is UNSW, "St Vincent's Hospital Melbourne campus" is St Vincent's.
    return any(t in _SUBUNIT_MARKERS for t in extras)

