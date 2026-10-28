"""Fill DeduplicatedEvents.ai_involvement and its three probabilities, and describe the AI events.

Each active event is judged by Jev (AI-attacker / AI-vulnerability / No-AI) and
every answer is stored in ModelJudgements, so a rerun asks only about events
that are new or whose text changed. Events with AI involvement get a 2-4
sentence description (fast OpenAI model, stored in EventDescriptions) for the
AI incident report. See cyber_data_collector/processing/ai_involvement.py.

Jev is required: without TYPESAFE_API_KEY / typesafe-sdk this exits 2 and
changes nothing. It exits 1 if any event could not be judged or described
(after storing the rest).

Run from the project root:
    python scripts/classify_ai_involvement.py [--db PATH] [--dry-run] [--limit N] [--no-descriptions]
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments, typesafe_model

logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="instance/cyber_events.db")
    p.add_argument("--limit", type=int, default=None, help="Only the first N active events (newest first)")
    p.add_argument("--dry-run", action="store_true",
                   help="Ask and store judgements, but write no event column and no description")
    p.add_argument("--no-descriptions", action="store_true",
                   help="Judge only; do not write descriptions for the AI incident report")
    add_model_arguments(p)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # The project's .env, not one found relative to the working directory.
    load_dotenv(ROOT / ".env")
    apply_model_arguments(args)

    from cyber_data_collector.processing.ai_involvement import AIInvolvementIncomplete, classify_events
    from cyber_data_collector.utils.api_budget import ensure_configured
    from cyber_data_collector.utils.jev import JevUnavailable

    ensure_configured()
    conn = sqlite3.connect(args.db)
    try:
        try:
            report = classify_events(conn, dry_run=args.dry_run, limit=args.limit,
                                     describe=not args.no_descriptions)
        except JevUnavailable as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except AIInvolvementIncomplete as exc:
            report = exc.report
            _print(report, args.dry_run)
            print(f"ERROR: {exc}", file=sys.stderr)
            for event_id, error in list(report.failed.items())[:10]:
                print(f"  {event_id}: {error}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    _print(report, args.dry_run)
    return 0


def _print(report, dry_run: bool) -> None:
    verb = "would update" if dry_run else "updated"
    print(f"AI involvement ({typesafe_model()}): {report.events} active event(s); "
          f"{report.asked} asked, {report.reused} from stored judgements; {report.updated} row(s) {verb}; "
          f"{report.described} new description(s)")
    for label, n in sorted(report.distribution.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<20} {n}")


if __name__ == "__main__":
    sys.exit(main())
