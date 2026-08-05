"""Classify what each entity IS, and what part it played in an event.

Entity extraction emits every organisation, product, regulator, attacker and
journalist mentioned, and links them all with ``relationship_type='affected'``
- all 23,388 rows carry that one value, so the column says nothing. Downstream
code then had to guess the victim from mention counts, which cannot separate
the breached company from the software it was breached through: the global
Canvas incident was attributed to "Canvas Learning Management System", a
product, because that is what the coverage names most.

Two levels, because they change independently:

* **Kind** is a property of the entity and does not vary by event. Canvas is
  always a product; the OAIC is always a government body. Cached on
  ``EntitiesV2.entity_kind``.
* **Role** is a property of the entity *in one event*. Instructure is the
  victim of its own breach and a vendor in the events about the universities
  it serves. Stored per link in ``EnrichedEventEntities.relationship_type``.

Victim is the priority output; vendor is recorded alongside it because a
supply-chain breach is only intelligible when both are known.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Set

from pydantic import BaseModel, Field, field_validator

from cyber_data_collector.dedup.victim_selection import (
    is_never_victim,
    is_non_organisation,
    is_regulator,
    is_threat_actor,
)

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "gpt-4o"


class EntityKind(str, Enum):
    """What an entity is, independent of any event."""

    ORGANISATION = "organisation"
    GOVERNMENT_BODY = "government_body"
    PRODUCT = "product"
    PERSON = "person"
    THREAT_ACTOR = "threat_actor"
    OTHER = "other"


class EntityRole(str, Enum):
    """What part an entity played in one specific event."""

    VICTIM = "victim"
    VENDOR = "vendor"
    AFFECTED_CUSTOMER = "affected_customer"
    REGULATOR = "regulator"
    THREAT_ACTOR = "threat_actor"
    PRODUCT = "product"
    BYSTANDER = "bystander"


# Products and platforms that are named as if they were the breached party.
PRODUCT_MARKERS: Set[str] = {
    "canvas", "sharepoint", "moveit", "accellion", "fta", "citrix", "fortinet",
    "exchange server", "log4j", "confluence", "jira", "salesforce", "workday",
    "office 365", "microsoft 365", "azure ad", "outlook", "gosuite",
    "learning management system", "lms", "portal", "platform", "software",
    "application", "erp", "crm",
}


def rule_kind(name: Optional[str]) -> Optional[EntityKind]:
    """Classify an entity by rule where that is safe, else None.

    Only the unambiguous cases are decided here; anything else is left for the
    model, which can see the surrounding event.
    """
    text = (name or "").strip()
    if not text:
        return EntityKind.OTHER
    lowered = text.lower()

    if is_threat_actor(text):
        return EntityKind.THREAT_ACTOR
    if is_regulator(text):
        return EntityKind.GOVERNMENT_BODY
    if is_non_organisation(text):
        return EntityKind.OTHER
    # "Canvas Learning Management System", "Microsoft SharePoint"
    if any(marker in lowered for marker in PRODUCT_MARKERS):
        return EntityKind.PRODUCT
    return None


class EntityAssignment(BaseModel):
    """One entity's kind and role within an event."""

    name: str = Field(description="The entity name exactly as given.")
    kind: EntityKind = Field(
        description="What the entity IS: organisation, government_body, "
                    "product (software or platform, not a company), person, "
                    "threat_actor, or other."
    )
    role: EntityRole = Field(
        description=(
            "Its part in THIS event. victim = the organisation whose systems "
            "or data were compromised. vendor = a third party whose breach or "
            "software caused the victim's exposure. affected_customer = an "
            "organisation harmed via the vendor. regulator = investigating or "
            "responding authority. product = the software involved. "
            "threat_actor = the attacker. bystander = commentator, journalist "
            "or anyone else."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("entity name is required")
        return v.strip()


class EventEntityRoles(BaseModel):
    """Structured output: a role for every entity in one event."""

    assignments: List[EntityAssignment] = Field(default_factory=list)

    def victim(self) -> Optional[str]:
        """Highest-confidence organisation acting as the victim.

        Guarded the same way as :meth:`vendor`. The model sometimes labels a
        collective noun as both organisation and victim - "Australians" on the
        Origin Energy breach, "law firm", "sheep farmers" - which names the
        people affected rather than the breached organisation. Those describe a
        group, not an entity that can be breached, so they are rejected here
        rather than being written to the database as the victim.
        """
        candidates = [
            a for a in self.assignments
            if a.role == EntityRole.VICTIM
            and a.kind in (EntityKind.ORGANISATION, EntityKind.GOVERNMENT_BODY)
            and not is_non_organisation(a.name)
            and not is_threat_actor(a.name)
            and not is_never_victim(a.name)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.confidence).name

    def vendor(self) -> Optional[str]:
        """Highest-confidence third party whose breach caused the exposure.

        Guarded against three misclassifications seen in production: a
        ransomware group labelled as the vendor ("CBS Tasmania via Lynx"), a
        descriptive phrase rather than a name ("via third-party provider"), and
        the victim repeated as its own vendor.
        """
        victim = self.victim()
        candidates = []
        for a in self.assignments:
            if a.role != EntityRole.VENDOR:
                continue
            if a.kind not in (EntityKind.ORGANISATION, EntityKind.GOVERNMENT_BODY):
                continue
            if is_threat_actor(a.name) or is_non_organisation(a.name):
                continue
            if victim and a.name.strip().lower() == victim.strip().lower():
                continue
            candidates.append(a)
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.confidence).name

    def by_role(self, role: EntityRole) -> List[str]:
        return [a.name for a in self.assignments if a.role == role]


SYSTEM_PROMPT = (
    "You label the organisations, products and people named in a cyber "
    "security incident report.\n\n"
    "Definitions:\n"
    "- victim: the organisation whose own systems or data were compromised. "
    "In a supply-chain breach the victim is the organisation whose data was "
    "exposed, which may be the supplier itself if the supplier was breached.\n"
    "- vendor: a third-party supplier, contractor or software provider whose "
    "compromise caused the victim's exposure.\n"
    "- affected_customer: an organisation harmed because a vendor it uses was "
    "breached.\n"
    "- regulator: an authority investigating or responding (OAIC, ACSC, "
    "courts, police). Never the victim unless the authority itself was "
    "breached.\n"
    "- product: software or a platform (Canvas, SharePoint, MOVEit). A product "
    "is NEVER the victim - name the company behind it instead.\n"
    "- threat_actor: the attacker or ransomware group.\n"
    "- bystander: journalists, commentators, news outlets, unrelated mentions.\n\n"
    "Label every entity given, using its name exactly as supplied. Prefer "
    "leaving role as bystander over guessing."
)


class EntityRoleClassifier:
    """Assigns kinds and roles to the entities of an event."""

    def __init__(self, openai_client=None, model: str = CLASSIFIER_MODEL):
        self._client = openai_client
        self._client_ready = openai_client is not None
        self.model = model
        self.stats: Dict[str, int] = {
            "events": 0, "llm_calls": 0, "llm_failures": 0, "rule_only": 0,
        }

    def _get_client(self):
        if self._client_ready:
            return self._client
        self._client_ready = True
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set; entity roles from rules only")
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

    def rule_assignments(self, entities: Sequence[str]) -> EventEntityRoles:
        """Rule-only classification, used as a fallback and a starting point."""
        out: List[EntityAssignment] = []
        for name in entities:
            kind = rule_kind(name) or EntityKind.ORGANISATION
            role = {
                EntityKind.THREAT_ACTOR: EntityRole.THREAT_ACTOR,
                EntityKind.GOVERNMENT_BODY: EntityRole.REGULATOR,
                EntityKind.PRODUCT: EntityRole.PRODUCT,
                EntityKind.PERSON: EntityRole.BYSTANDER,
                EntityKind.OTHER: EntityRole.BYSTANDER,
            }.get(kind, EntityRole.BYSTANDER)
            out.append(EntityAssignment(name=name, kind=kind, role=role,
                                        confidence=0.4))
        return EventEntityRoles(assignments=out)

    def classify(
        self,
        title: str,
        summary: Optional[str],
        entities: Sequence[str],
    ) -> EventEntityRoles:
        """Classify every entity of one event. Never raises."""
        self.stats["events"] += 1
        entities = [e for e in entities if e and e.strip()]
        if not entities:
            return EventEntityRoles()

        client = self._get_client()
        if client is None:
            self.stats["rule_only"] += 1
            return self.rule_assignments(entities)

        listing = "\n".join(f"- {name}" for name in entities)
        prompt = (
            f"Incident title: {title}\n"
            f"Detail: {(summary or '')[:700]}\n\n"
            f"Entities to label:\n{listing}"
        )
        try:
            self.stats["llm_calls"] += 1
            result: EventEntityRoles = client.chat.completions.create(
                model=self.model,
                response_model=EventEntityRoles,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - one event must not stop a run
            self.stats["llm_failures"] += 1
            logger.warning("Entity classification failed (%s); using rules", exc)
            return self.rule_assignments(entities)

        # Keep only entities we actually asked about, so a hallucinated name
        # cannot enter the database.
        known = {e.strip().lower() for e in entities}
        result.assignments = [
            a for a in result.assignments if a.name.strip().lower() in known
        ]
        missing = known - {a.name.strip().lower() for a in result.assignments}
        if missing:
            # Anything the model skipped falls back to the rule answer.
            fallback = self.rule_assignments(
                [e for e in entities if e.strip().lower() in missing])
            result.assignments.extend(fallback.assignments)
        return result


# ----------------------------------------------------------------------
# Applying a classification to one stored event
# ----------------------------------------------------------------------

def industry_for(conn, victim: Optional[str]) -> Optional[str]:
    """Best industry for an organisation, or None when it cannot be justified.

    Sources, in order: the entity record, the industry other events about the
    same organisation agree on, then the entity type. None is correct when none
    of those apply - an unjustified industry is worse than a blank one, and a
    stale one is how a technology vendor came to be tagged with its clients'
    sector.
    """
    if not victim:
        return None

    row = conn.execute(
        "SELECT industry, entity_type FROM EntitiesV2 WHERE entity_name = ? LIMIT 1",
        (victim,)).fetchone()
    if row and row["industry"]:
        return row["industry"]

    siblings = conn.execute(
        """
        SELECT victim_organization_industry AS ind, COUNT(*) AS n
        FROM DeduplicatedEvents
        WHERE COALESCE(status,'Active')='Active'
          AND victim_organization_name LIKE ?
          AND victim_organization_industry IS NOT NULL
          AND victim_organization_industry != ''
        GROUP BY ind ORDER BY n DESC
        """,
        (f"%{victim[:28]}%",)).fetchall()
    if siblings and siblings[0]["n"] >= 2:
        return siblings[0]["ind"]

    if row and row["entity_type"]:
        raw = str(row["entity_type"]).split(".")[-1]
        if raw and raw.isalpha():
            return raw.title()
    return None


def rank_entity_names(links, title: Optional[str], limit: int) -> List[str]:
    """Order candidate entities by relevance before truncating.

    Sorting alphabetically and cutting at ``limit`` once dropped "Instructure"
    and "University of Sydney" from the Canvas event - everything after
    "Flinders University" - so the classifier never saw the victim or vendor.
    """
    counts: Dict[str, int] = {}
    for link in links:
        name = link["name"]
        if name:
            counts[name] = counts.get(name, 0) + 1
    title_lower = (title or "").lower()
    return sorted(
        counts,
        key=lambda n: (n.lower() in title_lower, counts[n], -len(n)),
        reverse=True,
    )[:limit]


# Plural common nouns that turn an adjective phrase into a description of a
# GROUP: "Australian financial institutions", "Australian mining companies",
# "Commonwealth entities". Used ONLY when choosing an Australian stand-in for a
# foreign victim - deliberately not a global rule, because rejecting names by
# their final word previously destroyed real organisations (Western Sydney
# University, The Plastic Bag Company).
_COLLECTIVE_PLURALS = {
    "institutions", "companies", "universities", "hospitals", "schools",
    "entities", "organisations", "organizations", "businesses", "agencies",
    "providers", "users", "customers", "clients", "retailers", "operators",
    "firms", "departments", "councils", "banks", "insurers", "telcos",
    # Mass nouns that behave the same way: "Australian critical
    # infrastructure", "the education sector".
    "infrastructure", "sector", "industry", "community", "public", "population",
}


def _is_collective_phrase(name: str) -> bool:
    """True for "<adjectives> <plural common noun>" - a group, not a name."""
    words = (name or "").strip().lower().split()
    return len(words) >= 2 and words[-1] in _COLLECTIVE_PLURALS


# Kinds that can never be the breached organisation. The classifier already
# records this on EntitiesV2.entity_kind, so using it is more reliable than
# inferring from the name: "Australian Emails and Addresses" is the stolen data
# and was correctly typed 'other', but the stored victim was never re-checked
# against that.
NON_VICTIM_KINDS = ("other", "person", "product", "threat_actor")


# Roles that say, for this event, the entity was something other than the
# organisation that was attacked.
NON_VICTIM_ROLES = ("vendor", "regulator", "threat_actor", "product", "bystander")



def _has_vendor_role(conn, deduplicated_event_id: str) -> bool:
    """True when some entity on this event is already typed as the vendor."""
    row = conn.execute(
        "SELECT 1 FROM DeduplicatedEventEntities WHERE deduplicated_event_id = ? "
        "AND relationship_type = 'vendor' LIMIT 1", (deduplicated_event_id,)).fetchone()
    return row is not None


def stored_victim_is_invalid(
    conn, victim, deduplicated_event_id: Optional[str] = None
) -> Optional[str]:
    """Reason the stored victim cannot be the breached organisation, else None.

    Checks, in order of how directly each bears on this event:

    1. the role the classifier gave the entity *on this event* - the strongest
       signal, because it was decided with the incident in view. Cisco is a
       real organisation, so no name or kind test can rule it out; what rules
       it out is that on this event it is the vendor whose appliance was
       exploited;
    2. the entity's recorded kind, which is evidence rather than a guess;
    3. the name-based tests, which are the weakest and are never allowed to
       clear a victim on their own elsewhere.
    """
    if not victim or not victim.strip():
        return None

    if deduplicated_event_id:
        role = conn.execute(
            """
            SELECT dee.relationship_type FROM DeduplicatedEventEntities dee
            JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
            WHERE dee.deduplicated_event_id = ? AND v.entity_name = ?
            """, (deduplicated_event_id, victim)).fetchone()
        if role and role[0] in NON_VICTIM_ROLES:
            return f"classified as the {role[0]} on this event, not the victim"

    row = conn.execute(
        "SELECT entity_kind FROM EntitiesV2 WHERE entity_name = ? "
        "AND entity_kind IS NOT NULL LIMIT 1", (victim,)).fetchone()
    if row and row["entity_kind"] in NON_VICTIM_KINDS:
        return f"recorded as a {row['entity_kind']}, not an organisation"
    if is_never_victim(victim):
        return "receives breach reports, never breached"
    if is_threat_actor(victim):
        return "threat actor"
    if is_non_organisation(victim):
        return "names a group of people, not the organisation"
    return None


def foreign_vendor_not_victim(conn, victim, vendor, deduplicated_event_id):
    """Move a foreign party to `vendor` when Australians were the ones affected.

    :func:`localise_victim` only swaps when a *named* Australian organisation
    is available. Where the Australian side is only describable collectively
    ("Commonwealth entities" running exploited Cisco appliances), the foreign
    company still is not the victim - its product was exploited at Australian
    sites. Recording it as the vendor and leaving the victim unknown states
    what is actually known; naming Cisco as the breached party does not.

    Returns ``(victim, vendor)``.
    """
    if not victim:
        return victim, vendor
    row = conn.execute(
        "SELECT is_australian FROM EntitiesV2 WHERE entity_name = ? LIMIT 1",
        (victim,)).fetchone()
    if row is None or row["is_australian"] != 0:
        return victim, vendor

    affected = conn.execute(
        """
        SELECT 1 FROM EventDeduplicationMap m
        JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
        WHERE m.deduplicated_event_id = ? AND v.is_australian = 1
          AND ee.relationship_type IN ('victim', 'affected_customer')
        LIMIT 1
        """,
        (deduplicated_event_id,)).fetchone()
    if affected is None:
        return victim, vendor
    logger.info("Foreign party %r moved to vendor; Australian victim unnamed",
                victim)
    return None, (vendor or victim)


def localise_victim(conn, victim, vendor, deduplicated_event_id):
    """Prefer the Australian organisation as the victim of a foreign breach.

    This is an Australian cyber-events database, so an incident is recorded for
    the Australian organisation it affected. When a foreign company is breached
    and Australian organisations are caught up in it, naming the foreign company
    as the victim answers the wrong question: the Cisco breach was filed under
    "Cisco" when the Australian parties were NAB and Vodafone, and the SITA
    breach under "SITA" rather than Qantas.

    The foreign party is not discarded - it moves to ``vendor``, which is where
    a supply-chain breach records the organisation the exposure came through.

    Only swaps when the Australian candidate is a real named organisation. An
    event whose only Australian entity is "Australian customers" keeps its
    foreign victim, because a collective noun is a worse answer than a foreign
    company (see is_non_organisation).

    Returns ``(victim, vendor)``, unchanged when no swap applies.
    """
    if not victim:
        return victim, vendor

    row = conn.execute(
        "SELECT is_australian FROM EntitiesV2 WHERE entity_name = ? LIMIT 1",
        (victim,)).fetchone()
    # Only act on entities explicitly recorded as non-Australian; unknown
    # provenance is not evidence.
    if row is None or row["is_australian"] != 0:
        return victim, vendor

    for candidate in conn.execute(
        """
        SELECT DISTINCT v.entity_name AS n, ee.relationship_type AS role
        FROM EventDeduplicationMap m
        JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
        WHERE m.deduplicated_event_id = ?
          AND v.is_australian = 1
          AND ee.relationship_type IN ('victim', 'affected_customer')
          AND COALESCE(v.entity_kind, 'organisation')
              IN ('organisation', 'government_body')
        ORDER BY CASE ee.relationship_type WHEN 'victim' THEN 0 ELSE 1 END
        """,
        (deduplicated_event_id,),
    ):
        name = candidate["n"]
        if (is_non_organisation(name) or is_never_victim(name)
                or is_threat_actor(name) or _is_collective_phrase(name)):
            continue
        # Skip when the "Australian" candidate is really the same company under
        # another spelling (JBS / JBS S.A., Prosura / Prosura Pty Ltd). Swapping
        # then gains nothing and would record one organisation as both the
        # victim and the vendor of its own breach.
        from cyber_data_collector.dedup.entity_resolution import EntityResolver
        if EntityResolver().same_organisation(victim, name):
            continue
        logger.info("Localised victim: %r -> %r (foreign party recorded as vendor)",
                    victim, name)
        return name, (vendor or victim)

    return victim, vendor


def classify_event(
    conn,
    deduplicated_event_id: str,
    classifier: Optional["EntityRoleClassifier"] = None,
    max_entities: int = 25,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Classify one stored event's entities and persist the result.

    Shared by the ``classify-entities`` command and by the automatic refresh
    that runs after a merge or split, so both paths behave identically.

    Returns a summary dict with ``victim``, ``vendor``, ``labelled`` and
    ``assignments``.
    """
    from cyber_data_collector.dedup.role_maintenance import mark_classified

    classifier = classifier or EntityRoleClassifier()
    event = conn.execute(
        "SELECT title, summary FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
        (deduplicated_event_id,)).fetchone()
    if event is None:
        return {"victim": None, "vendor": None, "labelled": 0, "assignments": []}

    links = conn.execute(
        """
        SELECT DISTINCT ee.enriched_event_id AS eid, ee.entity_id AS entid,
               v.entity_name AS name
        FROM EventDeduplicationMap m
        JOIN EnrichedEventEntities ee
             ON ee.enriched_event_id = m.enriched_event_id
        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
        WHERE m.deduplicated_event_id = ?
        """,
        (deduplicated_event_id,)).fetchall()

    if not links:
        # Nothing to classify, so the (empty) roles trivially match the
        # membership. Marking it keeps the event out of the stale queue rather
        # than being re-selected on every run for ever.
        if not dry_run:
            mark_classified(conn, deduplicated_event_id)
        return {"victim": None, "vendor": None, "labelled": 0, "assignments": []}

    names = rank_entity_names(links, event["title"], max_entities)
    result = classifier.classify(event["title"] or "", event["summary"], names)
    if not result.assignments:
        return {"victim": None, "vendor": None, "labelled": 0, "assignments": []}

    by_name = {a.name.strip().lower(): a for a in result.assignments}
    labelled = 0
    for link in links:
        assignment = by_name.get((link["name"] or "").strip().lower())
        if not assignment:
            continue
        labelled += 1
        if dry_run:
            continue
        conn.execute(
            "UPDATE EnrichedEventEntities SET relationship_type = ? "
            "WHERE enriched_event_id = ? AND entity_id = ?",
            (assignment.role.value, link["eid"], link["entid"]))
        conn.execute("UPDATE EntitiesV2 SET entity_kind = ? WHERE entity_id = ?",
                     (assignment.kind.value, link["entid"]))

    victim, vendor = resolve_victim(conn, deduplicated_event_id, result,
                                    dry_run=dry_run)
    if not dry_run:
        mark_classified(conn, deduplicated_event_id)

    return {"victim": victim, "vendor": vendor, "labelled": labelled,
            "assignments": result.assignments}



def _sync_roles(conn, deduplicated_event_id: str,
                victim: Optional[str], vendor: Optional[str]) -> None:
    """Make the role table agree with the victim and vendor finally chosen.

    The scalar fields and ``relationship_type`` are two records of one fact,
    and the dashboards read the roles. When ``localise_victim`` swapped Cisco
    for NAB the scalar was corrected but NAB stayed ``affected_customer``, so
    the event rendered with no victim at all. Whatever wins the scalar must
    hold the matching role.
    """
    def retype(name: str, role: str) -> None:
        row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?",
                           (name,)).fetchone()
        if not row:
            return
        entity_id = row[0]
        conn.execute(
            "UPDATE DeduplicatedEventEntities SET relationship_type = ? "
            "WHERE deduplicated_event_id = ? AND entity_id = ?",
            (role, deduplicated_event_id, entity_id))
        conn.execute(
            "UPDATE EnrichedEventEntities SET relationship_type = ? WHERE entity_id = ? "
            "AND enriched_event_id IN (SELECT enriched_event_id FROM "
            "EventDeduplicationMap WHERE deduplicated_event_id = ?)",
            (role, entity_id, deduplicated_event_id))

    if vendor:
        retype(vendor, "vendor")
    if victim:
        retype(victim, "victim")


def resolve_victim(
    conn,
    deduplicated_event_id: str,
    result: "EventEntityRoles",
    dry_run: bool = False,
):
    """Settle the victim and vendor for one event and write them.

    Extracted so the pipeline and the ``classify-entities`` CLI cannot drift:
    the CLI carried its own copy of this logic, so fixes applied here silently
    did nothing when the command was the caller - which is why "Cisco" survived
    two corrected reruns.

    Returns the ``(victim, vendor)`` finally stored.
    """
    victim, vendor = result.victim(), result.vendor()
    victim, vendor = localise_victim(conn, victim, vendor, deduplicated_event_id)

    proposed = victim
    victim, vendor = foreign_vendor_not_victim(conn, victim, vendor,
                                               deduplicated_event_id)
    demoted = not victim and (bool(proposed) or _has_vendor_role(
        conn, deduplicated_event_id))

    # When a foreign vendor is demoted, its Australian customers are the
    # organisations that were attacked - the ProctorU shape, where the US
    # proctoring vendor was breached and ten Australian universities lost
    # student data. Promoting them is what makes the dashboard name a victim
    # instead of the vendor, because the dashboard reads roles, not this scalar.
    if demoted and not dry_run:
        customers = conn.execute(
            """
            SELECT DISTINCT v.entity_id, v.entity_name
            FROM DeduplicatedEventEntities dee
            JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
            WHERE dee.deduplicated_event_id = ?
              AND dee.relationship_type = 'affected_customer'
              AND v.is_australian = 1
              AND COALESCE(v.entity_kind, 'organisation')
                  IN ('organisation', 'government_body')
            """, (deduplicated_event_id,)).fetchall()
        # A collective ("TAFEs", "Australian hospitals") names a group, not the
        # organisation whose systems were attacked, so it is never promoted.
        real = [c for c in customers if not is_non_organisation(c[1])]
        for entity_id, name in real:
            conn.execute(
                "UPDATE DeduplicatedEventEntities SET relationship_type = 'victim' "
                "WHERE deduplicated_event_id = ? AND entity_id = ?",
                (deduplicated_event_id, entity_id))
            conn.execute(
                "UPDATE EnrichedEventEntities SET relationship_type = 'victim' "
                "WHERE entity_id = ? AND enriched_event_id IN (SELECT enriched_event_id "
                "FROM EventDeduplicationMap WHERE deduplicated_event_id = ?)",
                (entity_id, deduplicated_event_id))
            logger.info("Promoted %r to victim on %s (customer of demoted vendor %r)",
                        name, deduplicated_event_id, vendor)
        # One customer can name the scalar field; several are co-equal victims
        # and picking one would repeat the misattribution being corrected.
        if len(real) == 1:
            victim = real[0][1]

    # A stored victim survives only while it is plausible. Previously the field
    # was overwritten only when the classifier produced a new victim, so a wrong
    # one - "Australian Emails and Addresses" (the stolen data), "Cisco" (the
    # vendor whose appliance was exploited) - persisted through every rerun.
    # The kind recorded on the entity is the classifier's own typing, so it is
    # evidence rather than a guess about the name.
    stored = conn.execute(
        "SELECT victim_organization_name FROM DeduplicatedEvents "
        "WHERE deduplicated_event_id = ?", (deduplicated_event_id,)).fetchone()
    if not victim and stored and stored[0]:
        # A demotion is a positive finding, not an absence of one: the stored
        # name must go even though it is a real organisation. Without this the
        # `else` branch below wrote only the vendor and left "Cisco" standing
        # as the victim of a breach of Cisco's own appliances.
        reason = ("demoted to vendor" if demoted
                  else stored_victim_is_invalid(conn, stored[0],
                                                deduplicated_event_id))
        if reason and not dry_run:
            logger.info("Clearing victim %r on %s: %s", stored[0],
                        deduplicated_event_id, reason)
            conn.execute(
                "UPDATE DeduplicatedEvents SET victim_organization_name = NULL, "
                "victim_organization_industry = NULL WHERE deduplicated_event_id = ?",
                (deduplicated_event_id,))

    if not dry_run:
        # A role-labelled victim outranks the frequency heuristic: the
        # classifier saw the event and separated the breached organisation from
        # the product it was breached through, which counting cannot do.
        if victim:
            conn.execute(
                "UPDATE DeduplicatedEvents SET victim_organization_name = ?, "
                "victim_organization_industry = ?, vendor_organization_name = ?, "
                "vendor_organization_industry = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE deduplicated_event_id = ?",
                (victim, industry_for(conn, victim), vendor,
                 industry_for(conn, vendor) if vendor else None,
                 deduplicated_event_id))
        else:
            conn.execute(
                "UPDATE DeduplicatedEvents SET vendor_organization_name = ?, "
                "vendor_organization_industry = ? WHERE deduplicated_event_id = ?",
                (vendor, industry_for(conn, vendor) if vendor else None,
                 deduplicated_event_id))
        _sync_roles(conn, deduplicated_event_id, victim, vendor)
    return victim, vendor
