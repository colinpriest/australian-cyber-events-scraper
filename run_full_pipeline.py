#!/usr/bin/env python3
"""
Unified Australian Cyber Events Pipeline

This script combines event discovery, deduplication, and dashboard generation
into a single executable command. It orchestrates the three main phases:

1. Discovery & Enrichment (includes deduplication)
2. Dashboard Generation (static HTML and/or Flask server)

Usage:
    # Run full pipeline (all phases)
    python run_full_pipeline.py

    # Run specific phases
    python run_full_pipeline.py --discover-only
    python run_full_pipeline.py --dashboard-only

    # Control discovery parameters
    python run_full_pipeline.py --max-events 500 --source Perplexity OAIC

    # Dashboard options
    python run_full_pipeline.py --dashboard-type static  # or 'flask' or 'both'
    python run_full_pipeline.py --launch-server  # Auto-launch Flask server
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

# Add current directory to path for imports
sys.path.append(str(Path(__file__).parent))

# Import existing components
from discover_enrich_events import EventDiscoveryEnrichmentPipeline, check_gdelt_authentication
from build_static_dashboard import build_html, get_connection, get_monthly_event_counts, get_monthly_severity_trends, get_monthly_records_affected, get_monthly_event_type_mix, get_overall_event_type_mix, get_entity_type_distribution, get_records_affected_histogram, get_half_yearly_database_counts, prepare_oaic_comparison_data, load_oaic_data, compute_monthly_counts_stats, compute_event_type_correlation_matrix, get_maximum_severity_per_month, get_median_severity_per_month, get_severity_by_industry, get_severity_by_attack_type
from generate_dashboard import create_flask_app, DashboardDataService

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
            'dashboard': {'success': False, 'files_created': [], 'errors': []},
            'flask_server': {'success': False, 'port': None, 'errors': []}
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
        
        # Flask server results
        flask = self.results['flask_server']
        if flask['success']:
            print(f"🌐 FLASK SERVER: ✅ RUNNING on port {flask['port']}")
        elif flask['errors']:
            print(f"🌐 FLASK SERVER: ❌ FAILED")
            for error in flask['errors'][:3]:
                print(f"   - {error}")
        
        print("="*80)

    async def run_discovery_phase(self, args) -> bool:
        """Run the discovery and enrichment phase (includes deduplication)."""
        self.print_header("PHASE 1: EVENT DISCOVERY & DEDUPLICATION")
        
        try:
            # Check GDELT authentication if needed
            if not args.source or 'GDELT' in args.source:
                logger.info("Checking GDELT authentication...")
                if not await check_gdelt_authentication():
                    logger.warning("GDELT authentication failed - continuing without GDELT")
                    if args.source and 'GDELT' in args.source:
                        args.source.remove('GDELT')
                        if not args.source:
                            args.source = ['Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC']

            # Initialize pipeline
            pipeline = EventDiscoveryEnrichmentPipeline(self.db_path)
            
            # Run discovery (includes deduplication)
            logger.info("Starting event discovery and enrichment...")
            await pipeline.discover_events(
                source_types=args.source,
                date_range_days=args.days,
                max_events=args.max_events
            )
            
            # Print statistics
            pipeline.print_statistics()
            pipeline.print_filtering_statistics()
            
            # Capture results
            self.results['discovery']['success'] = True
            self.results['discovery']['events_found'] = pipeline.stats.get('events_discovered', 0)
            
            pipeline.close()
            return True
            
        except Exception as e:
            logger.error(f"Discovery phase failed: {e}")
            self.results['discovery']['errors'].append(str(e))
            return False

    def run_dashboard_phase(self, args) -> bool:
        """Run the dashboard generation phase."""
        self.print_header("PHASE 2: DASHBOARD GENERATION")
        
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
            
            # Generate static dashboard if requested
            if args.dashboard_type in ['static', 'both']:
                logger.info("Generating static HTML dashboard...")
                static_file = self._generate_static_dashboard(args)
                if static_file:
                    files_created.append(static_file)
            
            # Prepare Flask server if requested
            if args.dashboard_type in ['flask', 'both']:
                logger.info("Preparing Flask dashboard server...")
                flask_app = self._prepare_flask_server(args)
                if flask_app:
                    self.results['flask_server']['app'] = flask_app
                    self.results['flask_server']['success'] = True
                    self.results['flask_server']['port'] = args.port
            
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
                    'severity_by_industry': get_severity_by_industry(conn, start_date, end_date),
                    'severity_by_attack_type': get_severity_by_attack_type(conn, start_date, end_date),
                    'monthly_counts_stats': compute_monthly_counts_stats(monthly_counts),
                    'event_type_correlation': compute_event_type_correlation_matrix(get_monthly_event_type_mix(conn, start_date, end_date)),
                    'oaic_comparison': oaic_comparison,
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

    def _prepare_flask_server(self, args):
        """Prepare Flask server for dashboard."""
        try:
            app = create_flask_app(self.db_path, args.debug)
            logger.info(f"Flask server prepared for port {args.port}")
            return app
        except Exception as e:
            logger.error(f"Flask server preparation failed: {e}")
            self.results['flask_server']['errors'].append(str(e))
            return None

    def launch_flask_server(self, args):
        """Launch Flask server if requested."""
        if 'app' in self.results['flask_server']:
            app = self.results['flask_server']['app']
            print(f"\n🌐 Starting Flask dashboard server...")
            print(f"   URL: http://{args.host}:{args.port}")
            print(f"   Press Ctrl+C to stop the server")
            print("="*80)
            
            try:
                app.run(host=args.host, port=args.port, debug=args.debug)
            except KeyboardInterrupt:
                print("\nFlask server stopped.")
        else:
            logger.error("Flask server not available - dashboard generation may have failed")

    async def run_pipeline(self, args):
        """Run the complete unified pipeline."""
        print("🚀 AUSTRALIAN CYBER EVENTS UNIFIED PIPELINE")
        print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Database: {self.db_path}")
        
        # Phase 1: Discovery & Deduplication
        if not args.dashboard_only:
            discovery_success = await self.run_discovery_phase(args)
            if not discovery_success and not args.continue_on_error:
                logger.error("Discovery phase failed - stopping pipeline")
                return False
        else:
            logger.info("Skipping discovery phase (--dashboard-only specified)")
        
        # Phase 2: Dashboard
        if not args.discover_only:
            dashboard_success = self.run_dashboard_phase(args)
            if not dashboard_success and not args.continue_on_error:
                logger.error("Dashboard phase failed - stopping pipeline")
                return False
        else:
            logger.info("Skipping dashboard phase (--discover-only specified)")
        
        # Launch Flask server if requested
        if args.launch_server and 'app' in self.results['flask_server']:
            self.launch_flask_server(args)
        
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

  # Dashboard options
  python run_full_pipeline.py --dashboard-type static
  python run_full_pipeline.py --dashboard-type flask --launch-server
  python run_full_pipeline.py --dashboard-type both --launch-server
        """
    )

    # Phase control
    parser.add_argument('--discover-only', action='store_true',
                        help='Run only discovery phase')
    parser.add_argument('--dashboard-only', action='store_true',
                        help='Run only dashboard generation phase')
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
    parser.add_argument('--dashboard-type', choices=['static', 'flask', 'both'], default='static',
                        help='Type of dashboard to generate (default: static)')
    parser.add_argument('--out-dir', default='dashboard',
                        help='Output directory for static dashboard (default: dashboard)')
    parser.add_argument('--launch-server', action='store_true',
                        help='Launch Flask server after generation (requires --dashboard-type flask or both)')

    # Flask server parameters
    parser.add_argument('--port', type=int, default=5000,
                        help='Port for Flask server (default: 5000)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host for Flask server (default: 127.0.0.1)')
    parser.add_argument('--debug', action='store_true',
                        help='Run Flask server in debug mode')

    # Database
    parser.add_argument('--db-path', default='instance/cyber_events.db',
                        help='Path to SQLite database file')

    args = parser.parse_args()

    # Validate arguments
    if args.discover_only and args.dashboard_only:
        parser.error("Cannot specify both --discover-only and --dashboard-only")
    
    if args.launch_server and args.dashboard_type not in ['flask', 'both']:
        parser.error("--launch-server requires --dashboard-type flask or both")

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
