"""Find every organisation affected by a supplier breach, so the incident exists once with all its victims.

A supplier breach is one incident with several co-equal victims: ProctorU
(2020) exposed students of ten Australian universities, Frontier Software's
payroll breach hit the South Australian Government, Canvas/Instructure hit
universities and state education departments. The schema allows several
``victim`` roles per event, but nothing found the other victims, and the same
supplier incident was often stored as separate events, one per customer.

Three steps, each over stored evidence and each answer stored:

1. **Detect** supplier incidents (:func:`supplier_incidents`): active events
   with a vendor (role or ``vendor_organization_name``) or whose own text or
   member records describe a third-party, supplier, vendor or supply-chain
   compromise.
2. **Gather candidates.**
   * *Victim candidates* (:func:`victim_candidates`): organisations the event's
     records link as affected / affected customer / victim, the organisations
     Perplexity named on its records, and - with ``research`` - the
     organisations named by one stored Perplexity search per incident
     ("Australian organisations affected by <supplier> breach (<date>)",
     :func:`research_incident`; a live search cannot be replayed, so query,
     answer, citations and model are kept in ``SupplierIncidentSearches``).
   * *Event candidates* (:func:`pair_candidates`): other active events that may
     be the same supplier incident recorded under a different customer - from
     the multi-key candidate generation in :mod:`candidates` (shared article,
     rare subject word within 60 days), a shared supplier, or a victim
     candidate's own event within :data:`OWN_EVENT_WINDOW_DAYS`.
3. **Judge** with Jev (:mod:`utils.jev`): *is <org> an organisation whose data
   or systems were affected by this incident?* and, for event candidates,
   *is the other event the same supplier incident?* Scores inside
   :data:`ESCALATE_BAND` are re-judged by the reasoning model
   (:mod:`utils.escalation`); if that fails the item is **undecided** and never
   acted on with Jev's raw score.

**One event per customer, linked** (owner decision, 2026-10-31). A supplier breach is the
supplier's own event plus one event per major affected customer (victim = the customer, vendor =
the supplier), tied together by ``SupplierIncidents`` / ``SupplierIncidentLinks``
(:mod:`supplier_links`). No event carries several customers as co-victims and no customer event is
merged into another or into the supplier's. This module used to do both - attach accepted customers
as ``victim`` roles of the supplier incident and merge customers' events into it - which counted a
customer twice when it had its own event and filed another organisation's event under it (Allens,
attached to the NSW Health Accellion event beside its own January 2021 Accellion event, 2026-10-30).

The judgements above are now **link** decisions (labels first, then the calibrated cut-offs): an
event judged the same supplier incident is linked as its customer's event, and a named customer
accepted as affected with no event of its own near the incident is linked with no event. Without
``apply`` :func:`find_supplier_victims` reports the links it would write; with ``apply`` (labels and a
current calibration required, as before) it

* converts the old state (:func:`supplier_links.convert`): every victim role
  ``SupplierVictimAttachments`` added that is still present is removed (:func:`reverse_attachments`,
  audit row ``reversed`` with the owner decision as reason) and the customer becomes a link row, and an
  event the supplier step joined across customers is split per customer through the ledger;
* writes the links (idempotent upserts) and lists the events that hold or held several customers
  (:func:`multi_customer_events`).

:func:`attach_victim`, :func:`carry_victims` and :func:`merge_pairs` remain for the audit trail and the
manual ``dedup_admin.py merge`` command; ``find_supplier_victims`` never calls them.
Victims are organisations: products, people, threat actors, collectives
("Australian universities") and sector labels are never candidates.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup.entity_resolution import EntityResolver, canonical_key
from cyber_data_collector.dedup.victim_counting import same_organisation as same_org, with_acronym_policy
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = ROOT / "evaluation" / "supplier_victim_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "supplier_victim_calibration.json"

VICTIM_QUESTION_ID = "affected_by_supplier_incident"
VICTIM_VERSION = "supplier-victim-v1"
VICTIM_ESCALATION_ID = "affected_by_supplier_incident_escalated"
VICTIM_ESCALATION_VERSION = "supplier-victim-escalation-v1"
PAIR_QUESTION_ID = "same_supplier_incident"
PAIR_VERSION = "same-supplier-incident-v1"
PAIR_ESCALATION_ID = "same_supplier_incident_escalated"
PAIR_ESCALATION_VERSION = "same-supplier-incident-escalation-v1"
# Fixed before calibration, like the other filters' bands.
ESCALATE_BAND = (0.2, 0.8)

KIND_VICTIM = "victim"
KIND_PAIR = "same_incident"
POSITIVE = {KIND_VICTIM: "affected", KIND_PAIR: "same"}
# 'already_recorded': the candidate is one of the event's recorded victims under
# another name - attaching it would count that organisation twice.
NEGATIVE = {KIND_VICTIM: {"not_affected", "already_recorded"}, KIND_PAIR: {"different"}}
LABELS = {KIND_VICTIM: {"affected", "not_affected", "already_recorded", "unsure"},
          KIND_PAIR: {"same", "different", "unsure"}}

RECORDS_PER_EVENT = 6
# An event candidate must lie within this many days of the supplier incident.
PAIR_WINDOW_DAYS = 365
# A victim with its own active event this close to the incident is held back
# until that event is merged or judged a different incident.
OWN_EVENT_WINDOW_DAYS = 365
MAX_SEARCH_NAMES = 25

# Roles under which a record links an organisation that may have been affected.
# Not 'affected': extraction gives every unclassified link that role (it once
# held all 23,388 links), so it names journalists, regulators and dates too.
CANDIDATE_ROLES = ("victim", "affected_customer", "primary_target")

SUPPLIER_TEXT = re.compile(
    r"third[- ]part(?:y|ies)|\bsupplier|supply[- ]chain|\bvendor|service provider|"
    r"software provider|payroll provider|it provider|managed service|\bcontractor|"
    r"outsourc\w*|external (?:data )?partner|via (?:a|an|its|the) [\w-]+ (?:provider|platform|partner)",
    re.IGNORECASE)

# Words that make a name a group or a sector rather than one organisation.
_COLLECTIVE_WORDS = {"sector", "industry", "sectors", "industries", "customers", "clients",
                     "users", "students", "members", "staff", "employees", "patients",
                     "institutions", "organisations", "organizations", "providers",
                     "agencies", "businesses", "companies", "councils", "universities",
                     "schools", "hospitals", "retailers", "banks", "insurers", "firms"}
_UNUSABLE_NAMES = {"none", "null", "unknown", "n/a", "na", "not specified", "multiple", "various"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS SupplierIncidentSearches (
    deduplicated_event_id TEXT NOT NULL,
    query_hash            TEXT NOT NULL,
    query                 TEXT NOT NULL,
    model                 TEXT NOT NULL,
    answer                TEXT,
    citations_json        TEXT,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (deduplicated_event_id, query_hash)
);
CREATE TABLE IF NOT EXISTS SupplierVictimAttachments (
    attachment_id         TEXT PRIMARY KEY,
    deduplicated_event_id TEXT NOT NULL,
    entity_id             INTEGER NOT NULL,
    entity_name           TEXT NOT NULL,
    previous_role         TEXT,            -- NULL when the event had no link to the entity
    decided_by            TEXT NOT NULL,   -- jev / escalation / label / merge
    subject_id            TEXT,            -- judgement subject (event|org or pair)
    probability           REAL,
    cutoff                REAL,
    question_version      TEXT,
    merged_from           TEXT,            -- source event, for victims carried across a merge
    snapshot_batch        TEXT,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplier_attach_event
    ON SupplierVictimAttachments (deduplicated_event_id);
CREATE TABLE IF NOT EXISTS SupplierVictimReversals (
    attachment_id         TEXT PRIMARY KEY,   -- the SupplierVictimAttachments row undone
    deduplicated_event_id TEXT NOT NULL,
    entity_id             INTEGER NOT NULL,
    entity_name           TEXT NOT NULL,
    action                TEXT NOT NULL,      -- 'reversed'; 'restored' when it took the event's own victim
    restored_role         TEXT,               -- NULL: the event link was removed
    own_events_json       TEXT NOT NULL,      -- the organisation's other Active events as victim
    reason                TEXT NOT NULL,
    snapshot_batch        TEXT,
    created_at            TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------

@dataclass
class SupplierIncident:
    """An active event that describes a supplier or third-party compromise."""

    dedup_id: str
    title: str
    event_date: Optional[str]
    vendors: List[str]
    victims: List[Tuple[int, str]]
    reasons: List[str]
    members: int = 0
    described: bool = False   # vendor recorded, or the event's own text names a third party

    @property
    def searchable(self) -> bool:
        """Worth a supplier search: a stray record mentioning a contractor is not enough."""
        return self.described


def _usable_name(name: Optional[str]) -> bool:
    text = (name or "").strip()
    return bool(text) and text.lower() not in _UNUSABLE_NAMES and len(text) <= 120


def is_candidate_organisation(name: Optional[str], kind: Optional[str] = None) -> bool:
    """True when ``name`` can be a victim: one organisation, not a product, person,
    threat actor, regulator, collective or sector label."""
    from cyber_data_collector.dedup.entity_classification import rule_kind
    from cyber_data_collector.dedup.victim_selection import (
        is_descriptive_label, is_never_victim, is_non_organisation,
    )

    if not _usable_name(name):
        return False
    if kind is not None and kind not in ("organisation", "government_body"):
        return False
    text = name.strip()
    if "(" in text and re.search(r"multiple|various|several|affected", text, re.IGNORECASE):
        return False
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    if tokens & _COLLECTIVE_WORDS:
        return False
    if not re.search(r"[A-Z]", text):   # "state schools", "the advertiser"
        return False
    words = text.split()
    if len(words) >= 2 and all(re.fullmatch(r"[a-z][a-z'-]*", w) for w in words[1:]):
        return False   # a description, not a name: "Sydney radiologist", "Swedish pharmaceutical chain"
    if is_non_organisation(text) or is_never_victim(text) or is_descriptive_label(text):
        return False
    # Not looks_like_person: "Monash University" has a person's shape. People
    # are excluded by their recorded kind, and the judgement sees the rest.
    if kind is not None:
        return True   # the classifier's typing outranks name rules ("Pronto Software" is a company)
    rule = rule_kind(text)
    return rule is None or rule.value in ("organisation", "government_body")


def _usable_vendor(name: Optional[str]) -> bool:
    """A named supplier - "Frontier Software" counts even though it reads like a product -
    not a description such as "third-party IT provider"."""
    from cyber_data_collector.dedup.victim_selection import is_descriptive_label, is_non_organisation

    text = (name or "").strip()
    return (_usable_name(text) and bool(re.search(r"[A-Z]", text))
            and not is_non_organisation(text) and not is_descriptive_label(text))


def _records_text(conn: sqlite3.Connection, dedup_id: str) -> List[str]:
    return [" ".join(x for x in r if x) for r in conn.execute(
        """SELECT e.title, e.description FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           WHERE m.deduplicated_event_id = ?""", (dedup_id,))]


def supplier_incidents(conn: sqlite3.Connection,
                       resolver: Optional[EntityResolver] = None) -> List[SupplierIncident]:
    """Active events with a vendor, or whose text or records describe a supplier compromise."""
    resolver = with_acronym_policy(conn, resolver or EntityResolver(conn))
    out: List[SupplierIncident] = []
    for did, title, edate, desc, summ, vendor in conn.execute(
            """SELECT deduplicated_event_id, title, event_date, description, summary,
                      vendor_organization_name
               FROM DeduplicatedEvents WHERE status = 'Active'
               ORDER BY deduplicated_event_id""").fetchall():
        reasons: List[str] = []
        vendors: List[str] = []
        roles = conn.execute(
            """SELECT v.entity_id, v.entity_name, dee.relationship_type
               FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               WHERE dee.deduplicated_event_id = ? AND dee.relationship_type IN ('vendor', 'victim')
               ORDER BY v.entity_name""", (did,)).fetchall()
        for name in [vendor] + [r[1] for r in roles if r[2] == "vendor"]:
            if _usable_vendor(name) and not any(resolver.same_organisation(name, v) or name == v
                                              for v in vendors):
                vendors.append(name)
        if vendors:
            reasons.append("vendor recorded: " + "; ".join(vendors))
        if SUPPLIER_TEXT.search(" ".join(x for x in (title, desc, summ) if x)):
            reasons.append("event text describes a third-party compromise")
        described = bool(reasons)
        records = _records_text(conn, did)
        matching = sum(1 for t in records if SUPPLIER_TEXT.search(t))
        if matching:
            reasons.append(f"{matching} of {len(records)} record(s) describe a third-party compromise")
        if not reasons:
            continue
        out.append(SupplierIncident(did, title or "", edate, vendors,
                                    [(r[0], r[1]) for r in roles if r[2] == "victim"],
                                    reasons, len(records), described))
    return out


# ----------------------------------------------------------------------
# Research: one stored search per incident
# ----------------------------------------------------------------------

SEARCH_PROMPT = """Search for public reporting (news, regulator, company or university statements, \
breach notifications, ransomware leak-site trackers) about the cyber incident below, which \
involved a supplier, vendor or third-party service provider.

Incident: {title}
Date recorded: {date}
Supplier / vendor recorded: {vendor}

List the organisations - especially Australian organisations - whose data or systems were \
affected by THIS incident (customers of the supplier whose data was exposed, or the \
organisation itself). Do not list organisations affected by a different incident.

Answer in exactly this format, one organisation per line, at most {limit} lines:
AFFECTED: <organisation name> | <how it was affected, one short sentence> | <source URL>
If none can be identified, answer exactly: AFFECTED: NONE FOUND"""


def build_search_query(incident: SupplierIncident) -> str:
    return SEARCH_PROMPT.format(title=incident.title or "(untitled)", date=incident.event_date or "unknown",
                                vendor="; ".join(incident.vendors) or "(not recorded)",
                                limit=MAX_SEARCH_NAMES)


def _query_hash(query: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{query}".encode("utf-8")).hexdigest()


def stored_search(conn: sqlite3.Connection, dedup_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT answer, citations_json, model, created_at, query FROM SupplierIncidentSearches "
        "WHERE deduplicated_event_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (dedup_id,)).fetchone()
    if row is None:
        return None
    return {"answer": row[0], "citations": json.loads(row[1] or "[]")[:8], "model": row[2],
            "searched_at": row[3], "query": row[4]}


class SearchUnavailable(RuntimeError):
    """A supplier search was requested and cannot be made (no key)."""


def research_incident(conn: sqlite3.Connection, incident: SupplierIncident,
                      search: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Search once for the incident's affected organisations; reuse the stored answer.

    Raises :class:`SearchUnavailable` when no key is configured, and lets a
    failed search raise: a requested search is never silently skipped.
    """
    from cyber_data_collector.utils.llm_models import perplexity_model

    ensure_schema(conn)
    model = perplexity_model()
    query = build_search_query(incident)
    digest = _query_hash(query, model)
    if conn.execute("SELECT 1 FROM SupplierIncidentSearches WHERE deduplicated_event_id = ? "
                    "AND query_hash = ?", (incident.dedup_id, digest)).fetchone():
        return stored_search(conn, incident.dedup_id)
    if search is None:
        if not os.getenv("PERPLEXITY_API_KEY"):
            raise SearchUnavailable("PERPLEXITY_API_KEY is not set (is the project .env loaded?); "
                                    "--research needs it")
        from cyber_data_collector.dedup.corroboration import _perplexity_search
        search = _perplexity_search
    result = search(query, model)
    conn.execute("INSERT INTO SupplierIncidentSearches VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (incident.dedup_id, digest, query, model, result.get("answer"),
                  json.dumps(result.get("citations") or []), datetime.now().isoformat()))
    conn.commit()
    return stored_search(conn, incident.dedup_id)


_AFFECTED_LINE = re.compile(r"^\s*(?:[-*\d.)\s]*)AFFECTED:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_search_names(answer: Optional[str]) -> List[Dict[str, str]]:
    """Organisations named in a stored search answer, in order, without duplicates."""
    out: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for line in _AFFECTED_LINE.findall(answer or ""):
        parts = [p.strip() for p in line.split("|")]
        name = re.sub(r"\[\d+\]", "", parts[0]).strip(" *_\"'")
        if not name or name.upper().startswith("NONE FOUND"):
            continue
        key = canonical_key(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "how": parts[1] if len(parts) > 1 else "",
                    "source": parts[2] if len(parts) > 2 else ""})
    return out[:MAX_SEARCH_NAMES]


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------

@dataclass
class VictimCandidate:
    dedup_id: str
    name: str
    entity_id: Optional[int]
    kind: Optional[str]
    found_by: List[str] = field(default_factory=list)

    @property
    def subject_id(self) -> str:
        key = str(self.entity_id) if self.entity_id is not None else "name:" + canonical_key(self.name)
        return f"{self.dedup_id}|{key}"


@dataclass
class PairCandidate:
    supplier_id: str     # the supplier incident, the merge target's side
    other_id: str
    reasons: List[str]
    gap_days: Optional[int]

    @property
    def pair_id(self) -> str:
        return "|".join(sorted((self.supplier_id, self.other_id)))


def _formal_names(conn: sqlite3.Connection, dedup_id: str) -> List[str]:
    names = []
    for (pe,) in conn.execute(
            """SELECT e.perplexity_enrichment_data FROM EventDeduplicationMap m
               JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
               WHERE m.deduplicated_event_id = ?""", (dedup_id,)):
        try:
            name = json.loads(pe or "{}").get("formal_entity_name")
        except (TypeError, ValueError, AttributeError):
            name = None
        if _usable_name(name):
            names.append(str(name).strip())
    return names


class _EntityIndex:
    """Resolve a name to an existing entity: same canonical key, else the strict same-organisation test."""

    def __init__(self, conn: sqlite3.Connection, resolver: EntityResolver):
        self.resolver = resolver
        self.by_key: Dict[str, Tuple[int, str, Optional[str]]] = {}
        self.by_block: Dict[str, List[Tuple[int, str, Optional[str]]]] = defaultdict(list)
        for eid, name, kind in conn.execute(
                "SELECT entity_id, entity_name, entity_kind FROM EntitiesV2 ORDER BY entity_id"):
            if not name:
                continue
            self.by_key.setdefault(resolver.key_for(name), (eid, name, kind))
            for block in resolver.blocks_for(name):
                self.by_block[block].append((eid, name, kind))

    def find(self, name: str) -> Optional[Tuple[int, str, Optional[str]]]:
        hit = self.by_key.get(self.resolver.key_for(name))
        if hit:
            return hit
        seen = set()
        for block in sorted(self.resolver.blocks_for(name)):
            for row in self.by_block.get(block, []):
                if row[0] in seen:
                    continue
                seen.add(row[0])
                if same_org(self.resolver, name, row[1]):
                    return row
        return None


def victim_candidates(conn: sqlite3.Connection, incident: SupplierIncident,
                      resolver: EntityResolver, index: Optional[_EntityIndex] = None,
                      search: Optional[Dict[str, Any]] = None) -> List[VictimCandidate]:
    """Organisations that may have been affected, not yet recorded as the event's victims."""
    index = index or _EntityIndex(conn, resolver)
    excluded = [n for _, n in incident.victims] + list(incident.vendors)
    found: Dict[str, VictimCandidate] = {}

    def add(name: str, entity_id: Optional[int], kind: Optional[str], why: str) -> None:
        if not is_candidate_organisation(name, kind):
            return
        if any(name == x or same_org(resolver, name, x) for x in excluded):
            return
        if entity_id is None:
            hit = index.find(name)
            if hit:
                entity_id, name, kind = hit
                if not is_candidate_organisation(name, kind) or any(
                        name == x or same_org(resolver, name, x) for x in excluded):
                    return
        cand = VictimCandidate(incident.dedup_id, name, entity_id, kind)
        for existing in found.values():
            if (entity_id is not None and existing.entity_id == entity_id) or \
                    same_org(resolver, existing.name, name):
                if why not in existing.found_by:
                    existing.found_by.append(why)
                return
        cand.found_by.append(why)
        found[cand.subject_id] = cand

    linked: Dict[Tuple[int, str, Optional[str]], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for eid, name, kind, role in conn.execute(
            f"""SELECT v.entity_id, v.entity_name, v.entity_kind, ee.relationship_type
                FROM EventDeduplicationMap m
                JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
                JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                WHERE m.deduplicated_event_id = ?
                  AND ee.relationship_type IN ({','.join('?' * len(CANDIDATE_ROLES))})""",
            (incident.dedup_id, *CANDIDATE_ROLES)):
        linked[(eid, name, kind)][role] += 1
    for eid, name, kind, role in conn.execute(
            f"""SELECT v.entity_id, v.entity_name, v.entity_kind, dee.relationship_type
                FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
                WHERE dee.deduplicated_event_id = ?
                  AND dee.relationship_type IN ({','.join('?' * len(CANDIDATE_ROLES))})""",
            (incident.dedup_id, *CANDIDATE_ROLES)):
        linked[(eid, name, kind)].setdefault(f"event-level {role}", 1)
    for (eid, name, kind), roles in sorted(linked.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
        add(name, eid, kind, "linked on the records as " +
            ", ".join(f"{r} ({n})" for r, n in sorted(roles.items())))

    formal = defaultdict(int)
    for name in _formal_names(conn, incident.dedup_id):
        formal[name] += 1
    for name, n in sorted(formal.items()):
        add(name, None, None, f"named by Perplexity research on {n} record(s)")

    for item in parse_search_names((search or {}).get("answer")):
        add(item["name"], None, None, "named by the supplier search: " + (item["how"] or "")[:160])

    return sorted(found.values(), key=lambda c: c.subject_id)


def _parse_date(value: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def _gap(a: Optional[str], b: Optional[str]) -> Optional[int]:
    da, db = _parse_date(a), _parse_date(b)
    return abs((da - db).days) if da and db else None


def victim_events(conn: sqlite3.Connection) -> Dict[int, List[Tuple[str, Optional[str]]]]:
    """entity id -> its active events as victim, (event id, date)."""
    out: Dict[int, List[Tuple[str, Optional[str]]]] = defaultdict(list)
    for eid, did, edate in conn.execute(
            """SELECT dee.entity_id, d.deduplicated_event_id, d.event_date
               FROM DeduplicatedEventEntities dee
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE dee.relationship_type = 'victim' AND d.status = 'Active'"""):
        out[eid].append((did, edate))
    return out


def pair_candidates(conn: sqlite3.Connection, incidents: Sequence[SupplierIncident],
                    candidates: Dict[str, List[VictimCandidate]],
                    resolver: EntityResolver) -> List[PairCandidate]:
    """Other active events that may be the same supplier incident under a different customer."""
    from cyber_data_collector.dedup.candidates import content_pairs, url_pairs

    events = {r[0]: r for r in conn.execute(
        """SELECT deduplicated_event_id, title, event_date, COALESCE(summary, description, ''),
                  vendor_organization_name FROM DeduplicatedEvents WHERE status = 'Active'""")}
    urls: Dict[str, Set[str]] = defaultdict(set)
    for did, url in conn.execute(
            """SELECT m.deduplicated_event_id, r.source_url FROM EventDeduplicationMap m
               JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
               JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
               WHERE r.source_url IS NOT NULL"""):
        if did in events:
            urls[did].add(url)
    shared_url, _ = url_pairs(urls)
    shared_subject = content_pairs({d: (r[1] or "", r[3] or "") for d, r in events.items()},
                                   {d: r[2] for d, r in events.items()})
    names: Dict[str, Set[str]] = defaultdict(set)
    for did, name in conn.execute(
            """SELECT dee.deduplicated_event_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               WHERE dee.relationship_type IN ('vendor', 'victim', 'affected_customer', 'affected')"""):
        if did in events and name:
            names[did].add(name)
    for did, r in events.items():
        if r[4]:
            names[did].add(r[4])
    own = victim_events(conn)
    victims_of: Dict[str, Set[int]] = defaultdict(set)
    for eid, evs in own.items():
        for did, _ in evs:
            victims_of[did].add(eid)

    pairs: Dict[str, PairCandidate] = {}

    def add(supplier: SupplierIncident, other: str, why: str) -> None:
        if other == supplier.dedup_id or other not in events:
            return
        gap = _gap(supplier.event_date, events[other][2])
        if gap is None or gap > PAIR_WINDOW_DAYS:
            return
        # A pair sharing a victim is check-duplicates' question, not this one.
        if {e for e, _ in supplier.victims} & victims_of.get(other, set()):
            return
        pid = "|".join(sorted((supplier.dedup_id, other)))
        if pid not in pairs:
            pairs[pid] = PairCandidate(supplier.dedup_id, other, [], gap)
        if why not in pairs[pid].reasons:
            pairs[pid].reasons.append(why)

    for inc in incidents:
        for key, why in list(shared_url.items()) + list(shared_subject.items()):
            if inc.dedup_id in key:
                other = next(iter(set(key) - {inc.dedup_id}))
                for w in sorted(why):
                    add(inc, other, w)
        for vendor in inc.vendors:
            for did, ns in names.items():
                if did != inc.dedup_id and any(resolver.same_organisation(vendor, n) for n in ns):
                    add(inc, did, f"names the same supplier ({vendor})")
        for cand in candidates.get(inc.dedup_id, []):
            if cand.entity_id is None:
                continue
            for did, edate in own.get(cand.entity_id, []):
                gap = _gap(inc.event_date, edate)
                if gap is not None and gap <= OWN_EVENT_WINDOW_DAYS:
                    add(inc, did, f"own event of candidate victim {cand.name}")
    return sorted(pairs.values(), key=lambda p: p.pair_id)


# ----------------------------------------------------------------------
# Judgement
# ----------------------------------------------------------------------

VICTIM_INSTRUCTIONS = (
    "`incident` is a stored cyber incident that involved a supplier, vendor or third-party "
    "service provider (`incident.vendors`, when recorded). Its `records` hold the page actually "
    "stored for each source (often only partly about the incident) and separate Perplexity "
    "research; `corroboration` and `supplier_search`, when present, are stored web searches for "
    "the incident and for the organisations it affected. `candidate` is an organisation named in "
    "that evidence (`candidate.found_by` says where). Decide whether the candidate is an "
    "organisation whose own data, systems or customers' data were affected by THIS incident - "
    "for example a customer of the breached supplier whose students', staff or customers' records "
    "were exposed. Mentioned is not affected: a regulator, an investigator or security firm, a "
    "news outlet, the threat actor, the breached product or platform, an organisation that only "
    "commented, or one affected by a different incident is not a victim of this one. "
    "`incident.recorded_victims` are already recorded: a candidate that is one of them under "
    "another name (legal name, abbreviation, brand, or the same government) is NOT an additional "
    "victim - answer NO."
)
VICTIM_CRITERIA = {
    "true": ("The evidence shows the candidate organisation's data, systems or customers were "
             "affected by this incident (it was breached, or the supplier's breach exposed its data)."),
    "false": ("The candidate was not affected by this incident: it is the supplier itself, a "
              "regulator, responder, commentator, media outlet, product or threat actor, a group or "
              "sector rather than one organisation, another name for a recorded victim, or it was "
              "affected by a different incident, or the evidence does not show it was affected."),
}
PAIR_INSTRUCTIONS = (
    "`supplier_incident` is a stored cyber incident that involved a supplier, vendor or "
    "third-party service provider; `other_event` is a separate stored event recorded against "
    "another organisation, `gap_days` apart (`why_paired` says why the two were compared). Each "
    "event's `records` hold the stored pages and separate Perplexity research. Decide whether "
    "`other_event` is a report of the SAME incident - the same compromise of the same supplier "
    "or third party, reported about another of its customers, or about the supplier itself. One "
    "supplier breach reported about several customers is one incident."
)
PAIR_CRITERIA = {
    "true": ("Same incident: other_event describes the same supplier or third-party compromise "
             "(same supplier, same breach), whichever customer it is reported about."),
    "false": ("Different incidents: other_event is a separate attack on its own organisation, a "
              "different supplier's breach, a different breach of the same supplier at another "
              "time, or a roundup that merely mentions it."),
}


def _event_evidence(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    from cyber_data_collector.dedup.event_reality import build_state

    return build_state(conn, dedup_id, corroborated=True, max_records=RECORDS_PER_EVENT)


def build_victim_state(conn: sqlite3.Connection, incident: SupplierIncident,
                       cand: VictimCandidate) -> Dict[str, Any]:
    evidence = _event_evidence(conn, incident.dedup_id)
    state: Dict[str, Any] = {
        "incident": {**evidence["event"], "vendors": incident.vendors,
                     "recorded_victims": sorted(n for _, n in incident.victims),
                     "records": evidence["records"]},
        "candidate": {"name": cand.name, "kind": cand.kind or "unknown", "found_by": cand.found_by},
    }
    if "corroboration" in evidence:
        state["corroboration"] = evidence["corroboration"]
    found = stored_search(conn, incident.dedup_id)
    if found:
        state["supplier_search"] = {"answer": found["answer"], "citations": found["citations"]}
    return state


def build_pair_state(conn: sqlite3.Connection, incident: SupplierIncident,
                     pair: PairCandidate) -> Dict[str, Any]:
    a = _event_evidence(conn, pair.supplier_id)
    a["event"]["vendors"] = incident.vendors
    return {"supplier_incident": a, "other_event": _event_evidence(conn, pair.other_id),
            "gap_days": pair.gap_days, "why_paired": pair.reasons}


def victim_question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=VICTIM_INSTRUCTIONS, criteria=NoulCriteria(**VICTIM_CRITERIA))


def pair_question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=PAIR_INSTRUCTIONS, criteria=NoulCriteria(**PAIR_CRITERIA))


def victim_escalation_question() -> str:
    return (VICTIM_INSTRUCTIONS + f" YES means: {VICTIM_CRITERIA['true']} NO means: "
            f"{VICTIM_CRITERIA['false']} Was the candidate organisation affected by this incident?")


def pair_escalation_question() -> str:
    return (PAIR_INSTRUCTIONS + f" YES means: {PAIR_CRITERIA['true']} NO means: {PAIR_CRITERIA['false']}"
            " Is other_event the same incident as supplier_incident?")


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply."""
    from cyber_data_collector.utils.llm_models import openai_model
    from cyber_data_collector.dedup import calibration_guard, event_reality

    return {"question_version": f"{VICTIM_VERSION}+{VICTIM_ESCALATION_VERSION}|"
                                f"{PAIR_VERSION}+{PAIR_ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}",
            # The incident events are shown as event reality shows them (build_state, RECORDS_PER_EVENT records).
            "evidence": calibration_guard.fingerprint(
                "supplier-victims", records_per_event=RECORDS_PER_EVENT, escalate_band=list(ESCALATE_BAND),
                events=event_reality.evidence_fingerprint())}


def _steps():
    """The two judging steps as :mod:`final_decisions` stores them: each final score is kept against the one
    state Jev and the escalation were both shown, and read back only while the subject's evidence is that state."""
    from cyber_data_collector.dedup import final_decisions

    return {
        KIND_VICTIM: final_decisions.Step(
            name="supplier-victim", question_id=VICTIM_QUESTION_ID, version=VICTIM_VERSION,
            make_question=victim_question, escalation_id=VICTIM_ESCALATION_ID,
            escalation_version=VICTIM_ESCALATION_VERSION, escalation_text=victim_escalation_question,
            band=ESCALATE_BAND),
        KIND_PAIR: final_decisions.Step(
            name="supplier-same-incident", question_id=PAIR_QUESTION_ID, version=PAIR_VERSION,
            make_question=pair_question, escalation_id=PAIR_ESCALATION_ID,
            escalation_version=PAIR_ESCALATION_VERSION, escalation_text=pair_escalation_question,
            band=ESCALATE_BAND),
    }


@dataclass
class Scores:
    """Final score per subject: the escalation's inside the band, Jev's outside it."""

    final: Dict[str, float] = field(default_factory=dict)
    jev: Dict[str, float] = field(default_factory=dict)
    escalated: Dict[str, float] = field(default_factory=dict)
    undecided: List[str] = field(default_factory=list)   # in band, escalation failed or not run
    failed: List[str] = field(default_factory=list)      # Jev itself failed


def judge(conn: sqlite3.Connection, items: Sequence[Tuple[str, Dict[str, Any]]], kind: str,
          judge_client=None, escalation_client=None, escalate_band: bool = True) -> Scores:
    """Ask Jev about each (subject id, state); escalate the middle band. Stored answers are reused.

    Each final score is recorded in ``FinalDecisions`` against the state both answers were given, so a stored
    score is never an escalation of one evidence state paired with a Jev answer on another."""
    from cyber_data_collector.dedup import final_decisions
    from cyber_data_collector.utils.escalation import escalate
    from cyber_data_collector.utils.jev import JevJudge

    if kind == KIND_VICTIM:
        qid, version, q = VICTIM_QUESTION_ID, VICTIM_VERSION, victim_question()
        eqid, eversion, eq, subject_type = (VICTIM_ESCALATION_ID, VICTIM_ESCALATION_VERSION,
                                            victim_escalation_question(), "event_organisation")
    else:
        qid, version, q = PAIR_QUESTION_ID, PAIR_VERSION, pair_question()
        eqid, eversion, eq, subject_type = (PAIR_ESCALATION_ID, PAIR_ESCALATION_VERSION,
                                            pair_escalation_question(), "event_pair")
    step = _steps()[kind]
    final_decisions.ensure_schema(conn)
    scores = Scores()
    owned = judge_client is None
    jev = judge_client or JevJudge(conn)
    lo, hi = ESCALATE_BAND
    try:
        for i, (sid, state) in enumerate(items, 1):
            try:
                p = float(jev.ask(subject_type, sid, version, state, {qid: q})[qid].value)
            except Exception as exc:  # noqa: BLE001 - reported; an unjudged item is never acted on
                logger.warning("Jev failed for %s %s: %s", kind, sid, exc)
                scores.failed.append(sid)
                continue
            scores.jev[sid] = p
            if lo <= p < hi:
                value = (escalate(conn, subject_type, sid, eqid, eversion, state, eq, client=escalation_client)
                         if escalate_band else None)
                if value is None:
                    scores.undecided.append(sid)
                    continue
                scores.escalated[sid] = value
                p = value
            scores.final[sid] = p
            final_decisions.record(conn, step, sid, state, p, sid in scores.escalated)
            if i % 25 == 0:
                logger.info("Judged %d/%d %s item(s)", i, len(items), kind)
                conn.commit()
    finally:
        conn.commit()
        if owned:
            jev.close()
    return scores


def stored_final_probabilities(conn: sqlite3.Connection, kind: str,
                               items: Optional[Sequence[Tuple[str, Dict[str, Any]]]] = None) -> Dict[str, float]:
    """Final score per subject from the decision :func:`judge` recorded on its **current** evidence
    (:mod:`final_decisions`); a subject with none - never judged, escalation failed, or evidence changed since -
    has no score. ``items`` are ``(subject id, state)``; without them the current candidates are gathered.

    It used to pair the newest Jev answer with the newest escalation, each chosen alone, so a calibration could
    read an escalation of evidence the Jev answer was not given."""
    from cyber_data_collector.dedup import final_decisions

    if items is None:
        gathered = gather(conn)
        items = gathered.victim_items(conn) if kind == KIND_VICTIM else gathered.pair_items(conn)
    return final_decisions.read_current(conn, _steps()[kind], [(sid, lambda st=state: st) for sid, state in items])


def stored_probabilities(conn: sqlite3.Connection, kind: str) -> Tuple[Dict[str, float], Dict[str, float]]:
    """(Jev's newest score, final score on current evidence) per subject from storage. Jev's own score is for
    stratifying a labelling sample only; anything that decides reads the final score."""
    qid, version = (VICTIM_QUESTION_ID, VICTIM_VERSION) if kind == KIND_VICTIM else (PAIR_QUESTION_ID, PAIR_VERSION)

    def latest(question_id: str, ver: str, model: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for sid, value in conn.execute(
                "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
                "AND model_requested = ? ORDER BY rowid", (question_id, ver, model)):
            out[sid] = float(value)
        return out

    from cyber_data_collector.utils.jev import ensure_schema as jev_schema
    jev_schema(conn)
    jev = latest(qid, version, typesafe_model())
    return jev, stored_final_probabilities(conn, kind)


# ----------------------------------------------------------------------
# Labels and calibration
# ----------------------------------------------------------------------

class CalibrationError(RuntimeError):
    """Acting was requested and the labels or the calibration do not allow it."""


def read_labels(path: Path = LABELS_PATH) -> Dict[Tuple[str, str], str]:
    """(kind, subject id) -> label. Raises :class:`CalibrationError` if missing or unreadable."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        raise CalibrationError(f"labels file {path} cannot be read: {exc}") from exc
    allowed = LABELS
    out: Dict[Tuple[str, str], str] = {}
    for i, r in enumerate(rows, 2):
        kind, sid, label = (r.get("kind") or "").strip(), (r.get("subject_id") or "").strip(), \
            (r.get("label") or "").strip()
        if kind not in allowed or not sid or label not in allowed[kind]:
            raise CalibrationError(f"labels file {path} line {i} is malformed: kind={kind!r} "
                                   f"subject_id={sid!r} label={label!r}")
        out[(kind, sid)] = label
    if not out:
        raise CalibrationError(f"labels file {path} holds no labels")
    return out


def active_calibration(path: Path = CALIBRATION_PATH) -> Dict[str, Any]:
    """The calibration, only if measured on the current models and questions; else raise saying why."""
    path = Path(path)
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CalibrationError(f"no calibration at {path} ({exc}); run "
                               "scripts/calibrate_supplier_victims.py calibrate") from exc
    except ValueError as exc:
        raise CalibrationError(f"calibration {path} is not valid JSON: {exc}") from exc
    from cyber_data_collector.dedup import calibration_guard

    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        raise CalibrationError(f"calibration {path} does not apply to the current model, question and evidence "
                               f"processing ({reason}); recalibrate before applying")
    from cyber_data_collector.dedup import deployment

    # Each kind's cut-off the deployment rule holds back is None in the copy returned: that kind acts on
    # labels only, and a pair with no calibrated 'different' bound stays unresolved (held).
    return deployment.gate("supplier-victims", cal, path)


def accepted(scores: Dict[str, float], labels: Dict[Tuple[str, str], str], kind: str,
             cutoff: Optional[float]) -> Dict[str, str]:
    """subject id -> how it was accepted ('label' or 'model'), labels winning both ways."""
    out: Dict[str, str] = {}
    for (k, sid), label in labels.items():
        if k == kind and label == POSITIVE[kind]:
            out[sid] = "label"
    if cutoff is not None:
        for sid, p in scores.items():
            if p >= cutoff and sid not in out and labels.get((kind, sid)) not in NEGATIVE[kind]:
                out[sid] = "model"
    return out


# ----------------------------------------------------------------------
# Applying
# ----------------------------------------------------------------------

def _entity_for(conn: sqlite3.Connection, cand: VictimCandidate, index: _EntityIndex) -> int:
    if cand.entity_id is not None:
        return cand.entity_id
    from cyber_data_collector.dedup.entity_resolution import canonical_entity_name

    name = canonical_entity_name(conn, cand.name)
    hit = index.find(name)
    if hit:
        return hit[0]
    cur = conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')",
                       (name,))
    return int(cur.lastrowid)


def attach_victim(conn: sqlite3.Connection, dedup_id: str, entity_id: int, decided_by: str,
                  subject_id: Optional[str] = None, probability: Optional[float] = None,
                  cutoff: Optional[float] = None, question_version: Optional[str] = None,
                  merged_from: Optional[str] = None, snapshot: bool = True) -> bool:
    """Make ``entity_id`` a victim of ``dedup_id`` (event and member links), with an audit row.

    Returns False when it already was. Never demotes another victim and never
    rewrites the scalar ``victim_organization_name``: co-equal victims are
    recorded as roles.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    ensure_schema(conn)
    row = conn.execute("SELECT relationship_type FROM DeduplicatedEventEntities "
                       "WHERE deduplicated_event_id = ? AND entity_id = ?", (dedup_id, entity_id)).fetchone()
    previous = row[0] if row else None
    if previous == "victim":
        return False
    batch = None
    if snapshot:
        batch = f"supplier-victims-{datetime.now().strftime('%Y%m%d%H%M%S')}-{dedup_id[:8]}"
        DedupLedger(conn).snapshot_event(batch, dedup_id)
    members = [r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?", (dedup_id,))]
    if row:
        conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'victim' "
                     "WHERE deduplicated_event_id = ? AND entity_id = ?", (dedup_id, entity_id))
    else:
        linked = conn.execute(
            f"SELECT COUNT(*) FROM EnrichedEventEntities WHERE entity_id = ? AND enriched_event_id IN "
            f"({','.join('?' * len(members))})", (entity_id, *members)).fetchone()[0] if members else 0
        conn.execute("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, "
                     "relationship_type, confidence_score, source_count) VALUES (?, ?, 'victim', ?, ?)",
                     (dedup_id, entity_id, probability, max(linked, 1)))
    if members:
        conn.execute(f"UPDATE EnrichedEventEntities SET relationship_type = 'victim' WHERE entity_id = ? "
                     f"AND enriched_event_id IN ({','.join('?' * len(members))})", (entity_id, *members))
    name = conn.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = ?", (entity_id,)).fetchone()[0]
    conn.execute("INSERT INTO SupplierVictimAttachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (str(uuid.uuid4()), dedup_id, entity_id, name, previous, decided_by, subject_id, probability,
                  cutoff, question_version, merged_from, batch, datetime.now().isoformat()))
    conn.execute("UPDATE DeduplicatedEvents SET updated_at = CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
                 (dedup_id,))
    logger.info("Attached %r as a victim of %s (%s)", name, dedup_id, decided_by)
    return True


def variant_of_victim(conn: sqlite3.Connection, dedup_id: str, entity_id: Optional[int], name: str,
                      resolver: EntityResolver) -> Optional[str]:
    """The event's existing victim that ``name`` is another name for, if any.

    Attaching both "Australian Red Cross" and "Australian Red Cross Society"
    would count one organisation twice for one incident.
    """
    from cyber_data_collector.dedup.victim_counting import same_within_event

    for eid, existing in conn.execute(
            """SELECT v.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""", (dedup_id,)):
        if eid != entity_id and same_within_event(resolver, existing, name):
            return existing
    return None


def _components(edges: Iterable[Tuple[str, str]]) -> List[List[str]]:
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: Dict[str, List[str]] = defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def event_victims(conn: sqlite3.Connection, dedup_id: str) -> List[Tuple[int, str, Optional[str]]]:
    """``(entity_id, name, kind)`` of each organisation recorded as a victim of the event."""
    return conn.execute(
        """SELECT dee.entity_id, v.entity_name, v.entity_kind FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""",
        (dedup_id,)).fetchall()


def carry_victims(conn: sqlite3.Connection, target: str, source: str,
                  carried: Sequence[Tuple[int, str, Optional[str]]], vendors: Sequence[str],
                  resolver: EntityResolver, *, subject_id: Optional[str] = None,
                  probability: Optional[float] = None, cutoff: Optional[float] = None,
                  question_version: str = PAIR_VERSION) -> Dict[str, List[str]]:
    """Make each victim of a merged-away event a victim of the event it was merged into.

    A supplier incident has several co-equal victims, and merging two reports of it must not lose the customers
    only one of them named. ``carried`` is read with :func:`event_victims` *before* the merge. Not carried: the
    supplier itself, a description rather than an organisation ("Sydney radiologist"), and another name for a
    victim the target already has (it would count one organisation twice). Each attachment is audited in
    ``SupplierVictimAttachments`` with the event it came from.

    Returns:
        ``{"carried_victims": [...], "not_carried": ["name (why)", ...]}``.
    """
    entry: Dict[str, List[str]] = {"carried_victims": [], "not_carried": []}
    for entity_id, name, kind in carried:
        if any(same_org(resolver, name, v) or name == v for v in vendors):
            entry["not_carried"].append(f"{name} (the supplier)")
            continue
        if not is_candidate_organisation(name, kind):
            entry["not_carried"].append(f"{name} (not an organisation)")
            continue
        variant = variant_of_victim(conn, target, entity_id, name, resolver)
        if variant:
            entry["not_carried"].append(f"{name} (another name for {variant})")
            continue
        if attach_victim(conn, target, entity_id, "merge", subject_id=subject_id, probability=probability,
                         cutoff=cutoff, question_version=question_version, merged_from=source):
            entry["carried_victims"].append(name)
    return entry


def merge_pairs(conn: sqlite3.Connection, pairs: Sequence[PairCandidate], incidents: Dict[str, SupplierIncident],
                resolver: EntityResolver, different: Set[str], role_refresher=None,
                scores: Optional[Dict[str, float]] = None, cutoff: Optional[float] = None,
                how: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Fold each component of accepted pairs into its largest event, carrying victims across.

    Never across a labelled-different pair; a cached 'different' ruling makes
    ``merge_events`` refuse, and that refusal is reported.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    ledger = DedupLedger(conn, role_refresher=role_refresher)
    by_id = {p.pair_id: p for p in pairs}
    merged: List[Dict[str, Any]] = []
    for group in _components((by_id[pid].supplier_id, by_id[pid].other_id) for pid in by_id):
        sizes = {d: conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
                                 (d,)).fetchone()[0] for d in group}
        # The supplier incident with most records survives; ties go to the lowest id.
        target = sorted(group, key=lambda d: (d not in incidents, -sizes[d], d))[0]
        vendors = list(incidents[target].vendors) if target in incidents else []
        kept = [target]
        for source in sorted(d for d in group if d != target):
            if any("|".join(sorted((source, k))) in different for k in kept):
                continue
            carried = event_victims(conn, source)
            if source in incidents:
                vendors += [v for v in incidents[source].vendors if v not in vendors]
            try:
                ledger.merge_events(target, source, actor="rule",
                                    reason=f"Jev {PAIR_VERSION}: the same supplier incident recorded under "
                                           "another customer")
            except ValueError as exc:   # a cached 'different' ruling wins
                logger.info("Merge %s -> %s refused: %s", source, target, exc)
                merged.append({"target": target, "source": source, "refused": str(exc)})
                continue
            kept.append(source)
            pid = "|".join(sorted((source, target)))
            entry = {"target": target, "source": source,
                     **carry_victims(conn, target, source, carried, vendors, resolver, subject_id=pid,
                                     probability=(scores or {}).get(pid), cutoff=cutoff,
                                     question_version=PAIR_VERSION)}
            merged.append(entry)
    conn.commit()
    return merged


def nearby_own_events(cand: VictimCandidate, incident: SupplierIncident,
                      own: Dict[int, List[Tuple[str, Optional[str]]]]) -> List[str]:
    """The candidate's own active events near the incident (undated ones too), which could count it twice."""
    if cand.entity_id is None:
        return []
    out: List[str] = []
    for did, edate in own.get(cand.entity_id, []):
        if did == incident.dedup_id:
            continue
        gap = _gap(incident.event_date, edate)
        if gap is None or gap <= OWN_EVENT_WINDOW_DAYS:
            out.append(did)
    return out


def held_by_own_event(cand: VictimCandidate, incident: SupplierIncident,
                      own: Dict[int, List[Tuple[str, Optional[str]]]]) -> Optional[str]:
    """The first of the candidate's own active events near the incident, if any."""
    blockers = nearby_own_events(cand, incident, own)
    return blockers[0] if blockers else None


OWNER_DECISION = ("owner decision 2026-10-31: one event per customer; a supplier's customers are not "
                  "co-victims of one event")
# The member-link role given back when the attachment recorded no event-level role to restore.
_UNRECORDED_MEMBER_ROLE = "affected_customer"


def _own_victim(conn: sqlite3.Connection, did: str, entity_id: int, name: str) -> bool:
    """The organisation is the event's own victim, not a customer attached to it: the event's only victim
    role and exactly its scalar victim ("Medical information leaked in NSW Health hack" - NSW Health)."""
    scalar = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                          (did,)).fetchone()
    if not (scalar and scalar[0] and scalar[0].strip().lower() == (name or "").strip().lower()):
        return False
    others = conn.execute("SELECT COUNT(*) FROM DeduplicatedEventEntities WHERE deduplicated_event_id = ? "
                          "AND relationship_type = 'victim' AND entity_id != ?", (did, entity_id)).fetchone()[0]
    return others == 0


def restore_own_victims(conn: sqlite3.Connection, apply: bool = True) -> List[Dict[str, Any]]:
    """Give back the victim role a reversal took from the event's own victim.

    The 2026-10-31 conversion reversed every attachment, including those naming the organisation the event
    itself is about, and left events such as "Western Sydney University cyber incident" with no victim. A
    reversal is restored when its Active event now has no victim role and its scalar victim is exactly the
    organisation (the event-level role, and member links the reversal changed); snapshotted through the
    ledger, and the reversal row is marked ``restored`` so it is never reversed again.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    ensure_schema(conn)
    rows = conn.execute(
        """SELECT r.attachment_id, r.deduplicated_event_id, r.entity_id, r.entity_name, r.restored_role
           FROM SupplierVictimReversals r
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = r.deduplicated_event_id
           WHERE r.action = 'reversed' AND d.status = 'Active'
             AND LOWER(TRIM(d.victim_organization_name)) = LOWER(TRIM(r.entity_name))
             AND NOT EXISTS (SELECT 1 FROM DeduplicatedEventEntities x
                             WHERE x.deduplicated_event_id = r.deduplicated_event_id AND x.relationship_type = 'victim')
           ORDER BY r.created_at, r.attachment_id""").fetchall()
    out: List[Dict[str, Any]] = []
    done: Set[Tuple[str, int]] = set()
    for attachment_id, did, entity_id, name, previous in rows:
        if (did, entity_id) in done:
            continue
        done.add((did, entity_id))
        out.append({"attachment_id": attachment_id, "deduplicated_event_id": did, "victim": name})
        if not apply:
            continue
        DedupLedger(conn).snapshot_event(f"supplier-victims-restore-{datetime.now().strftime('%Y%m%d%H%M%S')}-"
                                         f"{did[:8]}", did)
        if not conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'victim' "
                            "WHERE deduplicated_event_id = ? AND entity_id = ?", (did, entity_id)).rowcount:
            conn.execute("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, relationship_type) "
                         "VALUES (?, ?, 'victim')", (did, entity_id))
        members = [r[0] for r in conn.execute(
            "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?", (did,))]
        if members:
            conn.execute(f"UPDATE EnrichedEventEntities SET relationship_type = 'victim' WHERE entity_id = ? "
                         f"AND relationship_type = ? AND enriched_event_id IN ({','.join('?' * len(members))})",
                         (entity_id, previous or _UNRECORDED_MEMBER_ROLE, *members))
        conn.execute("UPDATE SupplierVictimReversals SET action = 'restored' WHERE deduplicated_event_id = ? "
                     "AND entity_id = ? AND action = 'reversed'", (did, entity_id))
        logger.info("Restored %r as the victim of %s: the event's own victim, not an attached customer", name, did)
    # A restoration holds only while its basis does: once the event's victim is another organisation (ASIC's
    # Accellion event, whose victim had been rewritten to NSW Health, got its own back when NSW Health's records
    # left), the restored role is reversed again.
    for attachment_id, did, entity_id, name, previous in conn.execute(
            """SELECT r.attachment_id, r.deduplicated_event_id, r.entity_id, r.entity_name, r.restored_role
               FROM SupplierVictimReversals r
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = r.deduplicated_event_id
               WHERE r.action = 'restored' AND d.status = 'Active'
                 AND LOWER(TRIM(COALESCE(d.victim_organization_name, ''))) != LOWER(TRIM(r.entity_name))
               ORDER BY r.created_at, r.attachment_id""").fetchall():
        if (did, entity_id) in done:
            continue
        done.add((did, entity_id))
        out.append({"attachment_id": attachment_id, "deduplicated_event_id": did, "victim": name,
                    "withdrawn": True})
        if not apply:
            continue
        DedupLedger(conn).snapshot_event(f"supplier-victims-unrestore-{datetime.now().strftime('%Y%m%d%H%M%S')}-"
                                         f"{did[:8]}", did)
        if previous is None:
            conn.execute("DELETE FROM DeduplicatedEventEntities WHERE deduplicated_event_id = ? AND entity_id = ? "
                         "AND relationship_type = 'victim'", (did, entity_id))
        else:
            conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = ? WHERE deduplicated_event_id = ? "
                         "AND entity_id = ? AND relationship_type = 'victim'", (previous, did, entity_id))
        conn.execute("UPDATE SupplierVictimReversals SET action = 'reversed' WHERE deduplicated_event_id = ? "
                     "AND entity_id = ? AND action = 'restored'", (did, entity_id))
        logger.info("Withdrew the restored victim %r of %s: the event's victim is now another organisation",
                    name, did)
    if apply and out:
        conn.commit()
    return out


def reverse_attachments(conn: sqlite3.Connection, apply: bool = True) -> List[Dict[str, Any]]:
    """Undo every victim role this module attached that is still on its Active event.

    Except the event's own victim (:func:`_own_victim`): one event per customer means the event keeps the
    customer it is about; only the customers attached to another's event are reversed.

    The event-level role goes back to the attachment's ``previous_role`` (the link is removed when
    there was none). Member links the attachment set to ``victim`` go back to ``previous_role`` or,
    when none was recorded, ``affected_customer`` - except for a victim carried across a merge
    (``decided_by`` ``merge``): those records are the customer's own and keep their links. Each
    reversal is snapshotted through the ledger and recorded in ``SupplierVictimReversals``
    (``action`` ``reversed``); a reversed attachment is never reversed again. ``apply`` False only
    lists them.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    ensure_schema(conn)
    own = victim_events(conn)
    rows = conn.execute(
        """SELECT a.attachment_id, a.deduplicated_event_id, a.entity_id, a.entity_name, a.previous_role,
                  a.decided_by
           FROM SupplierVictimAttachments a
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = a.deduplicated_event_id
           JOIN DeduplicatedEventEntities dee ON dee.deduplicated_event_id = a.deduplicated_event_id
                AND dee.entity_id = a.entity_id AND dee.relationship_type = 'victim'
           WHERE d.status = 'Active'
             AND a.attachment_id NOT IN (SELECT attachment_id FROM SupplierVictimReversals)
           ORDER BY a.created_at, a.attachment_id""").fetchall()
    out: List[Dict[str, Any]] = []
    done: Set[Tuple[str, int]] = set()
    for attachment_id, did, entity_id, name, previous, decided_by in rows:
        if (did, entity_id) in done:
            continue
        done.add((did, entity_id))
        if _own_victim(conn, did, entity_id, name):
            continue
        others = sorted(e for e, _ in own.get(entity_id, []) if e != did)
        out.append({"attachment_id": attachment_id, "deduplicated_event_id": did, "victim": name,
                    "decided_by": decided_by, "restored_role": previous, "own_events": others,
                    "why": OWNER_DECISION})
        if not apply:
            continue
        batch = f"supplier-victims-reverse-{datetime.now().strftime('%Y%m%d%H%M%S')}-{did[:8]}"
        DedupLedger(conn).snapshot_event(batch, did)
        if previous is None:
            conn.execute("DELETE FROM DeduplicatedEventEntities WHERE deduplicated_event_id = ? AND entity_id = ?",
                         (did, entity_id))
        else:
            conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = ? "
                         "WHERE deduplicated_event_id = ? AND entity_id = ?", (previous, did, entity_id))
        members = [r[0] for r in conn.execute(
            "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?", (did,))]
        if members and decided_by != "merge":
            conn.execute(f"UPDATE EnrichedEventEntities SET relationship_type = ? WHERE entity_id = ? "
                         f"AND relationship_type = 'victim' AND enriched_event_id IN ({','.join('?' * len(members))})",
                         (previous or _UNRECORDED_MEMBER_ROLE, entity_id, *members))
        # Every attachment row of this (event, organisation) is undone by this one reversal.
        for (aid,) in conn.execute(
                """SELECT attachment_id FROM SupplierVictimAttachments WHERE deduplicated_event_id = ?
                   AND entity_id = ? AND attachment_id NOT IN (SELECT attachment_id FROM SupplierVictimReversals)""",
                (did, entity_id)).fetchall():
            conn.execute("INSERT INTO SupplierVictimReversals VALUES (?, ?, ?, ?, 'reversed', ?, ?, ?, ?, ?)",
                         (aid, did, entity_id, name, previous, json.dumps(others), OWNER_DECISION, batch,
                          datetime.now().isoformat()))
        conn.execute("UPDATE DeduplicatedEvents SET updated_at = CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
                     (did,))
        logger.info("Reversed %r as a victim of %s: %s", name, did, OWNER_DECISION)
    if apply and out:
        conn.commit()
    return out


def multi_customer_events(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Active events that hold, or held, the victims of more than one customer through this module.

    An event qualifies when it has a ``SupplierVictimAttachments`` row, was the target of a merge this
    module made (``DedupDecisions`` whose reasoning names :data:`PAIR_VERSION`), or was snapshotted under a
    ``supplier-victims-*`` batch, and the distinct organisations it records as victims now or had
    attached number more than one - or it absorbed another event through such a merge.
    """
    ensure_schema(conn)
    has = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    touched: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"attached": set(), "merged_from": set(),
                                                               "batches": set()})
    for did, name, merged_from in conn.execute(
            "SELECT deduplicated_event_id, entity_name, merged_from FROM SupplierVictimAttachments"):
        touched[did]["attached"].add(name)
        if merged_from:
            touched[did]["merged_from"].add(merged_from)
    if "DedupDecisions" in has:
        for did, batch in conn.execute(
                "SELECT DISTINCT deduplicated_event_id, batch_id FROM DedupDecisions WHERE action = 'merge' "
                "AND reasoning LIKE ?", (f"%{PAIR_VERSION}%",)):
            touched[did]["batches"].add(batch)
    if "DedupSnapshots" in has:
        for did, batch in conn.execute(
                "SELECT DISTINCT deduplicated_event_id, batch_id FROM DedupSnapshots "
                "WHERE batch_id LIKE 'supplier-victims-%'"):
            touched[did]["batches"].add(batch)
    active = {r[0]: r[1] for r in conn.execute(
        "SELECT deduplicated_event_id, title FROM DeduplicatedEvents WHERE status = 'Active'")}
    out = []
    for did in sorted(touched):
        if did not in active:
            continue
        victims = sorted(r[0] for r in conn.execute(
            """SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v USING (entity_id)
               WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""", (did,)))
        t = touched[did]
        customers = set(victims) | t["attached"]
        if len(customers) > 1 or t["merged_from"]:
            out.append({"deduplicated_event_id": did, "title": active[did], "victims_now": victims,
                        "attached_customers": sorted(t["attached"]), "merged_from": sorted(t["merged_from"]),
                        "supplier_batches": sorted(t["batches"])})
    return out


RULED_SAME, RULED_DIFFERENT, UNRESOLVED = "same", "different", "unresolved"
CONFLICT = "conflicting rulings: "


@dataclass
class OwnEventRuling:
    """Whether a candidate victim's own event is the supplier incident, and on what ground."""

    own_event: str
    verdict: str                          # RULED_SAME / RULED_DIFFERENT / UNRESOLVED
    basis: str                            # label / override / model / (for UNRESOLVED) the reason
    probability: Optional[float] = None   # the calibrated judgement, when that was the ground

    def as_dict(self) -> Dict[str, Any]:
        return {"own_event": self.own_event, "verdict": self.verdict, "basis": self.basis,
                "p_same": self.probability}


def override_verdicts(conn: sqlite3.Connection, left_dedup_id: str, right_dedup_id: str) -> Set[str]:
    """The verdicts ('same' / 'different') of active ``DedupOverrides`` between any member of each event.

    Checked across all members, like :meth:`DedupLedger.override_conflict`. An unreadable table
    raises: it must not read as "no ruling".
    """
    members = "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?"
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT o.verdict FROM DedupOverrides o WHERE o.active = 1 AND (
                  (o.left_enriched_event_id IN ({members}) AND o.right_enriched_event_id IN ({members}))
                  OR (o.left_enriched_event_id IN ({members}) AND o.right_enriched_event_id IN ({members})))""",
            (left_dedup_id, right_dedup_id, right_dedup_id, left_dedup_id)).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read DedupOverrides to rule on a held victim: {exc}") from exc
    return {r[0] for r in rows}


def rule_on_own_event(conn: sqlite3.Connection, incident_id: str, own_id: str,
                      labels: Dict[Tuple[str, str], str], pair_scores: Optional[Scores],
                      cutoff: Optional[float], known_pairs: Iterable[str] = (),
                      different_cutoff: Optional[float] = None) -> OwnEventRuling:
    """Has the candidate's own event been ruled the same incident as the supplier incident, or a different one?

    In order of authority: a pair label or an active ``DedupOverrides`` ruling (ground truth;
    two that disagree leave it unresolved, never a pick), then the calibrated same-incident
    judgement: at or above the merge ``cutoff`` the same incident, below ``different_cutoff``
    a different one. The merge cut-off was calibrated for the precision of merging only, so a
    score just under it is not evidence of "different": the band between the two bounds, and
    every score when there is no ``different_cutoff``, is UNRESOLVED. So is anything else (no
    judgement, a failed or un-escalated one, no cut-off), with the reason.
    """
    pid = "|".join(sorted((incident_id, own_id)))
    signals: List[Tuple[str, str]] = []
    label = labels.get((KIND_PAIR, pid))
    if label in (POSITIVE[KIND_PAIR], *NEGATIVE[KIND_PAIR]):
        signals.append(("label", RULED_SAME if label == POSITIVE[KIND_PAIR] else RULED_DIFFERENT))
    signals += [("override", RULED_SAME if v == "same" else RULED_DIFFERENT)
                for v in sorted(override_verdicts(conn, incident_id, own_id))]
    if len({v for _, v in signals}) > 1:
        return OwnEventRuling(own_id, UNRESOLVED,
                              CONFLICT + ", ".join(f"{k} says {v}" for k, v in signals))
    if signals:
        return OwnEventRuling(own_id, signals[0][1], signals[0][0])
    scores = pair_scores
    if scores is not None and pid in scores.final:
        p = scores.final[pid]
        if cutoff is not None and p >= cutoff:
            return OwnEventRuling(own_id, RULED_SAME, "model", p)
        if different_cutoff is not None and p < different_cutoff:
            return OwnEventRuling(own_id, RULED_DIFFERENT, "model", p)
        if cutoff is None:
            why = "no calibrated same-incident cut-off to read the judgement against"
        elif different_cutoff is None:
            why = (f"score {p:.2f} is below the merge cut-off {cutoff:.2f}, and no calibrated bound for "
                   "'different' exists (recalibrate: scripts/calibrate_supplier_victims.py calibrate)")
        else:
            why = (f"score {p:.2f} is in the band between the calibrated 'different' bound "
                   f"({different_cutoff:.2f}) and the merge cut-off ({cutoff:.2f}): neither is calibrated")
        return OwnEventRuling(own_id, UNRESOLVED, why, p)
    if scores is not None and pid in scores.failed:
        reason = "Jev failed on the pair"
    elif scores is not None and pid in scores.undecided:
        reason = "the pair's score is in the escalation band and has no escalated answer"
    elif pid not in set(known_pairs):
        reason = ("the pair was never a candidate (undated, over 365 days apart, or sharing a victim with the "
                  "incident), so nothing judged it; check-duplicates or a human ruling decides")
    else:
        reason = "the pair has not been judged"
    return OwnEventRuling(own_id, UNRESOLVED, reason)


# ----------------------------------------------------------------------
# The command
# ----------------------------------------------------------------------

@dataclass
class Gathered:
    incidents: List[SupplierIncident]
    candidates: Dict[str, List[VictimCandidate]]
    pairs: List[PairCandidate]
    index: Any
    resolver: EntityResolver

    @property
    def by_id(self) -> Dict[str, SupplierIncident]:
        return {i.dedup_id: i for i in self.incidents}

    def victim_items(self, conn: sqlite3.Connection) -> List[Tuple[str, Dict[str, Any]]]:
        by_id = self.by_id
        return [(c.subject_id, build_victim_state(conn, by_id[c.dedup_id], c))
                for cs in self.candidates.values() for c in cs]

    def pair_items(self, conn: sqlite3.Connection) -> List[Tuple[str, Dict[str, Any]]]:
        by_id = self.by_id
        return [(p.pair_id, build_pair_state(conn, by_id[p.supplier_id], p)) for p in self.pairs]


def gather(conn: sqlite3.Connection, limit: Optional[int] = None) -> Gathered:
    """Supplier incidents and their current candidates, from stored evidence (and stored searches)."""
    resolver = with_acronym_policy(conn, EntityResolver(conn))
    resolver.fit(r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2"))
    incidents = supplier_incidents(conn, resolver)
    if limit:
        incidents = incidents[:limit]
    index = _EntityIndex(conn, resolver)
    cands = {inc.dedup_id: victim_candidates(conn, inc, resolver, index, stored_search(conn, inc.dedup_id))
             for inc in incidents}
    return Gathered(incidents, cands, pair_candidates(conn, incidents, cands, resolver), index, resolver)


def find_supplier_victims(conn: sqlite3.Connection, apply: bool = False, research: bool = False,
                          judge_client=None, escalation_client=None, search=None,
                          labels_path: Path = LABELS_PATH, calibration_path: Path = CALIBRATION_PATH,
                          role_refresher=None, limit: Optional[int] = None, split: bool = False,
                          profile_search=None) -> Dict[str, Any]:
    """Detect supplier incidents, judge their customers and other events, and report or write the links.

    One event per customer (owner decision 2026-10-31): nothing here attaches a co-victim or merges
    events of different customers. With ``apply``, raises :class:`CalibrationError` before changing
    anything when the labels are missing or unreadable or the calibration does not match the current
    models and questions; then converts the old state (attachments reversed into links, events joined
    across customers split per customer) and writes the links (:mod:`supplier_links`). Without it, the
    same plan is reported and nothing is written. ``role_refresher`` is kept for callers.
    """
    from cyber_data_collector.dedup import supplier_links as sl

    ensure_schema(conn)
    sl.ensure_schema(conn)
    cal: Dict[str, Any] = {}
    labels: Dict[Tuple[str, str], str] = {}
    if apply:
        labels = read_labels(labels_path)
        cal = active_calibration(calibration_path)
    else:
        try:
            labels = read_labels(labels_path)
        except CalibrationError:
            labels = {}

    search_failures: List[Dict[str, str]] = []
    if research:
        resolver = with_acronym_policy(conn, EntityResolver(conn))
        resolver.fit(r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2"))
        found = supplier_incidents(conn, resolver)
        for inc in (found[:limit] if limit else found):
            if not inc.searchable:
                continue
            try:
                research_incident(conn, inc, search=search)
            except SearchUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - reported and turned into a non-zero exit
                logger.warning("Supplier search failed for %s: %s", inc.dedup_id, exc)
                search_failures.append({"deduplicated_event_id": inc.dedup_id, "error": str(exc)})

    # The old state is converted first (its splits create the customers' events), then everything is judged.
    before = gather(conn, limit)
    repairs = sl.repair_registry(conn, before.resolver, labels)
    registry = sl.Registry(conn, before.resolver)
    conversion = sl.convert(conn, before, registry, apply, split=split)
    g = gather(conn, limit) if apply else before
    victim_items = g.victim_items(conn)
    pair_items = g.pair_items(conn)
    vscores = judge(conn, victim_items, KIND_VICTIM, judge_client, escalation_client)
    pscores = judge(conn, pair_items, KIND_PAIR, judge_client, escalation_client)
    vcut = (cal.get("victim") or {}).get("cutoff") if apply else None
    pcut = (cal.get("same_incident") or {}).get("cutoff") if apply else None
    if not apply:
        try:
            current = active_calibration(calibration_path)
            vcut = (current.get("victim") or {}).get("cutoff")
            pcut = (current.get("same_incident") or {}).get("cutoff")
        except CalibrationError:
            pass
    planned = sl.plan_links(conn, g, labels, vscores, pscores, vcut, pcut, registry)

    result: Dict[str, Any] = {
        "supplier_incidents": len(g.incidents), "victim_candidates": len(victim_items),
        "pair_candidates": len(pair_items),
        "judged": {"victim": len(vscores.final), "same_incident": len(pscores.final)},
        "escalated": {"victim": len(vscores.escalated), "same_incident": len(pscores.escalated)},
        "undecided": {"victim": vscores.undecided, "same_incident": pscores.undecided},
        "failed": {"victim": vscores.failed, "same_incident": pscores.failed},
        "search_failures": search_failures, "applied": False, "calibrated": bool(cal) if apply else None,
        "cutoffs": {"victim": vcut, "same_incident": pcut},
        "incidents": [], "merged": [], "attached": [], "held": [], "variants": [],
        "reversed": conversion["attachments_reversed"], "splits": conversion["splits"],
        "attachments_converted": conversion["attachments_converted"],
        "ambiguous": conversion["ambiguous"] + planned["ambiguous"],
        "links": [vars(link) for link in registry.links.values()],
        "supplier_incident_rows": len(registry.incidents), "written": {},
        "multi_customer_events": [], "registry_repairs": repairs, "customer_jurisdictions": {},
    }
    for inc in g.incidents:
        result["incidents"].append({
            "deduplicated_event_id": inc.dedup_id, "title": inc.title, "event_date": inc.event_date,
            "vendors": inc.vendors, "victims": [n for _, n in inc.victims], "reasons": inc.reasons,
            "candidates": [{"name": c.name, "entity_id": c.entity_id, "found_by": c.found_by,
                            "p_affected": vscores.final.get(c.subject_id), "p_jev": vscores.jev.get(c.subject_id)}
                           for c in g.candidates[inc.dedup_id]],
            "pairs": [{"other": p.other_id, "gap_days": p.gap_days, "reasons": p.reasons,
                       "p_same": pscores.final.get(p.pair_id), "p_jev": pscores.jev.get(p.pair_id)}
                      for p in g.pairs if p.supplier_id == inc.dedup_id],
        })
    if not apply:
        conn.rollback()     # planning may have created entity rows for named customers; keep nothing
        return result
    result["written"] = registry.write()
    result["applied"] = True
    result["multi_customer_events"] = multi_customer_events(conn)
    conn.commit()
    if research:
        # A customer only named in the reporting is reported as Australian only by its researched jurisdiction
        # (multi_customer_incidents): look up those with none. A failed lookup stays unplaced and is retried.
        from cyber_data_collector.dedup import entity_profiles, multi_customer_incidents

        looked = failed = 0
        for entity_id, name in multi_customer_incidents.unplaced_customers(conn):
            looked += 1
            if entity_profiles.research_profile(conn, entity_id, name, search=profile_search) is None:
                failed += 1
        result["customer_jurisdictions"] = {"looked_up": looked, "failed": failed,
                                            "unplaced": len(multi_customer_incidents.unplaced_customers(conn))}
    return result
