"""Choose the victim organisation for a deduplicated event.

The victim used to be copied from the *master* record's highest-confidence
entity. Every row in ``EnrichedEventEntities`` is tagged ``affected`` with no
victim/regulator distinction, so that routinely picked the wrong organisation:

* the 106-record Optus breach was attributed to the **Australian Cyber Security
  Centre** - the master record listed ACSC and Optus both at confidence 1.0 and
  ACSC happened to sort first, even though "Optus" appears on 100 of the 106
  members and ACSC on 7;
* the 47-record global Canvas incident was attributed to a single university,
  when the breached party is the vendor **Instructure** (28 mentions).

Two signals fix this, and neither depends on the master record:

1. **Frequency across every member.** A regulator is mentioned by a handful of
   follow-up articles; the victim is named by nearly all of them.
2. **Appearing in the event title.** Titles are now derived to name the victim
   (see :mod:`title_selection`), so agreement between the two is strong
   evidence.

Regulators, legislation, locations and threat actors are demoted. Threat actors
are identified from ``EnrichedEvents.attacking_entity_name`` where available -
data, not a hand-maintained list - with a small fallback list for the rest.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup.entity_resolution import EntityResolver

logger = logging.getLogger(__name__)

# Bodies that investigate or respond to breaches. They are legitimately linked
# to an event, but they are never the victim.
REGULATOR_MARKERS: Set[str] = {
    "australian cyber security centre", "acsc",
    "office of the australian information commissioner", "oaic",
    "australian information commissioner", "information commissioner",
    "national cyber security coordinator", "national office of cyber security",
    "australian signals directorate", "asd",
    "australian federal police", "afp",
    "federal court", "supreme court", "scamwatch",
    "australian competition and consumer commission", "accc",
    "australian securities and investments commission", "asic",
    "australian prudential regulation authority", "apra",
    "australian communications and media authority", "acma",
    "privacy commissioner", "cyber security centre",
}

# Legislation, schemes and other non-organisations that appear as entities.
NON_ORGANISATION_MARKERS: Set[str] = {
    "privacy act", "notifiable data breaches", "ndb scheme", "cyber security act",
    "security of critical infrastructure", "gdpr", "essential eight",
}

# Collective nouns and unnamed groups. Entity extraction emits these freely
# ("hackers", "Australian universities", "hospitals and clinics") but they name
# no specific organisation.
GENERIC_GROUP_MARKERS: Set[str] = {
    "hackers", "attackers", "cybercriminals", "criminals", "threat actors",
    "australians", "customers", "clients", "students", "patients", "staff",
    "employees", "residents", "users", "individuals", "victims", "members",
    "universities", "schools", "hospitals", "clinics", "businesses",
    "organisations", "organizations", "agencies", "departments", "councils",
    "banks", "retailers", "tafes", "government agencies", "companies",
    # Descriptive stand-ins for an unnamed supplier: "third-party provider",
    # "external contractor". They describe a relationship, not an organisation.
    "provider", "providers", "supplier", "suppliers", "vendor", "vendors",
    "contractor", "contractors", "third", "party", "external", "partner",
    "partners", "subcontractor",
}

# Bodies whose function is to RECEIVE, COORDINATE or REPORT breach
# notifications. They appear in the coverage of almost every incident because
# they were notified, never because their own systems were attacked, so they
# can never be the victim.
#
# Courts and police are deliberately NOT here: they hold data and can genuinely
# be breached, and this database contains a real Federal Court breach. They stay
# subject to the softer regulator rule instead.
NEVER_VICTIM_BODIES: Set[str] = {
    "office of the australian information commissioner",
    "australian information commissioner",
    "information commissioner",
    "privacy commissioner",
    "oaic",
    "australian cyber security centre",
    "acsc",
    "australian signals directorate",
    "national cyber security coordinator",
    "national office of cyber security",
    "scamwatch",
    "notifiable data breaches scheme",
}
# Police were briefly listed above and removed: this database contains a real
# "AFP officer data leak", so police hold data and can be breached like anyone
# else. Only bodies that exist to RECEIVE notifications belong in the set.


def is_never_victim(name: Optional[str]) -> bool:
    """True for bodies that receive or report breaches but are never breached.

    The OAIC is named in the coverage of nearly every Australian incident
    because it was notified; that is the opposite of being the victim. Its
    database was not attacked.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    if lowered in NEVER_VICTIM_BODIES:
        return True
    # Also catch parenthetical and suffixed forms:
    # "Office of the Australian Information Commissioner (OAIC)".
    return any(body in lowered for body in NEVER_VICTIM_BODIES
               if len(body) > 6)


# Retained for reference only - see the note in is_non_organisation for why
# this is NOT used to reject names.
COLLECTIVE_TAIL_NOUNS: Set[str] = {
    "institution", "institutions", "school", "schools", "university",
    "universities", "hospital", "hospitals", "clinic", "clinics",
    "company", "companies", "organisation", "organisations",
    "organization", "organizations", "provider", "providers",
    "agency", "agencies", "business", "businesses", "entity", "entities",
    "farmer", "farmers", "retailer", "retailers", "employer", "employers",
    "operator", "operators", "firm", "firms", "body", "bodies",
}

# Bare place names carry no organisational identity.
PLACE_NAMES: Set[str] = {
    "australia", "australian", "new south wales", "victoria", "queensland",
    "western australia", "south australia", "tasmania", "sydney", "melbourne",
    "brisbane", "perth", "adelaide", "canberra", "hobart", "darwin",
    "australian government", "australian universities", "commonwealth",
}

# Fallback threat actors for records with no attacking_entity_name recorded.
THREAT_ACTOR_MARKERS: Set[str] = {
    "scattered spider", "lockbit", "alphv", "blackcat", "cl0p", "clop",
    "ransomhub", "dragonforce", "lynx", "inc ransom", "medusa", "akira",
    "revil", "conti", "hive", "stormous", "rhysida", "qilin", "sarcoma",
    "dienet", "rippersec", "trinity of chaos", "shinyhunters", "lapsus",
    "cl0p ransomware", "kairos", "the gentlemen", "mailto", "netwalker",
}

_TOKEN = re.compile(r"[a-z0-9]+")
_PERSON = re.compile(r"^[A-Z][a-z]+ [A-Z][a-z]+$")
_QUANTITY = re.compile(r"\d")
# "March 2023", "2023", "May 2024" - extracted as entities, never victims.
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
# A bare host name is not an organisation ("hms.com.au"), but plenty of
# companies ARE their single-dot domain - Paradox.ai, Bigfooty.com. Only
# multi-label hosts (three or more parts, e.g. "x.com.au") are rejected.
_DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+){2,}$")
_DATE_LIKE = re.compile(
    r"^(?:(?:" + "|".join(_MONTHS) + r")\s+)?(?:19|20)\d{2}$", re.IGNORECASE)
# Malware and ransomware families are the weapon, not the victim.
_MALWARE_WORDS = ("ransomware", "malware", "trojan", "botnet", "wiper",
                  "infostealer", "spyware")
# Corporate-form words mark a formal legal name, preferred for display.
_LEGAL_FORM = ("pty", "ltd", "limited", "inc", "incorporated", "corporation",
               "plc", "llc", "group")


def _tokens(text: Optional[str]) -> Set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def is_regulator(name: Optional[str]) -> bool:
    """True when the name is an investigating or responding body."""
    lowered = (name or "").lower().strip()
    if not lowered:
        return False
    return any(marker in lowered for marker in REGULATOR_MARKERS)


def is_non_organisation(name: Optional[str]) -> bool:
    """True for legislation, schemes, bare place names and quantities."""
    lowered = (name or "").lower().strip()
    if not lowered:
        return True
    if lowered in PLACE_NAMES:
        return True
    if any(marker in lowered for marker in NON_ORGANISATION_MARKERS):
        return True
    # "12.9 million individuals", "6 million customers"
    if _QUANTITY.search(lowered) and any(
        w in lowered for w in ("million", "individuals", "customers", "records", "people")
    ):
        return True
    # Bare dates ("March 2023") are extracted as entities but name nobody.
    if _DATE_LIKE.match(lowered.strip()):
        return True
    # Bare domains and email addresses ("hms.com.au").
    if _DOMAIN.match(lowered.strip()) or "@" in lowered:
        return True
    # Descriptive stand-ins rather than names: "third-party ticketing
    # platform", "external IT provider".
    if lowered.startswith(("third-party ", "third party ", "external ",
                           "unnamed ", "unknown ", "an unnamed", "a third")):
        return True
    # A real organisation name carries at least one capital. A multi-word
    # all-lowercase string ("practice software", "third-party IT code
    # library") is a description of a supplier, not its name. Single lowercase
    # words are left alone - they are often just a brand stored uncapitalised.
    raw = (name or "").strip()
    if len(raw.split()) >= 2 and not any(ch.isupper() for ch in raw):
        return True
    # Collective nouns: "hospitals and clinics", "Australian universities".
    words = set(_TOKEN.findall(lowered))
    if words and words <= (GENERIC_GROUP_MARKERS | PLACE_NAMES | {"and", "the", "of"}):
        return True
    if lowered in GENERIC_GROUP_MARKERS:
        return True
    # A trailing collective noun with only descriptive words before it
    # ("Australian universities", "government agencies").
    words_list = lowered.split()
    last = words_list[-1] if words_list else ""
    if last in GENERIC_GROUP_MARKERS and len(words_list) <= 2:
        return True
    # A trailing-class-noun rule was tried here and REMOVED. Rejecting any name
    # ending in a class noun ("companies", "university", "company") also
    # rejected real organisations - Western Sydney University, The Plastic Bag
    # Company, TMA Group of Companies, Albright Institute - and replacing them
    # produced far worse values than the ones it was meant to fix. Names like
    # "Australian mining companies" are better left alone than risk that: a
    # slightly vague name is still the right event, a wrong one is not.
    return False


def is_threat_actor(name: Optional[str], known_attackers: Optional[Set[str]] = None) -> bool:
    """True when the name is the attacker rather than the victim."""
    lowered = (name or "").lower().strip()
    if not lowered:
        return False
    if known_attackers and lowered in {a.lower() for a in known_attackers if a}:
        return True
    # "Mailto ransomware", "Snake malware" - the family, not the target.
    if any(word in lowered for word in _MALWARE_WORDS):
        return True
    return any(marker in lowered for marker in THREAT_ACTOR_MARKERS)


def looks_like_person(name: Optional[str]) -> bool:
    """Weak hint that a name is a person rather than an organisation.

    NOT reliable on its own: the "Capitalised Capitalised" shape matches
    "Oxfam Australia", "Deakin University", "Compumedics Limited" and
    "Brydens Lawyers" just as readily as "David Hollingworth". Using it as a
    rejection test discarded valid victims wholesale, so it now only
    contributes a small penalty, and real filtering of bylines and officials
    is done by :func:`ubiquitous_entities`, which measures how many unrelated
    events a name appears in.
    """
    text = (name or "").strip()
    if not text:
        return False
    if "@" in text:
        return True
    return bool(_PERSON.match(text))


def ubiquitous_entities(
    per_event_entities: Iterable[Iterable[str]],
    max_share: float = 0.02,
    min_absolute: int = 8,
) -> Set[str]:
    """Names that appear across too many unrelated events to be a victim.

    Journalists ("David Hollingworth" bylines every Cyber Daily article),
    regulators, news agencies and boilerplate turn up across the whole corpus;
    a victim appears in the handful of events about its own breach. Measuring
    that is more robust than trying to recognise a person by the shape of
    their name, and needs no hand-maintained list.
    """
    doc_freq: Dict[str, int] = {}
    total = 0
    for names in per_event_entities:
        total += 1
        for name in {(n or "").strip().lower() for n in names if n}:
            doc_freq[name] = doc_freq.get(name, 0) + 1
    ceiling = max(min_absolute, int(total * max_share))
    ubiquitous = {n for n, c in doc_freq.items() if c > ceiling}
    logger.info(
        "Entity ubiquity fitted on %d event(s): %d name(s) appear in more than "
        "%d of them", total, len(ubiquitous), ceiling)
    return ubiquitous


def score_victim(
    name: str,
    share: float,
    title_tokens: Optional[Set[str]] = None,
    known_attackers: Optional[Set[str]] = None,
    ubiquitous: Optional[Set[str]] = None,
) -> float:
    """Score a candidate victim. Higher is better.

    Args:
        name: Candidate organisation name.
        share: Fraction of the event's members that mention it (0..1).
        title_tokens: Tokens of the event title.
        known_attackers: Attacker names recorded on the member events.
    """
    text = (name or "").strip()
    if not text:
        return -100.0

    if is_never_victim(text):
        # Notified, not breached - never a candidate however often it appears.
        return -80.0
    if is_non_organisation(text):
        return -60.0
    if is_threat_actor(text, known_attackers):
        return -50.0
    score = 0.0
    if ubiquitous and text.strip().lower() in ubiquitous:
        # Appears across many unrelated events - usually a byline, official or
        # news agency. Scaled, NOT a hard reject: the largest victims are also
        # corpus-common (Optus spans dozens of events), and rejecting them
        # outright handed the 106-record Optus breach to a bystander. A name
        # that dominates THIS event's members overcomes the penalty; one
        # mentioned in passing does not.
        score -= 8.0
    if looks_like_person(text):
        score -= 3.0
    if is_regulator(text):
        # Heavy, but not absolute: a regulator can itself be breached, in which
        # case it will dominate the mentions and can still win.
        score -= 12.0

    # Frequency is the primary signal.
    score += share * 10.0

    if title_tokens:
        overlap = _tokens(text) & title_tokens
        if overlap:
            score += 4.0 + min(len(overlap), 2)

    if len(text) < 3:
        score -= 5.0
    return score


def derive_victim(
    entity_counts: Sequence[Tuple[str, int]],
    member_count: int,
    title: Optional[str] = None,
    known_attackers: Optional[Set[str]] = None,
    current_victim: Optional[str] = None,
    resolver: Optional[EntityResolver] = None,
    ubiquitous: Optional[Set[str]] = None,
) -> Optional[str]:
    """Pick the victim organisation for an event.

    Args:
        entity_counts: ``(entity_name, mentions)`` across all member records.
        member_count: Number of member records, for computing share.
        title: Event title, used to corroborate.
        known_attackers: Attacker names recorded on the members.
        current_victim: Currently stored victim, kept when nothing scores better.
        resolver: Used to fold name variants together so "Optus" and "Optus Pty
            Limited" compete as one organisation rather than splitting the vote.

    Returns:
        The best candidate, or ``current_victim`` when nothing beats it.
    """
    if not entity_counts:
        return current_victim

    resolver = resolver or EntityResolver()
    title_tokens = _tokens(title)
    total = max(member_count, 1)

    # Fold variants of one organisation together, summing their mentions.
    groups: List[Dict] = []
    for name, count in entity_counts:
        for group in groups:
            if resolver.same_organisation(group["name"], name):
                group["count"] += count
                group["variants"].append(name)
                break
        else:
            groups.append({"name": name, "count": count, "variants": [name]})

    best_score, best_name = -1e9, None
    for group in groups:
        share = min(group["count"] / total, 1.0)
        score = score_victim(group["name"], share, title_tokens,
                             known_attackers, ubiquitous)
        if score > best_score:
            best_score = score
            best_name = _preferred_variant(group["variants"])

    if best_name is None or best_score < 0:
        return current_victim

    # Only replace when the winner is a DIFFERENT organisation. Otherwise a
    # correctly-stored formal name ("MediSecure Ltd") would be overwritten by a
    # colloquial mention ("MediSecure") purely because the latter appears more
    # often in headlines.
    if current_victim and resolver.same_organisation(current_victim, best_name):
        return current_victim
    return best_name


def _preferred_variant(variants: Sequence[str]) -> str:
    """Choose how to spell an organisation among the variants seen.

    A formal legal name is preferred ("Singtel Optus Pty Limited" over
    "Optus"); failing that, the longest spelling.
    """
    formal = [v for v in variants
              if any(f in v.lower().split() for f in _LEGAL_FORM)]
    pool = formal or list(variants)
    return max(pool, key=len)

def should_replace(
    current_victim: Optional[str],
    candidate: Optional[str],
    current_share: float,
    candidate_share: float,
    known_attackers: Optional[Set[str]] = None,
) -> Tuple[bool, str]:
    """Decide whether to overwrite an existing victim, and say why.

    Frequency alone is too noisy to re-decide a victim that is already correct.
    Member records mention news agencies, suppliers and descriptive phrases
    often enough to win a popularity contest - trialling that policy proposed
    "Evolution Mining Limited" -> "Reuters" and "Nine Entertainment" -> "Sydney
    Morning Herald". So an existing value is replaced only on positive evidence
    that it is wrong:

    * nothing is recorded at all;
    * what is recorded is not a victim (a regulator, an attacker, a date, a
      collective noun, a person);
    * or it is barely mentioned by its own members while another organisation
      dominates them.

    Returns:
        ``(replace, reason)``.
    """
    if not candidate:
        return False, "no candidate"
    if not current_victim or not current_victim.strip():
        return True, "no victim recorded"
    if is_never_victim(current_victim):
        return True, (f"{current_victim!r} receives and reports breach "
                      "notifications; it is never the victim")
    if is_regulator(current_victim):
        # A regulator can itself be breached - ASIC was compromised through
        # Accellion's file-transfer service - so being a regulator is not proof
        # of a mistake. Only replace when it is also barely mentioned by its
        # own members, which is what a bystander looks like.
        if current_share >= 0.30:
            return False, (f"{current_victim!r} is a regulator but dominates its "
                           f"own members ({current_share:.0%}); treated as the victim")
        return True, (f"current victim {current_victim!r} is a regulator mentioned "
                      f"by only {current_share:.0%} of members")
    if is_threat_actor(current_victim, known_attackers):
        return True, f"current victim {current_victim!r} is a threat actor"
    if is_non_organisation(current_victim):
        return True, f"current victim {current_victim!r} is not an organisation"
    # Never trade a fuller name for a shorter form of the same one. Headlines
    # say "HWL Ebsworth" far more often than "HWL Ebsworth Lawyers", so a pure
    # frequency rule quietly strips the qualifier off correct victims.
    from cyber_data_collector.dedup.entity_resolution import significant_tokens
    current_tokens = set(significant_tokens(current_victim))
    candidate_tokens = set(significant_tokens(candidate))
    if candidate_tokens and candidate_tokens < current_tokens:
        return False, (f"{candidate!r} is a shorter form of {current_victim!r}; "
                       "keeping the fuller name")

    if current_share < 0.10 <= candidate_share and candidate_share >= 0.40:
        return True, (f"current victim mentioned by {current_share:.0%} of members, "
                      f"{candidate!r} by {candidate_share:.0%}")
    return False, "current victim looks valid"


def mention_share(
    name: Optional[str],
    entity_counts: Sequence[Tuple[str, int]],
    member_count: int,
    resolver: Optional[EntityResolver] = None,
) -> float:
    """Fraction of an event's members that mention ``name`` (or a variant)."""
    if not name or not entity_counts or member_count <= 0:
        return 0.0
    resolver = resolver or EntityResolver()
    total = 0
    for entity_name, count in entity_counts:
        # Strict equivalence: the loose blocking test folds different
        # universities together and inflates a wrong victim's support.
        if resolver.same_organisation(name, entity_name):
            total += count
    return min(total / member_count, 1.0)


# ---------------------------------------------------------------------------
# Descriptions masquerading as entity names
# ---------------------------------------------------------------------------
#
# Coverage frequently withholds the organisation's name in the headline -
# "Australia's largest home builder confirms ransomware attack", "KillSec
# claims breach on Australian educational support platform" - and extraction
# stored the description as the entity. These are not names: they cannot be
# matched against anything, they group unrelated events, and they read as the
# attacked organisation when they identify nobody.
#
# The name is usually available: it is in the event's own description ("...the
# Australian educational support platform Thanks For the Help (TFTH)"), or
# already recorded as a second entity on the same event (Metricon Homes,
# Vocus), or findable by search.

# Wording that marks a phrase as a description rather than a name.
_HARD_MARKERS = (
    "largest", "smallest", "biggest", "leading", "prominent", "major",
    "unnamed", "unknown", "undisclosed", "fourth", "third", "second-",
    "one of the", "-based firm", "-based company", "-based provider",
)

# Descriptive only when the rest of the name is not written as a proper noun.
_SOFT_MARKERS = ("australia's", "australias", "a major")

_DESCRIPTIVE_MARKERS = _HARD_MARKERS + _SOFT_MARKERS

# Nouns that describe a category of organisation rather than name one.
_CATEGORY_NOUNS = (
    "provider", "company", "firm", "platform", "organisation", "organization",
    "entity", "operator", "retailer", "manufacturer", "processor", "centre",
    "center", "hospital", "clinic", "agency", "department", "institution",
    "business", "startup", "start-up", "service", "supplier", "vendor",
    "fund", "funds", "insurer", "bank", "lender", "telco", "association",
    "society", "council", "group", "chain", "practice", "network", "college",
    "school", "university", "carrier", "airline", "miner", "grocer",
)

# Category nouns that essentially never appear in an organisation's real name.
# "Australian Digital Health Agency" is a body; "Australian Pharmaceutical
# Firm" is a description of one, and capitalising it does not change that - so
# these are treated as descriptive whatever the casing.
# Words naming a line of business. With a place adjective and a category noun
# they form a description - "Australian Pharmaceutical Firm" - but on their own
# they are not enough to condemn a name.
_SECTOR_WORDS = {
    "pharmaceutical", "pharmaceuticals", "communications", "communication",
    "construction", "logistics", "energy", "retail", "mining", "financial",
    "finance", "medical", "health", "healthcare", "education", "educational",
    "technology", "technologies", "engineering", "steel", "insurance", "legal",
    "transport", "manufacturing", "telecommunications", "telecom", "banking",
    "accounting", "hospitality", "agriculture", "agricultural", "defence",
    "utilities", "utility", "media", "process", "industry", "industrial",
}

_STOPWORDS = {"the", "a", "an", "of", "and", "for", "in", "at", "based"}

_NEVER_IN_A_NAME = (
    "firm", "company", "provider", "platform", "entity", "operator",
    "business", "organisation", "organization", "supplier", "vendor",
    "retailer", "manufacturer", "processor", "startup", "start-up",
    "telco", "insurer", "lender", "chain", "miner", "grocer", "carrier",
)


# Place adjectives that qualify a category noun without naming anything.
_PLACE_ADJECTIVES = {
    "australian", "aussie", "victorian", "queensland", "tasmanian", "nsw",
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra",
    "western", "northern", "southern", "eastern", "regional", "national",
    "state", "federal", "local",
}


def is_descriptive_label(name: Optional[str]) -> bool:
    """True when ``name`` describes an organisation instead of naming one.

    Deliberately conservative about capitalisation: several real Australian
    brands are styled lowercase - auDA, iiNet, digiDirect, youX - so a leading
    lowercase letter only counts against a name that has no internal capital
    and reads as a common-noun phrase.
    """
    text = (name or "").strip()
    if not text:
        return False
    lowered = text.lower()

    words = [w for w in re.split(r"[\s/]+", text) if w]
    if not words:
        return False

    significant = [w for w in words if len(w) > 3]
    titled = bool(significant) and all(w[0].isupper() for w in significant)

    # A superlative or an explicit "unnamed" is a description however it is
    # capitalised: "Major Private Hospital Group" names nobody.
    if any(marker in lowered for marker in _HARD_MARKERS):
        return True
    # A possessive country prefix only marks a description when the rest is not
    # written as a name - "Australia's largest home builder" versus
    # "Australia's Copyright Agency", which is a real body.
    if not titled and any(marker in lowered for marker in _SOFT_MARKERS):
        return True

    # "Australian educational support platform", "Victorian accommodation
    # provider": a place adjective plus a category noun describes an
    # organisation without naming one, and capitalising the adjective does not
    # make it a name.
    # Only when it is *written* as a description. Many real bodies are named
    # exactly this way - "Australian Digital Health Agency", "NSW Rural Fire
    # Service", "National Disability Insurance Agency" - and title case is what
    # distinguishes a name from a description of one. Demoting those deleted
    # real victims.
    # A trailing "Firm"/"Company"/"Provider" marks a description only when
    # everything before it is generic too. "The Plastic Bag Company" and
    # "Albright Institute of Language and Business" are real organisations;
    # flagging them on the last word alone cleared two correct victims.
    ends_in_common_noun = any(
        lowered.rstrip("s").endswith(noun.rstrip("s")) for noun in _NEVER_IN_A_NAME)
    if ends_in_common_noun and len(words) > 1:
        qualifiers = [w.lower().strip(".,'’") for w in words[:-1]]
        # "Sydney-based" is a place qualifier however it is punctuated.
        if all(q.endswith("-based") or q in _PLACE_ADJECTIVES
               or q in _SECTOR_WORDS or q in _STOPWORDS
               for q in qualifiers):
            return True

    if (not titled
            and lowered.split()[0] in _PLACE_ADJECTIVES
            and any(lowered.rstrip("s").endswith(noun.rstrip("s"))
                    for noun in _CATEGORY_NOUNS)):
        return True

    # A styled brand carries a capital somewhere ("iiNet", "youX", "auDA").
    has_internal_capital = any(any(c.isupper() for c in w[1:]) for w in words)
    starts_upper = text[0].isupper()
    if starts_upper or has_internal_capital:
        return False

    # All-lowercase and ending in a category noun: "law firm", "chicken
    # processor", "communications provider", "children's hospital".
    return any(lowered.rstrip("s").endswith(noun.rstrip("s"))
               for noun in _CATEGORY_NOUNS)
