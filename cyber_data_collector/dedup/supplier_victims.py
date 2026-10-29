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

Acting needs a **calibration** measured on labelled examples of this database
(``scripts/calibrate_supplier_victims.py``; labels in
``evaluation/supplier_victim_labels.csv``), for the current question versions
and models. Labels are ground truth: a labelled ``not_affected`` /
``different`` item is never acted on, a labelled ``affected`` / ``same`` one
always is. With a calibration, :func:`find_supplier_victims` with ``apply``:

* merges each accepted event candidate into the supplier incident through
  :meth:`DedupLedger.merge_events` (which refuses across a cached
  ``different`` ruling), carrying the merged event's victims across - except
  one that is the supplier itself;
* attaches each accepted victim candidate as a ``victim`` role. An organisation
  with its own separate event near the incident's date is decided by whether
  that event has been *ruled on* (:func:`rule_on_own_event`): ruled the **same**
  incident (a label, a ``same`` override, or a calibrated same-incident
  judgement at its cut-off) it is merged into the supplier incident through the
  ledger, which carries the customer across; ruled a **different** incident (a
  label, a ``different`` override, or a calibrated judgement below the cut-off)
  the customer is attached as a victim of the supplier incident as well; only
  an **unresolved** pair is held, with the reason it is unresolved. The model
  rules "different" only below its own calibrated ``different_cutoff``; a score
  between that and the merge cut-off is held, because the merge cut-off measured
  only the precision of merging.

Every attachment is recorded in ``SupplierVictimAttachments`` (who, which
judgement, probability, cut-off, question version) after a ledger snapshot.
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
    """Ask Jev about each (subject id, state); escalate the middle band. Stored answers are reused."""
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
            if i % 25 == 0:
                logger.info("Judged %d/%d %s item(s)", i, len(items), kind)
    finally:
        if owned:
            jev.close()
    return scores


def stored_final_probabilities(conn: sqlite3.Connection, kind: str) -> Dict[str, float]:
    """Final score per subject from storage, as :func:`judge` computes it; in-band items with no
    stored escalation are left out (undecided)."""
    return stored_probabilities(conn, kind)[1]


def stored_probabilities(conn: sqlite3.Connection, kind: str) -> Tuple[Dict[str, float], Dict[str, float]]:
    """(Jev's score, final score) per subject from storage."""
    from cyber_data_collector.utils.llm_models import openai_model

    qid, version, eqid, eversion = ((VICTIM_QUESTION_ID, VICTIM_VERSION, VICTIM_ESCALATION_ID,
                                     VICTIM_ESCALATION_VERSION) if kind == KIND_VICTIM else
                                    (PAIR_QUESTION_ID, PAIR_VERSION, PAIR_ESCALATION_ID, PAIR_ESCALATION_VERSION))

    def latest(question_id: str, ver: str, model: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for sid, value in conn.execute(
                "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
                "AND model_requested = ? ORDER BY created_at, rowid", (question_id, ver, model)):
            out[sid] = float(value)
        return out

    from cyber_data_collector.utils.jev import ensure_schema as jev_schema
    jev_schema(conn)
    jev = latest(qid, version, typesafe_model())
    escalated = latest(eqid, eversion, openai_model())
    lo, hi = ESCALATE_BAND
    out: Dict[str, float] = {}
    for sid, p in jev.items():
        if lo <= p < hi:
            if sid in escalated:
                out[sid] = escalated[sid]
        else:
            out[sid] = p
    return jev, out


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
    return cal


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
            carried = conn.execute(
                """SELECT dee.entity_id, v.entity_name, v.entity_kind FROM DeduplicatedEventEntities dee
                   JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
                   WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""",
                (source,)).fetchall()
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
            entry = {"target": target, "source": source, "carried_victims": [], "not_carried": []}
            for entity_id, name, kind in carried:
                # Not the supplier itself, not a description ("Sydney radiologist"),
                # and not another name for a victim the incident already has.
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
                if attach_victim(conn, target, entity_id, "merge", subject_id=pid,
                                 probability=(scores or {}).get(pid), cutoff=cutoff,
                                 question_version=PAIR_VERSION, merged_from=source):
                    entry["carried_victims"].append(name)
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
                          role_refresher=None, limit: Optional[int] = None) -> Dict[str, Any]:
    """Detect supplier incidents, gather and judge candidates, and (with ``apply``) act.

    With ``apply``, raises :class:`CalibrationError` before changing anything
    when the labels are missing or unreadable or the calibration is missing or
    was measured on other models or questions.
    """
    ensure_schema(conn)
    cal = labels = None
    if apply:
        labels = read_labels(labels_path)
        cal = active_calibration(calibration_path)

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

    g = gather(conn, limit)
    incidents, cands, pairs, index, resolver = g.incidents, g.candidates, g.pairs, g.index, g.resolver
    by_id = g.by_id
    victim_items = g.victim_items(conn)
    pair_items = g.pair_items(conn)
    vscores = judge(conn, victim_items, KIND_VICTIM, judge_client, escalation_client)
    pscores = judge(conn, pair_items, KIND_PAIR, judge_client, escalation_client)

    result: Dict[str, Any] = {
        "supplier_incidents": len(incidents), "victim_candidates": len(victim_items),
        "pair_candidates": len(pair_items),
        "judged": {"victim": len(vscores.final), "same_incident": len(pscores.final)},
        "escalated": {"victim": len(vscores.escalated), "same_incident": len(pscores.escalated)},
        "undecided": {"victim": vscores.undecided, "same_incident": pscores.undecided},
        "failed": {"victim": vscores.failed, "same_incident": pscores.failed},
        "search_failures": search_failures, "applied": False, "calibrated": cal is not None,
        "incidents": [], "merged": [], "attached": [], "held": [], "variants": [],
    }
    flat = {c.subject_id: c for cs in cands.values() for c in cs}
    for inc in incidents:
        result["incidents"].append({
            "deduplicated_event_id": inc.dedup_id, "title": inc.title, "event_date": inc.event_date,
            "vendors": inc.vendors, "victims": [n for _, n in inc.victims], "reasons": inc.reasons,
            "candidates": [{"name": c.name, "entity_id": c.entity_id, "found_by": c.found_by,
                            "p_affected": vscores.final.get(c.subject_id), "p_jev": vscores.jev.get(c.subject_id)}
                           for c in cands[inc.dedup_id]],
            "pairs": [{"other": p.other_id, "gap_days": p.gap_days, "reasons": p.reasons,
                       "p_same": pscores.final.get(p.pair_id), "p_jev": pscores.jev.get(p.pair_id)}
                      for p in pairs if p.supplier_id == inc.dedup_id],
        })
    if not apply:
        return result

    vcut = (cal.get("victim") or {}).get("cutoff")
    pcut = (cal.get("same_incident") or {}).get("cutoff")
    dcut = (cal.get("same_incident") or {}).get("different_cutoff")
    result.update({"applied": True, "cutoffs": {"victim": vcut, "same_incident": pcut,
                                                          "different": dcut}})
    known_pairs = {p.pair_id for p in pairs}
    pair_ok = {sid: how for sid, how in accepted(pscores.final, labels, KIND_PAIR, pcut).items()
               if sid in known_pairs}
    different = {sid for (k, sid), lab in labels.items() if k == KIND_PAIR and lab in NEGATIVE[KIND_PAIR]}

    # A victim with its own event near the incident is decided by whether that pair has been ruled on.
    victim_ok = accepted(vscores.final, labels, KIND_VICTIM, vcut)
    rulings: Dict[Tuple[str, str], OwnEventRuling] = {}
    to_merge = [p for p in pairs if p.pair_id in pair_ok]
    queued = {p.pair_id for p in to_merge}
    own_now = victim_events(conn)
    for sid in sorted(victim_ok):
        cand = flat.get(sid)
        if cand is None:
            continue
        inc = by_id[cand.dedup_id]
        for own_id in nearby_own_events(cand, inc, own_now):
            ruling = rulings[(sid, own_id)] = rule_on_own_event(conn, inc.dedup_id, own_id, labels, pscores,
                                                                pcut, known_pairs, dcut)
            pid = "|".join(sorted((inc.dedup_id, own_id)))
            if ruling.verdict == RULED_SAME and pid not in queued:
                # Ruled the same by a label or override (a model ruling is already in pair_ok).
                queued.add(pid)
                to_merge.append(PairCandidate(inc.dedup_id, own_id,
                                              [f"own event of candidate victim {cand.name}, ruled the same "
                                               f"incident ({ruling.basis})"],
                                              _gap(inc.event_date, dict(own_now[cand.entity_id]).get(own_id))))
    # A pair whose label and override disagree is held for a human, not offered to the ledger.
    conflicted = {"|".join(sorted((flat[sid].dedup_id, own_id)))
                  for (sid, own_id), r in rulings.items() if r.basis.startswith(CONFLICT)}
    to_merge = [p for p in to_merge if p.pair_id not in conflicted]
    result["merged"] = merge_pairs(conn, to_merge, by_id, resolver, different, role_refresher, pscores.final, pcut)
    if role_refresher is not None:
        role_refresher.flush(conn)   # merges reclassify roles before victims are attached
        conn.commit()
    gone = {m["source"] for m in result["merged"] if "refused" not in m}
    refused = {frozenset((m["target"], m["source"])): m["refused"] for m in result["merged"] if "refused" in m}

    own = victim_events(conn)   # after the merges
    for sid, how in sorted(victim_ok.items()):
        cand = flat.get(sid)
        if cand is None or cand.dedup_id in gone:
            continue
        inc = by_id[cand.dedup_id]
        ruled_different: List[OwnEventRuling] = []
        unresolved: List[OwnEventRuling] = []
        for own_id in nearby_own_events(cand, inc, own):
            ruling = rulings.get((sid, own_id)) or rule_on_own_event(conn, inc.dedup_id, own_id, labels, pscores,
                                                                     pcut, known_pairs, dcut)
            if ruling.verdict == RULED_DIFFERENT:
                ruled_different.append(ruling)
            elif ruling.verdict == RULED_SAME:
                # Ruled the same incident, but the event is still there: the merge was refused or did not run.
                why = refused.get(frozenset((inc.dedup_id, own_id)), "the merge did not run")
                unresolved.append(OwnEventRuling(own_id, UNRESOLVED,
                                                 f"ruled the same incident ({ruling.basis}) but not merged: {why}",
                                                 ruling.probability))
            else:
                unresolved.append(ruling)
        if unresolved:
            result["held"].append({"deduplicated_event_id": inc.dedup_id, "victim": cand.name,
                                   "own_event": unresolved[0].own_event, "p_affected": vscores.final.get(sid),
                                   "own_event_rulings": [r.as_dict() for r in unresolved],
                                   "why": "the organisation has its own event near this incident and the pair "
                                          "is unresolved: " + "; ".join(f"{r.own_event[:8]}: {r.basis}"
                                                                        for r in unresolved)})
            continue
        variant = variant_of_victim(conn, inc.dedup_id, cand.entity_id, cand.name, resolver)
        if variant:
            result["variants"].append({"deduplicated_event_id": inc.dedup_id, "candidate": cand.name,
                                       "victim": variant, "p_affected": vscores.final.get(sid)})
            continue
        entity_id = _entity_for(conn, cand, index)
        decided ="label" if how == "label" else ("escalation" if sid in vscores.escalated else "jev")
        if attach_victim(conn, inc.dedup_id, entity_id, decided, subject_id=sid,
                         probability=vscores.final.get(sid), cutoff=vcut, question_version=VICTIM_VERSION):
            result["attached"].append({"deduplicated_event_id": inc.dedup_id, "title": inc.title,
                                       "victim": cand.name, "p_affected": vscores.final.get(sid),
                                       "decided_by": decided,
                                       "own_events_ruled_different": [r.as_dict() for r in ruled_different]})
    conn.commit()
    return result
