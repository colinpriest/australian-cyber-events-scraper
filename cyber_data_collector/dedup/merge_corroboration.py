"""Incident-specific corroboration for the deterministic same-day merge.

The deterministic rule "same organisation on the same exact day" merged records
whose descriptions could concern different attacks: a shared day is a strong
*candidate* signal, not evidence that two records describe one incident - dates
are often publication dates, and a roundup or a follow-up about a second attack
carries the same day. The rule now merges automatically only when the two sides
agree on something that is specific to the incident, and defers everything else
to the calibrated ``check-duplicates`` judgement (which already pairs every two
events of one organisation, so a deferred pair is judged with both events'
evidence, date provenance and the organisation's timeline).

Agreements (any one is enough):

* **shared article** - both cite the same article URL, ignoring hub URLs
  (roundups, breach lists and section indexes cited by more than
  :data:`candidates.HUB_MAX_EVENTS` events: a list page cites many incidents);
* **record count** - both state how many records were exposed, at least
  :data:`COUNT_FLOOR` (small counts coincide by chance), within
  :data:`COUNT_AGREE_RATIO` of each other;
* **attacker** - both name an attacker and one name, without its qualifiers,
  appears whole in the other;
* **attack method** - both describe the method and share at least
  :data:`METHOD_MIN_SHARED` words, one of them *distinctive in the corpus* (at
  most :data:`METHOD_MAX_SHARE` of events use it; "data", "breach", "access"
  and "ransomware" are not). The distinctiveness is measured, not listed, so it
  needs the corpus (:meth:`CorpusStats.from_events`); without one this signal
  is silent.

Conflicts (any one, with no shared article, defers the pair): both sides state
a count of at least :data:`COUNT_FLOOR` and every pairing differs by more than
a factor of ``1 / COUNT_CONFLICT_RATIO``, or both name an attacker and the names
share no word. Attributions and counts are noisy across reports of one incident
(a global total beside an Australian one, two groups blamed for one breach), so
a conflict is a reason to ask the judgement, never a verdict.

A side may be an event of several records: every fact is a set, an agreement is
any pairing that agrees, and a conflict needs every pairing to disagree.

The thresholds are structural, not fitted: they are not calibrated cut-offs on
labels. They only ever remove automatic merges (an agreement is required on top
of the shared day), and ``scripts/evaluate_dedup.py`` measures what they change.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup import candidates

#: Bumped whenever the signals or their thresholds change; recorded with deferred pairs.
RULES_VERSION = "merge-corroboration-v1"

COUNT_FLOOR = 100
COUNT_AGREE_RATIO = 0.75
COUNT_CONFLICT_RATIO = 0.5
METHOD_MAX_SHARE = 0.02
METHOD_MIN_ABSOLUTE = 5
METHOD_MIN_SHARED = 2

CORROBORATED = "corroborated"
CONFLICT = "conflict"
UNCORROBORATED = "uncorroborated"

#: Values an extraction writes when it found nothing; a placeholder is not a name.
_NO_VALUE = frozenset({"none", "null", "nil", "n/a", "na", "unknown", "unidentified", "unnamed",
                       "not specified", "not available", "not known"})
_NO_VALUE_LEAD = frozenset({"unknown", "unidentified", "unnamed", "none"})
_WORD = re.compile(r"[a-z0-9$]+")
_PARENTHETICAL = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def _words(text: Optional[str]) -> List[str]:
    return _WORD.findall((text or "").lower())


def _has_value(text: Optional[str]) -> bool:
    value = (text or "").strip().lower()
    if not value or value in _NO_VALUE:
        return False
    words = _words(value)
    return bool(words) and words[0] not in _NO_VALUE_LEAD


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


@dataclass(frozen=True)
class Corroboration:
    """What two sides agree and disagree on, beyond sharing an organisation and a day."""

    agreements: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        if "shared article" in self.agreements:
            return CORROBORATED
        if self.conflicts:
            return CONFLICT
        return CORROBORATED if self.agreements else UNCORROBORATED

    def describe(self) -> str:
        parts = []
        if self.agreements:
            parts.append("agree on " + ", ".join(self.agreements))
        if self.conflicts:
            parts.append("conflict on " + ", ".join(self.conflicts))
        return "; ".join(parts) or "nothing beyond the shared day"


@dataclass
class CorpusStats:
    """What is common across the events being deduplicated, measured not listed."""

    hub_urls: Set[str] = field(default_factory=set)
    method_word_events: Counter = field(default_factory=Counter)
    method_events: int = 0

    @classmethod
    def from_events(cls, events: Sequence[Any]) -> "CorpusStats":
        events_by_url = {}
        stats = cls()
        for event in events:
            seen: Set[str] = set()
            for url in _as_list(getattr(event, "urls", None)):
                norm = candidates.normalise_url(url)
                if norm and norm not in seen:
                    seen.add(norm)
                    events_by_url[norm] = events_by_url.get(norm, 0) + 1
            words = set()
            for method in _as_list(getattr(event, "attack_methods", None)):
                if _has_value(method):
                    words.update(w for w in _words(method) if len(w) >= 3)
            if words:
                stats.method_events += 1
                stats.method_word_events.update(words)
        stats.hub_urls = {u for u, n in events_by_url.items() if n > candidates.HUB_MAX_EVENTS}
        return stats

    def is_distinctive_method_word(self, word: str) -> bool:
        ceiling = max(METHOD_MIN_ABSOLUTE, int(self.method_events * METHOD_MAX_SHARE))
        return self.method_word_events.get(word, 0) <= ceiling


def _urls(event: Any, corpus: CorpusStats) -> Set[str]:
    out = set()
    for url in _as_list(getattr(event, "urls", None)):
        norm = candidates.normalise_url(url)
        if norm and norm not in corpus.hub_urls:
            out.add(norm)
    return out


def _counts(event: Any) -> List[int]:
    values = [getattr(event, "records_affected", None)] + _as_list(getattr(event, "record_counts", None))
    return sorted({int(v) for v in values if isinstance(v, (int, float)) and v >= COUNT_FLOOR})


def _actors(event: Any) -> List[str]:
    return [a.strip() for a in _as_list(getattr(event, "threat_actors", None))
            if isinstance(a, str) and _has_value(a)]


def _methods(event: Any) -> List[str]:
    return [m for m in _as_list(getattr(event, "attack_methods", None)) if isinstance(m, str) and _has_value(m)]


def _contains_sequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(list(haystack[i:i + n]) == list(needle) for i in range(len(haystack) - n + 1))


def _actor_agree(left: str, right: str) -> bool:
    """One name, without its parenthetical qualifiers, appears whole in the other."""
    for a, b in ((left, right), (right, left)):
        core = _words(_PARENTHETICAL.sub(" ", a))
        if core and _contains_sequence(core, _words(b)):
            return True
    return False


def assess(left: Any, right: Any, corpus: Optional[CorpusStats] = None) -> Corroboration:
    """What ``left`` and ``right`` agree or conflict on besides the organisation and the day."""
    corpus = corpus or CorpusStats()
    agreements: List[str] = []
    conflicts: List[str] = []

    if _urls(left, corpus) & _urls(right, corpus):
        agreements.append("shared article")

    left_counts, right_counts = _counts(left), _counts(right)
    if left_counts and right_counts:
        ratios = [min(a, b) / max(a, b) for a in left_counts for b in right_counts]
        if max(ratios) >= COUNT_AGREE_RATIO:
            agreements.append("record count")
        elif max(ratios) < COUNT_CONFLICT_RATIO:
            conflicts.append("record count")

    left_actors, right_actors = _actors(left), _actors(right)
    if left_actors and right_actors:
        if any(_actor_agree(a, b) for a in left_actors for b in right_actors):
            agreements.append("attacker")
        elif not ({w for a in left_actors for w in _words(a)} & {w for b in right_actors for w in _words(b)}):
            conflicts.append("attacker")

    if corpus.method_events:
        left_words = {w for m in _methods(left) for w in _words(m) if len(w) >= 3}
        right_words = {w for m in _methods(right) for w in _words(m) if len(w) >= 3}
        shared = left_words & right_words
        if (len(shared) >= METHOD_MIN_SHARED
                and any(corpus.is_distinctive_method_word(w) for w in shared)):
            agreements.append("attack method")

    return Corroboration(tuple(agreements), tuple(conflicts))
