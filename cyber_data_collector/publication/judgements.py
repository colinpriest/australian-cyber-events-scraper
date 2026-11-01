"""Bring the stored per-event judgements the outputs show up to date before a publication snapshot.

The AI incident report and the OAIC comparisons show stored judgements (AI involvement and its
descriptions; breach source and personal-information types) keyed on a hash of each event's evidence.
Anything that merges, splits or re-attributes events after they were judged (the supplier steps, a
coherence split, a role refresh) makes those judgements stale, and a publication built from them shows
"stale label" on the AI incident report. So every new publication first runs both steps; each asks only
about events whose evidence changed since it was judged, so on unchanged data neither makes a model call.

A step that cannot run (no ``TYPESAFE_API_KEY``) or does not finish raises :class:`JudgementRefreshFailed`:
the caller publishes nothing, rather than publishing judgements it was asked to bring up to date.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class JudgementRefreshFailed(RuntimeError):
    """A judgement step did not finish; nothing should be published from the database as it is."""


def _steps() -> List[Tuple[str, Callable[[Sequence[str]], int]]]:
    from scripts import classify_ai_involvement, enrich_pii_and_source_category

    return [("breach source and personal-information types", enrich_pii_and_source_category.main),
            ("AI involvement and AI incident descriptions", classify_ai_involvement.main)]


def refresh_judgements(db_path: Path,
                       steps: Optional[List[Tuple[str, Callable[[Sequence[str]], int]]]] = None,
                       run: Optional[Callable[[Callable, List[str]], int]] = None) -> List[str]:
    """Run every judgement step on ``db_path``; returns the steps run.

    ``run(main, argv)`` calls a step (``run_full_pipeline.run_step`` inside the pipeline's event loop).

    Raises:
        JudgementRefreshFailed: a step exited non-zero or raised (the others still ran).
    """
    failures: List[str] = []
    ran: List[str] = []
    for name, main in (_steps() if steps is None else steps):
        logger.info("Bringing stored judgements up to date before publication: %s", name)
        try:
            argv = ["--db", str(db_path)]
            code = run(main, argv) if run else main(argv)
        except SystemExit as exc:          # argparse and friends
            code = exc.code
        except Exception as exc:  # noqa: BLE001 - reported together below
            code = f"error: {exc}"
        ran.append(name)
        if code not in (0, None):
            failures.append(f"{name} (exit {code})")
    if failures:
        raise JudgementRefreshFailed("stored judgements could not be brought up to date: " + "; ".join(failures))
    return ran
