#!/usr/bin/env python3
"""
Cyber Events Database Export Script


This script exports cyber events data from the SQLite database to CSV and Excel formats
for sharing and analysis. It supports multiple export options and filtering.

Usage:
    python export_cyber_events.py --format csv --output cyber_events_export.csv
    python export_cyber_events.py --format excel --output cyber_events_export.xlsx
    python export_cyber_events.py --format both --table deduplicated --date-range 2024-01-01,2024-12-31

    # Export with anonymization (removes entity names, dates, and titles from descriptions)
    python export_cyber_events.py --format csv --output anon_export.csv --detailed --anonymize

    # Exclude events where records affected is unknown
    python export_cyber_events.py --format csv --output known_records.csv --detailed --exclude-unknown-records
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd


class CyberEventsExporter:
    """Export cyber events data from SQLite database to CSV/Excel formats."""

    # Common date/time patterns for anonymization
    DATE_PATTERNS = [
        # Full dates: 2024-01-15, 15/01/2024, 15-01-2024, 01/15/2024
        r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b',
        r'\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b',
        r'\b\d{1,2}[-/]\d{1,2}[-/]\d{2}\b',
        # Month names: January 15, 2024, 15 January 2024, Jan 2024
        r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b',
        r'\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December),?\s+\d{4}\b',
        r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b',
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b',
        r'\b\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?,?\s+\d{4}\b',
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{4}\b',
        # Quarters: Q1 2024, Q2 2024
        r'\b[Qq][1-4]\s+\d{4}\b',
        # Standalone years (only 4-digit years that look like years 1990-2099)
        r'\b(?:19|20)\d{2}\b',
        # Relative time references
        r'\b(?:early|mid|late)\s+\d{4}\b',
        r'\b(?:early|mid|late)\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\b',
        # Time expressions: "in 2024", "during 2024", "since 2024"
        r'\b(?:in|during|since|before|after|around|circa)\s+(?:19|20)\d{2}\b',
    ]

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

    def _get_all_entity_names(self) -> List[str]:
        """Get all entity names from the database for thorough anonymization."""
        cursor = self.conn.cursor()
        entity_names = set()

        # Get entity names from EntitiesV2
        try:
            cursor.execute("SELECT DISTINCT entity_name FROM EntitiesV2 WHERE entity_name IS NOT NULL")
            for row in cursor.fetchall():
                if row[0]:
                    entity_names.add(row[0].strip())
        except sqlite3.Error:
            pass

        # Get victim organization names from DeduplicatedEvents
        try:
            cursor.execute("SELECT DISTINCT victim_organization_name FROM DeduplicatedEvents WHERE victim_organization_name IS NOT NULL")
            for row in cursor.fetchall():
                if row[0]:
                    entity_names.add(row[0].strip())
        except sqlite3.Error:
            pass

        # Get attacking entity names from DeduplicatedEvents
        try:
            cursor.execute("SELECT DISTINCT attacking_entity_name FROM DeduplicatedEvents WHERE attacking_entity_name IS NOT NULL")
            for row in cursor.fetchall():
                if row[0]:
                    entity_names.add(row[0].strip())
        except sqlite3.Error:
            pass

        # Sort by length descending to replace longer names first (e.g., "Company Ltd" before "Company")
        return sorted(list(entity_names), key=len, reverse=True)

    def _remove_title_from_description(self, description: str, title: str) -> str:
        """Remove title from the beginning of description if present."""
        if not description or not title:
            return description

        description = description.strip()
        title = title.strip()

        # Check if description starts with the title (case-insensitive)
        if description.lower().startswith(title.lower()):
            # Remove title and any following punctuation/whitespace
            remaining = description[len(title):].lstrip()
            # Remove common separators after title
            for sep in [':', '-', '–', '—', '.', '\n', '\r\n']:
                if remaining.startswith(sep):
                    remaining = remaining[len(sep):].lstrip()
                    break
            return remaining if remaining else description

        # Also check for title followed by colon pattern at start
        title_patterns = [
            f"{re.escape(title)}:",
            f"{re.escape(title)} -",
            f"{re.escape(title)} –",
            f"{re.escape(title)} —",
        ]
        for pattern in title_patterns:
            match = re.match(pattern, description, re.IGNORECASE)
            if match:
                return description[match.end():].lstrip()

        return description

    def _remove_dates_from_text(self, text: str) -> str:
        """Remove all dates and years from text."""
        if not text:
            return text

        result = text
        for pattern in self.DATE_PATTERNS:
            result = re.sub(pattern, '', result, flags=re.IGNORECASE)

        # Clean up resulting double spaces and punctuation issues
        result = re.sub(r'\s{2,}', ' ', result)  # Multiple spaces to single space
        result = re.sub(r'\s+([,.])', r'\1', result)  # Space before comma/period
        result = re.sub(r'([,.])\s*\1+', r'\1', result)  # Double punctuation
        result = re.sub(r'^\s*[,.:;-]\s*', '', result)  # Leading punctuation
        result = re.sub(r'\s*[,.:;-]\s*$', '', result)  # Trailing punctuation (except periods for sentences)
        result = result.strip()

        return result

    def _anonymize_description(self, description: str, title: str,
                               entity_names: List[str],
                               victim_name: Optional[str] = None,
                               attacker_name: Optional[str] = None,
                               industry: Optional[str] = None) -> str:
        """
        Anonymize a description by removing identifying information.

        Args:
            description: The description text to anonymize
            title: The event title (to be removed from description)
            entity_names: List of all known entity names for replacement
            victim_name: The victim organization name (for replacement)
            attacker_name: The attacker entity name (for replacement)
            industry: The industry of the victim (for context-aware replacement)

        Returns:
            Anonymized description text
        """
        if not description:
            return description

        result = description

        # Step 1: Remove title from description
        if title:
            result = self._remove_title_from_description(result, title)

        # Step 2: Build entity replacement mapping
        # Create a mapping of entity names to anonymized versions
        entity_counter = {'victim': 0, 'attacker': 0, 'organization': 0}
        entity_mapping = {}

        # First, handle known victim and attacker names specifically
        if victim_name and victim_name.strip():
            victim_label = f"[Victim Organization]" if not industry else f"[Victim Organization - {industry}]"
            entity_mapping[victim_name.strip().lower()] = victim_label
            # Also add common variations
            for variation in self._get_name_variations(victim_name):
                entity_mapping[variation.lower()] = victim_label

        if attacker_name and attacker_name.strip():
            entity_mapping[attacker_name.strip().lower()] = "[Threat Actor]"
            for variation in self._get_name_variations(attacker_name):
                entity_mapping[variation.lower()] = "[Threat Actor]"

        # Step 3: Replace all known entity names
        # Sort by length descending to replace longer names first
        for entity_name in entity_names:
            if not entity_name:
                continue

            entity_lower = entity_name.lower()

            # Skip if already mapped (victim/attacker)
            if entity_lower in entity_mapping:
                continue

            # Create anonymized label
            entity_mapping[entity_lower] = "[Organization]"

            # Add variations
            for variation in self._get_name_variations(entity_name):
                if variation.lower() not in entity_mapping:
                    entity_mapping[variation.lower()] = "[Organization]"

        # Step 4: Perform replacements (case-insensitive)
        # Sort by length descending for proper replacement order
        sorted_entities = sorted(entity_mapping.keys(), key=len, reverse=True)

        for entity_lower in sorted_entities:
            replacement = entity_mapping[entity_lower]
            # Use word boundaries to avoid partial matches
            pattern = r'\b' + re.escape(entity_lower) + r'\b'
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # Step 5: Remove dates and years
        result = self._remove_dates_from_text(result)

        # Step 6: Clean up redundant anonymization markers
        # Replace multiple consecutive [Organization] with single instance
        result = re.sub(r'(\[Organization\]\s*)+', '[Organization] ', result)
        result = re.sub(r'(\[Victim Organization[^\]]*\]\s*)+', lambda m: m.group(0).split(']')[0] + '] ', result)
        result = re.sub(r'(\[Threat Actor\]\s*)+', '[Threat Actor] ', result)

        # Step 7: Final cleanup
        result = re.sub(r'\s{2,}', ' ', result)
        result = result.strip()

        # Ensure the description starts with a capital letter
        if result and result[0].islower():
            result = result[0].upper() + result[1:]

        return result

    def _get_name_variations(self, name: str) -> List[str]:
        """Generate common variations of an entity name."""
        if not name:
            return []

        variations = [name]
        name_stripped = name.strip()

        # Remove common suffixes
        suffixes = [
            ' Pty Ltd', ' Pty. Ltd.', ' Pty. Ltd', ' PTY LTD',
            ' Ltd', ' Ltd.', ' Limited',
            ' Inc', ' Inc.', ' Incorporated',
            ' Corp', ' Corp.', ' Corporation',
            ' LLC', ' L.L.C.',
            ' PLC', ' plc',
            ' Group', ' Holdings',
            ' Australia', ' (Australia)', ' AU',
            ' International', ' Intl',
            ' Company', ' Co', ' Co.',
            ' Services', ' Solutions',
        ]

        for suffix in suffixes:
            if name_stripped.lower().endswith(suffix.lower()):
                base_name = name_stripped[:-len(suffix)].strip()
                if base_name and len(base_name) > 2:
                    variations.append(base_name)

        # Add possessive forms
        variations.append(f"{name_stripped}'s")
        variations.append(f"{name_stripped}'")

        # Remove "The " prefix
        if name_stripped.lower().startswith('the '):
            variations.append(name_stripped[4:])

        return variations

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
            # Validate table name against available tables to prevent SQL injection
            available_tables = self.get_available_tables()
            if table_name not in available_tables:
                raise ValueError(f"Table '{table_name}' not found in database")

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
                                              include_sources: bool = True,
                                              exclude_unknown_records: bool = False,
                                              anonymize: bool = False) -> bool:
        """
        Export deduplicated events with related entity and source information.

        Args:
            output_file: Output file path
            format: Export format ('csv' or 'excel')
            date_range: Tuple of (start_date, end_date) for filtering
            include_entities: Whether to include entity information
            include_sources: Whether to include source information
            exclude_unknown_records: Whether to exclude events where records_affected is unknown/null
            anonymize: Whether to anonymize descriptions (removes entity names, dates, titles)

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

            # Exclude events with unknown records_affected
            if exclude_unknown_records:
                conditions.append("de.records_affected IS NOT NULL AND de.records_affected != '' AND de.records_affected != 'Unknown'")

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

            # Get all entity names for thorough anonymization if needed
            all_entity_names = self._get_all_entity_names() if anonymize else []

            # Convert to list of dictionaries
            events_data = [dict(event) for event in events]

            # Process each event for category labels and anonymization
            for event_data in events_data:
                # Use "Unknown" for unknown entity types
                if not event_data.get('event_type') or event_data['event_type'].lower() in ('unknown', 'none', '', 'null'):
                    event_data['event_type'] = 'Unknown'

                # Use "Unknown" for unknown attack methods
                if not event_data.get('attack_method') or event_data['attack_method'].lower() in ('unknown', 'none', '', 'null'):
                    event_data['attack_method'] = 'Unknown'

                # Anonymize description if requested
                if anonymize:
                    event_data['description'] = self._anonymize_description(
                        description=event_data.get('description', ''),
                        title=event_data.get('title', ''),
                        entity_names=all_entity_names,
                        victim_name=event_data.get('victim_organization_name'),
                        attacker_name=event_data.get('attacking_entity_name'),
                        industry=event_data.get('victim_organization_industry')
                    )
                    # Also anonymize summary if present
                    if event_data.get('summary'):
                        event_data['summary'] = self._anonymize_description(
                            description=event_data.get('summary', ''),
                            title='',  # Summary doesn't have a title to remove
                            entity_names=all_entity_names,
                            victim_name=event_data.get('victim_organization_name'),
                            attacker_name=event_data.get('attacking_entity_name'),
                            industry=event_data.get('victim_organization_industry')
                        )
            
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

                    # Convert entities and normalize unknown entity types
                    processed_entities = []
                    for entity in entities:
                        entity_dict = dict(entity)
                        # Use "Unknown" for unknown entity types
                        if not entity_dict.get('entity_type') or entity_dict['entity_type'].lower() in ('unknown', 'none', '', 'null'):
                            entity_dict['entity_type'] = 'Unknown'
                        processed_entities.append(entity_dict)

                    # Add entity information as JSON strings for CSV compatibility
                    event_data['entities'] = json.dumps(processed_entities, default=str)
                    event_data['entity_count'] = len(processed_entities)
            
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

  # Export with anonymization (removes entity names, dates, titles from descriptions)
  python export_cyber_events.py --format csv --output anon_events.csv --detailed --anonymize

  # Exclude events where records affected is unknown
  python export_cyber_events.py --format csv --output known_records.csv --detailed --exclude-unknown-records

  # Combine options: anonymize and exclude unknown records
  python export_cyber_events.py --format csv --output clean_export.csv --detailed --anonymize --exclude-unknown-records

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

    parser.add_argument('--exclude-unknown-records', action='store_true',
                       help='Exclude events where the number of customer records affected is unknown')

    parser.add_argument('--anonymize', action='store_true',
                       help='Anonymize descriptions by removing entity names, dates, years, and titles')

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
            successful_files = []

            for format_type in formats:
                output_file = output_files[format_type]

                if args.detailed and args.table == 'DeduplicatedEvents':
                    # Use detailed export for deduplicated events
                    success = exporter.export_deduplicated_events_with_details(
                        output_file=output_file,
                        format=format_type,
                        date_range=args.date_range,
                        include_entities=not args.no_entities,
                        include_sources=not args.no_sources,
                        exclude_unknown_records=args.exclude_unknown_records,
                        anonymize=args.anonymize
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
                    successful_files.append(output_file)

            # Report results
            if success_count == total_exports:
                print(f"\n[SUCCESS] Successfully exported data in {success_count} format(s)")
                for file_path in successful_files:
                    abs_path = Path(file_path).resolve()
                    print(f"   Output file: {abs_path}")
                if args.date_range:
                    print(f"   Date range: {args.date_range[0]} to {args.date_range[1]}")
                if args.detailed:
                    print(f"   Included detailed entity and source information")
                if args.exclude_unknown_records:
                    print(f"   Excluded events with unknown records affected")
                if args.anonymize:
                    print(f"   Anonymized descriptions (removed entity names, dates, and titles)")
            else:
                print(f"\n[WARNING] Completed {success_count}/{total_exports} exports successfully")
                sys.exit(1)
                
    except Exception as e:
        print(f"[ERROR] Export failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
