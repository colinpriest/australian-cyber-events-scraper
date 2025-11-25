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
   - Performs initial GPT-4o-mini filtering (fast, basic quality check)

2. **Perplexity AI Enrichment**
   - AUTOMATICALLY runs after discovery to enrich all new events with Perplexity AI
   - Extracts: formal entity names, threat actors, attack methods, victim counts
   - Uses sophisticated multi-source verification
   - Much higher quality than initial GPT-4o-mini pass

3. **Global Deduplication**
   - Merges duplicate events using entity-based matching
   - RULE 1: Same entity + same date → merge
   - RULE 2: Same entity + similar titles (0.15 threshold) → merge
   - Uses EARLIEST date for merged events

4. **ASD Risk Classification**
   - Classifies events using Australian Signals Directorate (ASD) risk matrix framework
   - Assigns severity categories (C1-C6) and stakeholder levels
   - Uses GPT-4o for intelligent classification based on impact and records affected
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

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Add current directory to path for imports
sys.path.append(str(Path(__file__).parent))

# Import existing components
from discover_enrich_events import EventDiscoveryEnrichmentPipeline
from build_static_dashboard import (
    build_html, get_connection, get_monthly_event_counts, get_monthly_severity_trends,
    get_monthly_records_affected, get_monthly_event_type_mix, get_overall_event_type_mix,
    get_entity_type_distribution, get_records_affected_histogram, get_half_yearly_database_counts,
    prepare_oaic_comparison_data, load_oaic_data, compute_monthly_counts_stats,
    compute_event_type_correlation_matrix, get_maximum_severity_per_month,
    get_median_severity_per_month, get_maximum_records_affected_per_month,
    get_severity_by_industry, get_severity_by_attack_type, get_records_affected_by_attack_type,
    get_asd_risk_matrix, prepare_oaic_cyber_incidents_data, prepare_oaic_attack_types_data,
    prepare_oaic_sectors_data, prepare_oaic_individuals_affected_data
)
from cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine
from perplexity_backfill_events import PerplexityBackfillProcessor
from asd_risk_classifier import ASDRiskClassifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('unified_pipeline.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


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
        2. Performs initial GPT-4o-mini filtering (fast pass)
        3. AUTOMATICALLY enriches all new events with Perplexity AI
        4. Runs global deduplication
        """
        self.print_header("PHASE 1: EVENT DISCOVERY & PERPLEXITY ENRICHMENT")

        try:
            # Step 1: Run initial discovery with GPT-4o-mini filtering
            logger.info("Step 1/3: Discovering events from sources (with initial GPT-4o-mini filtering)...")
            pipeline = EventDiscoveryEnrichmentPipeline(self.db_path)

            await pipeline.discover_events(
                source_types=args.source,
                date_range_days=args.days,
                max_events=args.max_events
            )

            pipeline.print_statistics()
            pipeline.print_filtering_statistics()

            # Capture initial discovery count
            initial_events = pipeline.stats.get('events_discovered', 0)
            self.results['discovery']['events_found'] = initial_events

            pipeline.close()
            logger.info(f"Initial discovery complete: {initial_events} events found")

            # Step 2: Automatically run Perplexity enrichment on newly discovered events
            logger.info("\nStep 2/3: Enriching events with Perplexity AI (high-quality enrichment)...")
            logger.info("This automatically upgrades GPT-4o-mini enrichment to Perplexity AI...")

            perplexity_success = await self._run_auto_perplexity_enrichment(args)

            if not perplexity_success:
                logger.warning("Perplexity enrichment had errors, but continuing with pipeline...")

            # Step 3: Run global deduplication
            logger.info("\nStep 3/3: Running global deduplication...")
            dedup_success = self.run_deduplication_phase(args)

            if not dedup_success:
                logger.warning("Deduplication had errors, but discovery phase completed")

            self.results['discovery']['success'] = True
            logger.info(f"Discovery phase complete: {initial_events} events discovered and enriched")
            return True

        except Exception as e:
            logger.error(f"Discovery phase failed: {e}")
            self.results['discovery']['errors'].append(str(e))
            return False

    async def _run_auto_perplexity_enrichment(self, args) -> bool:
        """
        Automatically run Perplexity enrichment on events discovered in this session.
        This upgrades the initial GPT-4o-mini enrichment to high-quality Perplexity AI.
        """
        try:
            # Load Perplexity API key
            perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')
            if not perplexity_api_key:
                logger.warning("Perplexity API key not found - skipping automatic enrichment")
                return False

            # Initialize enrichment engine
            db = CyberEventDataV2(self.db_path)
            perplexity_engine = PerplexityEnrichmentEngine(perplexity_api_key)
            processor = PerplexityBackfillProcessor(
                db=db,
                perplexity_engine=perplexity_engine,
                dry_run=False
            )

            # Get events needing enrichment (all events without Perplexity data)
            events = processor.get_events_needing_enrichment(limit=None)

            if not events:
                logger.info("No events need Perplexity enrichment")
                return True

            logger.info(f"Enriching {len(events)} events with Perplexity AI...")

            # Enrich events
            enriched_count = 0
            failed_count = 0

            for event in events:
                try:
                    enriched_data = await processor.enrich_event(event)
                    if enriched_data:
                        processor.apply_enrichment_to_database(enriched_data)
                        enriched_count += 1
                        if enriched_count % 10 == 0:
                            logger.info(f"Progress: {enriched_count}/{len(events)} events enriched")
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.warning(f"Failed to enrich event: {e}")
                    failed_count += 1

            logger.info(f"Perplexity enrichment complete: {enriched_count} enriched, {failed_count} failed")
            self.results['reenrichment']['success'] = True
            self.results['reenrichment']['events_enriched'] = enriched_count
            return True

        except Exception as e:
            logger.error(f"Automatic Perplexity enrichment failed: {e}")
            return False

    async def run_reenrichment_phase(self, args) -> bool:
        """Run re-enrichment on existing events with updated Perplexity prompt."""
        self.print_header("PHASE: RE-ENRICHMENT OF EXISTING EVENTS")

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

            logger.info(f"Re-enriching {len(events)} events...")

            # Process events
            enriched_count = 0
            failed_count = 0

            for event in events:
                try:
                    enriched_data = await processor.enrich_event(event)
                    if enriched_data:
                        processor.apply_enrichment_to_database(enriched_data)
                        enriched_count += 1
                        if enriched_count % 10 == 0:
                            logger.info(f"Progress: {enriched_count}/{len(events)} events re-enriched")
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.error(f"Failed to enrich event {event.get('enriched_event_id')}: {e}")
                    failed_count += 1
                    self.results['reenrichment']['errors'].append(str(e))

            # Print statistics
            logger.info(f"\nRe-enrichment complete:")
            logger.info(f"  Successfully enriched: {enriched_count}")
            logger.info(f"  Failed: {failed_count}")
            logger.info(f"  Total processed: {len(events)}")

            self.results['reenrichment']['success'] = True
            self.results['reenrichment']['events_enriched'] = enriched_count
            return True

        except Exception as e:
            logger.error(f"Re-enrichment phase failed: {e}")
            self.results['reenrichment']['errors'].append(str(e))
            return False

    def run_deduplication_phase(self, args) -> bool:
        """Run global deduplication on all enriched events."""
        self.print_header("PHASE: GLOBAL DEDUPLICATION")

        try:
            logger.info("Running global deduplication...")

            # Run the global deduplication script
            import subprocess
            result = subprocess.run(
                ['python', 'run_global_deduplication.py', '--db-path', self.db_path],
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout
            )

            if result.returncode != 0:
                raise RuntimeError(f"Deduplication failed with exit code {result.returncode}: {result.stderr}")

            # Parse output for statistics
            output = result.stdout
            logger.info(output)

            # Extract deduplicated count from output
            import re
            match = re.search(r'Created (\d+) deduplicated events', output)
            if match:
                dedup_count = int(match.group(1))
                self.results['deduplication']['events_deduplicated'] = dedup_count

            self.results['deduplication']['success'] = True
            logger.info("Global deduplication completed successfully")
            return True

        except Exception as e:
            logger.error(f"Deduplication phase failed: {e}")
            self.results['deduplication']['errors'].append(str(e))
            return False

    def run_classification_phase(self, args) -> bool:
        """
        Run ASD risk classification on unclassified events.

        This phase:
        1. Identifies events that don't have ASD risk classifications
        2. Classifies them using GPT-4o based on ASD risk matrix framework
        3. Stores classifications in ASDRiskClassifications table
        4. Incremental: Only processes new events, uses cache for existing ones
        """
        self.print_header("PHASE: ASD RISK CLASSIFICATION")

        try:
            # Check if OpenAI API key is available
            openai_api_key = os.getenv('OPENAI_API_KEY')
            if not openai_api_key:
                logger.warning("OpenAI API key not found - skipping ASD risk classification")
                logger.warning("Set OPENAI_API_KEY in .env to enable classification")
                self.results['classification']['success'] = True  # Not an error, just skipped
                return True

            # Initialize classifier
            logger.info("Initializing ASD risk classifier...")
            classifier = ASDRiskClassifier(self.db_path, model='gpt-4o', api_key=openai_api_key)

            try:
                # Get count of unclassified events
                import sqlite3
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Count total active events
                cursor.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'")
                total_events = cursor.fetchone()[0]

                # Count events already classified
                cursor.execute("""
                    SELECT COUNT(DISTINCT de.deduplicated_event_id)
                    FROM DeduplicatedEvents de
                    INNER JOIN ASDRiskClassifications arc
                        ON de.deduplicated_event_id = arc.deduplicated_event_id
                    WHERE de.status = 'Active'
                """)
                classified_events = cursor.fetchone()[0]
                conn.close()

                unclassified_count = total_events - classified_events

                logger.info(f"Total active events: {total_events}")
                logger.info(f"Already classified: {classified_events}")
                logger.info(f"Need classification: {unclassified_count}")

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
                    logger.info(f"Classifying all {unclassified_count} unclassified events")

                # Process events (this will use cache for already-classified events)
                logger.info("Starting classification...")
                results = classifier.process_events(limit=limit, force_reclassify=False)

                # Track results
                new_classifications = classifier.api_calls  # New API calls made
                cache_hits = classifier.cache_hits

                self.results['classification']['events_classified'] = new_classifications
                self.results['classification']['cache_hits'] = cache_hits
                self.results['classification']['success'] = True

                logger.info(f"\nClassification complete:")
                logger.info(f"  New classifications: {new_classifications}")
                logger.info(f"  Cache hits: {cache_hits}")
                logger.info(f"  Total tokens used: {classifier.total_tokens}")

                # Export risk matrices
                logger.info("\nExporting risk matrices...")
                output_path = Path('risk_matrix')
                excel_files = classifier.compile_risk_matrix(output_path)

                if excel_files:
                    logger.info(f"Risk matrices exported to:")
                    for excel_file in excel_files:
                        logger.info(f"  - {excel_file}")

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

            files_created = []

            # Generate static dashboard
            logger.info("Generating static HTML dashboard...")
            static_file = self._generate_static_dashboard(args)
            if static_file:
                files_created.append(static_file)

            self.results['dashboard']['success'] = True
            self.results['dashboard']['files_created'] = files_created
            return True

        except Exception as e:
            logger.error(f"Dashboard phase failed: {e}")
            self.results['dashboard']['errors'].append(str(e))
            return False

    def _generate_static_dashboard(self, args) -> Optional[str]:
        """Generate static HTML dashboard."""
        try:
            from datetime import date
            
            start_date = '2020-01-01'
            end_date = date.today().strftime('%Y-%m-%d')
            
            # Create output directory
            out_dir = Path(args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / 'index.html'
            
            # Load OAIC data
            oaic_data = load_oaic_data()

            # Query database and build dashboard
            with get_connection(self.db_path) as conn:
                monthly_counts = get_monthly_event_counts(conn, start_date, end_date)
                database_half_yearly = get_half_yearly_database_counts(conn, start_date, end_date)
                oaic_comparison = prepare_oaic_comparison_data(database_half_yearly, oaic_data)

                # Prepare all OAIC data components
                oaic_cyber_incidents = prepare_oaic_cyber_incidents_data(oaic_data)
                oaic_attack_types = prepare_oaic_attack_types_data(oaic_data)
                oaic_sectors = prepare_oaic_sectors_data(oaic_data, self.db_path)
                oaic_individuals_affected = prepare_oaic_individuals_affected_data(oaic_data, self.db_path)

                # Get current year for ASD risk matrix
                current_year = date.today().year

                data = {
                    'monthly_counts': monthly_counts,
                    'severity_trends': get_monthly_severity_trends(conn, start_date, end_date),
                    'records_affected': get_monthly_records_affected(conn, start_date, end_date),
                    'event_type_mix': get_monthly_event_type_mix(conn, start_date, end_date),
                    'overall_event_type_mix': get_overall_event_type_mix(conn, start_date, end_date),
                    'entity_types': get_entity_type_distribution(conn, start_date, end_date),
                    'records_histogram': get_records_affected_histogram(conn, start_date, end_date),
                    'max_severity_per_month': get_maximum_severity_per_month(conn, start_date, end_date),
                    'median_severity_per_month': get_median_severity_per_month(conn, start_date, end_date),
                    'max_records_per_month': get_maximum_records_affected_per_month(conn, start_date, end_date),
                    'severity_by_industry': get_severity_by_industry(conn, start_date, end_date),
                    'severity_by_attack_type': get_severity_by_attack_type(conn, start_date, end_date),
                    'records_by_attack_type': get_records_affected_by_attack_type(conn, start_date, end_date),
                    'monthly_counts_stats': compute_monthly_counts_stats(monthly_counts),
                    'event_type_correlation': compute_event_type_correlation_matrix(get_monthly_event_type_mix(conn, start_date, end_date)),
                    'oaic_comparison': oaic_comparison,
                    'oaic_cyber_incidents': oaic_cyber_incidents,
                    'oaic_attack_types': oaic_attack_types,
                    'oaic_sectors': oaic_sectors,
                    'oaic_individuals_affected': oaic_individuals_affected,
                    'asd_risk_all': get_asd_risk_matrix(conn),
                    'asd_risk_current': get_asd_risk_matrix(conn, current_year),
                }
            
            # Generate HTML
            html = build_html(data, start_date, end_date)
            
            # Write to file
            with open(out_file, 'w', encoding='utf-8') as f:
                f.write(html)
            
            logger.info(f"Static dashboard created: {out_file}")
            return str(out_file)
            
        except Exception as e:
            logger.error(f"Static dashboard generation failed: {e}")
            self.results['dashboard']['errors'].append(f"Static dashboard: {e}")
            return None


    async def run_pipeline(self, args):
        """Run the complete unified pipeline."""
        print("🚀 AUSTRALIAN CYBER EVENTS UNIFIED PIPELINE")
        print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Database: {self.db_path}")

        # Phase 1: Discovery & Deduplication
        if not args.dashboard_only and not args.re_enrich and not args.classify_only:
            discovery_success = await self.run_discovery_phase(args)
            if not discovery_success and not args.continue_on_error:
                logger.error("Discovery phase failed - stopping pipeline")
                return False
        elif args.re_enrich:
            logger.info("Skipping discovery phase (--re-enrich specified)")
        elif args.classify_only:
            logger.info("Skipping discovery phase (--classify-only specified)")
        else:
            logger.info("Skipping discovery phase (--dashboard-only specified)")

        # Phase 1b: Re-enrichment (if requested)
        if args.re_enrich:
            reenrich_success = await self.run_reenrichment_phase(args)
            if not reenrich_success and not args.continue_on_error:
                logger.error("Re-enrichment phase failed - stopping pipeline")
                return False

            # Run deduplication after re-enrichment
            dedup_success = self.run_deduplication_phase(args)
            if not dedup_success and not args.continue_on_error:
                logger.error("Deduplication phase failed - stopping pipeline")
                return False

        # Phase 2: ASD Risk Classification
        if not args.dashboard_only and not args.discover_only and not args.skip_classification:
            classification_success = self.run_classification_phase(args)
            if not classification_success and not args.continue_on_error:
                logger.error("ASD classification phase failed - stopping pipeline")
                return False
        elif args.skip_classification:
            logger.info("Skipping classification phase (--skip-classification specified)")
        elif args.dashboard_only:
            logger.info("Skipping classification phase (--dashboard-only specified)")
        elif args.discover_only:
            logger.info("Skipping classification phase (--discover-only specified)")

        # Phase 3: Dashboard
        if not args.discover_only and not args.classify_only:
            dashboard_success = self.run_dashboard_phase(args)
            if not dashboard_success and not args.continue_on_error:
                logger.error("Dashboard phase failed - stopping pipeline")
                return False
        elif args.classify_only:
            logger.info("Skipping dashboard phase (--classify-only specified)")
        else:
            logger.info("Skipping dashboard phase (--discover-only specified)")

        # Print final summary
        self.print_summary()
        return True


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
    parser.add_argument('--continue-on-error', action='store_true',
                        help='Continue to next phase even if previous phase fails')

    # Discovery parameters
    parser.add_argument('--source', choices=['GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC'],
                        help='Data sources to use (can specify multiple)', action='append')
    parser.add_argument('--max-events', type=int, default=1000,
                        help='Maximum events to process per source per month (default: 1000)')
    parser.add_argument('--days', type=int, default=7,
                        help='Number of days to look back for discovery (default: 7)')

    # Dashboard parameters
    parser.add_argument('--out-dir', default='dashboard',
                        help='Output directory for static dashboard (default: dashboard)')

    # Database
    parser.add_argument('--db-path', default='instance/cyber_events.db',
                        help='Path to SQLite database file')

    args = parser.parse_args()

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
    
    try:
        success = asyncio.run(pipeline.run_pipeline(args))
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nPipeline interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
