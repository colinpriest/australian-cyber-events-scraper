"""Persisted evidence and per-record acceptance.

Two defects shared one cause: stages read different copies of a record.

* Scraping wrote the fetched article to ``RawEvents.raw_content``, but
  classification was handed the in-memory object built at discovery time, so it
  judged the search-result *snippet*.  A Google snippet that quoted a site menu
  ("Cyber incident") or a privacy policy's data-breach clause became an
  incident.
* A filter that rejected a record only appended it to a "failed scrapes" list;
  the same in-memory object carried on into classification and storage.

This module is the single path both stages now go through:

* :func:`load_record_evidence` reads what is *persisted* for a record and says
  explicitly whether that is an article, only a snippet, or nothing.
* :class:`AcceptanceLedger` holds one accept/reject decision per record.  A
  rejection is persisted (``RawEvents.is_processed`` + a ``Rejected at <stage>``
  marker) so it also holds on a later run and in the other pipeline entry
  points.
* :func:`prepare_records_for_processing` applies both to a batch of in-memory
  events, dropping rejected ones and attaching the persisted evidence to the
  rest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Scraped text shorter than this is not treated as the article.  Scraping
#: itself accepts anything over 50 characters; that is enough to call a fetch
#: "successful" but not enough to judge an incident from.
MIN_ARTICLE_CHARS = 200

BASIS_ARTICLE = "article"
BASIS_SNIPPET_ONLY = "snippet_only"
BASIS_NONE = "none"
#: Set on events that never went through :func:`prepare_records_for_processing`.
BASIS_UNSCRAPED = "unscraped"

REJECTION_PREFIX = "Rejected at "

# Stages whose rejection is final.  An LLM rejection is deliberately not in
# the persisted-rejection set: it may have been made on a bare snippet, and
# the record deserves another look once scraping succeeds.
STAGE_DOMAIN_BLOCKLIST = "domain_blocklist"
#: The calibrated Jev page gate (``filtering/page_gate.py``).
STAGE_PAGE_GATE = "page_gate"
#: Historical: the retired Random Forest filter.  Markers with these stages
#: may exist in older databases and are still honoured.
STAGE_RF_URL_PREFILTER = "rf_url_prefilter"
STAGE_RF_CONTENT_FILTER = "rf_content_filter"
STAGE_NO_RECORD = "no_persisted_record"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordEvidence:
    """What is actually persisted for a record, and how much to trust it.

    Attributes:
        basis: ``article`` (usable scraped text), ``snippet_only`` (only the
            discovery snippet/summary), or ``none``.
        article_text: The scraped article when ``basis == 'article'``.
        snippet: The discovery description (a search snippet for Google).
        reason: Human-readable account of why this basis was chosen.
        rejection: ``(stage, reason)`` if the record was rejected earlier.
    """

    raw_event_id: Optional[str]
    title: str
    url: Optional[str]
    source_type: Optional[str]
    basis: str
    article_text: Optional[str]
    snippet: Optional[str]
    reason: str
    discovered_at: Optional[str] = None
    rejection: Optional[Tuple[str, str]] = None

    @property
    def has_article(self) -> bool:
        return self.basis == BASIS_ARTICLE

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RecordEvidence":
        """Build evidence from a ``RawEvents`` row (or a join that includes it).

        Accepts the ``raw_*`` column names, and the unprefixed ``title`` /
        ``description`` / ``content`` aliases used by joined queries.
        """
        def pick(*keys: str) -> Optional[str]:
            for key in keys:
                try:
                    value = row[key]
                except (KeyError, IndexError):
                    continue
                if value is not None:
                    return str(value)
            return None

        content = (pick("raw_content") or "").strip()
        description = (pick("raw_description") or "").strip()
        if len(content) >= MIN_ARTICLE_CHARS:
            basis, article = BASIS_ARTICLE, content
            snippet = description or None
            reason = f"scraped article text ({len(content)} chars)"
        elif content:
            # Fetched, but too little to judge from: say so, keep it as snippet.
            basis, article = BASIS_SNIPPET_ONLY, None
            snippet = description or content
            reason = f"stored article text too short ({len(content)} chars < {MIN_ARTICLE_CHARS})"
        elif description:
            basis, article, snippet = BASIS_SNIPPET_ONLY, None, description
            reason = "no article text stored (scraping failed or not attempted)"
        else:
            basis, article, snippet = BASIS_NONE, None, None
            reason = "no text stored for this record"

        return cls(
            raw_event_id=pick("raw_event_id"),
            title=pick("raw_title", "title") or "",
            url=pick("source_url"),
            source_type=pick("source_type"),
            basis=basis,
            article_text=article,
            snippet=snippet,
            reason=reason,
            discovered_at=pick("discovered_at"),
            rejection=parse_rejection(pick("processing_error"), pick("is_processed")),
        )

    def match_text(self) -> str:
        """The text a fact must be found in when no source is cited."""
        return "\n".join(part for part in (self.title, self.article_text or self.snippet) if part)

    def prompt_block(self, max_chars: int = 4000) -> str:
        """Render the evidence for an LLM prompt, labelled by what it is."""
        if self.has_article and self.article_text:
            text = self.article_text[:max_chars]
            return (
                f"ARTICLE TEXT (scraped from {self.url or 'the source page'}; "
                f"judge the incident from this text):\n{text}"
            )
        snippet = (self.snippet or "")[:max_chars]
        return (
            "NO ARTICLE TEXT WAS RETRIEVED for this record "
            f"({self.reason}). The only text available is a discovery snippet. "
            "Snippets often quote site navigation or boilerplate (a 'Cyber incident' "
            "menu link, a privacy policy's data-breach clause) rather than describing "
            "an incident - do not treat that as evidence.\n"
            f"SNIPPET: {snippet if snippet else '(none)'}"
        )


def load_record_evidence(db: Any, raw_event_id: str) -> Optional[RecordEvidence]:
    """Read the persisted evidence for one record.

    Args:
        db: A ``CyberEventDataV2`` (uses its lock and connection).
        raw_event_id: The record to load.

    Returns:
        The evidence, or None if the record is not in the database.
    """
    with db._lock:
        cursor = db._conn.cursor()
        cursor.execute(
            "SELECT raw_event_id, source_type, raw_title, raw_description, raw_content, "
            "source_url, CAST(discovered_at AS TEXT) AS discovered_at, is_processed, processing_error "
            "FROM RawEvents WHERE raw_event_id = ?",
            (raw_event_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return RecordEvidence.from_row(dict(row))


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordAcceptance:
    """One record's explicit accept/reject decision."""

    raw_event_id: str
    is_accepted: bool
    stage: str = ""
    reason: str = ""


def format_rejection(stage: str, reason: str) -> str:
    """The marker stored in ``RawEvents.processing_error`` for a rejection."""
    return f"{REJECTION_PREFIX}{stage}: {reason}"


def parse_rejection(
    processing_error: Optional[str],
    is_processed: Optional[Any] = None,
) -> Optional[Tuple[str, str]]:
    """Recover ``(stage, reason)`` from a stored rejection marker, else None."""
    if not processing_error or not processing_error.startswith(REJECTION_PREFIX):
        return None
    if is_processed is not None and str(is_processed).lower() in ("0", "false", ""):
        # Cleared by someone re-queueing the record; the marker is stale.
        return None
    stage, _, reason = processing_error[len(REJECTION_PREFIX):].partition(": ")
    return stage, reason


class AcceptanceLedger:
    """Collects one decision per record; downstream stages consult it.

    The first rejection wins so the reason recorded is the earliest stage that
    stopped the record.
    """

    def __init__(self) -> None:
        self._rejected: Dict[str, RecordAcceptance] = {}
        self._already_persisted: set = set()

    def reject(
        self,
        raw_event_id: str,
        stage: str,
        reason: str,
        already_persisted: bool = False,
    ) -> RecordAcceptance:
        """Record a rejection.

        Args:
            already_persisted: True when the rejection was read back from the
                database, so :func:`persist_rejections` must not write it again.
        """
        existing = self._rejected.get(raw_event_id)
        if existing is not None:
            return existing
        decision = RecordAcceptance(raw_event_id, False, stage, reason)
        self._rejected[raw_event_id] = decision
        if already_persisted:
            self._already_persisted.add(raw_event_id)
        return decision

    def get(self, raw_event_id: str) -> RecordAcceptance:
        return self._rejected.get(raw_event_id) or RecordAcceptance(raw_event_id, True)

    def is_rejected(self, raw_event_id: Optional[str]) -> bool:
        return raw_event_id is not None and raw_event_id in self._rejected

    def rejected(self) -> List[RecordAcceptance]:
        return list(self._rejected.values())

    def unpersisted(self) -> List[RecordAcceptance]:
        """Rejections made this run that are not yet in the database."""
        return [d for d in self._rejected.values() if d.raw_event_id not in self._already_persisted]

    def __len__(self) -> int:
        return len(self._rejected)


def persist_rejections(db: Any, ledger: AcceptanceLedger) -> int:
    """Write every rejection in *ledger* to the database.

    Marks the raw event processed with a ``Rejected at <stage>`` marker, which
    every queue query (``is_processed = FALSE``) already honours, and logs the
    decision to ``ProcessingLog``.  Rejections that were read back from the
    database are skipped.  Returns the number persisted.
    """
    count = 0
    for decision in ledger.unpersisted():
        try:
            db.mark_raw_event_processed(
                decision.raw_event_id,
                error_message=format_rejection(decision.stage, decision.reason),
            )
            db.log_processing_attempt(
                decision.raw_event_id, "acceptance", "rejected",
                result_data={"stage": decision.stage, "reason": decision.reason},
                error_message=decision.reason,
            )
            count += 1
        except Exception as exc:  # a failed audit write must not stop the batch
            logger.warning("Could not persist rejection for %s: %s", decision.raw_event_id, exc)
    return count


# ---------------------------------------------------------------------------
# The single path
# ---------------------------------------------------------------------------

def prepare_records_for_processing(
    db: Any,
    events: Iterable[Any],
    ledger: AcceptanceLedger,
) -> List[Any]:
    """Drop rejected records and attach persisted evidence to the rest.

    Call this after scraping and before classification/entity extraction.  The
    returned events are copies carrying ``evidence_basis`` and
    ``evidence_text`` read from the database, so a stage cannot see the
    pre-scrape snippet as if it were the article.

    Args:
        db: A ``CyberEventDataV2``.
        events: In-memory ``CyberEvent`` objects with ``raw_event_id`` set.
        ledger: Decisions made so far this run (updated with any rejection
            discovered here, e.g. a persisted one from an earlier run).

    Returns:
        The events that should continue.
    """
    accepted: List[Any] = []
    for event in events:
        raw_event_id = getattr(event, "raw_event_id", None)
        if not raw_event_id:
            # Storing it failed, so there is no persisted record to enrich.
            logger.warning("Skipping '%s': it was never stored", str(getattr(event, "title", ""))[:60])
            continue
        if ledger.is_rejected(raw_event_id):
            continue
        evidence = load_record_evidence(db, raw_event_id)
        if evidence is None:
            ledger.reject(raw_event_id, STAGE_NO_RECORD, "raw event not found in the database")
            continue
        if evidence.rejection is not None:
            stage, reason = evidence.rejection
            ledger.reject(raw_event_id, stage, reason, already_persisted=True)
            continue
        prepared = event.model_copy(deep=True)
        prepared.evidence_basis = evidence.basis
        prepared.evidence_text = evidence.article_text
        accepted.append(prepared)
    return accepted
