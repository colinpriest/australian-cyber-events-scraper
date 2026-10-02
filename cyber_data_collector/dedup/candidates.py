"""Multi-key candidate generation for missed-duplicate detection.

Deduplication can only merge what candidate generation puts in front of it.
Blocking on the single ``victim_organization_name`` surfaced 9 of 21 known
duplicates in the May/June 2026 review (2026-10-02). The misses fell into three
shapes, each of which needs its own key:

* **No usable victim name.** Fragments stored as "Queensland education sector",
  "Australian sugar producer" or with no victim at all share no name key with
  the main event. Key: every organisation linked to the event (entity links and
  the vendor), not just the scalar victim.
* **Same article, different framing.** "OpenAI slows down training after
  cyber-attack" and "OpenAI hacked Medicare portal" cite the same BBC/ABC
  coverage. Key: a shared source URL - excluding *hub* URLs (roundups, breach
  lists, section indexes) that are cited by many unrelated events.
* **Same subject, different organisation.** A supplier breach reported by each
  customer (Canvas/Instructure -> USyd, QLD DoE, DECYP ...) names a different
  victim every time. Key: a distinctive subject word shared within a date
  window. "Distinctive" is measured from the corpus, not curated: a token
  qualifies only when few events contain it.

These keys are recall-oriented. Precision comes from the adjudicator that
judges each pair, which is why a pair carries the reasons it was generated.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

# A URL cited by more events than this is a list / roundup / index page.
HUB_MAX_EVENTS = 3
# A subject token is distinctive when at most this share of events contain it
# (floored at CONTENT_MIN_ABSOLUTE so small corpora still work).
CONTENT_MAX_SHARE = 0.006
CONTENT_MIN_ABSOLUTE = 4
# Distinctive-subject pairs must fall within this many days of each other.
# Coverage of one incident clusters within weeks; the window stops a rare word
# (a town, a product) pairing unrelated incidents years apart.
CONTENT_WINDOW_DAYS = 60

_TOKEN = re.compile(r"[a-z][a-z0-9'-]{3,}")
# Words that carry no identity however rare they are in a given corpus.
_NON_IDENTIFYING = {
    "exclusive", "update", "updates", "confirms", "confirmed", "says", "after",
    "following", "about", "their", "with", "from", "this", "that", "into",
    "over", "under", "data", "breach", "cyber", "attack", "incident", "hack",
    "hacked", "hackers", "ransomware", "security", "australia", "australian",
    "australians", "customers", "personal", "information", "notice", "news",
    "statement", "media", "release", "page", "home", "blog",
}

PairReasons = Dict[FrozenSet[str], Set[str]]


def normalise_url(url: Optional[str]) -> Optional[str]:
    """``host/path`` without scheme, ``www.``, query, fragment or trailing slash.

    Returns None for a bare domain: a homepage identifies a site, not an
    article, and would link every event that cites it.
    """
    if not url:
        return None
    text = url.strip()
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/").lower()
    if not host or not path:
        return None
    return f"{host}{path}"


def subject_tokens(*texts: Optional[str]) -> Set[str]:
    tokens: Set[str] = set()
    for text in texts:
        if text:
            tokens.update(t.strip("'-") for t in _TOKEN.findall(text.lower()))
    return {t for t in tokens if len(t) >= 4 and t not in _NON_IDENTIFYING}


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _add(reasons: PairReasons, left: str, right: str, why: str) -> None:
    if left != right:
        reasons[frozenset((left, right))].add(why)


def url_pairs(urls_by_event: Dict[str, Iterable[str]],
              hub_max: int = HUB_MAX_EVENTS) -> Tuple[PairReasons, Set[str]]:
    """Pairs of events citing the same non-hub article URL.

    Returns:
        (pair reasons, hub URLs) - the hub set lets the adjudicator ignore
        those URLs as evidence too.
    """
    events_by_url: Dict[str, Set[str]] = defaultdict(set)
    for event_id, urls in urls_by_event.items():
        for url in urls:
            norm = normalise_url(url)
            if norm:
                events_by_url[norm].add(event_id)
    hubs = {u for u, ids in events_by_url.items() if len(ids) > hub_max}
    reasons: PairReasons = defaultdict(set)
    for url, ids in events_by_url.items():
        if url in hubs or len(ids) < 2:
            continue
        ordered = sorted(ids)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                _add(reasons, left, right, f"shared source {url}")
    return reasons, hubs


def content_pairs(
    texts_by_event: Dict[str, Tuple[str, Optional[str]]],
    dates_by_event: Dict[str, Optional[str]],
    window_days: int = CONTENT_WINDOW_DAYS,
    max_share: float = CONTENT_MAX_SHARE,
    min_absolute: int = CONTENT_MIN_ABSOLUTE,
) -> PairReasons:
    """Pairs of events sharing a corpus-rare subject word within ``window_days``.

    Args:
        texts_by_event: event id -> (title, summary/description).
        dates_by_event: event id -> ISO date or None. An undated event is not
            paired on content alone: without a date the window cannot bound
            how many unrelated incidents a rare word would join.
    """
    tokens_by_event = {eid: subject_tokens(*texts) for eid, texts in texts_by_event.items()}
    doc_freq: Dict[str, int] = defaultdict(int)
    for tokens in tokens_by_event.values():
        for token in tokens:
            doc_freq[token] += 1
    ceiling = max(min_absolute, int(len(tokens_by_event) * max_share))

    events_by_token: Dict[str, List[str]] = defaultdict(list)
    for eid, tokens in tokens_by_event.items():
        for token in tokens:
            if 2 <= doc_freq[token] <= ceiling:
                events_by_token[token].append(eid)

    parsed = {eid: _parse_date(d) for eid, d in dates_by_event.items()}
    reasons: PairReasons = defaultdict(set)
    for token, ids in events_by_token.items():
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                dl, dr = parsed.get(left), parsed.get(right)
                if dl is None or dr is None or abs((dl - dr).days) > window_days:
                    continue
                _add(reasons, left, right, f"shared subject '{token}'")
    return reasons


def candidate_components(
    pair_reasons: PairReasons,
    is_strong,
    weight=None,
    max_size: Optional[int] = None,
) -> List[List[str]]:
    """Groups of events joined by strong candidate edges, strongest first.

    Pairwise judgement fails on fragmented incidents: shown two records at a
    time - one misdated by years, or a supplier breach naming a different
    victim - the model confidently answers "different". Shown the whole group,
    the shared incident is obvious.

    Plain connected components do not work here: shared articles and rare
    words chain transitively, and on the live corpus one component swallowed
    545 of 820 events, which then had to be cut into arbitrary slices. So edges
    are applied strongest first and two groups are only joined if the result
    stays within ``max_size`` - local, strongly-linked groups survive, chains
    do not.

    Args:
        is_strong: ``(pair_key, reasons) -> bool`` - which edges may join groups.
        weight: ``(pair_key, reasons) -> float`` - edge strength; higher joins
            first. Defaults to equal weights.
        max_size: Largest group allowed; None for unbounded.

    Returns:
        Groups of two or more event ids, largest first.
    """
    parent: Dict[str, str] = {}
    size: Dict[str, int] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        size.setdefault(x, 1)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = [(key, why) for key, why in pair_reasons.items() if is_strong(key, why)]
    if weight is not None:
        edges.sort(key=lambda e: (-weight(*e), sorted(e[0])))
    else:
        edges.sort(key=lambda e: sorted(e[0]))
    for key, _ in edges:
        left, right = (find(x) for x in sorted(key))
        if left == right:
            continue
        if max_size is not None and size[left] + size[right] > max_size:
            continue
        parent[left] = right
        size[right] += size[left]
    groups: Dict[str, List[str]] = defaultdict(list)
    for node in list(parent):
        groups[find(node)].append(node)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=len, reverse=True)
