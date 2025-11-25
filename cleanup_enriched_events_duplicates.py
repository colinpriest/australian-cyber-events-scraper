#!/usr/bin/env python3
"""
Data Quality Cleanup Script for EnrichedEvents Table

This script identifies and removes duplicate events from the EnrichedEvents table
to improve data quality before deduplication.

The script:
1. Identifies duplicate events (same title + date)
2. Keeps the best version of each duplicate (most complete data)
3. Removes the duplicates
4. Reports on cleanup results
"""

import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Any, Tuple
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class EnrichedEventsCleanup:
    """Handles cleanup of duplicate events in EnrichedEvents table"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cleanup_stats = {
            'duplicates_found': 0,
            'duplicates_removed': 0,
            'events_kept': 0,
            'cleanup_timestamp': datetime.now().isoformat()
        }
    
    def find_duplicate_events(self) -> List[Tuple[str, str, int]]:
        """Find duplicate events (same title + date)"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
            SELECT title, event_date, COUNT(*) as count
            FROM EnrichedEvents
            WHERE status = 'Active'
            GROUP BY title, event_date
            HAVING COUNT(*) > 1
            ORDER BY count DESC
        """)
        
        duplicates = cursor.fetchall()
        logger.info(f"Found {len(duplicates)} groups of duplicate events")
        
        return duplicates
    
    def get_duplicate_group_details(self, title: str, event_date: str) -> List[Dict[str, Any]]:
        """Get details of all events in a duplicate group"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
            SELECT enriched_event_id, title, summary, event_type, severity, 
                   records_affected, confidence_score, created_at, updated_at
            FROM EnrichedEvents
            WHERE title = ? AND event_date = ? AND status = 'Active'
            ORDER BY created_at DESC
        """, (title, event_date))
        
        events = []
        for row in cursor.fetchall():
            events.append({
                'enriched_event_id': row[0],
                'title': row[1],
                'summary': row[2],
                'event_type': row[3],
                'severity': row[4],
                'records_affected': row[5],
                'confidence_score': row[6],
                'created_at': row[7],
                'updated_at': row[8]
            })
        
        return events
    
    def score_event_quality(self, event: Dict[str, Any]) -> float:
        """Score an event based on data completeness and quality"""
        score = 0.0
        
        # Title completeness
        if event['title'] and len(event['title'].strip()) > 10:
            score += 2.0
        
        # Summary completeness
        if event['summary'] and len(event['summary'].strip()) > 50:
            score += 2.0
        elif event['summary'] and len(event['summary'].strip()) > 20:
            score += 1.0
        
        # Event type
        if event['event_type']:
            score += 1.0
        
        # Severity
        if event['severity']:
            score += 1.0
        
        # Records affected
        if event['records_affected'] and event['records_affected'] > 0:
            score += 1.0
        
        # Confidence score
        if event['confidence_score']:
            score += event['confidence_score']
        
        # Recency (newer events get slight bonus)
        if event['created_at']:
            try:
                created_date = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
                days_old = (datetime.now() - created_date).days
                if days_old < 30:
                    score += 0.5
                elif days_old < 90:
                    score += 0.2
            except:
                pass
        
        return score
    
    def select_best_event(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Select the best event from a group of duplicates"""
        if not events:
            return None
        
        if len(events) == 1:
            return events[0]
        
        # Score all events
        scored_events = []
        for event in events:
            score = self.score_event_quality(event)
            scored_events.append((score, event))
        
        # Sort by score (highest first)
        scored_events.sort(key=lambda x: x[0], reverse=True)
        
        best_event = scored_events[0][1]
        logger.info(f"Selected best event: {best_event['enriched_event_id']} (score: {scored_events[0][0]:.2f})")
        
        return best_event
    
    def remove_duplicate_events(self, title: str, event_date: str, keep_event_id: str):
        """Remove duplicate events, keeping only the best one"""
        cursor = self.conn.cursor()
        
        # Get all event IDs in this duplicate group
        cursor.execute("""
            SELECT enriched_event_id
            FROM EnrichedEvents
            WHERE title = ? AND event_date = ? AND status = 'Active'
        """, (title, event_date))
        
        event_ids = [row[0] for row in cursor.fetchall()]
        
        # Remove all except the one we want to keep
        duplicates_to_remove = [eid for eid in event_ids if eid != keep_event_id]
        
        if duplicates_to_remove:
            # Mark as inactive instead of deleting (safer)
            placeholders = ','.join(['?' for _ in duplicates_to_remove])
            cursor.execute(f"""
                UPDATE EnrichedEvents 
                SET status = 'Duplicate_Removed', updated_at = CURRENT_TIMESTAMP
                WHERE enriched_event_id IN ({placeholders})
            """, duplicates_to_remove)
            
            logger.info(f"Marked {len(duplicates_to_remove)} duplicate events as inactive")
            return len(duplicates_to_remove)
        
        return 0
    
    def cleanup_duplicates(self) -> Dict[str, Any]:
        """Main cleanup process"""
        logger.info("Starting EnrichedEvents duplicate cleanup...")
        
        # Find all duplicate groups
        duplicate_groups = self.find_duplicate_events()
        self.cleanup_stats['duplicates_found'] = len(duplicate_groups)
        
        total_removed = 0
        total_kept = 0
        
        for title, event_date, count in duplicate_groups:
            logger.info(f"Processing duplicate group: '{title[:50]}...' on {event_date} ({count} events)")
            
            # Get all events in this group
            events = self.get_duplicate_group_details(title, event_date)
            
            # Select the best event
            best_event = self.select_best_event(events)
            
            if best_event:
                # Remove duplicates
                removed_count = self.remove_duplicate_events(title, event_date, best_event['enriched_event_id'])
                total_removed += removed_count
                total_kept += 1
        
        # Commit changes
        self.conn.commit()
        
        self.cleanup_stats['duplicates_removed'] = total_removed
        self.cleanup_stats['events_kept'] = total_kept
        
        logger.info(f"Cleanup complete: {total_removed} duplicates removed, {total_kept} events kept")
        
        return self.cleanup_stats
    
    def verify_cleanup(self) -> bool:
        """Verify that no duplicates remain"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
            SELECT COUNT(*) FROM (
                SELECT title, event_date, COUNT(*) as count
                FROM EnrichedEvents
                WHERE status = 'Active'
                GROUP BY title, event_date
                HAVING COUNT(*) > 1
            )
        """)
        
        remaining_duplicates = cursor.fetchone()[0]
        
        if remaining_duplicates == 0:
            logger.info("✅ No duplicates remain in EnrichedEvents table")
            return True
        else:
            logger.warning(f"⚠️ {remaining_duplicates} duplicate groups still remain")
            return False
    
    def get_cleanup_report(self) -> Dict[str, Any]:
        """Generate a cleanup report"""
        cursor = self.conn.cursor()
        
        # Get current counts
        cursor.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Active'")
        active_events = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Duplicate_Removed'")
        removed_events = cursor.fetchone()[0]
        
        # Get top duplicate groups that were cleaned
        cursor.execute("""
            SELECT title, event_date, COUNT(*) as count
            FROM EnrichedEvents
            WHERE status = 'Duplicate_Removed'
            GROUP BY title, event_date
            ORDER BY count DESC
            LIMIT 10
        """)
        
        top_cleaned = cursor.fetchall()
        
        return {
            'cleanup_stats': self.cleanup_stats,
            'current_active_events': active_events,
            'removed_events': removed_events,
            'top_cleaned_groups': [
                {'title': row[0], 'date': row[1], 'count': row[2]} 
                for row in top_cleaned
            ]
        }
    
    def close(self):
        """Close database connection"""
        self.conn.close()


def main():
    """Main cleanup function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Clean up duplicate events in EnrichedEvents table')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to database file')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be cleaned without making changes')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Initialize cleanup
    cleanup = EnrichedEventsCleanup(args.db_path)
    
    if args.dry_run:
        logger.info("🔍 DRY RUN: Analyzing duplicates...")
        duplicates = cleanup.find_duplicate_events()
        
        print(f"\nFound {len(duplicates)} groups of duplicate events:")
        for title, date, count in duplicates[:10]:  # Show top 10
            print(f"  - '{title[:50]}...' on {date} ({count} times)")
        
        if len(duplicates) > 10:
            print(f"  ... and {len(duplicates) - 10} more groups")
        
        print(f"\nTotal duplicate events that would be removed: {sum(count - 1 for _, _, count in duplicates)}")
        
    else:
        # Run actual cleanup
        logger.info("🧹 Starting duplicate cleanup...")
        
        # Run cleanup
        stats = cleanup.cleanup_duplicates()
        
        # Verify cleanup
        cleanup.verify_cleanup()
        
        # Generate report
        report = cleanup.get_cleanup_report()
        
        print("\n" + "="*60)
        print("CLEANUP REPORT")
        print("="*60)
        print(f"Duplicate groups found: {stats['duplicates_found']}")
        print(f"Duplicate events removed: {stats['duplicates_removed']}")
        print(f"Events kept: {stats['events_kept']}")
        print(f"Current active events: {report['current_active_events']}")
        print(f"Removed events: {report['removed_events']}")
        
        if report['top_cleaned_groups']:
            print(f"\nTop cleaned groups:")
            for group in report['top_cleaned_groups'][:5]:
                print(f"  - '{group['title'][:50]}...' on {group['date']} ({group['count']} removed)")
        
        print("="*60)
    
    cleanup.close()


if __name__ == "__main__":
    main()








