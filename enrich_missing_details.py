#!/usr/bin/env python3
"""
Perplexity-based event detail enrichment script.

Reviews deduplicated events for missing information and uses Perplexity AI to fill in gaps for:
- Attacker/threat actor information
- Security flaw/vulnerability details  
- Regulatory fines imposed
- Missing severity levels
- Missing records affected counts
- Vulnerability category classification

Usage:
    python enrich_missing_details.py
    python enrich_missing_details.py --dry-run
    python enrich_missing_details.py --fields attacker,fines --limit 50
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import List
from dotenv import load_dotenv

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Load environment variables from .env file
load_dotenv()

from cyber_data_collector.processing.perplexity_enricher import PerplexityEventEnricher


def setup_logging(debug: bool = False) -> logging.Logger:
    """Set up logging configuration."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('enrichment_details.log'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


def parse_fields(fields_str: str) -> List[str]:
    """Parse comma-separated fields string into list."""
    if not fields_str:
        return None
    
    field_mapping = {
        'attacker': 'attacker',
        'vulnerability': 'vulnerability', 
        'vulnerability_category': 'vulnerability_category',
        'fines': 'regulatory_fines',
        'severity': 'severity',
        'records': 'records_affected'
    }
    
    fields = [f.strip() for f in fields_str.split(',')]
    return [field_mapping.get(f, f) for f in fields]


def main():
    """Main entry point for the enrichment script."""
    parser = argparse.ArgumentParser(
        description='Enrich cyber events with missing details using Perplexity AI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python enrich_missing_details.py
  python enrich_missing_details.py --dry-run
  python enrich_missing_details.py --fields attacker,fines --limit 50
  python enrich_missing_details.py --start-date 2024-01-01 --end-date 2024-12-31
  python enrich_missing_details.py --interactive --batch-size 5
        """
    )
    
    # Database options
    parser.add_argument('--db-path', default='instance/cyber_events.db',
                       help='Path to SQLite database file (default: instance/cyber_events.db)')
    
    # Processing options
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be enriched without updating database')
    parser.add_argument('--fields', type=str,
                       help='Comma-separated list of fields to enrich: attacker,vulnerability,fines,severity,records (default: all)')
    parser.add_argument('--limit', type=int,
                       help='Maximum number of events to process (default: no limit)')
    parser.add_argument('--start-date', type=str,
                       help='Only enrich events from this date onward (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str,
                       help='Only enrich events up to this date (YYYY-MM-DD)')
    parser.add_argument('--force', action='store_true',
                       help='Re-enrich previously enriched events')
    parser.add_argument('--interactive', action='store_true',
                       help='Ask for confirmation before each update')
    
    # API options
    parser.add_argument('--batch-size', type=int, default=10,
                       help='Number of events to process in each batch (default: 10)')
    parser.add_argument('--delay', type=float, default=2.0,
                       help='Delay between API calls in seconds (default: 2.0)')
    
    # Logging options
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug logging')
    
    args = parser.parse_args()
    
    # Set up logging
    logger = setup_logging(args.debug)
    
    # Check for required environment variables
    perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')
    if not perplexity_api_key:
        logger.error("PERPLEXITY_API_KEY environment variable not set")
        logger.info("Please set your Perplexity API key:")
        logger.info("  export PERPLEXITY_API_KEY=your_api_key_here")
        sys.exit(1)
    
    # Check database exists
    if not os.path.exists(args.db_path):
        logger.error(f"Database file not found: {args.db_path}")
        sys.exit(1)
    
    # Parse fields
    fields = parse_fields(args.fields) if args.fields else None
    
    # Create enricher instance
    enricher = PerplexityEventEnricher(args.db_path, perplexity_api_key)
    
    # Print configuration
    logger.info("Enrichment Configuration:")
    logger.info(f"  Database: {args.db_path}")
    logger.info(f"  Dry run: {args.dry_run}")
    logger.info(f"  Fields: {fields or 'all'}")
    logger.info(f"  Limit: {args.limit or 'no limit'}")
    logger.info(f"  Date range: {args.start_date or 'no start'} to {args.end_date or 'no end'}")
    logger.info(f"  Force re-enrichment: {args.force}")
    logger.info(f"  Interactive: {args.interactive}")
    logger.info(f"  Batch size: {args.batch_size}")
    logger.info(f"  API delay: {args.delay}s")
    
    if args.dry_run:
        logger.info("DRY RUN MODE - No database updates will be made")
    
    try:
        # Run enrichment
        asyncio.run(enricher.run_enrichment(
            limit=args.limit,
            dry_run=args.dry_run,
            start_date=args.start_date,
            end_date=args.end_date,
            fields=fields,
            delay=args.delay,
            force=args.force
        ))
        
        logger.info("Enrichment process completed successfully")
        
    except KeyboardInterrupt:
        logger.info("Enrichment process interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Enrichment process failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
