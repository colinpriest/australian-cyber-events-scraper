"""Is a stored name the proper name of one organisation, or a description of one? A calibrated judgement.

49 of the 925 organisations recorded as the victim of an active event on
2026-10-29 were not names at all: "government agency in Australia",
"Australian health", "Australian Government Department", "gym website",
"unknown commercial entity". A description matches nothing, groups unrelated
events and - being the victim - keeps the real organisation from ever being
considered. Neither capitalisation nor vocabulary separates them from names
("Services Australia" and "Australian health service website" are built from
the same words; auDA and iiNet are lowercase), and a word list is exactly the
brittle rule this project avoids, so the question is put to Jev.

**The judgement.** For a name and the sentence it came from (the title of an
event that names it, or the words around it in the text), Jev gives P(this
identifies one specific organisation that could be looked up) - a Noul,
``is_proper_name``, stored in ``ModelJudgements`` by input hash like every
other answer and never re-asked while the name, its sentence, the question and
the model are unchanged.

**Acting** needs a calibration (``evaluation/name_calibration.json``, from
``scripts/calibrate_names.py`` on blind labels ``evaluation/name_labels.csv``:
``named`` / ``descriptive`` / ``unsure``) measured on the current model and
question: a name scoring below the calibrated cut-off is *descriptive*. Without
one nothing is treated as descriptive and the log says so - only a name
labelled ``descriptive`` is, because labels are ground truth (and one labelled
``named`` never is). There is no structural pre-filter and no word list.

**Where it is used.** :func:`is_descriptive_name` answers from the verdicts
loaded by :func:`activate` (every ``dedup_admin.py`` command loads them when it
opens the database), so the many places that ask "can this be a victim?" need no
connection or model: a name not yet judged is treated as a name, and
``judge-names`` (a hygiene step, before the victim steps) judges what is new.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup import calibration_guard
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "is_proper_name"
QUESTION_VERSION = "proper-name-v1"
#: Bump when the sentence a name is shown with, or the population judged, changes what a verdict means.
EVIDENCE_VERSION = "proper-name-evidence-v1"
SUBJECT_TYPE = "organisation_name"

ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = ROOT / "evaluation" / "name_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "name_calibration.json"

INSTRUCTIONS = (
    "`name` is how an organisation was written in coverage of an Australian cyber incident, and `sentence` is "
    "where it appeared. Decide whether `name` identifies ONE specific organisation that could be looked up: "
    "its legal, trading or common name, an acronym, or the official title of a department, agency or body. "
    "It does NOT if it only describes a kind of organisation, a sector, a group or an unnamed organisation "
    "(for example 'a regional hospital', 'online retailer', 'a government agency', 'the company'). "
    "Capitalisation is no evidence either way: a description can be written in capitals, and real names are "
    "sometimes lowercase or one word. A name that identifies one organisation counts even when it is little "
    "known, and a generic-sounding official title ('Department of Home Affairs') counts when it is the body's "
    "actual name."
)
CRITERIA = {
    "true": "The name identifies one specific organisation.",
    "false": "The name only describes a kind of organisation, a sector, a group or an unnamed organisation.",
}


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def evidence_fingerprint() -> str:
    return calibration_guard.fingerprint("proper-name", version=EVIDENCE_VERSION)


def calibration_identity() -> Dict[str, str]:
    return {"question_version": QUESTION_VERSION, "model": typesafe_model(), "evidence": evidence_fingerprint()}


# ---------------------------------------------------------------- the sentence a name came from

def sentence_for(conn: sqlite3.Connection, name: str) -> str:
    """The title of the first active event (by date) that names ``name``: the sentence it came from.

    Deterministic, so the judgement's input hash is stable. Empty when no event names it.
    """
    row = conn.execute(
        """SELECT d.title FROM DeduplicatedEvents d
           WHERE d.status = 'Active' AND (
                 d.victim_organization_name = ? COLLATE NOCASE
              OR d.deduplicated_event_id IN (
                   SELECT dee.deduplicated_event_id FROM DeduplicatedEventEntities dee
                   JOIN EntitiesV2 v ON v.entity_id = dee.entity_id WHERE v.entity_name = ? COLLATE NOCASE)
              OR d.deduplicated_event_id IN (
                   SELECT m.deduplicated_event_id FROM EventDeduplicationMap m
                   JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
                   JOIN EntitiesV2 v ON v.entity_id = ee.entity_id WHERE v.entity_name = ? COLLATE NOCASE))
           ORDER BY d.event_date, d.deduplicated_event_id LIMIT 1""", (name, name, name)).fetchone()
    return (row[0] or "") if row else ""


# ---------------------------------------------------------------- judging

def judge_names(conn: sqlite3.Connection, items: Sequence[Tuple[str, str]], judge=None) -> Dict[str, float]:
    """P(proper name) for each ``(name, sentence)``; stored answers are reused. Raises on a Jev failure."""
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    try:
        for i, (name, sentence) in enumerate(items, 1):
            answer = judge.ask(SUBJECT_TYPE, name, QUESTION_VERSION, {"name": name, "sentence": sentence}, q)[QUESTION_ID]
            out[name] = float(answer.value)
            if i % 100 == 0:
                logger.info("Judged %d/%d names", i, len(items))
    finally:
        if owned:
            judge.close()
    return out


def stored_probabilities(conn: sqlite3.Connection) -> Dict[str, float]:
    """The latest stored P(proper name) per name (lowercase), for the current question and model."""
    try:
        rows = conn.execute(
            "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
            "AND model_requested = ? ORDER BY rowid", (QUESTION_ID, QUESTION_VERSION, typesafe_model()))
        return {sid.strip().lower(): float(v) for sid, v in rows}
    except sqlite3.OperationalError:
        return {}


def names_to_judge(conn: sqlite3.Connection) -> List[str]:
    """Names that can matter to a victim decision and have no verdict yet, in a stable order.

    * every name recorded as a victim, role or scalar, of an active event;
    * every organisation linked to an active event that has no victim role (the candidates for it);
    * every name a stored text extraction (:mod:`event_organisations`) offered as a possible victim.
    """
    names: Dict[str, str] = {}

    def add(raw: Any) -> None:
        text = (raw or "").strip() if isinstance(raw, str) else ""
        if text:
            names.setdefault(text.lower(), text)

    for (n,) in conn.execute(
            """SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE dee.relationship_type = 'victim' AND d.status = 'Active'"""):
        add(n)
    for (n,) in conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE status = 'Active'"):
        add(n)
    for (n,) in conn.execute(
            """SELECT v.entity_name FROM DeduplicatedEvents d
               JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
               JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
               JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
               WHERE d.status = 'Active'
                 AND COALESCE(v.entity_kind, 'organisation') IN ('organisation', 'government_body')
                 AND COALESCE(ee.relationship_type, '') NOT IN ('threat_actor', 'product')
                 AND NOT EXISTS (SELECT 1 FROM DeduplicatedEventEntities x
                                 WHERE x.deduplicated_event_id = d.deduplicated_event_id
                                   AND x.relationship_type = 'victim')"""):
        add(n)
    try:
        for (answer,) in conn.execute("SELECT answer FROM ModelJudgements WHERE question_id = 'event_named_organisations'"):
            try:
                for n in json.loads(answer).get("possible_victims", []):
                    add(n)
            except (TypeError, ValueError, AttributeError):
                continue
    except sqlite3.OperationalError:
        pass
    done = stored_probabilities(conn)
    return [n for key, n in sorted(names.items()) if key not in done]


def judge_new_names(conn: sqlite3.Connection, judge=None, limit: Optional[int] = None) -> Dict[str, float]:
    """Judge every name in :func:`names_to_judge` (each with the sentence it came from)."""
    todo = names_to_judge(conn)
    if limit:
        todo = todo[:limit]
    return judge_names(conn, [(n, sentence_for(conn, n)) for n in todo], judge=judge)


# ---------------------------------------------------------------- labels, calibration

def load_labels(path: Path = LABELS_PATH) -> Dict[str, Dict[str, str]]:
    """Labels by lowercase name; an absent file is no labels (acting on verdicts is gated by the calibration)."""
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            return {r["name"].strip().lower(): r for r in csv.DictReader(fh) if r.get("name")}
    except (OSError, csv.Error, KeyError):
        return {}


def active_calibration(path: Path = CALIBRATION_PATH, quiet: bool = False) -> Optional[Dict[str, Any]]:
    """The calibration, only if measured on the current model, question and evidence (else logged, None).

    ``quiet`` logs the reason at INFO: every command loads the verdicts, and a missing calibration is
    reported at WARNING by the commands that depend on it (``judge-names``, ``fill-victims``).
    """
    log = logger.info if quiet else logger.warning
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("No proper-name calibration (%s): descriptive names are recognised from labels only", exc)
        return None
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        log("PROPER-NAME JUDGEMENT IS REPORT ONLY. %s",
            calibration_guard.report_only_message("proper name", cal, reason))
        return None
    if cal.get("cutoff") is None:
        log("PROPER-NAME JUDGEMENT IS REPORT ONLY: no cut-off reached the precision target")
        return None
    from cyber_data_collector.dedup import deployment

    return deployment.gate("proper-name", cal, Path(path), quiet=quiet)


# ---------------------------------------------------------------- the loaded verdicts

_DESCRIPTIVE: Set[str] = set()


def descriptive_names(conn: sqlite3.Connection, calibration_path: Path = CALIBRATION_PATH,
                      labels_path: Path = LABELS_PATH, quiet: bool = True) -> Set[str]:
    """Lowercase names treated as descriptive: labelled so, or scored below the calibrated cut-off.

    A name labelled ``named`` never is, whatever it scored.
    """
    labels = load_labels(labels_path)
    found = {n for n, r in labels.items() if (r.get("label") or "").strip() == "descriptive"}
    cal = active_calibration(calibration_path, quiet=quiet)
    if cal:
        found |= {n for n, p in stored_probabilities(conn).items()
                  if p < cal["cutoff"] and (labels.get(n, {}).get("label") or "").strip() != "named"}
    return found


def activate(conn: sqlite3.Connection, calibration_path: Path = CALIBRATION_PATH,
             labels_path: Path = LABELS_PATH) -> int:
    """Load the descriptive names for :func:`is_descriptive_name`; returns how many there are."""
    global _DESCRIPTIVE
    _DESCRIPTIVE = descriptive_names(conn, calibration_path, labels_path)
    logger.info("Proper-name verdicts loaded: %d name(s) treated as descriptive", len(_DESCRIPTIVE))
    return len(_DESCRIPTIVE)


def deactivate() -> None:
    global _DESCRIPTIVE
    _DESCRIPTIVE = set()


def is_descriptive_name(name: Optional[str]) -> bool:
    """True when the loaded verdicts say ``name`` describes an organisation instead of naming one.

    False for a name not judged, and for everything when no verdicts are loaded: nothing is decided here
    without a judgement behind it.
    """
    return bool(name) and name.strip().lower() in _DESCRIPTIVE
