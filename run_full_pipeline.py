#!/usr/bin/env python3
"""
===========================================================================================
SINGLE SOURCE OF TRUTH: Unified Australian Cyber Events Pipeline
===========================================================================================

**THIS IS THE CANONICAL ENTRY POINT** for all event discovery, enrichment, and processing.

All other scripts (discover_enrich_events.py, perplexity_backfill_events.py, etc.)
are DEPRECATED and should not be used directly. This unified pipeline ensures:

✓ Consistent Perplexity AI enrichment for all events
✓ Advanced deduplication (entity-based with 0.15 similarity threshold for same entities)
✓ Single event definition: One real-world incident = One event (not multiple news articles)
✓ Automated dashboard generation

===========================================================================================
PHASES:
===========================================================================================

1. **Discovery & Initial Processing**
   - Discovers cyber events from multiple sources (GDELT, Perplexity, Google, Webber Insurance)
   - Scrapes full article content
   - Performs initial fast-model filtering (fast, basic quality check)

2. **Perplexity AI Enrichment**
   - AUTOMATICALLY runs after discovery to enrich all new events with Perplexity AI
   - Extracts: formal entity names, threat actors, attack methods, victim counts
   - Uses sophisticated multi-source verification
   - Much higher quality than initial fast-model pass

3. **Global Deduplication**
   - Merges duplicate events using entity-based matching
   - RULE 1: Same entity + same date → merge
   - RULE 2: Same entity + similar titles (0.15 threshold) → merge
   - Uses EARLIEST date for merged events

4. **ASD Risk Classification**
   - Classifies events using Australian Signals Directorate (ASD) risk matrix framework
   - Assigns severity categories (C1-C6) and stakeholder levels
   - Uses the strong OpenAI model for intelligent classification based on impact and records affected
   - INCREMENTAL: Only processes events without existing classifications

5. **Dashboard Generation**
   - Static HTML dashboard with embedded analytics and visualizations
   - Includes ASD risk matrices (all years + current year)

===========================================================================================
USAGE:
===========================================================================================

    # Run full pipeline (recommended - all phases with latest algorithms)
    python run_full_pipeline.py

    # Discover new months only (auto-enriches with Perplexity)
    python run_full_pipeline.py --discover-only

    # Re-enrich existing events with updated Perplexity prompts
    python run_full_pipeline.py --re-enrich

    # Classify with ASD risk matrix only
    python run_full_pipeline.py --classify-only

    # Dashboard only
    python run_full_pipeline.py --dashboard-only

    # Control discovery parameters
    python run_full_pipeline.py --max-events 500 --source Perplexity OAIC

    # Skip ASD classification (faster pipeline)
    python run_full_pipeline.py --skip-classification

===========================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
import sqlite3
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

# Import existing components
from cyber_data_collector.pipelines.discovery import EventDiscoveryEnrichmentPipeline
from scripts.build_static_dashboard import (
    build_html, get_connection, get_monthly_event_counts, get_monthly_severity_trends,
    get_monthly_records_affected, get_monthly_event_type_mix, get_overall_event_type_mix,
    get_entity_type_distribution, get_records_affected_histogram, get_half_yearly_database_counts,
    prepare_oaic_comparison_data, load_oaic_data, compute_monthly_counts_stats,
    compute_event_type_correlation_matrix, get_maximum_severity_per_month,
    get_median_severity_per_month, get_maximum_records_affected_per_month,
    get_severity_by_industry, get_severity_by_attack_type, get_records_affected_by_attack_type,
    get_asd_risk_matrix, prepare_oaic_cyber_incidents_data, prepare_oaic_attack_types_data,
    prepare_oaic_sectors_data, prepare_oaic_individuals_affected_data, build_dashboard_file
)
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine
from cyber_data_collector.utils import ConfigManager, setup_logging
from cyber_data_collector.utils.llm_models import (
    add_model_arguments,
    apply_model_arguments,
)
from cyber_data_collector.utils.run_summary import (
    install_run_summary,
    print_run_summary,
)
from scripts.perplexity_backfill_events import PerplexityBackfillProcessor
from scripts.asd_risk_classifier import ASDRiskClassifier
from scripts.run_global_deduplication import DeduplicationMigration

# Configure logging
setup_logging(log_file="logs/unified_pipeline.log")
# Install end-of-run summary handler + demote noisy third-party loggers
# so WARNINGs/ERRORs don't get drowned out by per-request HTTP chatter.
install_run_summary()
logger = logging.getLogger(__name__)


def run_step(main, argv: List[str]) -> int:
    """Run a script's ``main(argv)`` from inside the pipeline.

    The pipeline itself runs under ``asyncio.run()``, and a step that runs its
    own coroutines with ``asyncio.run()`` (rescrape-short-pages, the Cyber Daily
    re-fetch) fails inside a running loop with "asyncio.run() cannot be called
    from a running event loop". Such a call goes to a worker thread, which has
    no loop; the caller still waits for it, as it did before.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return main(argv)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(main, argv).result()


class UnifiedPipeline:
    """Unified pipeline that orchestrates discovery, deduplication, and dashboard generation."""

    def __init__(self, db_path: str = "instance/cyber_events.db"):
        self.db_path = db_path
        self.start_time = time.time()
        self.results = {
            'discovery': {'success': False, 'events_found': 0, 'errors': []},
            'reenrichment': {'success': False, 'events_enriched': 0, 'errors': []},
            'deduplication': {'success': False, 'events_deduplicated': 0, 'errors': []},
            'classification': {'success': False, 'events_classified': 0, 'cache_hits': 0, 'errors': []},
            'dashboard': {'success': False, 'files_created': [], 'errors': []}
        }

    def print_header(self, title: str):
        """Print a formatted section header."""
        print("\n" + "="*80)
        print(f" {title}")
        print("="*80)

    def print_summary(self):
        """Print execution summary."""
        elapsed = time.time() - self.start_time
        self.print_header("EXECUTION SUMMARY")
        
        print(f"Total execution time: {elapsed:.1f} seconds")
        print()
        
        # Discovery results
        discovery = self.results['discovery']
        print(f"📊 DISCOVERY PHASE: {'✅ SUCCESS' if discovery['success'] else '❌ FAILED'}")
        if discovery['success']:
            print(f"   Events discovered: {discovery['events_found']}")
        if discovery['errors']:
            print(f"   Errors: {len(discovery['errors'])}")
            for error in discovery['errors'][:3]:  # Show first 3 errors
                print(f"   - {error}")

        # Classification results
        classification = self.results['classification']
        if classification.get('events_classified', 0) > 0 or classification.get('cache_hits', 0) > 0:
            print(f"🔒 ASD CLASSIFICATION PHASE: {'✅ SUCCESS' if classification['success'] else '❌ FAILED'}")
            if classification['success']:
                print(f"   Events classified: {classification['events_classified']}")
                print(f"   Cache hits: {classification['cache_hits']}")
            if classification['errors']:
                print(f"   Errors: {len(classification['errors'])}")
                for error in classification['errors'][:3]:
                    print(f"   - {error}")

        # Dashboard results
        dashboard = self.results['dashboard']
        print(f"📈 DASHBOARD PHASE: {'✅ SUCCESS' if dashboard['success'] else '❌ FAILED'}")
        if dashboard['success'] and dashboard['files_created']:
            print(f"   Files created: {len(dashboard['files_created'])}")
            for file in dashboard['files_created']:
                print(f"   - {file}")
        if dashboard['errors']:
            print(f"   Errors: {len(dashboard['errors'])}")
            for error in dashboard['errors'][:3]:
                print(f"   - {error}")

        print("="*80)

    async def run_discovery_phase(self, args) -> bool:
        """
        Run the discovery and enrichment phase with Perplexity AI.

        This phase:
        1. Discovers raw events from multiple sources
        2. Performs initial fast-model filtering (fast pass)
        3. AUTOMATICALLY enriches all new events with Perplexity AI
        4. Runs global deduplication
        """
        self.print_header("PHASE 1: EVENT DISCOVERY & PERPLEXITY ENRICHMENT")

        try:
            # Step 1: Run initial discovery with fast-model filtering
            logger.info("Step 1/3: Discovering events from sources (with initial fast-model filtering)...")
            page_gate_mode = "report-only" if getattr(args, 'page_gate_report_only', False) else None
            pipeline = (EventDiscoveryEnrichmentPipeline(self.db_path, page_gate_mode=page_gate_mode)
                        if page_gate_mode else EventDiscoveryEnrichmentPipeline(self.db_path))

            backfill_since = getattr(args, 'cyberdaily_backfill_since', '2020-01')
            await pipeline.discover_events(
                source_types=args.source,
                date_range_days=args.days,
                max_events=args.max_events,
                cyberdaily_backfill_since=(tuple(int(x) for x in backfill_since.split('-'))
                                           if backfill_since and backfill_since != 'none' else None),
                cyberdaily_refetch_days=(None if getattr(args, 'cyberdaily_refetch_all', False)
                                         else getattr(args, 'cyberdaily_refetch_days', 90)),
                cyberdaily_refetch=not getattr(args, 'skip_cyberdaily_refetch', False),
            )

            pipeline.print_statistics()
            pipeline.print_filtering_statistics()

            # Capture initial discovery count
            initial_events = pipeline.stats.get('events_discovered', 0)
            self.results['discovery']['events_found'] = initial_events

            # Unfinished discovery is a degraded run, not a quiet log line:
            # failed source-months are searched again next run, quarantined
            # records need a look, incomplete months still hold pending records.
            failed_months = pipeline.stats.get('source_months_failed', 0)
            quarantined = pipeline.stats.get('records_quarantined', 0)
            incomplete = len(pipeline.retry_queue.incomplete_months())
            for count, what in ((failed_months, "source-month search(es) failed"),
                                (quarantined, "record(s) quarantined after repeated failures"),
                                (incomplete, "month(s) still have unprocessed records")):
                if count:
                    message = f"Discovery: {count} {what}"
                    logger.error(message)
                    self.results['discovery'].setdefault('errors', []).append(message)

            pipeline.close()
            logger.info(f"Initial discovery complete: {initial_events} events found")

            # Sanity check: zero events from a non-trivial lookback window
            # is almost certainly a misconfiguration (API key missing, all
            # sources disabled, network down) - flag it as a warning.
            lookback_days = getattr(args, "days", 0) or 0
            if initial_events == 0 and lookback_days > 0:
                logger.warning(
                    f"Discovery returned zero events over a {lookback_days}-day "
                    "window. Check API keys (PERPLEXITY_API_KEY, "
                    "GOOGLE_CUSTOMSEARCH_API_KEY) and source filters."
                )

            # Step 2: Automatically run Perplexity enrichment on newly discovered events
            logger.info("\nStep 2/3: Enriching events with Perplexity AI (high-quality enrichment)...")
            logger.info("This automatically upgrades fast-model enrichment to Perplexity AI...")

            perplexity_success = await self._run_auto_perplexity_enrichment(args)

            if not perplexity_success:
                # Deduplication still runs (it is incremental and safe), but the
                # phase reports failure: it used to report success regardless.
                logger.error("Perplexity enrichment failed - continuing to deduplication, phase will fail")
                self.results['discovery']['errors'].append("Perplexity enrichment failed")

            # Step 3: Run global deduplication (cross-month, entity-based).
            # This is the ONLY place deduplication runs during a refresh. It is
            # incremental: new enriched events are merged into the existing,
            # curated DeduplicatedEvents rather than rebuilding them. The
            # discovery pipeline must not deduplicate - it previously wiped and
            # rebuilt the table on every refresh, erasing all dedup v3 repairs.
            # A full rebuild happens only with --force-dedup.
            logger.info("\nStep 3/3: Running global deduplication...")
            dedup_success = self.run_deduplication_phase(args)

            if not dedup_success:
                logger.error("Deduplication failed - discovery phase fails")
                self.results['discovery']['errors'].append("Deduplication failed")

            ok = perplexity_success and dedup_success
            self.results['discovery']['success'] = ok
            logger.info(f"Discovery phase {'complete' if ok else 'FAILED'}: {initial_events} events discovered")
            return ok

        except Exception as e:
            logger.error(f"Discovery phase failed: {e}")
            self.results['discovery']['errors'].append(str(e))
            return False

    async def _run_auto_perplexity_enrichment(self, args) -> bool:
        """
        Automatically run Perplexity enrichment on events discovered in this session.
        This upgrades the initial fast-model enrichment to high-quality Perplexity AI.
        """
        db = None
        try:
            # Load Perplexity API key
            perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')
            if not perplexity_api_key:
                # Not a silent skip: the run reports it (degraded) and names the cause.
                message = "PERPLEXITY_API_KEY not set - automatic enrichment did not run"
                logger.error(message)
                self.results['reenrichment'].setdefault('errors', []).append(message)
                return True

            # Initialize enrichment engine
            db = CyberEventDataV2(self.db_path)
            perplexity_engine = PerplexityEnrichmentEngine(perplexity_api_key)
            processor = PerplexityBackfillProcessor(
                db=db,
                perplexity_engine=perplexity_engine,
                dry_run=False
            )

            # Get events needing enrichment (all events without Perplexity data)
            # Bounded per run: the backlog is not lost, it stays unvalidated
            # and the next run continues it (fewest attempts first).
            events = processor.get_events_needing_enrichment(
                limit=getattr(args, 'enrich_limit', None) or None)

            if not events:
                logger.info("No events need Perplexity enrichment")
                return True

            logger.info(f"Enriching {len(events)} events with Perplexity AI (concurrent, max_concurrent=10)...")

            counts = await processor.enrich_events_concurrent(events, max_concurrent=10)
            enriched_count = counts['enriched']
            failed_count = counts['failed']

            logger.info(f"Perplexity enrichment complete: {enriched_count} enriched, {failed_count} failed")
            self.results['reenrichment']['success'] = True
            self.results['reenrichment']['events_enriched'] = enriched_count
            self.results['reenrichment']['events_failed'] = failed_count

            # Sanity check: a high failure rate suggests the API is
            # rate-limiting, the prompt has issues, or the network is
            # flaky. Surface it as a warning so the user notices.
            self._record_enrichment_problems(counts)
            return True

        except Exception as e:
            logger.error(f"Automatic Perplexity enrichment failed: {e}")
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _record_enrichment_problems(self, counts: Dict[str, int]) -> None:
        """Record enrichment failures as errors, so the run ends degraded.

        Failed database writes and corrupt stored baselines are never an
        acceptable loss; a failure rate above 25% means the API, key, prompt or
        network is wrong. All three used to leave the phase reporting success.
        """
        broken = counts.get('storage_failed', 0) + counts.get('corrupt_baseline', 0)
        if broken:
            message = (f"Perplexity enrichment: {counts.get('storage_failed', 0)} failed database write(s), "
                       f"{counts.get('corrupt_baseline', 0)} corrupt stored enrichment(s) (quarantined)")
            logger.error(message)
            self.results['reenrichment']['errors'].append(message)
        enriched, failed = counts.get('enriched', 0), counts.get('failed', 0)
        attempted = enriched + failed
        if attempted >= 10 and failed / attempted > 0.25:
            message = (f"Perplexity enrichment failure rate {failed}/{attempted} "
                       f"({failed / attempted:.0%}) above 25%. Check API key + network.")
            logger.error(message)
            self.results['reenrichment']['errors'].append(message)

    async def run_reenrichment_phase(self, args) -> bool:
        """Run re-enrichment on existing events with updated Perplexity prompt."""
        self.print_header("PHASE: RE-ENRICHMENT OF EXISTING EVENTS")

        db = None
        try:
            # Load Perplexity API key from environment
            perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')

            if not perplexity_api_key:
                raise ValueError("Perplexity API key not found in .env file (PERPLEXITY_API_KEY)")

            # Initialize database and enrichment engine
            logger.info("Initializing Perplexity enrichment engine...")
            db = CyberEventDataV2(self.db_path)
            perplexity_engine = PerplexityEnrichmentEngine(perplexity_api_key)

            # Initialize backfill processor
            processor = PerplexityBackfillProcessor(
                db=db,
                perplexity_engine=perplexity_engine,
                dry_run=False
            )

            # Get events needing enrichment
            logger.info("Finding events that need re-enrichment...")
            events = processor.get_events_needing_enrichment(
                limit=args.re_enrich_limit if hasattr(args, 're_enrich_limit') and args.re_enrich_limit else None
            )

            if not events:
                logger.info("No events need re-enrichment")
                self.results['reenrichment']['success'] = True
                self.results['reenrichment']['events_enriched'] = 0
                return True

            logger.info(f"Re-enriching {len(events)} events (concurrent, max_concurrent=10)...")

            counts = await processor.enrich_events_concurrent(events, max_concurrent=10)
            enriched_count = counts['enriched']
            failed_count = counts['failed']

            # Print statistics
            logger.info(f"\nRe-enrichment complete:")
            logger.info(f"  Successfully enriched: {enriched_count}")
            logger.info(f"  Failed: {failed_count}")
            logger.info(f"  Total processed: {len(events)}")

            self.results['reenrichment']['success'] = True
            self.results['reenrichment']['events_enriched'] = enriched_count
            self.results['reenrichment']['events_failed'] = failed_count

            self._record_enrichment_problems(counts)
            return True

        except Exception as e:
            logger.error(f"Re-enrichment phase failed: {e}")
            self.results['reenrichment']['errors'].append(str(e))
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def run_deduplication_phase(self, args) -> bool:
        """Run global deduplication on all enriched events."""
        self.print_header("PHASE: GLOBAL DEDUPLICATION")

        try:
            logger.info("Running global deduplication...")

            # Canonical enum values before anything compares them (legacy
            # "EventSeverity.HIGH" spellings re-enter via re-enrichment).
            from scripts import dedup_admin
            if run_step(dedup_admin.main, ["--db", self.db_path, "normalise-enums"]) != 0:
                raise RuntimeError("normalise-enums failed")

            force_rebuild = getattr(args, 'force_dedup', False)
            migration = DeduplicationMigration(self.db_path, dry_run=False, force=force_rebuild)
            if not migration.run_migration():
                raise RuntimeError("Deduplication migration failed")

            stats = migration.migration_report.get('statistics', {})
            dedup_count = stats.get('output_events') or stats.get('final_active_events')
            if dedup_count is not None:
                self.results['deduplication']['events_deduplicated'] = dedup_count

            # Sanity check: if dedup output is more than the input, or
            # input >> output by an unrealistic factor, something is off.
            input_events = stats.get('input_events') or stats.get('total_enriched_events')
            if (isinstance(input_events, int) and isinstance(dedup_count, int)
                    and input_events > 0 and dedup_count > 0):
                if dedup_count > input_events:
                    logger.warning(
                        f"Deduplication produced more output ({dedup_count}) "
                        f"than input ({input_events}) - schema invariant "
                        "violated; inspect DeduplicatedEvents."
                    )
                elif dedup_count < input_events * 0.10:
                    logger.warning(
                        f"Deduplication collapsed {input_events} input events "
                        f"to only {dedup_count} dedup events ({dedup_count / input_events:.1%}). "
                        "Threshold may be too aggressive."
                    )

            self.run_missed_merge_check(args)
            self.run_data_hygiene(args)
            self.run_recurrence_check(args)
            self.run_role_refresh(args)
            self.run_entity_sizing(args)

            self.results['deduplication']['success'] = True
            logger.info("Global deduplication completed successfully")
            return True

        except Exception as e:
            logger.error(f"Deduplication phase failed: {e}")
            self.results['deduplication']['errors'].append(str(e))
            return False

    def run_missed_merge_check(self, args) -> None:
        """Find and merge duplicates the incremental pass missed.

        Incremental dedup compares each new record against existing events by
        title and victim name only. Fragments with no usable victim name, a
        different victim per report (supplier breaches), or a wrong date are
        never matched - the May/June 2026 review found 21 such duplicates.
        This runs the multi-key ``find-missed`` scoped to events created or
        changed in the last ``--missed-merge-days`` days (older pairs were
        judged by earlier runs) and applies findings at or above
        ``--missed-merge-min-certainty``. Runs before the recurrence check,
        which judges what remains.
        """
        if getattr(args, 'skip_missed_merge_check', False):
            logger.info("Skipping missed-merge check (--skip-missed-merge-check)")
            return
        if not os.getenv('OPENAI_API_KEY'):
            message = "OPENAI_API_KEY not set - missed-merge check did not run"
            logger.error(message)
            self.results['deduplication']['errors'].append(message)
            return
        from scripts import dedup_admin
        days = getattr(args, 'missed_merge_days', 120)
        certainty = getattr(args, 'missed_merge_min_certainty', 0.9)
        base = ["--db", self.db_path]
        logger.info("Checking for missed duplicates among events changed in the "
                    "last %d days...", days)
        try:
            # Whole-group adjudication over the multi-key candidate graph
            # (dedup_admin adjudicate-candidates). Pairwise judgement rejected
            # misdated and supplier-breach fragments of one incident when shown
            # them two at a time; groups make the shared incident visible.
            apply = getattr(args, 'missed_merge_apply', False)
            cmd = base + ["adjudicate-candidates", "--recent-days", str(days),
                          "--min-certainty", str(certainty)]
            if not apply:
                cmd.append("--dry-run")
            if run_step(dedup_admin.main, cmd) != 0:
                raise RuntimeError("adjudicate-candidates failed")
            if not apply:
                logger.info("Missed-merge proposals written to "
                            "instance/dedup_candidate_clusters.json for review "
                            "(pass --missed-merge-apply to merge automatically)")
            self.results['deduplication']['missed_merge_check'] = 'ok'
        except Exception as exc:  # noqa: BLE001 - never fail a completed dedup
            logger.warning("Missed-merge check failed: %s", exc)
            self.results['deduplication']['errors'].append(f"missed-merge: {exc}")

    def run_data_hygiene(self, args) -> None:
        """Reject non-incident pages and correct victims the records contradict.

        Both defects put false repeat attacks into the short-gap band of the
        recurrence analysis: an agency's own service pages stored as later
        "attacks" on it, and TPG's breach filed under Optus because the
        article mentions Optus. Run before the recurrence check, which groups
        events by victim.

        -2. ``recalibrate --if-stale`` - a calibrated cut-off holds only for the
           model, question and evidence processing it was measured on; any
           calibration that no longer matches is recomputed by re-scoring its
           labelled cases on current evidence (Jev/escalation calls, only for
           evidence not already judged), else its step stays report-only. First, so the
           steps below act on a calibration that matches. Skipped with
           ``--skip-recalibration``; ``--report-event-rejections`` only lists.
        -1. ``label-evidence`` - labelled-real events with no active record
           get the report their label cites queued for the next refresh;
           processed reports are merged into their labelled event and its
           inactive records split out and rejected.
        -0.5 ``recover-evidence`` - incidents with neither page text nor
           validated research get the retrieval they lack queued (page
           re-fetch, labelled report, last a replacement search; capped) and
           every incident its ``evidence_status``; re-marked at the end.
        0. ``check-sources --apply`` - Google-sourced records count only if
           the page itself reports an incident (calibrated Jev gate; menus,
           help pages and policies do not). Junk records are removed and, where
           they pointed at an uncovered real incident, a replacement source is
           queued. The same gate runs at ingestion in run_global_deduplication.
        1. ``classify-pages`` - every unclassified suspect page (Jev only; it
           fails rather than switch model; each verdict stored and
           never paid for twice). Verdicts describe pages; they reject nothing.
        2. ``check-events --apply`` - Jev judges whether each event is a real
           incident from every record's page text *and* Perplexity research,
           and events below the **calibrated** cut-off are rejected. Without a
           calibration measured on the current Jev model and question
           (``evaluation/event_reality_calibration.json``) it only reports.
           It replaces the page gate (``reject-non-incidents``), which on
           2026-10-27 would have rejected real incidents whose stored page was
           a privacy policy or home page (LUSH, Nexia, Legal Aid ACT).
           ``--report-event-rejections`` keeps it report-only regardless.
        2a. ``check-scope --apply`` - Jev judges whether each event affects
           Australia (a US agency's breach is a real incident, but not one this
           dataset records); events below the **calibrated** cut-off
           (``evaluation/event_scope_calibration.json``) and events labelled out
           of scope are rejected, reversibly. Report-only without a calibration.
        2b. ``classify-entities --stale-only`` - re-derive victim and vendor
           roles for events whose members changed since they were classified
           (merges, splits, re-enriched records; see record_fingerprints).
        3. ``check-victims`` - deterministic, no API calls.
        4. ``check-duplicates --apply`` - Jev judges every pair of events
           sharing a victim (with both events' evidence, corroboration and
           date provenance) and merges pairs at or above the **calibrated**
           cut-off (``evaluation/duplicate_calibration.json``); report-only
           without a calibration for the current model and question. Runs
           after the victim check because pairs are formed by victim. It also
           judges every pair of AI-involved events within a year, whatever
           their victim, with a question that knows how rare AI incidents are,
           and merges them at their own calibrated cut-off
           (``evaluation/ai_duplicate_calibration.json``; report-only until
           pairs have been labelled and calibrated).
        5. ``check-victim-attribution --apply`` - Jev asks whether each
           event's recorded victim is really the organisation affected;
           calibrated demotion / re-attribution, otherwise flagged. Preceded by
           ``judge-names`` (is a stored name a name or a description?) and
           followed by ``fill-victims --apply``: an incident with no usable
           victim gets the organisation its own evidence names (calibrated),
           which is what lets the duplicate check pair it by organisation.
        6. ``check-coherence``, ``check-duplicates``, ``find-supplier-victims``
           change membership, so ``classify-entities --stale-only`` runs again
           before ``reconcile-timelines``. ``check-same-organisation`` (after
           ``check-coherence``) judges members that may be a different incident
           at the same organisation - report-only until calibrated - and
           ``resolve-label-contradictions`` (after the merging steps) splits
           apart any pair labelled different that has been merged. After the recurrence check (which
           merges too) :meth:`run_role_refresh` settles roles and attributions
           a last time.

        Steps 2 and 4 research each multi-event organisation's incident
        timeline once (Perplexity, stored) and escalate the cases Jev scores in
        the middle band to the reasoning model; both answers are stored.

        Never fails the phase.
        """
        if getattr(args, 'skip_data_hygiene', False):
            logger.info("Skipping data hygiene (--skip-data-hygiene)")
            return
        from scripts import dedup_admin
        base = ["--db", self.db_path]
        check = ["check-events"]
        if not getattr(args, 'report_event_rejections', False):
            check.append("--apply")
        scope = ["check-scope"]
        if not getattr(args, 'report_event_rejections', False):
            scope.append("--apply")
        duplicates = ["check-duplicates"]
        if not getattr(args, 'report_event_rejections', False):
            duplicates.append("--apply")
        # Stray records (calibrated Jev; after victims are corrected, before
        # check-duplicates re-homes what it splits off).
        coherence = ["check-coherence"]
        if not getattr(args, 'report_event_rejections', False):
            coherence.append("--apply")
        sources = ["check-sources"]
        if not getattr(args, 'report_event_rejections', False):
            sources.append("--apply")
        # Members that may be a separate incident at the same organisation: report-only until
        # their own labels and calibration exist (coherence_same_org). Resolving label
        # contradictions needs no model: pairs labelled different that were merged are split.
        same_org = ["check-same-organisation"]
        label_contradictions = ["resolve-label-contradictions"]
        recalibration = ["recalibrate", "--if-stale"]
        if not getattr(args, 'report_event_rejections', False):
            same_org.append("--apply")
        else:
            label_contradictions.append("--dry-run")
            recalibration.append("--dry-run")
        # Every step always runs. A step whose model or key is unavailable
        # fails loudly (jev.require and friends) and is recorded below, so the
        # run ends degraded - it is never skipped or switched to another model.
        # classify-entities --stale-only: roles (who is the victim, who the
        # vendor) go out of date when an event's members change - a merge, a
        # split, a re-enriched record - and nothing else refreshes them.
        # rescrape-short-pages first: pages with under 300 characters of text
        # cannot be judged by the source gate or the page classifier.
        # research-timelines: organisation histories are refreshed before any
        # judgement reads them. check-victim-attribution re-attributes (calibrated)
        # before strays and duplicates are judged by victim. reconcile-timelines
        # last: it reports what still disagrees and queues reporting of gaps.
        attribution = ["check-victim-attribution"]
        if not getattr(args, 'report_event_rejections', False):
            attribution.append("--apply")
        # judge-names before the victim steps: which stored names are descriptions rather than names
        # (calibrated Jev verdicts; asked once per name). fill-victims after attribution: an incident
        # with no usable victim gets the organisation its evidence names (calibrated), so the duplicate
        # check that follows can pair it by organisation.
        names = ["judge-names"]
        fill = ["fill-victims"]
        if not getattr(args, 'report_event_rejections', False):
            fill.append("--apply")
        # find-supplier-victims after duplicates: it merges supplier-incident
        # fragments and attaches co-victims (calibrated; report-only otherwise).
        suppliers = ["find-supplier-victims", "--research"]
        if not getattr(args, 'report_event_rejections', False):
            suppliers.append("--apply")
        # label-evidence first: a labelled-real event left with no active
        # record gets the report its label cites queued, and once that report
        # has been processed it is attached and the junk page split out.
        evidence = ["label-evidence"]
        if getattr(args, 'report_event_rejections', False):
            evidence.append("--dry-run")
        # publication-dates after the re-scrape and before any judgement: every
        # record's publication date, each event's first report and reporting lag,
        # and a date for an event no record dates (its first report, so marked).
        # Again at the end (no fetching): merges and splits change first reports.
        # canonicalise-entities: reviewed aliases fold; name-structure proposals
        # fold only when Perplexity confirms one organisation (verdicts stored).
        # classify-entities --stale-only again after suppliers: coherence splits,
        # duplicate and supplier merges change membership after the first
        # refresh (on 2026-10-28 they left 145 events with outdated roles), and
        # reconcile-timelines and the recurrence check read victims by role.
        # recover-evidence after label-evidence and before the re-scrape: it
        # queues re-fetches (fetched by rescrape-short-pages straight after),
        # labelled reports and, when nothing else can reach an incident's
        # evidence, one replacement search; at most --evidence-recovery-limit
        # incidents a run. Again at the end (--status-only): every step in
        # between changes what each incident's evidence status should be.
        recovery = ["recover-evidence", "--limit", str(getattr(args, 'evidence_recovery_limit', 100))]
        if getattr(args, 'report_event_rejections', False):
            recovery.append("--dry-run")
        # check-record-identity first of all: records whose enrichment describes another record are
        # re-attached to their own (free), and pages that are another article queue a re-fetch or a
        # replacement search that the steps below pick up (capped per run). Idempotent.
        identity = ["check-record-identity"]
        if getattr(args, 'report_event_rejections', False):
            identity.append("--dry-run")
        steps = [identity, evidence, recovery, ["rescrape-short-pages"], ["publication-dates"], ["canonicalise-entities"],
                 ["research-timelines"],
                 # Entity profiles (jurisdiction, sector, what it runs...): evidence for the alias, acronym and
                 # victim judgements below; one lookup per organisation whose evidence changed, 200 a run.
                 ["entity-profile", "research", "--limit", "200"],
                 ["classify-pages"], sources, check, scope,
                 ["classify-entities", "--stale-only"], names, ["check-victims"], attribution, fill,
                 coherence, same_org, duplicates, suppliers, label_contradictions,
                 ["classify-entities", "--stale-only"],
                 ["reconcile-timelines", "--queue-gaps"],
                 ["publication-dates", "--no-fetch"], ["recover-evidence", "--status-only"]]
        if not getattr(args, 'skip_recalibration', False):
            steps.insert(0, recalibration)
        for step in steps:
            try:
                if run_step(dedup_admin.main, base + step) != 0:
                    raise RuntimeError(f"{step[0]} failed")
            except Exception as exc:  # noqa: BLE001 - never fail a completed dedup
                logger.warning("Data hygiene step %s failed: %s", step[0], exc)
                self.results['deduplication']['errors'].append(f"{step[0]}: {exc}")

    def run_recurrence_check(self, args) -> None:
        """Re-check repeat attacks that follow the previous one within 90 days.

        Deduplication blocks candidates on entity and date, so one incident
        whose coverage was published weeks apart survives as two events - a
        repeat attack that never happened, landing in the short-gap band every
        recurrence analysis is estimated from. This runs after every dedup pass
        because that is when the false pairs appear.

        Never fails the phase: leaving a suspected duplicate in place is a
        smaller problem than aborting a completed deduplication.
        """
        if getattr(args, 'skip_recurrence_check', False):
            logger.info("Skipping recurrence check (--skip-recurrence-check)")
            return

        import sqlite3

        from cyber_data_collector.dedup import schema
        from cyber_data_collector.dedup.ledger import DedupLedger
        from cyber_data_collector.dedup.recurrence_check import (
            RecurrenceAuditor, attach_source_urls, build_runs,
            findings_from_partition, load_recurrence_events,
        )

        logger.info("Re-checking repeat attacks less than "
                    f"{args.recurrence_window} days apart...")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            schema.migrate(conn)
            ledger = DedupLedger(conn)
            auditor = RecurrenceAuditor(window_days=args.recurrence_window)
            runs = build_runs(load_recurrence_events(conn),
                              window_days=args.recurrence_window,
                              unverified_dates=args.recurrence_unverified_dates)
            if not runs:
                logger.info("No repeat attacks inside the window")
                return

            merged = 0
            for run in runs:
                attach_source_urls(conn, run)
                partition = auditor.audit_run(run)
                for finding in findings_from_partition(run, partition):
                    if finding["certainty"] < args.recurrence_min_certainty:
                        logger.info(
                            "Repeat at %r left alone (certainty %.2f below %.2f)",
                            finding["entity"], finding["certainty"],
                            args.recurrence_min_certainty)
                        continue
                    for source in finding["sources"]:
                        try:
                            ledger.merge_events(
                                finding["target"]["id"], source["id"],
                                reason=(f"[recurrence {finding['certainty']:.2f}] "
                                        f"{finding['label']}: apparent repeat "
                                        f"{finding['max_gap_days']} days apart is "
                                        f"the same incident reported again. "
                                        f"{finding['reasoning']}"),
                                actor="pipeline")
                            merged += 1
                        except (sqlite3.Error, ValueError) as exc:
                            logger.warning("Recurrence merge failed: %s", exc)
                conn.commit()
            self.results['deduplication']['recurrences_merged'] = merged
            logger.info("Recurrence check: %d run(s) examined, %d false repeat(s) "
                        "merged", len(runs), merged)
        except Exception as exc:  # noqa: BLE001 - never fail a completed dedup
            logger.warning("Recurrence check failed: %s", exc)
            self.results['deduplication']['errors'].append(f"recurrence: {exc}")
        finally:
            conn.close()

    def run_role_refresh(self, args) -> None:
        """Settle entity roles and victim attributions after the last membership change.

        Roles are judged against an event's members (``role_maintenance``), so
        every merge or split leaves them describing records the event no
        longer has. The recurrence check merges after data hygiene, and most
        hygiene merges come after hygiene's first role refresh, so this runs
        last:

        1. ``classify-entities --stale-only`` - every event whose members
           changed since its roles were classified.
        2. ``check-victims`` and ``check-victim-attribution --apply`` (skipped
           with ``--skip-data-hygiene``; report-only with
           ``--report-event-rejections``). Every pair is judged again, but Jev
           is asked only where the input changed - new members, evidence, a
           refreshed timeline, other victims - so earlier flags are settled:
           confirmed, demoted or re-attributed (calibrated), or kept flagged
           with the reason.

        Then no active event may have outdated or missing roles; any that do
        (a failed model call) are recorded as an error, so the run ends
        degraded rather than reporting success. Never fails the phase.
        """
        import sqlite3

        from cyber_data_collector.dedup.role_maintenance import staleness_report
        from scripts import dedup_admin

        base = ["--db", self.db_path]
        steps = [["classify-entities", "--stale-only"]]
        if not getattr(args, 'skip_data_hygiene', False):
            attribution = ["check-victim-attribution"]
            if not getattr(args, 'report_event_rejections', False):
                attribution.append("--apply")
            fill = ["fill-victims"]
            if not getattr(args, 'report_event_rejections', False):
                fill.append("--apply")
            steps += [["check-victims"], attribution, fill]
        for step in steps:
            try:
                if run_step(dedup_admin.main, base + step) != 0:
                    raise RuntimeError(f"{step[0]} failed")
            except Exception as exc:  # noqa: BLE001 - never fail a completed dedup
                logger.warning("Role refresh step %s failed: %s", step[0], exc)
                self.results['deduplication']['errors'].append(f"{step[0]}: {exc}")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            report = staleness_report(conn)
        finally:
            conn.close()
        self.results['deduplication']['roles'] = report
        if report['outdated'] or report['never_classified']:
            message = (f"{report['outdated']} active event(s) still have outdated roles and "
                       f"{report['never_classified']} have none after the role refresh")
            logger.error(message)
            self.results['deduplication']['errors'].append(f"roles: {message}")
        else:
            logger.info("Entity roles current for all %d active events", report['active'])

    def run_entity_sizing(self, args) -> None:
        """Give newly discovered entities an ordinal size band.

        Incremental: entities already carrying a band are skipped, so a monthly
        refresh sizes only what it just added. Without a Perplexity key the step
        is skipped rather than filling the column with guesses.
        """
        if getattr(args, 'skip_entity_sizing', False):
            logger.info("Skipping entity sizing (--skip-entity-sizing)")
            return

        import sqlite3

        from cyber_data_collector.dedup import schema
        from cyber_data_collector.dedup.entity_size import (
            EntitySizeResearcher, estimate_sizes, pending_entities,
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            schema.migrate(conn)
            rows = pending_entities(conn, limit=args.entity_size_limit)
            if not rows:
                logger.info("Every entity already has a size estimate")
                return
            researcher = EntitySizeResearcher()
            if not researcher.perplexity_key:
                logger.warning("PERPLEXITY_API_KEY not set; %d entit%s left "
                               "unsized rather than guessed", len(rows),
                               "y" if len(rows) == 1 else "ies")
                return
            logger.info("Estimating size for %d new entit%s...", len(rows),
                        "y" if len(rows) == 1 else "ies")
            stats = estimate_sizes(conn, rows, researcher=researcher, workers=6)
            self.results['deduplication']['entities_sized'] = stats["stored"]
            logger.info("Entity sizing: %s", stats["bands"])
        except Exception as exc:  # noqa: BLE001 - never fail a completed dedup
            logger.warning("Entity sizing failed: %s", exc)
            self.results['deduplication']['errors'].append(f"entity sizing: {exc}")
        finally:
            conn.close()

    def run_classification_phase(self, args) -> bool:
        """
        Run ASD risk classification on unclassified events.

        This phase:
        1. Identifies events that don't have ASD risk classifications
        2. Classifies them using the strong OpenAI model based on ASD risk matrix framework
        3. Stores classifications in ASDRiskClassifications table
        4. Incremental: Only processes new events, uses cache for existing ones
        """
        self.print_header("PHASE: ASD RISK CLASSIFICATION")

        try:
            # Check if OpenAI API key is available
            openai_api_key = os.getenv('OPENAI_API_KEY')
            if not openai_api_key:
                # A failure, not a skip: new and changed events would silently
                # keep stale or missing classifications.
                message = "OPENAI_API_KEY not set - ASD risk classification did not run"
                logger.error(message)
                self.results['classification']['success'] = False
                self.results['classification'].setdefault('errors', []).append(message)
                return False

            # Initialize classifier
            logger.info("Initializing ASD risk classifier...")
            classifier = ASDRiskClassifier(self.db_path, api_key=openai_api_key)

            try:
                # Events needing classification: none yet, or classified from
                # inputs that have since changed (a merge, a corrected victim or
                # date, a re-enrichment, a new prompt version or model).
                total_events = len(classifier.get_events(limit=10**9, prioritize_unclassified=False))
                unclassified_count = classifier.stale_count()
                classified_events = total_events - unclassified_count

                logger.info(f"Total active events: {total_events}")
                logger.info(f"Classified from current inputs: {classified_events}")
                logger.info(f"Need classification (new or changed): {unclassified_count}")

                if unclassified_count == 0:
                    logger.info("All events are already classified - skipping classification phase")
                    self.results['classification']['success'] = True
                    self.results['classification']['cache_hits'] = classified_events
                    return True

                # Determine limit based on args
                limit = None
                if hasattr(args, 'classify_limit') and args.classify_limit:
                    limit = args.classify_limit
                    logger.info(f"Classifying up to {limit} events (--classify-limit specified)")
                else:
                    # No limit - classify all unclassified events
                    limit = total_events
                    logger.info(f"Classifying all {unclassified_count} new or changed events")

                # Process events (this will use cache for already-classified events)
                logger.info("Starting classification...")
                results = classifier.process_events(limit=limit, force_reclassify=False)

                # Track results
                new_classifications = classifier.api_calls  # New API calls made
                cache_hits = classifier.cache_hits

                self.results['classification']['events_classified'] = new_classifications
                self.results['classification']['cache_hits'] = cache_hits
                if classifier.failed_count:
                    # The failed events keep their old (stale) classification
                    # and are retried next run; the phase must not say success.
                    raise RuntimeError(f"{classifier.failed_count} event(s) could not be classified "
                                       f"(e.g. {classifier.failed_event_ids[:3]}); they stay stale")
                self.results['classification']['success'] = True

                logger.info(f"\nClassification complete:")
                logger.info(f"  New classifications: {new_classifications}")
                logger.info(f"  Cache hits: {cache_hits}")
                logger.info(f"  Total tokens used: {classifier.total_tokens}")

                # The risk matrices are not written here: they are built from the publication
                # snapshot with the dashboards, and only after the quality gate passes.
                logger.info("Risk matrices are written at publication (dashboard phase), from the "
                            "published snapshot, after the quality gate")
                return True

            finally:
                classifier.close()

        except Exception as e:
            logger.error(f"ASD classification phase failed: {e}")
            self.results['classification']['errors'].append(str(e))
            return False

    def run_dashboard_phase(self, args) -> bool:
        """Run the dashboard generation phase."""
        self.print_header("PHASE 2: STATIC DASHBOARD GENERATION")

        try:
            # Check if database exists and has required tables
            if not os.path.exists(self.db_path):
                raise FileNotFoundError(f"Database not found: {self.db_path}")

            # Verify database schema
            try:
                with get_connection(self.db_path) as conn:
                    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = [row[0] for row in cursor.fetchall()]

                    required_tables = ['DeduplicatedEvents', 'EntitiesV2', 'DeduplicatedEventEntities']
                    missing_tables = [table for table in required_tables if table not in tables]

                    if missing_tables:
                        raise RuntimeError(f"Missing required database tables: {missing_tables}")
            except Exception as e:
                raise RuntimeError(f"Database schema validation failed: {e}")

            # One consistent snapshot of the database; the quality gate and every output (the
            # dashboards, the AI incident report, the recurrence page, the risk matrices) are
            # built from it into staging and moved into place together, only if the gate passes.
            # A blocked or failed publication leaves the previous outputs untouched.
            from cyber_data_collector.publication.outputs import publish
            if getattr(args, 'skip_quality_gate', False):
                logger.warning("Quality gate SKIPPED (--skip-quality-gate): publishing unchecked")
            result = publish(Path(self.db_path), Path(args.out_dir),
                             Path(getattr(args, 'risk_matrix_dir', 'risk_matrix')),
                             skip_gate=getattr(args, 'skip_quality_gate', False))
            published = result.files
            logger.info("Published %d file(s) from snapshot %s (quality gate: %s)", len(published),
                        result.snapshot_id, result.gate)
            for warning in result.warnings:
                self.results['dashboard'].setdefault('warnings', []).append(warning)

            self.results['dashboard']['success'] = True
            self.results['dashboard']['files_created'] = published
            return True

        except Exception as e:
            logger.error(f"Dashboard phase failed: {e}")
            self.results['dashboard']['errors'].append(str(e))
            return False

    @staticmethod
    def _validate_dashboard(path: Optional[str]) -> List[str]:
        """Reasons a built dashboard must not be published; empty when it is fine.

        The check lives in ``cyber_data_collector.publication.outputs.validate_dashboard``,
        which the publication step runs on the staged page before anything is moved.
        """
        from cyber_data_collector.publication.outputs import validate_dashboard
        return validate_dashboard(path)


    def outcome(self, failed_phases) -> str:
        """'ok', 'failed' (a required phase failed) or 'degraded' (all phases ran, but a step
        inside one failed - e.g. a data-hygiene step or a high enrichment failure rate)."""
        if failed_phases:
            return "failed"
        if any(r.get('errors') for r in self.results.values()):
            return "degraded"
        from cyber_data_collector.utils.api_budget import budget
        if budget.exhausted():
            # Work was left for the next run; the run did not finish its job.
            return "degraded"
        return "ok"

    async def run_pipeline(self, args):
        """Run the complete unified pipeline. Returns 'ok', 'degraded' or 'failed'.

        A failed required phase stops the run unless --continue-on-error, and is
        reported as a failure either way: the run used to return success after
        continuing past a failed phase.
        """
        failed_phases = []
        print("🚀 AUSTRALIAN CYBER EVENTS UNIFIED PIPELINE")
        print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Database: {self.db_path}")

        # Phase 1: Discovery & Deduplication
        if not args.dashboard_only and not args.re_enrich and not args.classify_only:
            discovery_success = await self.run_discovery_phase(args)
            if not discovery_success:
                failed_phases.append("discovery")
            if not discovery_success and not args.continue_on_error:
                logger.error("Discovery phase failed - stopping pipeline")
                return "failed"
        elif args.re_enrich:
            logger.info("Skipping discovery phase (--re-enrich specified)")
        elif args.classify_only:
            logger.info("Skipping discovery phase (--classify-only specified)")
        else:
            logger.info("Skipping discovery phase (--dashboard-only specified)")

        # Phase 1b: Re-enrichment (if requested)
        if args.re_enrich:
            reenrich_success = await self.run_reenrichment_phase(args)
            if not reenrich_success:
                failed_phases.append("reenrich")
            if not reenrich_success and not args.continue_on_error:
                logger.error("Re-enrichment phase failed - stopping pipeline")
                return "failed"

            # Run deduplication after re-enrichment
            dedup_success = self.run_deduplication_phase(args)
            if not dedup_success:
                failed_phases.append("dedup")
            if not dedup_success and not args.continue_on_error:
                logger.error("Deduplication phase failed - stopping pipeline")
                return "failed"

        # Phase 2: ASD Risk Classification
        if not args.dashboard_only and not args.discover_only and not args.skip_classification:
            classification_success = self.run_classification_phase(args)
            if not classification_success:
                failed_phases.append("classification")
            if not classification_success and not args.continue_on_error:
                logger.error("ASD classification phase failed - stopping pipeline")
                return "failed"
        elif args.skip_classification:
            logger.info("Skipping classification phase (--skip-classification specified)")
        elif args.dashboard_only:
            logger.info("Skipping classification phase (--dashboard-only specified)")
        elif args.discover_only:
            logger.info("Skipping classification phase (--discover-only specified)")

        # Phase 2b: breach source and personal-information types (Jev, stored;
        # only new or changed events are asked). Feeds the dashboard's OAIC
        # breach-source comparison, which showed 0 while this never ran.
        if (not args.dashboard_only and not args.discover_only and not args.classify_only
                and not args.skip_classification):
            from scripts import enrich_pii_and_source_category
            try:
                code = run_step(enrich_pii_and_source_category.main, ["--db", self.db_path])
            except Exception as exc:  # noqa: BLE001 - recorded, run ends degraded
                code = f"error: {exc}"
            if code != 0:
                message = f"Breach-source classification did not complete ({code})"
                logger.error(message)
                self.results['classification'].setdefault('errors', []).append(message)

            # AI involvement (Jev: AI-attacker / AI-vulnerability / AI-misuse / No-AI,
            # with all four probabilities), and a description of each AI incident for the
            # AI incident report. Stored, so only new or changed events are asked.
            from scripts import classify_ai_involvement
            try:
                code = run_step(classify_ai_involvement.main, ["--db", self.db_path])
            except Exception as exc:  # noqa: BLE001 - recorded, run ends degraded
                code = f"error: {exc}"
            if code != 0:
                message = f"AI-involvement classification did not complete ({code})"
                logger.error(message)
                self.results['classification'].setdefault('errors', []).append(message)

        # Phase 2c: OAIC statistics (data.gov.au), validated; a failing extract
        # keeps the last valid dataset and the run ends degraded.
        if not args.discover_only and not args.classify_only and not args.dashboard_only:
            import subprocess
            oaic = subprocess.run([sys.executable, "scripts/oaic/refresh_oaic_statistics.py"])
            if oaic.returncode != 0:
                message = (f"OAIC statistics refresh failed (exit {oaic.returncode}); "
                           "the last valid dataset is kept")
                logger.error(message)
                self.results['dashboard'].setdefault('errors', []).append(message)

        # Phase 3: Dashboard
        if not args.discover_only and not args.classify_only:
            dashboard_success = self.run_dashboard_phase(args)
            if not dashboard_success:
                failed_phases.append("dashboard")
            if not dashboard_success and not args.continue_on_error:
                logger.error("Dashboard phase failed - stopping pipeline")
                return "failed"
        elif args.classify_only:
            logger.info("Skipping dashboard phase (--classify-only specified)")
        else:
            logger.info("Skipping dashboard phase (--discover-only specified)")

        # Print final summary
        self.print_summary()

        # Print API token usage and cost report
        from cyber_data_collector.utils.token_tracker import tracker
        tracker.log_report()
        from cyber_data_collector.utils.api_budget import budget
        logger.info(budget.summary())
        if budget.exhausted():
            logger.warning("API budget exhausted (%s): unfinished work is left for the next run",
                           ", ".join(f"{p} refused {n}" for p, n in budget.exhausted().items()))

        result = self.outcome(failed_phases)
        if result != "ok":
            logger.error("Pipeline finished %s%s", result.upper(),
                         f" (failed phases: {', '.join(failed_phases)})" if failed_phases else
                         " (a step inside a phase failed - see the run summary)")
        return result


def main():
    """Main function with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Unified Australian Cyber Events Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full pipeline (all phases)
  python run_full_pipeline.py

  # Run specific phases
  python run_full_pipeline.py --discover-only
  python run_full_pipeline.py --dashboard-only

  # Control discovery parameters
    python run_full_pipeline.py --max-events 500 --source Perplexity OAIC
        """
    )

    # Phase control
    parser.add_argument('--discover-only', action='store_true',
                        help='Run only discovery phase')
    parser.add_argument('--classify-only', action='store_true',
                        help='Run only ASD risk classification phase (no discovery or dashboard)')
    parser.add_argument('--dashboard-only', action='store_true',
                        help='Run only dashboard generation phase')
    parser.add_argument('--re-enrich', action='store_true',
                        help='Re-enrich existing events with updated Perplexity prompt (includes deduplication and dashboard)')
    parser.add_argument('--re-enrich-limit', type=int,
                        help='Limit number of events to re-enrich (default: all events)')
    parser.add_argument('--skip-classification', action='store_true',
                        help='Skip ASD risk classification phase (faster pipeline)')
    parser.add_argument('--classify-limit', type=int,
                        help='Limit number of events to classify (default: all unclassified events)')
    parser.add_argument('--force-dedup', action='store_true',
                        help='Force full deduplication rebuild (skip incremental mode)')
    parser.add_argument('--skip-data-hygiene', action='store_true',
                        help='Skip rejecting non-incident pages and correcting victims '
                             'that the event\'s own records contradict')
    parser.add_argument('--skip-recalibration', action='store_true',
                        help='Do not recompute calibrated cut-offs whose evidence processing changed '
                             '(those steps then only report; recalibrate spends Jev/escalation calls)')
    parser.add_argument('--report-event-rejections', action='store_true',
                        help='Report Jev\'s rejections (non-incidents) and merges (duplicates) '
                             'without applying them, even when calibrated cut-offs exist')
    parser.add_argument('--evidence-recovery-limit', type=int, default=100, metavar='N',
                        help='Incidents with no usable evidence given new retrieval work per run '
                             '(re-fetches, labelled reports, replacement searches)')
    parser.add_argument('--skip-recurrence-check', action='store_true',
                        help='Skip re-checking repeat attacks on one entity that '
                             'fall within the recurrence window')
    parser.add_argument('--recurrence-window', type=int, default=90,
                        help='Inter-event gap in days below which a repeat attack '
                             'is re-checked as possible re-reporting (default: 90)')
    parser.add_argument('--recurrence-unverified-dates', choices=('stored', 'exclude'), default='stored',
                        help="Events whose date precision is unverified have no interval: 'stored' "
                             "(default) lets their stored date stand in; 'exclude' leaves them out "
                             "of the recurrence check")
    parser.add_argument('--recurrence-min-certainty', type=float, default=0.85,
                        help='Merge a suspected re-report only at or above this '
                             'certainty (default: 0.85)')
    parser.add_argument('--skip-missed-merge-check', action='store_true',
                        help='Skip the multi-key search for duplicates the '
                             'incremental pass missed')
    parser.add_argument('--missed-merge-apply', action='store_true',
                        help='Merge missed-duplicate findings automatically '
                             '(default: write them for review only)')
    parser.add_argument('--missed-merge-days', type=int, default=120,
                        help='Only judge candidate pairs touching events changed '
                             'in the last N days (default: 120)')
    parser.add_argument('--missed-merge-min-certainty', type=float, default=0.9,
                        help='Merge a missed duplicate only at or above this '
                             'certainty (default: 0.9)')
    parser.add_argument('--skip-entity-sizing', action='store_true',
                        help='Skip estimating an ordinal size band for new entities')
    parser.add_argument('--entity-size-limit', type=int, default=None,
                        help='Cap how many entities are sized per run (default: all)')
    parser.add_argument('--continue-on-error', action='store_true',
                        help='Continue to next phase even if previous phase fails')

    # Discovery parameters
    parser.add_argument('--source', choices=['GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC',
                                             'CyberDaily'],
                        help='Data sources to use (can specify multiple)', action='append', nargs='+')
    parser.add_argument('--cyberdaily-backfill-since', default='2020-01', metavar='YYYY-MM',
                        help="With CyberDaily: search every processed month back to this one that Cyber Daily "
                             "has not searched yet (resumable; 'none' to skip). Default: 2020-01")
    parser.add_argument('--cyberdaily-refetch-days', type=int, default=90,
                        help='With CyberDaily: re-fetch stored stories published or discovered in the last N '
                             'days, plus any cut-off or short ones (default: 90)')
    parser.add_argument('--cyberdaily-refetch-all', action='store_true',
                        help='With CyberDaily: re-fetch every stored Cyber Daily story')
    parser.add_argument('--skip-cyberdaily-refetch', action='store_true',
                        help='With CyberDaily: do not re-fetch stored stories this run')
    parser.add_argument('--max-events', type=int, default=1000,
                        help='Maximum events to process per source per month (default: 1000)')
    parser.add_argument('--days', type=int, default=0,
                        help='Number of days to look back for discovery (0 = full history)')

    # Dashboard parameters
    parser.add_argument('--out-dir', default='dashboard',
                        help='Output directory for static dashboard (default: dashboard)')
    parser.add_argument('--risk-matrix-dir', default='risk_matrix',
                        help='Output directory for the ASD risk-matrix workbooks (default: risk_matrix); '
                             'published with the dashboards from the same snapshot')

    # Database
    parser.add_argument('--db-path', default=None,
                        help='Path to SQLite database file (defaults to DATABASE_URL/.env setting)')

    parser.add_argument('--api-budget', nargs='+', default=None, metavar='PROVIDER=N',
                        help='Requests allowed this run per paid API, e.g. openai=3000 perplexity=1500 '
                             'typesafe=20000 (default: API_BUDGET_<PROVIDER> env, else unlimited). '
                             'Refused work is left for the next run.')
    parser.add_argument('--skip-quality-gate', action='store_true',
                        help='Publish the dashboard even if blocking data-quality checks fail (logged)')
    parser.add_argument('--page-gate-report-only', action='store_true',
                        help='Discovery page gate logs what it would drop but drops nothing')
    parser.add_argument('--enrich-limit', type=int, default=500,
                        help='Most records the automatic Perplexity enrichment takes per run; '
                             'the rest of the backlog waits for the next run (0 = no limit, default 500)')

    add_model_arguments(parser)
    args = parser.parse_args()
    apply_model_arguments(args)
    from cyber_data_collector.utils.api_budget import configure as configure_api_budget
    configure_api_budget(args.api_budget)
    from cyber_data_collector.utils.config_manager import resolve_db_path
    args.db_path = resolve_db_path(args.db_path)
    if args.source:
        args.source = [source for group in args.source for source in group]

    # Validate arguments
    if args.discover_only and args.dashboard_only:
        parser.error("Cannot specify both --discover-only and --dashboard-only")

    if args.discover_only and args.classify_only:
        parser.error("Cannot specify both --discover-only and --classify-only")

    if args.classify_only and args.dashboard_only:
        parser.error("Cannot specify both --classify-only and --dashboard-only")

    if args.re_enrich and (args.discover_only or args.dashboard_only or args.classify_only):
        parser.error("--re-enrich cannot be combined with --discover-only, --dashboard-only, or --classify-only")

    if args.skip_classification and args.classify_only:
        parser.error("Cannot specify both --skip-classification and --classify-only")

    # Run pipeline
    pipeline = UnifiedPipeline(args.db_path)

    exit_code = 1
    try:
        try:
            # 0 ok, 1 a required phase failed, 2 completed with failed steps.
            result = asyncio.run(pipeline.run_pipeline(args))
            exit_code = {"ok": 0, "degraded": 2}.get(result, 1)
        except KeyboardInterrupt:
            print("\nPipeline interrupted by user")
            logger.warning("Pipeline interrupted by user (Ctrl+C)")
            exit_code = 130
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            exit_code = 1
    finally:
        # Always print the end-of-run summary - success, failure, and
        # Ctrl+C alike. Includes phase-level results so the user can
        # see exactly which phase passed/failed at a glance.
        try:
            print_run_summary(phase_results=pipeline.results)
        except Exception:
            pass
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
