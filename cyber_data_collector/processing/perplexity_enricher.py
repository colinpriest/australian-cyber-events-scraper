"""
Perplexity AI-based event detail enrichment.

Fills in missing details for cyber events using Perplexity AI queries.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import requests
from cyber_data_collector.models.vulnerability_taxonomy import VULNERABILITY_CATEGORIES, validate_vulnerability_category


class PerplexityEventEnricher:
    """Enriches cyber events with missing details using Perplexity AI."""
    
    def __init__(self, db_path: str, perplexity_api_key: str):
        """Initialize with database connection and Perplexity API key."""
        self.db_path = db_path
        self.perplexity_api_key = perplexity_api_key
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {perplexity_api_key}',
            'Content-Type': 'application/json'
        })
        
    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with proper configuration."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def find_events_with_missing_details(self, start_date: str = None, end_date: str = None, 
                                       limit: int = None, force: bool = False) -> List[Dict]:
        """Query database for events missing key details."""
        query = """
        SELECT 
            deduplicated_event_id,
            title,
            summary,
            event_date,
            severity,
            records_affected,
            threat_actor,
            vulnerability_details,
            vulnerability_category,
            regulatory_fine_amount,
            regulatory_fine_currency,
            regulatory_authority,
            last_enrichment_date
        FROM DeduplicatedEvents
        WHERE status = 'Active'
        """
        params = []
        
        if start_date:
            query += " AND event_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND event_date <= ?"
            params.append(end_date)
        
        # Skip recently enriched events unless force is True
        if not force:
            query += " AND (last_enrichment_date IS NULL OR last_enrichment_date < datetime('now', '-30 days'))"
            
        query += " ORDER BY event_date DESC"
        
        if limit:
            query += " LIMIT ?"
            params.append(limit)
            
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error querying events: {e}")
            return []
    
    def check_missing_fields(self, event: Dict) -> Dict[str, bool]:
        """Return which fields are missing for an event."""
        missing = {}
        
        # Check for missing attacker information
        missing['attacker'] = not event.get('threat_actor') or event.get('threat_actor') == 'Unknown'
        
        # Check for missing vulnerability details
        missing['vulnerability'] = not event.get('vulnerability_details') or len(event.get('vulnerability_details', '')) < 50
        
        # Check for missing vulnerability category
        missing['vulnerability_category'] = not event.get('vulnerability_category')
        
        # Check for missing regulatory fines
        missing['regulatory_fines'] = not event.get('regulatory_fine_amount')
        
        # Check for missing severity
        missing['severity'] = not event.get('severity') or event.get('severity') == 'Unknown'
        
        # Check for missing records affected
        missing['records_affected'] = not event.get('records_affected') or event.get('records_affected') == 0
        
        return missing
    
    def _construct_perplexity_query(self, event: Dict, missing_fields: List[str]) -> str:
        """Construct a comprehensive Perplexity query for missing fields."""
        title = event.get('title', 'cyber incident')
        event_date = event.get('event_date', 'recent')
        
        # Extract entity name from title or summary
        entity_name = self._extract_entity_name(event)
        
        query_parts = []
        
        if 'attacker' in missing_fields:
            query_parts.append(f"Who was responsible for the {title} cyber attack on {entity_name} in {event_date}? What threat actor or group claimed responsibility?")
        
        if 'vulnerability' in missing_fields or 'vulnerability_category' in missing_fields:
            query_parts.append(f"What security vulnerability or weakness allowed the {title} attack on {entity_name} in {event_date}? What was the root cause or security flaw exploited?")
        
        if 'regulatory_fines' in missing_fields:
            query_parts.append(f"Were any regulatory fines or penalties imposed after the {title} incident involving {entity_name} in {event_date}? If so, what was the amount and which regulator imposed it?")
        
        if 'severity' in missing_fields:
            query_parts.append(f"What was the severity and impact of the {title} cyber incident involving {entity_name} in {event_date}? Was it classified as critical, high, medium, or low severity?")
        
        if 'records_affected' in missing_fields:
            query_parts.append(f"How many records or accounts were affected in the {title} incident involving {entity_name} in {event_date}? What was the scale of the data breach?")
        
        # Add vulnerability classification if needed
        if 'vulnerability_category' in missing_fields:
            categories_str = ", ".join(VULNERABILITY_CATEGORIES)
            query_parts.append(f"Classify the vulnerability into one of these categories: {categories_str}. Provide the category and explain why.")
        
        return " ".join(query_parts)
    
    def _extract_entity_name(self, event: Dict) -> str:
        """Extract entity name from event title or summary."""
        title = event.get('title', '')
        summary = event.get('summary', '')
        
        # Try to extract company/organization name from title
        # Look for patterns like "Company Name Data Breach" or "Attack on Company Name"
        patterns = [
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:Data Breach|Cyber Attack|Security Incident)',
            r'(?:Attack on|Breach at|Incident at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:suffers|experiences|reports)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                return match.group(1)
        
        # Fallback to first few words of title
        words = title.split()[:3]
        return " ".join(words) if words else "Unknown Entity"
    
    async def query_perplexity_for_details(self, event: Dict, missing_fields: List[str]) -> Dict:
        """Ask Perplexity to fill in missing details."""
        query = self._construct_perplexity_query(event, missing_fields)
        
        payload = {
            "model": "sonar-pro",
            "messages": [
                {
                    "role": "user",
                    "content": query
                }
            ],
            "max_tokens": 1000,
            "temperature": 0.1,
            "stream": False
        }
        
        try:
            response = self.session.post(
                "https://api.perplexity.ai/chat/completions",
                json=payload,
                timeout=30
            )
            
            if response.status_code != 200:
                self.logger.error(f"Perplexity API error: {response.status_code} - {response.text}")
                return {}
            
            data = response.json()
            
            if 'choices' not in data or not data['choices']:
                self.logger.error(f"Invalid API response: {data}")
                return {}
            
            content = data['choices'][0]['message']['content']
            
            return self._parse_perplexity_response(content, missing_fields)
            
        except Exception as e:
            self.logger.error(f"Perplexity API error: {e}")
            return {}
    
    def _parse_perplexity_response(self, response: str, missing_fields: List[str]) -> Dict:
        """Extract structured data from Perplexity response."""
        enriched_data = {}
        
        # Parse attacker information
        if 'attacker' in missing_fields:
            attacker = self._extract_attacker_info(response)
            if attacker:
                enriched_data['threat_actor'] = attacker
        
        # Parse vulnerability details
        if 'vulnerability' in missing_fields:
            vulnerability = self._extract_vulnerability_details(response)
            if vulnerability:
                enriched_data['vulnerability_details'] = vulnerability
        
        # Parse vulnerability category
        if 'vulnerability_category' in missing_fields:
            category = self._extract_vulnerability_category(response)
            if category:
                enriched_data['vulnerability_category'] = category
        
        # Parse regulatory fines
        if 'regulatory_fines' in missing_fields:
            fine_info = self._extract_regulatory_fines(response)
            if fine_info:
                enriched_data.update(fine_info)
        
        # Parse severity
        if 'severity' in missing_fields:
            severity = self._extract_severity(response)
            if severity:
                enriched_data['severity'] = severity
        
        # Parse records affected
        if 'records_affected' in missing_fields:
            records = self._extract_records_affected(response)
            if records:
                enriched_data['records_affected'] = records
        
        return enriched_data
    
    def _extract_attacker_info(self, response: str) -> Optional[str]:
        """Extract attacker/threat actor information."""
        # Look for patterns indicating threat actors
        patterns = [
            r'(?:attributed to|claimed by|responsible for).*?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
            r'(?:threat actor|group|organization).*?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
            r'(?:hacker|attacker).*?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                return match.group(1)
        
        # Check for "unknown" or "not disclosed"
        if any(phrase in response.lower() for phrase in ['unknown', 'not disclosed', 'not publicly identified']):
            return 'Unknown (not publicly disclosed)'
        
        return None
    
    def _extract_vulnerability_details(self, response: str) -> Optional[str]:
        """Extract vulnerability details."""
        # Look for technical details about the vulnerability
        sentences = response.split('.')
        vulnerability_sentences = []
        
        keywords = ['vulnerability', 'exploit', 'weakness', 'flaw', 'breach', 'compromise', 'attack vector']
        
        for sentence in sentences:
            if any(keyword in sentence.lower() for keyword in keywords):
                vulnerability_sentences.append(sentence.strip())
        
        if vulnerability_sentences:
            return '. '.join(vulnerability_sentences[:3])  # Limit to 3 sentences
        
        return None
    
    def _extract_vulnerability_category(self, response: str) -> Optional[str]:
        """Extract vulnerability category from response."""
        response_lower = response.lower()
        
        # Map keywords to categories
        category_keywords = {
            'Authentication Weakness': ['password', 'credential', 'authentication', 'login'],
            'Access Control Failure': ['access control', 'unauthorized access', 'privilege'],
            'Injection Attacks': ['injection', 'sql injection', 'command injection'],
            'Phishing/Social Engineering': ['phishing', 'social engineering', 'email'],
            'Ransomware': ['ransomware', 'encryption', 'ransom'],
            'Malware': ['malware', 'trojan', 'virus', 'backdoor'],
            'Configuration Error': ['misconfigured', 'configuration', 'exposed'],
            'Unpatched Software': ['unpatched', 'outdated', 'cve', 'vulnerability'],
            'Supply Chain Attack': ['supply chain', 'third party', 'vendor'],
            'Zero-Day Exploit': ['zero-day', 'zero day', 'unknown vulnerability'],
            'DDoS Attack': ['ddos', 'denial of service', 'overwhelm'],
            'Insider Threat': ['insider', 'employee', 'internal'],
            'Physical Security': ['physical', 'stolen', 'device'],
            'API Vulnerability': ['api', 'endpoint', 'interface'],
            'Cross-Site Scripting (XSS)': ['xss', 'cross-site scripting'],
            'Business Logic Flaw': ['business logic', 'application logic'],
            'Cryptographic Failure': ['encryption', 'cryptographic', 'cipher']
        }
        
        # Find best matching category
        best_match = None
        max_matches = 0
        
        for category, keywords in category_keywords.items():
            matches = sum(1 for keyword in keywords if keyword in response_lower)
            if matches > max_matches:
                max_matches = matches
                best_match = category
        
        # Also check for explicit category mentions
        for category in VULNERABILITY_CATEGORIES:
            if category.lower() in response_lower:
                return category
        
        return best_match if max_matches > 0 else None
    
    def _extract_regulatory_fines(self, response: str) -> Optional[Dict]:
        """Extract regulatory fine information."""
        # Look for monetary amounts
        amount_patterns = [
            r'(\$[\d,]+(?:\.\d{2})?)\s*(million|billion|thousand)?',
            r'([\d,]+(?:\.\d{2})?)\s*(million|billion|thousand)\s*(dollars?|USD|AUD)',
            r'fine.*?(\$[\d,]+(?:\.\d{2})?)',
            r'penalty.*?(\$[\d,]+(?:\.\d{2})?)'
        ]
        
        for pattern in amount_patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                amount_str = match.group(1).replace('$', '').replace(',', '')
                try:
                    amount = float(amount_str)
                    
                    # Check for multipliers
                    if 'million' in response.lower():
                        amount *= 1000000
                    elif 'billion' in response.lower():
                        amount *= 1000000000
                    elif 'thousand' in response.lower():
                        amount *= 1000
                    
                    # Determine currency
                    currency = 'USD'
                    if 'aud' in response.lower() or 'australian' in response.lower():
                        currency = 'AUD'
                    elif 'eur' in response.lower() or 'euro' in response.lower():
                        currency = 'EUR'
                    
                    # Extract regulatory authority
                    authority = 'Unknown'
                    authority_patterns = [
                        r'(ACMA|Australian Communications and Media Authority)',
                        r'(OAIC|Office of the Australian Information Commissioner)',
                        r'(ASIC|Australian Securities and Investments Commission)',
                        r'(FTC|Federal Trade Commission)',
                        r'(GDPR|General Data Protection Regulation)'
                    ]
                    
                    for auth_pattern in authority_patterns:
                        auth_match = re.search(auth_pattern, response, re.IGNORECASE)
                        if auth_match:
                            authority = auth_match.group(1)
                            break
                    
                    return {
                        'regulatory_fine_amount': amount,
                        'regulatory_fine_currency': currency,
                        'regulatory_authority': authority
                    }
                except ValueError:
                    continue
        
        return None
    
    def _extract_severity(self, response: str) -> Optional[str]:
        """Extract severity level."""
        response_lower = response.lower()
        
        if any(word in response_lower for word in ['critical', 'severe', 'catastrophic']):
            return 'Critical'
        elif any(word in response_lower for word in ['high', 'significant', 'major']):
            return 'High'
        elif any(word in response_lower for word in ['medium', 'moderate', 'modest']):
            return 'Medium'
        elif any(word in response_lower for word in ['low', 'minor', 'minimal']):
            return 'Low'
        
        return None
    
    def _extract_records_affected(self, response: str) -> Optional[int]:
        """Extract number of records affected."""
        # Look for number patterns
        patterns = [
            r'(\d+(?:,\d{3})*)\s*(?:records?|accounts?|customers?|users?)',
            r'(\d+(?:,\d{3})*)\s*(?:million|billion|thousand)',
            r'(\d+(?:,\d{3})*)\s*(?:people|individuals)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                number_str = match.group(1).replace(',', '')
                try:
                    number = int(number_str)
                    
                    # Check for multipliers
                    if 'million' in response.lower():
                        number *= 1000000
                    elif 'billion' in response.lower():
                        number *= 1000000000
                    elif 'thousand' in response.lower():
                        number *= 1000
                    
                    return number
                except ValueError:
                    continue
        
        return None
    
    def update_event_in_database(self, event_id: str, enriched_data: Dict):
        """Update database with newly enriched information."""
        if not enriched_data:
            return
        
        # Build update query dynamically
        set_clauses = []
        params = []
        
        for field, value in enriched_data.items():
            if field in ['threat_actor', 'vulnerability_details', 'vulnerability_category', 
                        'regulatory_fine_amount', 'regulatory_fine_currency', 'regulatory_authority',
                        'severity', 'records_affected']:
                set_clauses.append(f"{field} = ?")
                params.append(value)
        
        if not set_clauses:
            return
        
        # Add enrichment metadata
        set_clauses.append("enrichment_source = ?")
        params.append("Perplexity AI")
        
        set_clauses.append("last_enrichment_date = ?")
        params.append(datetime.now().isoformat())
        
        params.append(event_id)
        
        query = f"""
        UPDATE DeduplicatedEvents 
        SET {', '.join(set_clauses)}
        WHERE deduplicated_event_id = ?
        """
        
        try:
            with self._get_connection() as conn:
                conn.execute(query, params)
                conn.commit()
                self.logger.info(f"Updated event {event_id} with enriched data")
        except Exception as e:
            self.logger.error(f"Error updating event {event_id}: {e}")
    
    async def run_enrichment(self, limit: int = None, dry_run: bool = False, 
                           start_date: str = None, end_date: str = None,
                           fields: List[str] = None, delay: float = 2.0, force: bool = False):
        """Main enrichment process."""
        self.logger.info("Starting Perplexity enrichment process...")
        
        # Get events with missing details
        events = self.find_events_with_missing_details(start_date, end_date, limit, force)
        
        if not events:
            self.logger.info("No events found for enrichment")
            return
        
        self.logger.info(f"Found {len(events)} events to process")
        
        enriched_count = 0
        failed_count = 0
        
        for i, event in enumerate(events, 1):
            event_id = event['deduplicated_event_id']
            title = event['title']
            
            self.logger.info(f"Processing event {i}/{len(events)}: {title}")
            
            # Check which fields are missing
            missing_fields = self.check_missing_fields(event)
            
            # Filter by requested fields if specified
            if fields:
                missing_fields = {k: v for k, v in missing_fields.items() if k in fields}
            
            # Skip if no missing fields
            if not any(missing_fields.values()):
                self.logger.info(f"  No missing fields for {title}")
                continue
            
            missing_list = [k for k, v in missing_fields.items() if v]
            self.logger.info(f"  Missing fields: {', '.join(missing_list)}")
            
            if dry_run:
                self.logger.info(f"  [DRY RUN] Would query Perplexity for: {', '.join(missing_list)}")
                continue
            
            try:
                # Query Perplexity for missing details
                enriched_data = await self.query_perplexity_for_details(event, missing_list)
                
                if enriched_data:
                    self.logger.info(f"  [SUCCESS] Found data for: {', '.join(enriched_data.keys())}")
                    
                    # Update database
                    self.update_event_in_database(event_id, enriched_data)
                    enriched_count += 1
                else:
                    self.logger.info(f"  [FAILED] No additional information found")
                    failed_count += 1
                
                # Rate limiting
                if delay > 0:
                    await asyncio.sleep(delay)
                    
            except Exception as e:
                self.logger.error(f"  [ERROR] Error processing {title}: {e}")
                failed_count += 1
        
        self.logger.info(f"Enrichment complete! Successfully enriched: {enriched_count}, Failed: {failed_count}")
