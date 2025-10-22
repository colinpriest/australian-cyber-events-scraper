#!/usr/bin/env python3
"""
Cyber Events Database Export Script

This script exports cyber events data from the SQLite database to CSV and Excel formats
for sharing and analysis. It supports multiple export options and filtering.

Usage:
    python export_cyber_events.py --format csv --output cyber_events_export.csv
    python export_cyber_events.py --format excel --output cyber_events_export.xlsx
    python export_cyber_events.py --format both --table deduplicated --date-range 2024-01-01,2024-12-31
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd


class CyberEventsExporter:
    """Export cyber events data from SQLite database to CSV/Excel formats."""
    
    def __init__(self, db_path: str = "instance/cyber_events.db"):
        """Initialize the exporter with database path."""
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found at {self.db_path}")
        
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()
    
    def get_available_tables(self) -> List[str]:
        """Get list of available tables for export."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%_backup%'")
        return [row[0] for row in cursor.fetchall()]
    
    def get_table_info(self, table_name: str) -> List[Tuple[str, str]]:
        """Get column information for a table."""
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return [(row[1], row[2]) for row in cursor.fetchall()]
    
    def export_table(self, table_name: str, output_file: str, format: str, 
                    date_range: Optional[Tuple[str, str]] = None,
                    filters: Optional[Dict[str, Any]] = None) -> bool:
        """
        Export a specific table to CSV or Excel format.
        
        Args:
            table_name: Name of the table to export
            output_file: Output file path
            format: Export format ('csv' or 'excel')
            date_range: Tuple of (start_date, end_date) for filtering
            filters: Additional filters to apply
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Build query
            query = f"SELECT * FROM {table_name}"
            params = []
            conditions = []
            
            # Add date range filter if specified
            if date_range and 'event_date' in [col[0] for col in self.get_table_info(table_name)]:
                start_date, end_date = date_range
                conditions.append("event_date BETWEEN ? AND ?")
                params.extend([start_date, end_date])
            
            # Add additional filters
            if filters:
                for column, value in filters.items():
                    if isinstance(value, str):
                        conditions.append(f"{column} = ?")
                        params.append(value)
                    elif isinstance(value, list):
                        placeholders = ','.join(['?' for _ in value])
                        conditions.append(f"{column} IN ({placeholders})")
                        params.extend(value)
            
            # Apply conditions
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            # Execute query
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            if not rows:
                print(f"No data found in {table_name} with the specified filters")
                return False
            
            # Convert to DataFrame
            df = pd.DataFrame([dict(row) for row in rows])
            
            # Export based on format
            if format.lower() == 'csv':
                df.to_csv(output_file, index=False, encoding='utf-8')
            elif format.lower() == 'excel':
                df.to_excel(output_file, index=False, engine='openpyxl')
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            print(f"Successfully exported {len(df)} records from {table_name} to {output_file}")
            return True
            
        except Exception as e:
            print(f"Error exporting {table_name}: {e}")
            return False
    
    def export_deduplicated_events_with_details(self, output_file: str, format: str,
                                              date_range: Optional[Tuple[str, str]] = None,
                                              include_entities: bool = True,
                                              include_sources: bool = True) -> bool:
        """
        Export deduplicated events with related entity and source information.
        
        Args:
            output_file: Output file path
            format: Export format ('csv' or 'excel')
            date_range: Tuple of (start_date, end_date) for filtering
            include_entities: Whether to include entity information
            include_sources: Whether to include source information
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Base query for deduplicated events
            base_query = """
                SELECT 
                    de.deduplicated_event_id,
                    de.title,
                    de.description,
                    de.summary,
                    de.event_type,
                    de.severity,
                    de.event_date,
                    de.records_affected,
                    de.is_australian_event,
                    de.is_specific_event,
                    de.confidence_score,
                    de.australian_relevance_score,
                    de.total_data_sources,
                    de.contributing_raw_events,
                    de.contributing_enriched_events,
                    de.similarity_score,
                    de.deduplication_method,
                    de.status,
                    de.attacking_entity_name,
                    de.attack_method,
                    de.victim_organization_name,
                    de.victim_organization_industry,
                    de.created_at,
                    de.updated_at
                FROM DeduplicatedEvents de
            """
            
            params = []
            conditions = []
            
            # Add date range filter
            if date_range:
                start_date, end_date = date_range
                conditions.append("de.event_date BETWEEN ? AND ?")
                params.extend([start_date, end_date])
            
            if conditions:
                base_query += " WHERE " + " AND ".join(conditions)
            
            base_query += " ORDER BY de.event_date DESC, de.confidence_score DESC"
            
            # Execute base query
            cursor = self.conn.cursor()
            cursor.execute(base_query, params)
            events = cursor.fetchall()
            
            if not events:
                print("No deduplicated events found with the specified filters")
                return False
            
            # Convert to list of dictionaries
            events_data = [dict(event) for event in events]
            
            # Add entity information if requested
            if include_entities:
                for event_data in events_data:
                    event_id = event_data['deduplicated_event_id']
                    
                    # Get entities for this event
                    entity_query = """
                        SELECT 
                            e.entity_name,
                            e.entity_type,
                            e.industry,
                            e.turnover,
                            e.employee_count,
                            e.is_australian,
                            e.headquarters_location,
                            dee.relationship_type,
                            dee.confidence_score as entity_confidence
                        FROM DeduplicatedEventEntities dee
                        JOIN EntitiesV2 e ON dee.entity_id = e.entity_id
                        WHERE dee.deduplicated_event_id = ?
                    """
                    cursor.execute(entity_query, (event_id,))
                    entities = cursor.fetchall()
                    
                    # Add entity information as JSON strings for CSV compatibility
                    event_data['entities'] = json.dumps([dict(entity) for entity in entities], default=str)
                    event_data['entity_count'] = len(entities)
            
            # Add source information if requested
            if include_sources:
                for event_data in events_data:
                    event_id = event_data['deduplicated_event_id']
                    
                    # Get sources for this event
                    source_query = """
                        SELECT 
                            source_url,
                            source_type,
                            credibility_score,
                            content_snippet,
                            discovered_at
                        FROM DeduplicatedEventSources
                        WHERE deduplicated_event_id = ?
                    """
                    cursor.execute(source_query, (event_id,))
                    sources = cursor.fetchall()
                    
                    # Add source information as JSON strings for CSV compatibility
                    event_data['sources'] = json.dumps([dict(source) for source in sources], default=str)
                    event_data['source_count'] = len(sources)
            
            # Convert to DataFrame
            df = pd.DataFrame(events_data)
            
            # Export based on format
            if format.lower() == 'csv':
                df.to_csv(output_file, index=False, encoding='utf-8')
            elif format.lower() == 'excel':
                df.to_excel(output_file, index=False, engine='openpyxl')
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            print(f"Successfully exported {len(df)} deduplicated events with details to {output_file}")
            return True
            
        except Exception as e:
            print(f"Error exporting deduplicated events with details: {e}")
            return False
    
    def get_export_summary(self) -> Dict[str, Any]:
        """Get summary statistics of available data for export."""
        cursor = self.conn.cursor()
        
        summary = {}
        
        # Count records in main tables
        tables_to_count = [
            'RawEvents', 'EnrichedEvents', 'DeduplicatedEvents', 
            'EntitiesV2', 'DeduplicatedEventEntities', 'DeduplicatedEventSources'
        ]
        
        for table in tables_to_count:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                summary[table] = count
            except sqlite3.Error:
                summary[table] = 0
        
        # Get date ranges
        try:
            cursor.execute("SELECT MIN(event_date), MAX(event_date) FROM DeduplicatedEvents")
            date_range = cursor.fetchone()
            summary['date_range'] = {
                'earliest': date_range[0],
                'latest': date_range[1]
            }
        except sqlite3.Error:
            summary['date_range'] = {'earliest': None, 'latest': None}
        
        return summary


def parse_date_range(date_range_str: str) -> Tuple[str, str]:
    """Parse date range string in format 'YYYY-MM-DD,YYYY-MM-DD'."""
    try:
        start_date, end_date = date_range_str.split(',')
        # Validate date format
        datetime.strptime(start_date.strip(), '%Y-%m-%d')
        datetime.strptime(end_date.strip(), '%Y-%m-%d')
        return start_date.strip(), end_date.strip()
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"Invalid date range format. Use 'YYYY-MM-DD,YYYY-MM-DD': {e}")


def main():
    """Main function to handle command line arguments and execute export."""
    parser = argparse.ArgumentParser(
        description="Export cyber events data from SQLite database to CSV/Excel formats",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export all deduplicated events to CSV
  python export_cyber_events.py --format csv --output events.csv

  # Export to Excel with date range
  python export_cyber_events.py --format excel --output events.xlsx --date-range 2024-01-01,2024-12-31

  # Export both formats with entity and source details
  python export_cyber_events.py --format both --output events --detailed

  # Export specific table
  python export_cyber_events.py --table RawEvents --format csv --output raw_events.csv

  # Show available tables and summary
  python export_cyber_events.py --list-tables
  python export_cyber_events.py --summary
        """
    )
    
    parser.add_argument('--db-path', default='instance/cyber_events.db',
                       help='Path to SQLite database file (default: instance/cyber_events.db)')
    
    parser.add_argument('--format', choices=['csv', 'excel', 'both'], default='csv',
                       help='Export format (default: csv)')
    
    parser.add_argument('--output', '-o',
                       help='Output file path (without extension for "both" format)')
    
    parser.add_argument('--table', default='DeduplicatedEvents',
                       help='Table to export (default: DeduplicatedEvents)')
    
    parser.add_argument('--date-range', type=parse_date_range,
                       help='Date range in format "YYYY-MM-DD,YYYY-MM-DD"')
    
    parser.add_argument('--detailed', action='store_true',
                       help='Include entity and source details (only for DeduplicatedEvents)')
    
    parser.add_argument('--no-entities', action='store_true',
                       help='Exclude entity information from detailed export')
    
    parser.add_argument('--no-sources', action='store_true',
                       help='Exclude source information from detailed export')
    
    parser.add_argument('--list-tables', action='store_true',
                       help='List available tables and exit')
    
    parser.add_argument('--summary', action='store_true',
                       help='Show export summary and exit')
    
    args = parser.parse_args()
    
    try:
        with CyberEventsExporter(args.db_path) as exporter:
            # Handle special commands
            if args.list_tables:
                tables = exporter.get_available_tables()
                print("Available tables for export:")
                for table in tables:
                    print(f"  - {table}")
                return
            
            if args.summary:
                summary = exporter.get_export_summary()
                print("Export Summary:")
                print(f"  Raw Events: {summary.get('RawEvents', 0):,}")
                print(f"  Enriched Events: {summary.get('EnrichedEvents', 0):,}")
                print(f"  Deduplicated Events: {summary.get('DeduplicatedEvents', 0):,}")
                print(f"  Entities: {summary.get('EntitiesV2', 0):,}")
                print(f"  Event-Entity Relationships: {summary.get('DeduplicatedEventEntities', 0):,}")
                print(f"  Event Sources: {summary.get('DeduplicatedEventSources', 0):,}")
                
                if summary.get('date_range', {}).get('earliest'):
                    print(f"  Date Range: {summary['date_range']['earliest']} to {summary['date_range']['latest']}")
                return
            
            # Check if output is required for export operations
            if not args.output and not (args.list_tables or args.summary):
                parser.error("--output is required for export operations")
            
            # Determine export formats
            formats = ['csv', 'excel'] if args.format == 'both' else [args.format]
            
            # Generate output filenames
            if args.format == 'both':
                output_files = {
                    'csv': f"{args.output}.csv",
                    'excel': f"{args.output}.xlsx"
                }
            else:
                output_files = {args.format: args.output}
            
            # Perform exports
            success_count = 0
            total_exports = len(formats)
            
            for format_type in formats:
                output_file = output_files[format_type]
                
                if args.detailed and args.table == 'DeduplicatedEvents':
                    # Use detailed export for deduplicated events
                    success = exporter.export_deduplicated_events_with_details(
                        output_file=output_file,
                        format=format_type,
                        date_range=args.date_range,
                        include_entities=not args.no_entities,
                        include_sources=not args.no_sources
                    )
                else:
                    # Use standard table export
                    success = exporter.export_table(
                        table_name=args.table,
                        output_file=output_file,
                        format=format_type,
                        date_range=args.date_range
                    )
                
                if success:
                    success_count += 1
            
            # Report results
            if success_count == total_exports:
                print(f"\n[SUCCESS] Successfully exported data in {success_count} format(s)")
                if args.date_range:
                    print(f"   Date range: {args.date_range[0]} to {args.date_range[1]}")
                if args.detailed:
                    print(f"   Included detailed entity and source information")
            else:
                print(f"\n[WARNING] Completed {success_count}/{total_exports} exports successfully")
                sys.exit(1)
                
    except Exception as e:
        print(f"[ERROR] Export failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
