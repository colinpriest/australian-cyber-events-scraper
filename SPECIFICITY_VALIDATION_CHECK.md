# Specificity Validation Check - Catch GPT-4o Mistakes

## Problem

Even with improved prompts, GPT-4o may misclassify incidents. We need a validation layer to catch obvious errors.

## Solution: Add Heuristic Override in Validation Stage

Add a new validation method in `enrichment_validator.py` that checks if GPT-4o's `is_specific` classification makes sense given other signals.

### Heuristic Rules

**OVERRIDE is_specific=False → TRUE if:**

1. **Victim + Impact indicators present:**
   - Victim organization identified (not null)
   - AND (records_affected > 0 OR incident_date present OR attack_type specific)
   - Australian relevance > 0.7

2. **Title contains incident keywords:**
   - Title contains: "breach", "attack", "hack", "ransomware", "incident", "compromised", "exposed"
   - AND victim name in title
   - AND not an aggregate URL (blog/weekly/monthly/roundup)

3. **Concrete details present:**
   - Incident date identified
   - Specific attack type (not "cyber incident" generic)
   - Impact metrics (records_affected, systems_affected, etc.)

**OVERRIDE is_specific=True → FALSE if:**

1. **Aggregate article indicators:**
   - URL contains: /blog/, /weekly, /monthly, /roundup, /digest
   - AND title is generic (doesn't mention specific organization)
   - AND article discusses 5+ organizations

2. **Educational content indicators:**
   - Title starts with: "How to", "Guide to", "Best practices"
   - No specific victim identified
   - No specific date/timeline

## Implementation Code

Add to `enrichment_validator.py`:

```python
def _validate_specificity(self, extraction: Dict, event_title: str = None, event_url: str = None) -> Dict:
    """
    Validate is_specific_incident classification using heuristics.

    Catches GPT-4o mistakes by checking if the classification makes sense
    given other extracted signals.
    """

    warnings = []
    overrides = []

    is_specific = extraction.get('specificity', {}).get('is_specific_incident')
    victim = extraction.get('victim', {}).get('organization')
    australian_rel = extraction.get('australian_relevance', {}).get('relevance_score', 0)
    records_affected = extraction.get('incident', {}).get('records_affected')
    incident_date = extraction.get('incident', {}).get('incident_date')
    attack_type = extraction.get('attacker', {}).get('attack_type', '')

    # RULE 1: Override False → True if strong incident indicators present
    if is_specific == False and victim and australian_rel > 0.7:
        # Check for concrete incident details
        has_concrete_details = (
            records_affected and records_affected > 0
        ) or (
            incident_date is not None
        ) or (
            attack_type and attack_type.lower() not in ['cyber incident', 'unknown', 'not specified']
        )

        if has_concrete_details:
            overrides.append({
                'original': False,
                'override': True,
                'reason': f"Event has concrete incident details (victim: {victim}, australian_relevance: {australian_rel:.2f}, details present)"
            })
            warnings.append(
                f"SPECIFICITY OVERRIDE: GPT-4o marked as non-specific, but event has victim + concrete details. "
                f"Overriding to is_specific=True for Australian event about {victim}"
            )

    # RULE 2: Override False → True if title contains incident keywords
    if is_specific == False and event_title and victim:
        title_lower = event_title.lower()
        incident_keywords = ['breach', 'attack', 'hack', 'ransomware', 'incident', 'compromised', 'exposed', 'hit by']

        has_incident_keyword = any(kw in title_lower for kw in incident_keywords)
        victim_in_title = victim.lower() in title_lower or any(word.lower() in title_lower for word in victim.split() if len(word) > 3)

        # Check if it's NOT an aggregate URL
        is_aggregate = False
        if event_url:
            aggregate_patterns = ['blog/', 'weekly', 'monthly', 'roundup', 'digest', 'update']
            is_aggregate = any(pattern in event_url.lower() for pattern in aggregate_patterns)

        if has_incident_keyword and victim_in_title and not is_aggregate and australian_rel > 0.5:
            overrides.append({
                'original': False,
                'override': True,
                'reason': f"Title contains incident keywords and victim name, high Australian relevance"
            })
            warnings.append(
                f"SPECIFICITY OVERRIDE: Title '{event_title[:60]}...' contains incident keywords + victim name. "
                f"Overriding to is_specific=True"
            )

    # RULE 3: Override True → False if clearly educational/generic
    if is_specific == True and event_title:
        title_lower = event_title.lower()
        educational_prefixes = ['how to', 'guide to', 'best practices', 'tips for', '5 ways', '10 steps']

        is_educational = any(title_lower.startswith(prefix) for prefix in educational_prefixes)

        if is_educational and not victim:
            overrides.append({
                'original': True,
                'override': False,
                'reason': f"Title suggests educational content, no victim identified"
            })
            warnings.append(
                f"SPECIFICITY OVERRIDE: Title '{event_title[:60]}...' appears educational without specific victim. "
                f"Overriding to is_specific=False"
            )

    return {
        'has_warnings': len(warnings) > 0,
        'warnings': warnings,
        'overrides': overrides
    }
```

## Integration

Call this in `validate()` method after existing checks:

```python
def validate(self, extraction: Dict[str, Any], fact_check: Dict[str, Any],
             event_title: str = None, event_url: str = None) -> Dict[str, Any]:

    # ... existing validation checks ...

    # NEW: Specificity validation
    if event_title:
        spec_check = self._validate_specificity(extraction, event_title, event_url)
        if spec_check['has_warnings']:
            warnings.extend(spec_check['warnings'])

        # Apply overrides to the extraction result
        if spec_check['overrides']:
            for override in spec_check['overrides']:
                extraction['specificity']['is_specific_incident'] = override['override']
                extraction['specificity']['specificity_reasoning'] += f" [VALIDATOR OVERRIDE: {override['reason']}]"

    # ... rest of validation ...
```

## Expected Impact

With this validation check:

**Example 1: "Qantas cyber security breach: What personal details were exposed"**
- GPT-4o: is_specific = False (sees "analysis")
- Validator:
  - Title contains "breach" ✓
  - Victim "Qantas" in title ✓
  - Australian relevance = 0.8 ✓
  - NOT aggregate URL ✓
- **OVERRIDE → is_specific = True** ✅

**Example 2: "How to Protect Your Business from Ransomware"**
- GPT-4o: is_specific = True (mistakenly)
- Validator:
  - Title starts with "How to" ✓
  - No victim identified ✓
- **OVERRIDE → is_specific = False** ✅

**Example 3: "Weekly Cyber News Roundup"**
- GPT-4o: is_specific = False ✓
- Validator: No override needed
- **Result: is_specific = False** ✅

## Benefits

1. **Catches GPT-4o mistakes** without requiring perfect prompts
2. **Uses multiple signals** (title, victim, australian_relevance, URL)
3. **Logged as warnings** for transparency
4. **Updates reasoning** to show override was applied
5. **Conservative** - only overrides when strong evidence
