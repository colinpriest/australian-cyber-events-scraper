#!/usr/bin/env python3
"""
Enhance EntitiesV2 table with proper entity types and detailed information using Perplexity.

This script will:
1. Fix entity_type to use proper categories (government/business/not-for-profit/individual)
2. Populate industry, turnover, employee_count, headquarters_location, website_url using Perplexity
3. Improve Australian entity detection using LLM analysis
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any

import openai
from pydantic import BaseModel, Field

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2

# Load environment variables
from dotenv import load_dotenv
import os
load_dotenv()


class EntityDetails(BaseModel):
    """Structured entity information from Perplexity."""
    entity_type: str = Field(..., description="One of: government, business, not-for-profit, individual, threat-actor")
    industry: Optional[str] = Field(None, description="Industry sector (e.g., Banking, Healthcare, Mining)")
    turnover: Optional[str] = Field(None, description="Annual revenue/turnover (e.g., '$50M', 'Unknown')")
    employee_count: Optional[int] = Field(None, description="Number of employees (numeric or None)")
    is_australian: bool = Field(..., description="True if entity is Australian-based or Australian-owned")
    headquarters_location: Optional[str] = Field(None, description="Primary location (e.g., 'Sydney, NSW, Australia')")
    website_url: Optional[str] = Field(None, description="Official website URL")
    confidence_score: float = Field(..., description="Confidence in the information (0.0-1.0)")


class EntityEnhancer:
    """Enhance entity information using Perplexity API."""

    def __init__(self):
        self.perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')
        if not self.perplexity_api_key:
            raise ValueError("PERPLEXITY_API_KEY not found in environment variables")

        self.client = openai.OpenAI(
            api_key=self.perplexity_api_key,
            base_url="https://api.perplexity.ai"
        )

    async def enhance_entity(self, entity_name: str) -> Optional[EntityDetails]:
        """Get detailed information about an entity from Perplexity."""

        try:
            # Create specific prompt for entity analysis
            prompt = f"""Analyze the entity "{entity_name}" and provide structured information:

1. Entity Type: Classify as one of:
   - government: Government agencies, councils, public sector bodies
   - business: Private companies, corporations, commercial entities
   - not-for-profit: Charities, NGOs, community organizations, foundations
   - individual: Named individuals, people
   - threat-actor: Criminal groups, hacking collectives, ransomware groups

2. Industry: Main business sector (if applicable)
3. Turnover: Annual revenue/turnover if known
4. Employee Count: Number of employees if known
5. Australian Connection: Is this entity Australian-based, Australian-owned, or operating primarily in Australia?
6. Headquarters: Primary location/headquarters
7. Website: Official website URL if known
8. Confidence: How confident are you in this information?

Focus on factual, current information. If unsure about any field, indicate 'Unknown' or None."""

            response = await asyncio.to_thread(
                lambda: self.client.chat.completions.create(
                    model="sonar",
                    messages=[
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=1000,
                    temperature=0.1
                )
            )

            content = response.choices[0].message.content

            # Parse the response to extract structured data
            entity_details = self._parse_response(content, entity_name)
            return entity_details

        except Exception as e:
            print(f"Error enhancing entity {entity_name}: {e}")
            return None

    def _parse_response(self, content: str, entity_name: str) -> EntityDetails:
        """Parse Perplexity response into structured EntityDetails."""

        # Initialize with defaults
        details = {
            'entity_type': 'business',  # Default assumption
            'industry': None,
            'turnover': None,
            'employee_count': None,
            'is_australian': False,
            'headquarters_location': None,
            'website_url': None,
            'confidence_score': 0.7
        }

        content_lower = content.lower()

        # Determine entity type based on keywords
        if any(word in content_lower for word in ['government', 'council', 'department', 'agency', 'ministry', 'bureau']):
            details['entity_type'] = 'government'
        elif any(word in content_lower for word in ['charity', 'foundation', 'non-profit', 'ngo', 'community']):
            details['entity_type'] = 'not-for-profit'
        elif any(word in content_lower for word in ['ransomware', 'hacker', 'cybercriminal', 'threat actor']):
            details['entity_type'] = 'threat-actor'
        elif any(word in entity_name.lower() for word in ['group', 'gang', 'collective']) and 'ransomware' in content_lower:
            details['entity_type'] = 'threat-actor'

        # Check for Australian indicators
        if any(word in content_lower for word in ['australia', 'australian', 'sydney', 'melbourne', 'brisbane', 'perth', 'adelaide', 'canberra', '.au']):
            details['is_australian'] = True

        # Try to extract specific information
        lines = content.split('\n')
        for line in lines:
            line_lower = line.lower()

            if 'industry' in line_lower or 'sector' in line_lower:
                # Try to extract industry
                if ':' in line:
                    industry = line.split(':', 1)[1].strip()
                    if industry and len(industry) < 100:
                        details['industry'] = industry

            if 'revenue' in line_lower or 'turnover' in line_lower or '$' in line:
                # Try to extract financial info
                if ':' in line:
                    turnover = line.split(':', 1)[1].strip()
                    if turnover and len(turnover) < 50:
                        details['turnover'] = turnover

            if 'employee' in line_lower or 'staff' in line_lower:
                # Try to extract employee count
                import re
                numbers = re.findall(r'(\d+(?:,\d+)*)', line)
                if numbers:
                    try:
                        details['employee_count'] = int(numbers[0].replace(',', ''))
                    except:
                        pass

            if 'headquarters' in line_lower or 'located' in line_lower or 'based' in line_lower:
                # Try to extract location
                if ':' in line:
                    location = line.split(':', 1)[1].strip()
                    if location and len(location) < 100:
                        details['headquarters_location'] = location

            if 'website' in line_lower or 'http' in line_lower or 'www.' in line_lower:
                # Try to extract website
                import re
                urls = re.findall(r'https?://[^\s]+|www\.[^\s]+', line)
                if urls:
                    details['website_url'] = urls[0]

        return EntityDetails(**details)


def get_entities_to_enhance(db):
    """Get entities that need enhancement (skip already enhanced entities)."""

    with db._lock:
        cursor = db._conn.cursor()
        cursor.execute("""
            SELECT entity_id, entity_name, entity_type, industry, is_australian,
                   turnover, employee_count, headquarters_location, website_url
            FROM EntitiesV2
            WHERE entity_type = 'Organization'
               OR (industry IS NULL AND entity_type != 'Organization')
               OR (headquarters_location IS NULL AND entity_type != 'Organization')
            ORDER BY entity_name
        """)
        return [dict(row) for row in cursor.fetchall()]


def update_entity_details(db, entity_id: int, details: EntityDetails):
    """Update entity with enhanced details."""

    with db._lock:
        cursor = db._conn.cursor()
        cursor.execute("""
            UPDATE EntitiesV2 SET
                entity_type = ?,
                industry = ?,
                turnover = ?,
                employee_count = ?,
                is_australian = ?,
                headquarters_location = ?,
                website_url = ?,
                confidence_score = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE entity_id = ?
        """, (
            details.entity_type,
            details.industry,
            details.turnover,
            details.employee_count,
            details.is_australian,
            details.headquarters_location,
            details.website_url,
            details.confidence_score,
            entity_id
        ))
        db._conn.commit()


async def enhance_all_entities():
    """Main function to enhance all entities."""

    print("Starting entity enhancement process...")

    db = CyberEventDataV2()
    enhancer = EntityEnhancer()

    entities = get_entities_to_enhance(db)
    print(f"Found {len(entities)} entities to enhance...")

    # Process only first 5 entities as a test
    test_entities = entities[:5]
    print(f"Processing first {len(test_entities)} entities as test...")

    enhanced_count = 0

    for i, entity in enumerate(test_entities, 1):
        print(f"\n[{i}/{len(test_entities)}] Enhancing: {entity['entity_name']}")

        try:
            # Check if entity already has sufficient data
            if (entity['entity_type'] != 'Organization' and
                entity.get('industry') and
                entity.get('headquarters_location')):
                print(f"  Already enhanced - skipping API call")
                print(f"  Type: {entity['entity_type']}")
                print(f"  Industry: {entity['industry'] or 'Unknown'}")
                print(f"  Australian: {entity['is_australian']}")
                print(f"  Location: {entity['headquarters_location'] or 'Unknown'}")
                enhanced_count += 1
                continue

            details = await enhancer.enhance_entity(entity['entity_name'])

            if details:
                update_entity_details(db, entity['entity_id'], details)
                print(f"  Type: {details.entity_type}")
                print(f"  Industry: {details.industry or 'Unknown'}")
                print(f"  Australian: {details.is_australian}")
                print(f"  Location: {details.headquarters_location or 'Unknown'}")
                enhanced_count += 1
            else:
                print("  Failed to enhance entity")

            # Rate limiting - Perplexity allows 100 requests per minute
            await asyncio.sleep(1.0)  # Conservative rate limiting

        except Exception as e:
            print(f"  Error: {e}")

    print(f"\nTest enhancement complete! Enhanced {enhanced_count}/{len(test_entities)} entities.")
    print(f"Remaining entities to process: {len(entities) - len(test_entities)}")
    db.close()


if __name__ == "__main__":
    asyncio.run(enhance_all_entities())