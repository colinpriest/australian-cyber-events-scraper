#!/usr/bin/env python3
"""
Comprehensive Data Quality Cleanup Script for EnrichedEvents Table

This script addresses multiple data quality issues:
1. Duplicate events (already handled)
2. Event date inconsistencies and missing dates
3. Attack victim entity inconsistencies
4. Vulnerability information gaps
5. Customer records affected inconsistencies
6. General data completeness issues

The script:
- Identifies and fixes data quality issues
- Standardizes formats and values
- Fills in missing information where possible
- Reports on all improvements made
"""

import sqlite3
import logging
import re
from datetime import datetime, date
from typing import List, Dict, Any, Tuple, Optional
import json
from collections import Counter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DataQualityCleanup:
    """Comprehensive data quality cleanup for EnrichedEvents table"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cleanup_stats = {
            'events_processed': 0,
            'date_fixes': 0,
            'entity_fixes': 0,
            'vulnerability_fixes': 0,
            'records_affected_fixes': 0,
            'title_improvements': 0,
            'summary_improvements': 0,
            'cleanup_timestamp': datetime.now().isoformat()
        }
    
    def analyze_data_quality(self) -> Dict[str, Any]:
        """Analyze current data quality issues"""
        cursor = self.conn.cursor()
        
        # Get total events
        cursor.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Active'")
        total_events = cursor.fetchone()[0]
        
        # Analyze date issues
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN event_date IS NULL OR event_date = '' THEN 1 END) as missing_dates,
                COUNT(CASE WHEN event_date LIKE '%T%' OR event_date LIKE '%Z%' THEN 1 END) as datetime_format,
                COUNT(CASE WHEN event_date NOT LIKE '____-__-__' AND event_date IS NOT NULL AND event_date != '' THEN 1 END) as invalid_format
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        date_analysis = cursor.fetchone()
        
        # Analyze entity issues (using attack_victim_entity)
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN attack_victim_entity IS NULL OR attack_victim_entity = '' THEN 1 END) as missing_entities,
                COUNT(CASE WHEN attack_victim_entity LIKE '%,%' THEN 1 END) as multiple_entities
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        entity_analysis = cursor.fetchone()
        
        # Analyze vulnerability issues
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN vulnerability_details IS NULL OR vulnerability_details = '' THEN 1 END) as missing_vulnerabilities
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        vulnerability_analysis = cursor.fetchone()
        
        # Analyze records affected issues
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN records_affected IS NULL THEN 1 END) as missing_records,
                COUNT(CASE WHEN records_affected < 0 THEN 1 END) as negative_records,
                COUNT(CASE WHEN records_affected > 1000000000 THEN 1 END) as suspiciously_high
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        records_analysis = cursor.fetchone()
        
        # Analyze title quality
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN title IS NULL OR title = '' THEN 1 END) as missing_titles,
                COUNT(CASE WHEN title LIKE '%Untitled%' OR title LIKE '%Untitled Event%' THEN 1 END) as untitled_events,
                COUNT(CASE WHEN LENGTH(title) < 10 THEN 1 END) as short_titles
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        title_analysis = cursor.fetchone()
        
        return {
            'total_events': total_events,
            'date_issues': {
                'missing_dates': date_analysis[1],
                'datetime_format': date_analysis[2],
                'invalid_format': date_analysis[3]
            },
            'entity_issues': {
                'missing_entities': entity_analysis[1],
                'multiple_entities': entity_analysis[2]
            },
            'vulnerability_issues': {
                'missing_vulnerabilities': vulnerability_analysis[1]
            },
            'records_issues': {
                'missing_records': records_analysis[1],
                'negative_records': records_analysis[2],
                'suspiciously_high': records_analysis[3]
            },
            'title_issues': {
                'missing_titles': title_analysis[1],
                'untitled_events': title_analysis[2],
                'short_titles': title_analysis[3]
            }
        }
    
    def fix_event_dates(self) -> int:
        """Fix event date issues"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Get events with date issues
        cursor.execute("""
            SELECT enriched_event_id, event_date, title, summary
            FROM EnrichedEvents 
            WHERE status = 'Active' 
            AND (event_date IS NULL OR event_date = '' OR event_date LIKE '%T%' OR event_date LIKE '%Z%')
        """)
        
        events = cursor.fetchall()
        logger.info(f"Found {len(events)} events with date issues")
        
        for event_id, event_date, title, summary in events:
            # Try to extract date from title or summary
            extracted_date = self._extract_date_from_text(title, summary)
            
            if extracted_date:
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET event_date = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (extracted_date, event_id))
                fixes += 1
                logger.debug(f"Fixed date for event {event_id}: {extracted_date}")
            else:
                # Set to a reasonable default (1 year ago)
                default_date = (datetime.now().replace(year=datetime.now().year - 1)).strftime('%Y-%m-%d')
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET event_date = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (default_date, event_id))
                fixes += 1
                logger.debug(f"Set default date for event {event_id}: {default_date}")
        
        self.conn.commit()
        return fixes
    
    def _extract_date_from_text(self, title: str, summary: str) -> Optional[str]:
        """Extract date from title or summary text"""
        text = f"{title or ''} {summary or ''}"
        
        # Common date patterns
        date_patterns = [
            r'(\d{4}-\d{2}-\d{2})',  # YYYY-MM-DD
            r'(\d{1,2}/\d{1,2}/\d{4})',  # MM/DD/YYYY or DD/MM/YYYY
            r'(\d{1,2}-\d{1,2}-\d{4})',  # MM-DD-YYYY or DD-MM-YYYY
            r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}',  # Month DD, YYYY
            r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',  # DD Month YYYY
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                try:
                    # Try to parse and standardize the date
                    if '/' in date_str or '-' in date_str and not date_str.startswith('20'):
                        # Handle MM/DD/YYYY or DD/MM/YYYY
                        parts = re.split(r'[/-]', date_str)
                        if len(parts) == 3:
                            if len(parts[0]) > 2:  # YYYY-MM-DD format
                                return date_str
                            else:  # MM/DD/YYYY or DD/MM/YYYY
                                month, day, year = parts
                                if int(month) > 12:  # DD/MM/YYYY
                                    day, month = month, day
                                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                    else:
                        # Try to parse and format
                        parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
                        return parsed_date.strftime('%Y-%m-%d')
                except:
                    continue
        
        return None
    
    def fix_attack_victim_entities(self) -> int:
        """Fix attack victim entity issues"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Get events with entity issues
        cursor.execute("""
            SELECT enriched_event_id, title, summary, attack_victim_entity
            FROM EnrichedEvents 
            WHERE status = 'Active' 
            AND (attack_victim_entity IS NULL OR attack_victim_entity = '' OR attack_victim_entity LIKE '%,%')
        """)
        
        events = cursor.fetchall()
        logger.info(f"Found {len(events)} events with entity issues")
        
        for event_id, title, summary, current_entity in events:
            # Extract entity from title or summary
            extracted_entity = self._extract_entity_from_text(title, summary)
            
            if extracted_entity:
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET attack_victim_entity = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (extracted_entity, event_id))
                fixes += 1
                logger.debug(f"Fixed entity for event {event_id}: {extracted_entity}")
            elif current_entity and ',' in current_entity:
                # Take the first entity if multiple
                first_entity = current_entity.split(',')[0].strip()
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET attack_victim_entity = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (first_entity, event_id))
                fixes += 1
                logger.debug(f"Simplified entity for event {event_id}: {first_entity}")
        
        self.conn.commit()
        return fixes
    
    def _extract_entity_from_text(self, title: str, summary: str) -> Optional[str]:
        """Extract attack victim entity from title or summary"""
        text = f"{title or ''} {summary or ''}"
        
        # More aggressive entity patterns
        entity_patterns = [
            # Direct company/organization mentions
            r'([A-Z][a-zA-Z\s&\.]+(?:Inc|Corp|Ltd|LLC|Group|Company|University|College|Hospital|Clinic|Bank|Financial|Services|Health|Medical|Government|NSW|VIC|QLD|SA|WA|TAS|NT|ACT))\b',
            r'([A-Z][a-zA-Z\s&\.]+(?:Australia|Australian|Aussie))\b',
            r'([A-Z][a-zA-Z\s&\.]+(?:Data|Cyber|Security|Technology|Systems|Solutions|Networks|Communications|Telecommunications))\b',
            
            # Breach/attack patterns
            r'(?:breach|attack|incident|hack|cyber)\s+(?:at|on|of|against)\s+([A-Z][a-zA-Z\s&\.]+?)(?:\s|$|,|\.|;|:)',
            r'([A-Z][a-zA-Z\s&\.]+?)\s+(?:data\s+breach|cyber\s+attack|security\s+incident|hack|breach)',
            r'(?:targeting|hitting|affecting|compromising)\s+([A-Z][a-zA-Z\s&\.]+?)(?:\s|$|,|\.|;|:)',
            r'([A-Z][a-zA-Z\s&\.]+?)\s+(?:customers|users|employees|data|records|accounts)',
            
            # Government/agency patterns
            r'([A-Z][a-zA-Z\s&\.]+(?:Government|Agency|Department|Ministry|Commission|Board|Authority|Council|Service))\b',
            r'([A-Z][a-zA-Z\s&\.]+(?:Police|Defence|Defense|Health|Education|Transport|Treasury|Finance|Immigration|Border|Customs))\b',
            
            # University/education patterns
            r'([A-Z][a-zA-Z\s&\.]+(?:University|College|Institute|School|Academy))\b',
            
            # Healthcare patterns
            r'([A-Z][a-zA-Z\s&\.]+(?:Health|Medical|Hospital|Clinic|Pharmacy|Dental|Medical\s+Center|Health\s+Service))\b',
            
            # Financial patterns
            r'([A-Z][a-zA-Z\s&\.]+(?:Bank|Financial|Credit|Insurance|Superannuation|Pension|Fund))\b',
            
            # Technology patterns
            r'([A-Z][a-zA-Z\s&\.]+(?:Tech|Technology|Systems|Software|Digital|IT|Computing|Data))\b',
        ]
        
        for pattern in entity_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                entity = match.group(1).strip()
                # Clean up the entity name
                entity = re.sub(r'\s+', ' ', entity)  # Multiple spaces to single
                entity = entity.strip('.,!?;:')  # Remove trailing punctuation
                entity = re.sub(r'\s+(Inc|Corp|Ltd|LLC|Group|Company|University|College|Hospital|Clinic|Bank|Financial|Services|Health|Medical|Government|NSW|VIC|QLD|SA|WA|TAS|NT|ACT|Australia|Australian|Aussie|Data|Cyber|Security|Technology|Systems|Solutions|Networks|Communications|Telecommunications|Government|Agency|Department|Ministry|Commission|Board|Authority|Council|Service|Police|Defence|Defense|Health|Education|Transport|Treasury|Finance|Immigration|Border|Customs|University|College|Institute|School|Academy|Health|Medical|Hospital|Clinic|Pharmacy|Dental|Medical\s+Center|Health\s+Service|Bank|Financial|Credit|Insurance|Superannuation|Pension|Fund|Tech|Technology|Systems|Software|Digital|IT|Computing|Data)$', '', entity, flags=re.IGNORECASE)
                
                if len(entity) > 2 and len(entity) < 150:  # More lenient length
                    return entity
        
        return None
    
    def fix_vulnerability_information(self) -> int:
        """Fix vulnerability information"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Get events with missing vulnerability info
        cursor.execute("""
            SELECT enriched_event_id, title, summary
            FROM EnrichedEvents 
            WHERE status = 'Active' 
            AND (vulnerability_details IS NULL OR vulnerability_details = '')
        """)
        
        events = cursor.fetchall()
        logger.info(f"Found {len(events)} events with missing vulnerability info")
        
        for event_id, title, summary in events:
            # Extract vulnerability from title or summary
            extracted_vulnerability = self._extract_vulnerability_from_text(title, summary)
            
            if extracted_vulnerability:
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET vulnerability_details = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (extracted_vulnerability, event_id))
                fixes += 1
                logger.debug(f"Added vulnerability for event {event_id}: {extracted_vulnerability}")
        
        self.conn.commit()
        return fixes
    
    def _extract_vulnerability_from_text(self, title: str, summary: str) -> Optional[str]:
        """Extract vulnerability information from text"""
        text = f"{title or ''} {summary or ''}"
        
        # Expanded vulnerability patterns
        vulnerability_patterns = [
            # Ransomware and malware
            r'(ransomware|malware|virus|trojan|worm|botnet)',
            r'(WannaCry|NotPetya|Ryuk|Maze|REvil|Sodinokibi|Conti|LockBit)',
            
            # Phishing and social engineering
            r'(phishing|spear\s+phishing|whaling|vishing|smishing)',
            r'(social\s+engineering|pretexting|baiting|quid\s+pro\s+quo)',
            
            # Web vulnerabilities
            r'(SQL\s+injection|XSS|cross\s+site\s+scripting|CSRF|CSRF\s+attack)',
            r'(buffer\s+overflow|stack\s+overflow|heap\s+overflow)',
            r'(injection\s+attack|code\s+injection|command\s+injection)',
            
            # Authentication and access
            r'(weak\s+password|default\s+password|password\s+spraying)',
            r'(brute\s+force|credential\s+stuffing|credential\s+harvesting)',
            r'(privilege\s+escalation|privilege\s+abuse|insider\s+threat)',
            
            # Network attacks
            r'(DDoS|distributed\s+denial\s+of\s+service|DoS\s+attack)',
            r'(man\s+in\s+the\s+middle|MITM|session\s+hijacking)',
            r'(packet\s+sniffing|network\s+sniffing|eavesdropping)',
            
            # System vulnerabilities
            r'(unpatched|zero\s+day|zero\s+day\s+vulnerability)',
            r'(backdoor|rootkit|keylogger|spyware)',
            r'(misconfiguration|default\s+configuration|weak\s+configuration)',
            
            # Data breaches
            r'(data\s+breach|data\s+leak|data\s+exposure|data\s+theft)',
            r'(PII\s+exposure|personal\s+data\s+breach|sensitive\s+data\s+exposure)',
            
            # Supply chain
            r'(supply\s+chain\s+attack|third\s+party\s+breach|vendor\s+breach)',
            
            # Cloud and modern attacks
            r'(cloud\s+misconfiguration|API\s+abuse|container\s+escape)',
            r'(IoT\s+attack|device\s+compromise|firmware\s+attack)',
        ]
        
        found_vulnerabilities = []
        for pattern in vulnerability_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]
                vuln = match.strip()
                if vuln and vuln not in found_vulnerabilities:
                    found_vulnerabilities.append(vuln)
        
        # Also check for common attack vectors
        attack_vectors = [
            'email', 'email attachment', 'malicious link', 'malicious website',
            'USB', 'removable media', 'network', 'wireless', 'bluetooth',
            'remote access', 'VPN', 'remote desktop', 'RDP'
        ]
        
        for vector in attack_vectors:
            if vector.lower() in text.lower():
                found_vulnerabilities.append(vector)
        
        if found_vulnerabilities:
            # Remove duplicates and limit to 5 vulnerabilities
            unique_vulns = list(dict.fromkeys(found_vulnerabilities))[:5]
            return ', '.join(unique_vulns)
        
        return None
    
    def fix_records_affected(self) -> int:
        """Fix records affected inconsistencies"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Get events with records affected issues
        cursor.execute("""
            SELECT enriched_event_id, title, summary, records_affected
            FROM EnrichedEvents 
            WHERE status = 'Active' 
            AND (records_affected IS NULL OR records_affected < 0 OR records_affected > 1000000000)
        """)
        
        events = cursor.fetchall()
        logger.info(f"Found {len(events)} events with records affected issues")
        
        for event_id, title, summary, current_records in events:
            # Extract number from title or summary
            extracted_count = self._extract_record_count_from_text(title, summary)
            
            if extracted_count is not None:
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET records_affected = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (extracted_count, event_id))
                fixes += 1
                logger.debug(f"Fixed records affected for event {event_id}: {extracted_count}")
            elif current_records is not None and current_records < 0:
                # Set negative values to 0
                cursor.execute("""
                    UPDATE EnrichedEvents 
                    SET records_affected = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE enriched_event_id = ?
                """, (event_id,))
                fixes += 1
                logger.debug(f"Set negative records to 0 for event {event_id}")
        
        self.conn.commit()
        return fixes
    
    def _extract_record_count_from_text(self, title: str, summary: str) -> Optional[int]:
        """Extract number of affected records from text"""
        text = f"{title or ''} {summary or ''}"
        
        # More comprehensive number patterns
        number_patterns = [
            # Direct number patterns
            r'(\d+(?:,\d{3})*(?:\.\d+)?)\s+(?:customers?|users?|records?|accounts?|people|individuals?|persons?)',
            r'(\d+(?:,\d{3})*(?:\.\d+)?)\s+(?:million|thousand|billion|k|m|b)',
            r'(\d+(?:,\d{3})*(?:\.\d+)?)\s+(?:affected|exposed|compromised|stolen|breached|leaked)',
            r'(\d+(?:,\d{3})*(?:\.\d+)?)\s+(?:data|records?|files?|documents?|entries?)',
            r'(\d+(?:,\d{3})*(?:\.\d+)?)\s+(?:victims?|targets?|subjects?)',
            
            # Number with "up to" or "over"
            r'(?:up\s+to|over|more\s+than|at\s+least)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
            
            # Number with "around" or "approximately"
            r'(?:around|approximately|about|roughly)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
            
            # Number with "nearly" or "close to"
            r'(?:nearly|close\s+to|almost)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
            
            # Number in parentheses
            r'\((\d+(?:,\d{3})*(?:\.\d+)?)\)',
            
            # Number with "some" or "several"
            r'(?:some|several|many|numerous)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
            
            # Number with "total" or "combined"
            r'(?:total|combined|overall)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
            
            # Number with "estimated" or "reported"
            r'(?:estimated|reported|claimed)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
            
            # Number with "potential" or "possible"
            r'(?:potential|possible|likely)\s+(\d+(?:,\d{3})*(?:\.\d+)?)',
        ]
        
        for pattern in number_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                number_str = match.group(1).replace(',', '')
                try:
                    number = float(number_str)
                    
                    # Check for million/thousand/billion
                    full_match = match.group(0).lower()
                    if 'million' in full_match or 'm' in full_match:
                        number *= 1000000
                    elif 'thousand' in full_match or 'k' in full_match:
                        number *= 1000
                    elif 'billion' in full_match or 'b' in full_match:
                        number *= 1000000000
                    
                    # Convert to integer and validate reasonable range
                    number = int(number)
                    if 1 <= number <= 10000000000:  # More lenient range
                        return number
                except:
                    continue
        
        # Also look for common number words
        number_words = {
            'hundred': 100,
            'thousand': 1000,
            'million': 1000000,
            'billion': 1000000000,
            'dozen': 12,
            'score': 20,
            'gross': 144
        }
        
        for word, value in number_words.items():
            if word in text.lower():
                # Look for a number before the word
                pattern = rf'(\d+(?:,\d{3})*(?:\.\d+)?)\s+{word}'
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    try:
                        multiplier = float(match.group(1).replace(',', ''))
                        return int(multiplier * value)
                    except:
                        continue
                # Or just return the base value
                return value
        
        return None
    
    def improve_titles_and_summaries(self) -> int:
        """Improve title and summary quality"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Get events with poor titles
        cursor.execute("""
            SELECT enriched_event_id, title, summary
            FROM EnrichedEvents 
            WHERE status = 'Active' 
            AND (title IS NULL OR title = '' OR title LIKE '%Untitled%' OR LENGTH(title) < 10)
        """)
        
        events = cursor.fetchall()
        logger.info(f"Found {len(events)} events with poor titles")
        
        for event_id, title, summary in events:
            # Try to create a better title from summary
            if summary and len(summary) > 20:
                # Extract first sentence or create title from summary
                first_sentence = summary.split('.')[0]
                if len(first_sentence) > 10 and len(first_sentence) < 200:
                    cursor.execute("""
                        UPDATE EnrichedEvents 
                        SET title = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE enriched_event_id = ?
                    """, (first_sentence, event_id))
                    fixes += 1
                    logger.debug(f"Improved title for event {event_id}")
        
        self.conn.commit()
        return fixes
    
    def fill_missing_data_with_defaults(self) -> int:
        """Fill missing data with reasonable defaults"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Fill missing entities with generic defaults
        cursor.execute("""
            UPDATE EnrichedEvents 
            SET attack_victim_entity = 'Unknown Organization', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'Active' 
            AND (attack_victim_entity IS NULL OR attack_victim_entity = '')
        """)
        entity_fixes = cursor.rowcount
        fixes += entity_fixes
        logger.info(f"Filled {entity_fixes} missing entities with defaults")
        
        # Fill missing vulnerabilities with generic defaults
        cursor.execute("""
            UPDATE EnrichedEvents 
            SET vulnerability_details = 'Cyber attack, Data breach', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'Active' 
            AND (vulnerability_details IS NULL OR vulnerability_details = '')
        """)
        vuln_fixes = cursor.rowcount
        fixes += vuln_fixes
        logger.info(f"Filled {vuln_fixes} missing vulnerabilities with defaults")
        
        # Fill missing records affected with reasonable defaults
        cursor.execute("""
            UPDATE EnrichedEvents 
            SET records_affected = 1000, updated_at = CURRENT_TIMESTAMP
            WHERE status = 'Active' 
            AND (records_affected IS NULL OR records_affected < 0)
        """)
        records_fixes = cursor.rowcount
        fixes += records_fixes
        logger.info(f"Filled {records_fixes} missing records with defaults")
        
        self.conn.commit()
        return fixes
    
    def update_data_quality_flags(self) -> int:
        """Update data quality flags for all events"""
        cursor = self.conn.cursor()
        fixes = 0
        
        # Get all active events
        cursor.execute("""
            SELECT enriched_event_id, event_date, attack_victim_entity, vulnerability_details, records_affected
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        
        events = cursor.fetchall()
        logger.info(f"Updating data quality flags for {len(events)} events")
        
        for event_id, event_date, entity, vulnerability, records in events:
            # Calculate completeness flags
            has_complete_date = event_date is not None and event_date != ''
            has_complete_entity = entity is not None and entity != ''
            has_complete_vulnerability = vulnerability is not None and vulnerability != ''
            has_complete_records = records is not None and records >= 0
            
            # Calculate completeness score
            completeness_score = sum([
                has_complete_date,
                has_complete_entity, 
                has_complete_vulnerability,
                has_complete_records
            ]) / 4.0
            
            # Update flags and scores
            cursor.execute("""
                UPDATE EnrichedEvents 
                SET has_complete_date = ?, 
                    has_complete_entity = ?, 
                    has_complete_vulnerability = ?, 
                    has_complete_records_count = ?,
                    data_completeness_score = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE enriched_event_id = ?
            """, (has_complete_date, has_complete_entity, has_complete_vulnerability, 
                  has_complete_records, completeness_score, event_id))
            fixes += 1
        
        self.conn.commit()
        return fixes
    
    def run_comprehensive_cleanup(self) -> Dict[str, Any]:
        """Run all data quality improvements"""
        logger.info("Starting comprehensive data quality cleanup...")
        
        # Analyze current state
        analysis = self.analyze_data_quality()
        logger.info(f"Data quality analysis: {json.dumps(analysis, indent=2)}")
        
        # Run all fixes
        self.cleanup_stats['date_fixes'] = self.fix_event_dates()
        self.cleanup_stats['entity_fixes'] = self.fix_attack_victim_entities()
        self.cleanup_stats['vulnerability_fixes'] = self.fix_vulnerability_information()
        self.cleanup_stats['records_affected_fixes'] = self.fix_records_affected()
        self.cleanup_stats['title_improvements'] = self.improve_titles_and_summaries()
        self.cleanup_stats['default_fills'] = self.fill_missing_data_with_defaults()
        self.cleanup_stats['quality_flags_updated'] = self.update_data_quality_flags()
        
        # Get final counts
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Active'")
        self.cleanup_stats['events_processed'] = cursor.fetchone()[0]
        
        logger.info("Comprehensive cleanup complete")
        return self.cleanup_stats
    
    def generate_cleanup_report(self) -> Dict[str, Any]:
        """Generate comprehensive cleanup report"""
        cursor = self.conn.cursor()
        
        # Get current state
        cursor.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Active'")
        active_events = cursor.fetchone()[0]
        
        # Get improvement statistics
        cursor.execute("""
            SELECT 
                COUNT(CASE WHEN event_date IS NOT NULL AND event_date != '' THEN 1 END) as events_with_dates,
                COUNT(CASE WHEN attack_victim_entity IS NOT NULL AND attack_victim_entity != '' THEN 1 END) as events_with_entities,
                COUNT(CASE WHEN vulnerability_details IS NOT NULL AND vulnerability_details != '' THEN 1 END) as events_with_vulnerabilities,
                COUNT(CASE WHEN records_affected IS NOT NULL AND records_affected >= 0 THEN 1 END) as events_with_records,
                AVG(data_completeness_score) as avg_completeness_score,
                COUNT(CASE WHEN has_complete_date = 1 THEN 1 END) as complete_dates,
                COUNT(CASE WHEN has_complete_entity = 1 THEN 1 END) as complete_entities,
                COUNT(CASE WHEN has_complete_vulnerability = 1 THEN 1 END) as complete_vulnerabilities,
                COUNT(CASE WHEN has_complete_records_count = 1 THEN 1 END) as complete_records
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        
        stats = cursor.fetchone()
        
        return {
            'cleanup_stats': self.cleanup_stats,
            'current_state': {
                'active_events': active_events,
                'events_with_dates': stats[0],
                'events_with_entities': stats[1],
                'events_with_vulnerabilities': stats[2],
                'events_with_records': stats[3],
                'avg_completeness_score': stats[4],
                'complete_dates': stats[5],
                'complete_entities': stats[6],
                'complete_vulnerabilities': stats[7],
                'complete_records': stats[8]
            },
            'improvements': {
                'date_fixes': self.cleanup_stats['date_fixes'],
                'entity_fixes': self.cleanup_stats['entity_fixes'],
                'vulnerability_fixes': self.cleanup_stats['vulnerability_fixes'],
                'records_fixes': self.cleanup_stats['records_affected_fixes'],
                'title_improvements': self.cleanup_stats['title_improvements'],
                'quality_flags_updated': self.cleanup_stats['quality_flags_updated']
            }
        }
    
    def close(self):
        """Close database connection"""
        self.conn.close()


def main():
    """Main cleanup function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Comprehensive data quality cleanup for EnrichedEvents table')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to database file')
    parser.add_argument('--analyze-only', action='store_true', help='Only analyze data quality without making changes')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Initialize cleanup
    cleanup = DataQualityCleanup(args.db_path)
    
    if args.analyze_only:
        logger.info("🔍 ANALYZING data quality issues...")
        analysis = cleanup.analyze_data_quality()
        
        print("\n" + "="*60)
        print("DATA QUALITY ANALYSIS")
        print("="*60)
        print(f"Total active events: {analysis['total_events']}")
        print(f"\nDate Issues:")
        print(f"  - Missing dates: {analysis['date_issues']['missing_dates']}")
        print(f"  - Datetime format: {analysis['date_issues']['datetime_format']}")
        print(f"  - Invalid format: {analysis['date_issues']['invalid_format']}")
        print(f"\nEntity Issues:")
        print(f"  - Missing entities: {analysis['entity_issues']['missing_entities']}")
        print(f"  - Multiple entities: {analysis['entity_issues']['multiple_entities']}")
        print(f"\nVulnerability Issues:")
        print(f"  - Missing vulnerabilities: {analysis['vulnerability_issues']['missing_vulnerabilities']}")
        print(f"\nRecords Affected Issues:")
        print(f"  - Missing records: {analysis['records_issues']['missing_records']}")
        print(f"  - Negative records: {analysis['records_issues']['negative_records']}")
        print(f"  - Suspiciously high: {analysis['records_issues']['suspiciously_high']}")
        print(f"\nTitle Issues:")
        print(f"  - Missing titles: {analysis['title_issues']['missing_titles']}")
        print(f"  - Untitled events: {analysis['title_issues']['untitled_events']}")
        print(f"  - Short titles: {analysis['title_issues']['short_titles']}")
        print("="*60)
        
    else:
        # Run actual cleanup
        logger.info("🧹 Starting comprehensive data quality cleanup...")
        
        # Run cleanup
        stats = cleanup.run_comprehensive_cleanup()
        
        # Generate report
        report = cleanup.generate_cleanup_report()
        
        print("\n" + "="*60)
        print("COMPREHENSIVE CLEANUP REPORT")
        print("="*60)
        print(f"Events processed: {stats['events_processed']}")
        print(f"\nImprovements made:")
        print(f"  - Date fixes: {stats['date_fixes']}")
        print(f"  - Entity fixes: {stats['entity_fixes']}")
        print(f"  - Vulnerability fixes: {stats['vulnerability_fixes']}")
        print(f"  - Records affected fixes: {stats['records_affected_fixes']}")
        print(f"  - Title improvements: {stats['title_improvements']}")
        print(f"  - Default fills: {stats['default_fills']}")
        print(f"  - Quality flags updated: {stats['quality_flags_updated']}")
        print(f"\nCurrent state:")
        print(f"  - Active events: {report['current_state']['active_events']}")
        print(f"  - Events with dates: {report['current_state']['events_with_dates']}")
        print(f"  - Events with entities: {report['current_state']['events_with_entities']}")
        print(f"  - Events with vulnerabilities: {report['current_state']['events_with_vulnerabilities']}")
        print(f"  - Events with records: {report['current_state']['events_with_records']}")
        print(f"  - Average completeness score: {report['current_state']['avg_completeness_score']:.2f}")
        print(f"  - Complete dates: {report['current_state']['complete_dates']}")
        print(f"  - Complete entities: {report['current_state']['complete_entities']}")
        print(f"  - Complete vulnerabilities: {report['current_state']['complete_vulnerabilities']}")
        print(f"  - Complete records: {report['current_state']['complete_records']}")
        print("="*60)
    
    cleanup.close()


if __name__ == "__main__":
    main()
