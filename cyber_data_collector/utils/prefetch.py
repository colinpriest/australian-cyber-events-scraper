"""Answer many independent searches concurrently, then hand them out one at a time.

Timeline research and coverage-gap searches ask Perplexity one question per organisation or incident and store each
answer on the caller's SQLite connection, which is not shared across threads. :class:`PrefetchedSearch` runs the
network calls in a thread pool and is then passed to the unchanged sequential code as its ``search`` callable: a
prefetched question is answered from memory (a failure is raised exactly where the call would have raised), any
other question goes to the real search. Nothing is cached beyond the object's life; the callers' own stores
(``OrganisationTimelines``, ``ReconciliationGaps``) remain the record.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, Tuple

logger = logging.getLogger(__name__)

Search = Callable[[str, str], Dict[str, Any]]


class PrefetchedSearch:
    """``search(query, model)`` answered from a concurrent prefetch of ``queries`` (see the module docstring)."""

    def __init__(self, search: Search, queries: Iterable[str], model: str, workers: int = 8):
        self.search = search
        self.model = model
        unique = list(dict.fromkeys(q for q in queries if q))
        self.results: Dict[Tuple[str, str], Any] = {}

        def run(query: str) -> Tuple[str, Any]:
            try:
                return query, search(query, model)
            except Exception as exc:  # noqa: BLE001 - re-raised when the caller asks this question
                return query, exc

        if unique:
            logger.info("Prefetching %d search(es) with %d worker(s)", len(unique), max(1, workers))
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for query, outcome in pool.map(run, unique):
                    self.results[(query, model)] = outcome

    def __call__(self, query: str, model: str) -> Dict[str, Any]:
        outcome = self.results.pop((query, model), None)
        if outcome is None:
            return self.search(query, model)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
