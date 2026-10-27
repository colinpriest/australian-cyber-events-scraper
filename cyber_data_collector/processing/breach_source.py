"""OAIC breach source and personal-information types for every active event.

The dashboard compares the database with OAIC's Notifiable Data Breaches
statistics on two axes: the *source* of each breach (OAIC's three: malicious or
criminal attack, human error, system fault) and the *kinds of personal
information* involved. Both need a value on every event -
``DeduplicatedEvents.breach_source_category`` and ``personal_info_types_json`` -
and both were empty on all 2,066 events (2026-10-27): the script meant to fill
them was never part of the pipeline, called the fast OpenAI model and dropped
every failed call silently, so the database side of both charts read zero.

Each event is asked of Jev (one request: a Choice over the breach source and a
Noul per personal-information category) over its evidence: the event's title
and, for up to ``MAX_RECORDS`` member records, each record's title, snippet,
an excerpt of its stored page and the Perplexity research attached to it
(attack method, threat actor, victim count, reasoning). Deduplicated events
carry no description, summary or attack method of their own - on 2026-10-27
all 709 active events had none - so a question over the event row alone saw
only a title and answered "cannot tell" for half of them. Answers are
stored in ``ModelJudgements`` with their full distributions and are reused while
the event's text, the question and the model are unchanged, so:

* reruns cost nothing for unchanged events (incremental by construction);
* an event whose text changed - a merge, a re-enrichment - is asked again;
* a rebuilt ``DeduplicatedEvents`` table is refilled from the stored answers.

Jev is required. If the key or SDK is missing, or any event cannot be judged,
:func:`classify_events` raises after storing what it could, so a run never
reports success with events left unclassified.

The personal-information Nouls are reported at 0.5 and are **not calibrated**;
the breach source is the Choice's most probable option.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

QUESTION_VERSION = "breach-source-v2"
SOURCE_QUESTION_ID = "breach_source"
PII_QUESTION_PREFIX = "pii_"
MAX_RECORDS = 5
PAGE_CHARS = 1200
SNIPPET_CHARS = 600
REASONING_CHARS = 600

#: Jev option -> stored label. The dashboard rolls 'Malicious or Criminal
#: Attack' (and the legacy 'Cyber Incident') into OAIC's malicious bucket and
#: leaves 'Unknown' out of the comparison.
SOURCE_LABELS = {
    "malicious_or_criminal_attack": "Malicious or Criminal Attack",
    "human_error": "Human Error",
    "system_fault": "System Fault",
    "cannot_tell": "Unknown",
}

EVIDENCE = (
    "judging from `event` (its title) and `records` - for each source record its title, `snippet`, "
    "an excerpt of its stored `page_text` (which may be boilerplate such as a site menu or privacy "
    "policy rather than the article) and `research` (separate Perplexity research: attack method, "
    "threat actor, victim count, reasoning)"
)

SOURCE_INSTRUCTIONS = (
    "This is a reported cyber-security or data-breach event. Classify the SOURCE of the breach the "
    "way the Office of the Australian Information Commissioner (OAIC) does in its Notifiable Data "
    "Breaches statistics, " + EVIDENCE + ". If an attacker or criminal accessed, stole, encrypted, leaked or extorted the data "
    "or systems, it is a malicious or criminal attack even when a mistake or a flaw made that possible."
)
SOURCE_CRITERIA = {
    "malicious_or_criminal_attack": (
        "A deliberate attack or crime: hacking, ransomware, malware, phishing, compromised or stolen "
        "credentials, brute force, denial of service, extortion or a leak-site claim, data theft by an "
        "outsider or a rogue insider, social engineering, or theft of devices or paperwork."),
    "human_error": (
        "An unintended action by the organisation or its staff with no attacker involved: personal "
        "information emailed, mailed or disclosed to the wrong person, published by mistake, not "
        "redacted, lost paperwork or devices, or insecure disposal."),
    "system_fault": (
        "A technical or business-process failure with no attacker and no individual's mistake: a "
        "software bug, a system or process fault, or a misconfiguration that released or exposed "
        "information without anyone exploiting it."),
    "cannot_tell": (
        "The evidence does not say how the incident happened, or does not describe a specific incident."),
}

#: Category key (as the dashboard and OAIC data use it) -> what counts.
PII_CATEGORIES = {
    "contact_information": "contact information such as names, email or postal addresses, phone numbers",
    "identity_information": ("identity information such as dates of birth, driver licence, passport, "
                             "Medicare or other identity document numbers"),
    "financial_details": "financial details such as bank account, credit card or payment information",
    "health_information": "health information such as medical records, diagnoses, prescriptions or treatment",
    "tax_file_numbers": "tax file numbers (TFNs)",
    "other_sensitive_information": ("other sensitive personal information, such as criminal records, "
                                    "racial or ethnic origin, sexual orientation, biometrics or "
                                    "employment and HR records"),
    "consumer_data_right": "data held under the Consumer Data Right (CDR) regime",
    "digital_id": "Digital ID or myGov-style digital identity credentials",
}
PII_INSTRUCTIONS = (
    "This is a reported cyber-security or data-breach event. Answer whether the evidence reports "
    "that {what} were exposed, accessed, stolen or leaked in THIS incident, " + EVIDENCE + ". "
    "A general warning, a guess, or an "
    "incident where no personal information is said to be involved is not enough."
)


class BreachSourceUnavailable(RuntimeError):
    """Jev, which this classification requires, cannot be used."""


class BreachSourceIncomplete(RuntimeError):
    """Some events could not be judged; what was judged has been stored."""


@dataclass
class BreachSourceReport:
    events: int = 0
    asked: int = 0                 # events that needed a Jev request
    reused: int = 0                # events answered entirely from stored judgements
    updated: int = 0               # rows whose stored values changed (or would)
    failed: Dict[str, str] = field(default_factory=dict)
    distribution: Dict[str, int] = field(default_factory=dict)
    pii_counts: Dict[str, int] = field(default_factory=dict)


def require_jev() -> None:
    """Raise :class:`BreachSourceUnavailable` naming what is missing."""
    import os

    if not os.getenv("TYPESAFE_API_KEY"):
        raise BreachSourceUnavailable(
            "TYPESAFE_API_KEY is not set: breach-source classification requires Jev "
            "(set it in .env; no other model is used)")
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError as exc:
        raise BreachSourceUnavailable(
            "typesafe-sdk is not installed: breach-source classification requires Jev "
            "(pip install -r requirements.txt)") from exc


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add the two columns to older databases (they are in the base schema)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    if "personal_info_types_json" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN personal_info_types_json TEXT")
    if "breach_source_category" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN breach_source_category VARCHAR(40)")
    conn.commit()


def questions() -> Dict[str, Any]:
    """The Choice and the Nouls asked of every event, keyed by question id."""
    from typesafe_sdk import Choice, Noul, NoulCriteria

    out: Dict[str, Any] = {
        SOURCE_QUESTION_ID: Choice(instructions=SOURCE_INSTRUCTIONS, criteria=SOURCE_CRITERIA)}
    for key, what in PII_CATEGORIES.items():
        out[PII_QUESTION_PREFIX + key] = Noul(
            instructions=PII_INSTRUCTIONS.format(what=what),
            criteria=NoulCriteria(true=f"The evidence reports that {what} were involved.",
                                  false=f"The evidence does not report that {what} were involved."))
    return out


def _research(blob: Optional[str]) -> Dict[str, Any]:
    try:
        data = json.loads(blob or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: data.get(k) for k in ("formal_entity_name", "attack_method", "threat_actor", "victim_count")
           if data.get(k) not in (None, "", "None", [], {})}
    if data.get("reasoning"):
        out["reasoning"] = str(data["reasoning"])[:REASONING_CHARS]
    return out


def event_state(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """What the questions are answered from. Deterministic, so its hash decides reuse."""
    event = conn.execute("SELECT title, victim_organization_name FROM DeduplicatedEvents "
                         "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
    rows = conn.execute(
        """SELECT e.enriched_event_id, COALESCE(e.title, ''), COALESCE(e.description, ''),
                  COALESCE(r.raw_content, ''), e.perplexity_enrichment_data
           FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE m.deduplicated_event_id = ?""", (dedup_id,)).fetchall()
    # Records with page text first, then a stable order: a large event always
    # shows the same records, so its input hash is stable.
    rows = sorted(rows, key=lambda r: (not r[3].strip(), r[0]))
    records = [{"title": r[1], "snippet": r[2][:SNIPPET_CHARS],
                "page_text": r[3].strip()[:PAGE_CHARS] or "(none stored)", "research": _research(r[4])}
               for r in rows[:MAX_RECORDS]]
    return {"event": {"title": event[0] if event else "", "victim": event[1] if event else None,
                      "record_count": len(rows)},
            "records": records}


def active_events(conn: sqlite3.Connection, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = ("SELECT deduplicated_event_id, breach_source_category, personal_info_types_json "
           "FROM DeduplicatedEvents WHERE status = 'Active' ORDER BY event_date DESC, deduplicated_event_id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.row_factory = previous


def values_from_judgements(judgements: Mapping[str, Any]) -> Dict[str, Any]:
    """Column values from one event's answers."""
    source = judgements[SOURCE_QUESTION_ID]
    if source.answer not in SOURCE_LABELS:
        raise ValueError(f"Jev returned unknown breach-source option {source.answer!r}")
    pii = {key: bool((judgements[PII_QUESTION_PREFIX + key].value or 0.0) >= 0.5)
           for key in PII_CATEGORIES}
    return {"breach_source_category": SOURCE_LABELS[source.answer],
            "personal_info_types_json": json.dumps(pii, sort_keys=True)}


def classify_events(conn: sqlite3.Connection, dry_run: bool = False, limit: Optional[int] = None,
                    judge: Any = None) -> BreachSourceReport:
    """Fill both columns for every active event, asking Jev only what is not stored.

    ``dry_run`` still asks Jev for events with no stored answer (answers are
    stored, so the later real run reuses them) but writes no event column.

    Raises:
        BreachSourceUnavailable: Jev is not configured (checked before anything runs).
        BreachSourceIncomplete: after storing everything it could, when any event
            failed; the report is attached as ``exc.report``.
    """
    from cyber_data_collector.utils.api_budget import is_budget_refusal
    from cyber_data_collector.utils.jev import JevJudge

    if judge is None:
        require_jev()
    ensure_schema(conn)
    owned = judge is None
    judge = judge or JevJudge(conn)
    qs = questions()
    report = BreachSourceReport()
    events = active_events(conn, limit)
    report.events = len(events)
    try:
        for i, event in enumerate(events, 1):
            event_id = event["deduplicated_event_id"]
            calls_before = judge.calls
            try:
                answers = judge.ask("deduplicated_event", event_id, QUESTION_VERSION,
                                    event_state(conn, event_id), qs)
                values = values_from_judgements(answers)
            except Exception as exc:  # noqa: BLE001 - recorded and raised at the end
                report.failed[event_id] = f"{type(exc).__name__}: {exc}"
                logger.error("Breach-source judgement failed for %s: %s", event_id, exc)
                if is_budget_refusal(exc):
                    break   # every later request would be refused too
                continue
            if judge.calls > calls_before:
                report.asked += 1
            else:
                report.reused += 1
            label = values["breach_source_category"]
            report.distribution[label] = report.distribution.get(label, 0) + 1
            for key, flag in json.loads(values["personal_info_types_json"]).items():
                report.pii_counts[key] = report.pii_counts.get(key, 0) + int(flag)
            if (event["breach_source_category"], event["personal_info_types_json"]) != (
                    values["breach_source_category"], values["personal_info_types_json"]):
                report.updated += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE DeduplicatedEvents SET breach_source_category = ?, personal_info_types_json = ? "
                        "WHERE deduplicated_event_id = ?",
                        (values["breach_source_category"], values["personal_info_types_json"], event_id))
            if i % 100 == 0:
                if not dry_run:
                    conn.commit()
                logger.info("Breach source: %d/%d events (%d asked, %d reused)",
                            i, len(events), report.asked, report.reused)
        if not dry_run:
            conn.commit()
    finally:
        if owned:
            judge.close()
    if report.failed:
        exc = BreachSourceIncomplete(
            f"{len(report.failed)} of {report.events} event(s) could not be judged; "
            "the rest are stored - rerun to finish")
        exc.report = report  # type: ignore[attr-defined]
        raise exc
    return report
