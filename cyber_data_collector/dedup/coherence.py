"""Find and remove stray records: members of an event that describe a different incident.

The Sharp Motor Group event (2026-10-02) held 18 records of which only 5 were
about Sharp Motor Group: 9 were unrelated Cyber Daily articles and 4 were about
a different company, Sharp Office Systems. Its date came from a stray Partnered
Health record, and strays like it pull every later duplicate check toward
wrong matches. Labellers later found the same defect elsewhere: Western Sydney
University holding WA Health records, the University of Tasmania holding
University of Sydney and Canvas records, NT Health holding an Updoc record.

Three stages, cheapest first:

1. **Suspects (free).** A member is suspect when the organisation Perplexity
   identified for it shares no distinctive name key with the event's victim,
   vendor or the organisation most of its members name - or, in an event whose
   recorded victim the attribution check flagged as probably wrong with nothing
   to replace it, when its research names no organisation at all (nothing then
   vouches for it). Those last are outside the calibrated suspect population:
   they are judged and reported, and split by label only. Only suspects can be
   split out by the model here: same-organisation members are usually misdated
   copies or follow-ups (Partnered Health, Latitude, OpenAI in the 2026-10-02
   review). A different incident at the same organisation is
   :mod:`coherence_same_org`'s question - its candidates need evidence of
   another incident, and splitting needs its own calibration.
2. **Judgement (Jev, stored).** For each suspect, Jev answers one question
   over the record's own page text and Perplexity research, the event's victim,
   title and other records, and the victim's researched incident timeline:
   *is this record about the same incident as the event?* Answers are stored
   in ``ModelJudgements`` (``subject_type='event_member'``) and reused. Scores
   inside :data:`ESCALATE_BAND` - fixed before calibration - are re-judged by
   the reasoning model (:mod:`utils.escalation`); the final score is that
   one's where there is one, else Jev's.
3. **Repair.** A suspect is split out when its final P(same) is below the
   **calibrated** cut-off (``evaluation/coherence_calibration.json``, written by
   ``scripts/calibrate_coherence.py``, recorded with the models and question
   versions it was measured on). With no matching calibration nothing is
   split: the run reports what it would do and logs that prominently. Labels
   in ``evaluation/coherence_labels.csv`` are ground truth: a member labelled
   ``same`` never leaves, one labelled ``different`` always does.

Guards kept from wrong splits seen in review, applied to model-driven splits
(labels override them):

* **Brand variants** - a suspect whose organisation shares a distinctive name
  key with the event's *title* stays ("youX" in "Vroom by YouX Data Breach").
* **Mislabelled event** - when the departing members are half or more of the
  event and all name one organisation, the event's *label* is what is wrong,
  not its contents (12 youX records judged foreign to "Vroom by YouX").
  Nothing is split; the event is reported for review.
* **Supplier customers** - the question states the project rule that one
  supplier's breach reported about several customers is ONE incident
  (Canvas/Instructure, Frontier payroll / NSW Health), and the event's vendor
  counts as one of its own organisations when finding suspects.
* An event is never stripped bare.

Splits go through :meth:`DedupLedger.split_members`, so they are snapshotted,
reversible and cached as ``different`` rulings that later merge passes
respect. Strays naming the same organisation leave together as one event.
Split-off records are *not* folded into another event here: the calibrated
duplicate check (``check-duplicates``), which runs after this step, decides
that.

There is **no fallback**: if Jev or the escalation model cannot answer, the
run raises :class:`CoherenceJudgementError` rather than skip a member or ask
another model.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard, final_decisions
from cyber_data_collector.dedup.entity_resolution import EntityResolver
from cyber_data_collector.dedup.event_reality import PAGE_CHARS, ROOT, _perplexity
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

SUBJECT_TYPE = "event_member"
QUESTION_ID = "member_same_incident"
# v1 (2026-10-27, never used to split) read the stored page as the record's
# subject. Calibrated against 92 labelled suspects it reached no cut-off: at
# its lowest scores (<= 0.1) a third of the "strays" were records whose stored
# page is a *different article* - a scraping mismatch (Scouts Victoria's
# notice stored with a UTAS page, Suncorp Bank's with an HTW page) - while
# their title and research name the event's incident; the labelling
# convention, fixed before any score was seen, keeps those. v2 states that
# convention: a record is identified by its title, its own enrichment summary
# and its research; the page is supporting evidence only.
QUESTION_VERSION = "member-coherence-v2"
ESCALATION_QUESTION_ID = "member_same_incident_escalated"
ESCALATION_VERSION = "member-coherence-escalation-v2"
ESCALATE_BAND = (0.1, 0.7)
HOME_WINDOW_DAYS = 90       # model-judged strays of one organisation further apart leave separately
MAX_OTHER_RECORDS = 15
OTHER_SUMMARY_CHARS = 200
RECORD_SUMMARY_CHARS = 600
#: Bump when what a member judgement shows a model changes (which records, what of each).
EVIDENCE_VERSION = "coherence-evidence-v1"

LABELS_PATH = ROOT / "evaluation" / "coherence_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "coherence_calibration.json"

_EMPTY = {"", "none", "null", "unknown", "n/a"}


class CoherenceJudgementError(RuntimeError):
    """Jev could not judge a member; the run stops before anything is split."""


class CoherenceConfigError(RuntimeError):
    """The labels or the calibration file is missing, unreadable or empty.

    Under ``--apply`` this stops the run: without the labels, members a
    labeller marked 'same' would lose their protection, and an unreadable
    calibration is a fault, not a reason to quietly report.
    """


INSTRUCTIONS = (
    "`event` is a stored event in an Australian cyber-incident database. It should hold records "
    "about ONE real-world incident: the incident its victim suffered that its title and most of "
    "its records describe (the title is generated and can be wrong). `other_records` are the "
    "event's other records - the title, date, organisation and a short summary of each. `record` "
    "is one more of its records, under review. What the record is about is given by its `title`, "
    "its `summary` and `perplexity` - separate research about it naming the organisation, dates, "
    "attack details and sources. `page_text` is the page stored for it and is only supporting "
    "evidence: it is often a privacy policy, an index, or a DIFFERENT article stored by mistake, "
    "so a page about another incident does not make the record about that incident when its "
    "title, summary and research describe the event's incident. When title, summary and research "
    "are vague, the page and the research decide. `organisation_timeline`, when present, is "
    "researched history of the victim's publicly reported cyber incidents with dates and sources: "
    "a record matching a different incident in it is about that incident. Decide whether `record` "
    "reports the SAME real-world incident as the event. Organisation fields can be wrong - judge "
    "by what the record says happened."
)
CRITERIA = {
    "true": ("Same incident: the record reports the event's incident or follow-up about it - later "
             "coverage, a notification, regulator action, a class action, an aggregator entry or the "
             "organisation's own notice. One supplier's breach reported about one of its customers is "
             "that supplier's incident, so a customer's record belongs to the supplier's event and the "
             "supplier's record belongs to a customer's event about it. A parent company and its brand "
             "(Singtel/Optus, TPG/iiNet) are one organisation. A record whose title, summary or "
             "research points to this incident is the same incident even when its stored page is "
             "unhelpful or about something else."),
    "false": ("A different incident: another organisation's own breach or attack, a separate incident "
              "at the same organisation (a different time, system, attacker or method), or a roundup, "
              "newsletter or index item about something else."),
}


# ----------------------------------------------------------------------
# Loading events and members
# ----------------------------------------------------------------------

def _formal_name(pe: Optional[str]) -> Optional[str]:
    try:
        name = json.loads(pe or "{}").get("formal_entity_name")
    except (TypeError, ValueError, AttributeError):
        return None
    if name is None or str(name).strip().lower() in _EMPTY:
        return None
    return str(name)


def load_event_members(conn: sqlite3.Connection, dedup_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.enriched_event_id, e.title, e.event_date, COALESCE(e.summary, e.description, '') AS body,
               e.perplexity_enrichment_data AS pe, r.source_url
        FROM EventDeduplicationMap m
        JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
        WHERE m.deduplicated_event_id = ?
        ORDER BY e.event_date, e.enriched_event_id
        """, (dedup_id,)).fetchall()
    return [{"id": r[0], "title": r[1] or "", "date": r[2], "body": r[3] or "",
             "org": _formal_name(r[4]), "url": r[5]} for r in rows]


def _multi_member_events(conn: sqlite3.Connection) -> List[Tuple]:
    return conn.execute(
        """SELECT deduplicated_event_id, title, event_date, victim_organization_name, vendor_organization_name
           FROM DeduplicatedEvents d
           WHERE status = 'Active'
             AND (SELECT COUNT(*) FROM EventDeduplicationMap m
                  WHERE m.deduplicated_event_id = d.deduplicated_event_id) >= 2
           ORDER BY deduplicated_event_id""").fetchall()


def fitted_resolver(conn: sqlite3.Connection, resolver: Optional[EntityResolver] = None,
                    events: Optional[Sequence[Tuple]] = None,
                    members_by_event: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> EntityResolver:
    """A resolver fitted on every organisation name the multi-member events use."""
    resolver = resolver or EntityResolver(conn)
    events = events if events is not None else _multi_member_events(conn)
    if members_by_event is None:
        members_by_event = {e[0]: load_event_members(conn, e[0]) for e in events}
    resolver.fit([m["org"] for ms in members_by_event.values() for m in ms if m["org"]]
                 + [e[3] for e in events if e[3]] + [e[4] for e in events if e[4]])
    return resolver


def find_suspect_events(conn: sqlite3.Connection, resolver: Optional[EntityResolver] = None,
                        only: Optional[Sequence[str]] = None,
                        include: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Active multi-member events with at least one member naming another organisation.

    ``include`` adds events by id even when they hold no suspect (events with a
    labelled member), so labels apply wherever the labelled record still is.
    """
    events = _multi_member_events(conn)
    if only:
        events = [e for e in events if any(e[0].startswith(p) for p in only)]
    members_by_event = {e[0]: load_event_members(conn, e[0]) for e in events}
    resolver = fitted_resolver(conn, resolver, events, members_by_event)
    wanted = set(include or ())
    doubted = victim_in_doubt(conn)

    found = []
    for did, title, event_date, victim, vendor in events:
        members = members_by_event[did]
        odd = suspect_members(resolver, members, victim, vendor)
        # The name test vouches for a member only through the event's victim; when that victim is itself in doubt,
        # a member whose research names no organisation has nothing vouching for it (the Hürriyet report of the
        # Medicare breach sat in the OpenAI rogue-agent event, 2026-10-30).
        unidentified = [m for m in members if not m["org"]] if did in doubted else []
        if odd or unidentified or did in wanted:
            found.append({"id": did, "title": title, "date": event_date, "victim": victim,
                          "vendor": vendor, "members": members,
                          "suspect_ids": [m["id"] for m in odd] + [m["id"] for m in unidentified],
                          "unidentified_ids": [m["id"] for m in unidentified]})
    return found


def victim_in_doubt(conn: sqlite3.Connection) -> set:
    """Events whose recorded victim the calibrated attribution check flagged with nothing to replace it
    (:func:`victim_judgement.unresolved_attributions`). Empty for a database that has never been checked."""
    from cyber_data_collector.dedup.victim_judgement import unresolved_attributions

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'VictimAttributionActions'"
                        ).fetchone():
        return set()
    return {did for did, _entity in unresolved_attributions(conn)}


def suspect_members(resolver: EntityResolver, members: Sequence[Dict[str, Any]],
                    victim: Optional[str], vendor: Optional[str]) -> List[Dict[str, Any]]:
    """Members whose organisation shares no distinctive key with the victim, vendor or majority."""
    orgs = [m["org"] for m in members if m["org"]]
    if not orgs:
        return []
    majority = Counter(orgs).most_common(1)[0][0]
    refs = [n for n in (victim, vendor, majority) if n]
    # Distinctive blocking keys, not are_candidates: its name-similarity
    # fallback treats "University of Tasmania" and "University of Sydney"
    # as one organisation, hiding real strays, while a shared key keeps
    # true variants together ("Singtel Optus"/"Optus", "youX"/"Vroom by YouX").
    ref_keys = set().union(*(resolver.blocks_for(r) for r in refs))
    return [m for m in members if m["org"] and not (resolver.blocks_for(m["org"]) & ref_keys)]


# ----------------------------------------------------------------------
# What Jev sees
# ----------------------------------------------------------------------

def member_subject_id(dedup_id: str, enriched_id: str) -> str:
    return f"{dedup_id}|{enriched_id}"


def _record(conn: sqlite3.Connection, enriched_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """SELECT COALESCE(e.title, ''), e.event_date, COALESCE(r.source_url, ''),
                  COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''), e.perplexity_enrichment_data,
                  COALESCE(e.summary, e.description, '')
           FROM EnrichedEvents e LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE e.enriched_event_id = ?""", (enriched_id,)).fetchone()
    if row is None:
        raise ValueError(f"no enriched record {enriched_id}")
    return {"title": row[0], "date": row[1], "url": row[2], "summary": (row[5] or "")[:RECORD_SUMMARY_CHARS],
            "perplexity": _perplexity(row[4]), "page_text": (row[3] or "")[:PAGE_CHARS] or "(none stored)"}


def build_member_state(conn: sqlite3.Connection, event: Dict[str, Any], enriched_id: str) -> Dict[str, Any]:
    """Everything Jev sees about one member of one event. Deterministic, so the input hash is stable."""
    from cyber_data_collector.dedup.org_timeline import timeline_for_event

    others = [m for m in event["members"] if m["id"] != enriched_id]
    others = sorted(others, key=lambda m: (str(m["date"] or ""), m["id"]))[:MAX_OTHER_RECORDS]
    state: Dict[str, Any] = {
        "event": {"title": event.get("title") or "", "date": event.get("date"),
                  "victim": event.get("victim"), "vendor": event.get("vendor"),
                  "record_count": len(event["members"])},
        "other_records": [{"title": m["title"], "date": m["date"], "organisation": m["org"],
                           "summary": (m["body"] or "")[:OTHER_SUMMARY_CHARS]} for m in others],
        "record": _record(conn, enriched_id),
    }
    # Already in the judgement shape: organisation / incidents / citations, plus
    # a warning only when the history may be incomplete.
    timeline = timeline_for_event(conn, event["id"])
    if timeline:
        state["organisation_timeline"] = timeline
    return state


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def escalation_question() -> str:
    return (INSTRUCTIONS + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Does `record` report the same real-world cyber incident as the event?")


def evidence_fingerprint() -> str:
    """A hash of the evidence a member judgement sees (shared by the same-organisation variant)."""
    from cyber_data_collector.dedup import event_reality

    return calibration_guard.fingerprint(
        "coherence", version=EVIDENCE_VERSION, max_other_records=MAX_OTHER_RECORDS,
        other_summary_chars=OTHER_SUMMARY_CHARS, record_summary_chars=RECORD_SUMMARY_CHARS,
        page_chars=PAGE_CHARS, research_fields=list(event_reality.RESEARCH_FIELDS),
        reasoning_chars=event_reality.REASONING_CHARS, escalate_band=list(ESCALATE_BAND),
        **calibration_guard.shared_parts())


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply: question, escalation, both models, evidence."""
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


# ----------------------------------------------------------------------
# Judging
# ----------------------------------------------------------------------

def judge_members(conn: sqlite3.Connection, items: Sequence[Tuple[Dict[str, Any], str]], judge=None,
                  escalation_client=None, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Final P(same) for each (event, member id), escalating the middle band.

    Returns ``{subject_id: {"p_jev", "p_escalated", "p", "error"}}``. Stored
    answers are reused. A Jev failure raises :class:`CoherenceJudgementError`
    (nothing is split that run). A failed escalation leaves the member
    *undecided* - ``p`` is None and ``error`` says why - so it is neither kept
    nor split on Jev's un-escalated score; it is retried next run and the
    command exits non-zero.
    """
    from cyber_data_collector.utils.escalation import escalate
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    lo, hi = ESCALATE_BAND
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for i, (event, enriched_id) in enumerate(items, 1):
            sid = member_subject_id(event["id"], enriched_id)
            state = build_member_state(conn, event, enriched_id)
            try:
                answer = judge.ask(SUBJECT_TYPE, sid, QUESTION_VERSION, state, q, refresh=refresh)[QUESTION_ID]
            except Exception as exc:  # noqa: BLE001 - re-raised with what failed
                raise CoherenceJudgementError(f"Jev ({typesafe_model()}) could not judge member {sid}: "
                                              f"{exc}") from exc
            p_jev = float(answer.value)
            if not lo <= p_jev < hi:
                out[sid] = {"p_jev": p_jev, "p_escalated": None, "p": p_jev, "error": None}
                final_decisions.record(conn, STEP, sid, state, p_jev, escalated=False)
                continue
            p_esc = escalate(conn, SUBJECT_TYPE, sid, ESCALATION_QUESTION_ID, ESCALATION_VERSION,
                             state, escalation_question(), client=escalation_client)
            if p_esc is None:
                logger.error("UNDECIDED: escalation failed for member %s (Jev %.3f, inside %s); it is "
                             "not acted on this run", sid, p_jev, ESCALATE_BAND)
                out[sid] = {"p_jev": p_jev, "p_escalated": None, "p": None,
                            "error": "escalation failed (see log); retried next run"}
            else:
                out[sid] = {"p_jev": p_jev, "p_escalated": p_esc, "p": p_esc, "error": None}
                final_decisions.record(conn, STEP, sid, state, p_esc, escalated=True)
            if i % 25 == 0:
                logger.info("Judged %d/%d members", i, len(items))
    finally:
        conn.commit()
        if owned:
            judge.close()
    return out


STEP = final_decisions.Step(
    name="coherence", question_id=QUESTION_ID, version=QUESTION_VERSION, make_question=question,
    escalation_id=ESCALATION_QUESTION_ID, escalation_version=ESCALATION_VERSION,
    escalation_text=escalation_question, band=ESCALATE_BAND)


def stored_final_probabilities(conn: sqlite3.Connection, items: Sequence[Tuple[Dict[str, Any], str]]
                               ) -> Dict[str, float]:
    """Final score per ``(event, member id)`` from its decision on the evidence it has **now**
    (:mod:`final_decisions`); a member with no decision on its current evidence has no score here."""
    return final_decisions.read_current(
        conn, STEP, [(member_subject_id(event["id"], mid), lambda e=event, m=mid: build_member_state(conn, e, m)) for event, mid in items])


# ----------------------------------------------------------------------
# Labels and calibration
# ----------------------------------------------------------------------

LABEL_VALUES = {"same", "different", "unsure"}


def load_labels(path: Path = LABELS_PATH) -> Dict[str, str]:
    """``{subject_id: label}`` from the labels file; label is same / different / unsure.

    A label describes a record *in a particular event*; it applies while that
    record is still a member of that event. A missing, unreadable or empty
    file, a missing column or an unknown label raises
    :class:`CoherenceConfigError` - it is never read as "no labels".
    """
    return {sid: row["label"] for sid, row in _label_rows(path).items()}


def load_label_incidents(path: Path = LABELS_PATH) -> Dict[str, str]:
    """``{subject_id: incident}`` for labelled strays whose real incident is named.

    The optional ``incident`` column names the incident a 'different' record is
    actually about (transcribed from the labeller's rationale); labelled strays
    naming one incident leave their event together.
    """
    return {sid: row["incident"].strip() for sid, row in _label_rows(path).items()
            if (row.get("incident") or "").strip()}


def _label_rows(path: Path) -> Dict[str, Dict[str, str]]:
    try:
        with Path(path).open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise CoherenceConfigError(f"coherence labels {path} unreadable: {exc}") from exc
    if not rows:
        raise CoherenceConfigError(f"coherence labels {path} hold no rows")
    out: Dict[str, Dict[str, str]] = {}
    for n, r in enumerate(rows, 2):
        did, eid, label = r.get("deduplicated_event_id"), r.get("enriched_event_id"), r.get("label")
        if not did or not eid or label not in LABEL_VALUES:
            raise CoherenceConfigError(f"coherence labels {path} line {n}: need deduplicated_event_id, "
                                       f"enriched_event_id and a label in {sorted(LABEL_VALUES)}")
        out[member_subject_id(did, eid)] = r
    return out


def load_calibration(path: Path = CALIBRATION_PATH) -> Dict[str, Any]:
    """The calibration file; raises :class:`CoherenceConfigError` if missing, unreadable or empty."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoherenceConfigError(f"coherence calibration {path} unreadable: {exc}. "
                                   "Run scripts/calibrate_coherence.py.") from exc
    if not isinstance(cal, dict) or not cal.get("question_version") or not cal.get("model"):
        raise CoherenceConfigError(f"coherence calibration {path} is empty or has no model/question identity")
    return cal


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The split calibration, only if measured on the current models and questions.

    A calibration for a different model or question version, or one whose
    labels reached no cut-off meeting the precision target, turns model-judged
    splitting into a report: that rule is deliberate (an unmeasured cut-off is
    not used) and is logged at WARNING so a run that split nothing says why.
    A missing, unreadable or empty file is a fault and raises
    :class:`CoherenceConfigError`.
    """
    cal = load_calibration(path)
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning("%s Nothing will be split except labelled strays.",
                       calibration_guard.report_only_message("coherence", cal, reason))
        return None
    if cal.get("cutoff") is None:
        logger.warning("REPORT ONLY: the coherence calibration found no cut-off meeting its precision "
                       "target; nothing will be split except labelled strays.")
        return None
    from cyber_data_collector.dedup import deployment

    return deployment.gate("coherence", cal, Path(path))


# ----------------------------------------------------------------------
# Deciding
# ----------------------------------------------------------------------

def decide_strays(event: Dict[str, Any], scores: Dict[str, Dict[str, Any]], labels: Dict[str, str],
                  cutoff: Optional[float], resolver: EntityResolver,
                  incidents: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Which members of one event leave, and why.

    Returns ``{"strays": [...], "held": [...], "note": str|None}``. ``held``
    lists members the model would split but a guard keeps.
    """
    members = {m["id"]: m for m in event["members"]}
    suspects = set(event.get("suspect_ids") or [])
    # Members suspected only because the victim is in doubt are not the population the cut-off was calibrated on
    # (members naming another organisation): they are judged and reported, and split by label alone.
    unidentified = set(event.get("unidentified_ids") or [])
    labelled_out, model_out, held = [], [], []
    for mid, m in members.items():
        sid = member_subject_id(event["id"], mid)
        label = labels.get(sid)
        score = scores.get(sid) or {}
        entry = {**m, "subject_id": sid, "p_same": score.get("p"), "p_jev": score.get("p_jev"),
                 "p_escalated": score.get("p_escalated"), "label": label,
                 "incident": (incidents or {}).get(sid) if label == "different" else None}
        if label == "different":
            labelled_out.append({**entry, "basis": "label"})
        elif label == "same":
            continue
        elif (mid in suspects and cutoff is not None and score.get("p") is not None
              and score["p"] < cutoff):
            if mid in unidentified:
                held.append({**entry, "basis": "model", "guard": "victim in doubt and no organisation identified: "
                             "outside the calibrated suspects, split by label only"})
            else:
                model_out.append({**entry, "basis": "model"})

    note = None
    undecided = [mid for mid in suspects
                 if (scores.get(member_subject_id(event["id"], mid)) or {}).get("error")
                 and labels.get(member_subject_id(event["id"], mid)) is None]
    if undecided and model_out:
        # The guards weigh the whole departing set, so with a member undecided
        # no model-driven split in this event is final; retry next run.
        note = f"{len(undecided)} member(s) undecided (escalation failed): model-judged splits deferred"
        held.extend({**s, "guard": "event has an undecided member"} for s in model_out)
        model_out = []
    title_keys = resolver.blocks_for(event.get("title"))
    kept_model = []
    for s in model_out:
        if title_keys and resolver.blocks_for(s["org"]) & title_keys:
            held.append({**s, "guard": "organisation named in the event title"})
        else:
            kept_model.append(s)
    model_out = kept_model

    total = len(members)
    departing = labelled_out + model_out
    if model_out and len(departing) * 2 >= total:
        keys = [frozenset(resolver.blocks_for(s["org"])) for s in departing]
        first = keys[0]
        if first and all(k & first for k in keys):
            note = "mislabelled rather than contaminated: most members leave and all name one organisation"
            held.extend({**s, "guard": "mislabelled event"} for s in model_out)
            model_out = []
    if len(labelled_out) + len(model_out) >= total:
        held.extend({**s, "guard": "would leave the event with no records"} for s in model_out)
        model_out = []
        if len(labelled_out) >= total:
            logger.warning("Every member of %s is labelled 'different'; nothing split", event["id"])
            held.extend({**s, "guard": "every member labelled different"} for s in labelled_out)
            labelled_out = []
    return {"strays": labelled_out + model_out, "held": held, "note": note}


def _date(value: Any):
    from datetime import date
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def group_strays(strays: Sequence[Dict[str, Any]], resolver: EntityResolver) -> List[List[Dict[str, Any]]]:
    """Strays about one incident leave together as one event.

    Labelled strays group by the incident their label names. Model-judged
    strays group only when they share a distinctive organisation key AND lie
    within ``HOME_WINDOW_DAYS`` of each other: one organisation can have two
    incidents in one event (the University of Sydney's December 2025 code
    library breach and its 2026 Canvas exposure, both inside a UTas event).
    Groups that leave separately get no 'different' ruling between them, so a
    later merge pass (``check-duplicates``) can still join them; strays in one
    group cannot be re-separated without a ruling, hence the conservative rule.
    """
    by_incident: Dict[str, List[Dict[str, Any]]] = {}
    groups: List[Tuple[set, List[Dict[str, Any]]]] = []
    for s in strays:
        if s.get("incident"):
            by_incident.setdefault(s["incident"].strip().lower(), []).append(s)
            continue
        keys = resolver.blocks_for(s["org"]) if s["org"] else set()
        when = _date(s.get("date"))
        for gkeys, members in groups:
            near = all(when is None or _date(m.get("date")) is None
                       or abs((when - _date(m.get("date"))).days) <= HOME_WINDOW_DAYS for m in members)
            if keys and gkeys & keys and near:
                gkeys |= keys
                members.append(s)
                break
        else:
            groups.append((set(keys), [s]))
    return list(by_incident.values()) + [members for _, members in groups]


def check_coherence(conn: sqlite3.Connection, apply: bool = False, judge=None, escalation_client=None,
                    only: Optional[Sequence[str]] = None, calibration_path: Path = CALIBRATION_PATH,
                    labels_path: Path = LABELS_PATH, ledger=None) -> Dict[str, Any]:
    """Judge every suspect member, and split out strays below the calibrated cut-off.

    With a calibration for other models or another question version, only
    labelled strays are split; without ``apply`` nothing is split. With
    ``apply``, a missing, unreadable or empty labels or calibration file
    raises :class:`CoherenceConfigError` before anything is judged. A Jev
    failure raises :class:`CoherenceJudgementError`; a failed escalation
    leaves the member undecided (``undecided`` in the result).
    """
    labels: Dict[str, str] = {}
    incidents: Dict[str, str] = {}
    cal: Optional[Dict[str, Any]] = None
    for load in ("labels", "calibration"):
        try:
            if load == "labels":
                labels = load_labels(labels_path)
                incidents = load_label_incidents(labels_path)
            else:
                cal = active_calibration(calibration_path)
        except CoherenceConfigError as exc:
            if apply:
                raise
            logger.warning("REPORT ONLY (no --apply): %s", exc)
    cutoff = cal["cutoff"] if cal else None
    labelled_events = {sid.split("|", 1)[0] for sid in labels}
    resolver = EntityResolver(conn)
    events = find_suspect_events(conn, resolver, only=only, include=labelled_events)
    # Labels decide first: a member labelled same never leaves and one labelled different always does, whatever
    # the model says, so it is not (re-)asked about.
    items = [(e, mid) for e in events for mid in e["suspect_ids"]
             if labels.get(member_subject_id(e["id"], mid)) not in ("same", "different")]
    scores = judge_members(conn, items, judge=judge, escalation_client=escalation_client)
    undecided = sorted(sid for sid, s in scores.items() if s.get("error"))

    findings, splits = [], []
    for event in events:
        decision = decide_strays(event, scores, labels, cutoff, resolver, incidents)
        if not decision["strays"] and not decision["held"]:
            continue
        groups = group_strays(decision["strays"], resolver)
        entry = {"event": event["id"], "title": event["title"], "victim": event["victim"],
                 "records": len(event["members"]), "note": decision["note"],
                 "groups": [[_public(s) for s in g] for g in groups],
                 "held": [_public(s) for s in decision["held"]], "new_events": []}
        if apply and groups:
            entry["new_events"] = split_groups(conn, event["id"], groups, ledger=ledger)
            splits.extend(entry["new_events"])
        findings.append(entry)
    return {"events_judged": len(events), "members_judged": len(scores),
            "settled_by_label": sum(len(e["suspect_ids"]) for e in events) - len(items), "undecided": undecided,
            "escalated": sum(1 for s in scores.values() if s["p_escalated"] is not None),
            "calibrated": cal is not None, "cutoff": cutoff, "applied": bool(apply and splits),
            "strays": sum(len(g) for f in findings for g in f["groups"]),
            "events_with_strays": sum(1 for f in findings if f["groups"]),
            "split_records": sum(len(s["records"]) for s in splits), "findings": findings,
            "scores": scores}


def _public(s: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("id", "title", "date", "org", "url", "p_same", "p_jev", "p_escalated", "label", "incident",
            "basis", "guard")
    return {k: s.get(k) for k in keep if k in s}


def split_groups(conn: sqlite3.Connection, dedup_id: str, groups: Sequence[Sequence[Dict[str, Any]]],
                 ledger=None) -> List[Dict[str, Any]]:
    """Split each group out of ``dedup_id`` with the ledger (snapshotted, rulings cached)."""
    from cyber_data_collector.dedup.ledger import DedupLedger

    previous = conn.row_factory
    conn.row_factory = sqlite3.Row      # the ledger reads columns by name
    try:
        ledger = ledger or DedupLedger(conn)
        out = []
        for group in groups:
            ids = [s["id"] for s in group]
            if any(s.get("basis") == "label" for s in group):
                why = "labelled a different incident"
            else:
                why = (f"P(same) {max(s['p_same'] for s in group):.3f} below the calibrated cut-off "
                       f"({QUESTION_VERSION})")
            about = group[0].get("incident") or ", ".join(sorted({s["org"] for s in group if s["org"]}))
            reason = f"[coherence] stray record(s) about {about or 'another incident'}: {why}"
            new_id = ledger.split_members(dedup_id, ids, reason=reason, actor="rule")
            out.append({"new_event": new_id, "records": ids})
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous
