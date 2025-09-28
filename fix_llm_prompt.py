#!/usr/bin/env python3
"""
Script to fix the LLM prompt to be less aggressive and match training data expectations.
"""

import os
from pathlib import Path

def backup_original_file():
    """Create a backup of the original LLM classifier."""
    original_file = Path("cyber_data_collector/processing/llm_classifier.py")
    backup_file = Path("cyber_data_collector/processing/llm_classifier_backup.py")

    if original_file.exists() and not backup_file.exists():
        with open(original_file, 'r') as f:
            content = f.read()
        with open(backup_file, 'w') as f:
            f.write(content)
        print(f"✓ Created backup: {backup_file}")

def get_optimized_prompt():
    """Get the optimized prompt that matches training data expectations."""

    return '''FIRST, determine if this is actually a cybersecurity INCIDENT and if it's Australian-relevant.

Event Title: {request.title}
Event Description: {request.description}
Affected Entities: {', '.join(request.entity_names)}
Raw Data Snippets: {' '.join(request.raw_data_sources)}

STEP 1 - VALIDATION (CRITICAL):
- `is_cybersecurity_event`: Is this genuinely about ONE SPECIFIC cybersecurity INCIDENT that actually happened to a named organization?
  - Return TRUE for:
    * Specific incidents affecting named organizations (e.g., "Toll Group Ransomware Attack", "Perth Mint data breach")
    * Individual cyber attacks, data breaches, malware infections that OCCURRED
    * Specific security incidents with clear organizational impact
  - Return FALSE for:
    * General summaries with words: "Multiple", "Several", "Various", "incidents"
    * Time-period reports: "January 2020", "Q1 2020", "2020 breaches"
    * OAIC regulatory reports and summaries
    * Policy documents: "action plan", "framework", "guidance", "guidelines"
    * Educational content: "What is a cyber attack?", training materials
    * General trend analyses or market reports

- `is_australian_relevant`: Does this SPECIFIC INCIDENT affect Australian organizations, systems, or citizens?
  - Return TRUE for incidents affecting Australian entities
  - Return FALSE for: generic global events without Australian connection

IMPORTANT: Be LESS STRICT than before. If an event describes a specific incident affecting a named organization, ACCEPT it.

Examples to ACCEPT:
- "Toll Group Ransomware Attack" ✓
- "Perth Mint visitor data stolen" ✓
- "Australian National University cyber attack" ✓
- "Canva Security Incident" ✓
- "Travelex website hit by ransomware" ✓

Examples to REJECT:
- "Multiple Cyber Incidents Reported in Australia (January 2020)" ✗
- "OAIC Notifiable Data Breaches: January–June 2020" ✗
- "What is a cyber attack?" ✗
- "Australian Data Breach Action Plan" ✗

When in doubt about whether something is a specific incident, ACCEPT it rather than reject it.'''

def get_optimized_system_prompt():
    """Get the optimized system prompt."""

    return '''You are a cybersecurity incident analyst focused on identifying SPECIFIC cybersecurity incidents affecting Australian organizations.

ACCEPT events that describe:
- Specific cyber attacks on named organizations
- Individual data breaches affecting Australian entities
- Particular security incidents with organizational impact
- Ransomware attacks on specific companies
- Data theft from named Australian businesses

REJECT events that are:
- General summaries or reports covering multiple incidents
- Regulatory reports from OAIC or government agencies
- Policy documents, frameworks, or guidelines
- Educational content about cybersecurity
- Trend analyses or market reports

BIAS TOWARD ACCEPTANCE: If an event describes something that happened to a specific organization, even if details are limited, ACCEPT it rather than reject it. The goal is to capture all potential incidents for further analysis.

Be less strict than previous iterations. Err on the side of inclusion rather than exclusion.'''

def update_llm_classifier():
    """Update the LLM classifier with optimized prompts."""

    llm_classifier_file = Path("cyber_data_collector/processing/llm_classifier.py")

    if not llm_classifier_file.exists():
        print(f"❌ File not found: {llm_classifier_file}")
        return False

    # Read current content
    with open(llm_classifier_file, 'r') as f:
        content = f.read()

    # Find and replace the user prompt
    old_user_prompt_start = 'user_prompt = f"""'
    old_system_prompt_start = '"content": ('

    new_user_prompt = get_optimized_prompt()
    new_system_prompt = get_optimized_system_prompt()

    # Replace user prompt
    if old_user_prompt_start in content:
        start_idx = content.find(old_user_prompt_start) + len(old_user_prompt_start)
        end_idx = content.find('"""', start_idx)

        if end_idx != -1:
            new_content = (
                content[:start_idx] + '\n' +
                new_user_prompt + '\n        ' +
                content[end_idx:]
            )
            content = new_content
            print("✓ Updated user prompt")
        else:
            print("❌ Could not find end of user prompt")
            return False
    else:
        print("❌ Could not find user prompt section")
        return False

    # Replace system prompt
    system_prompt_marker = '"You are a strict cybersecurity incident analyst.'
    if system_prompt_marker in content:
        start_idx = content.find(system_prompt_marker)
        end_idx = content.find('"', start_idx + 1)

        if end_idx != -1:
            new_content = (
                content[:start_idx] + '"' +
                new_system_prompt + '"' +
                content[end_idx + 1:]
            )
            content = new_content
            print("✓ Updated system prompt")
        else:
            print("❌ Could not find end of system prompt")
    else:
        print("⚠️  Could not find system prompt section - this is okay")

    # Write updated content
    with open(llm_classifier_file, 'w') as f:
        f.write(content)

    print(f"✓ Updated {llm_classifier_file}")
    return True

def show_recommended_changes():
    """Show the recommended changes that need to be made."""

    print("RECOMMENDED LLM PROMPT CHANGES")
    print("=" * 60)

    print("\n1. MAKE LLM LESS STRICT:")
    print("   - Current prompt is rejecting valid incidents like 'Toll Group Ransomware Attack'")
    print("   - Need to bias toward ACCEPTANCE of specific incidents")
    print("   - Only reject obvious summaries, reports, and policy documents")

    print("\n2. KEY CHANGES:")
    print("   - Add explicit examples of what to ACCEPT")
    print("   - Emphasize 'when in doubt, ACCEPT'")
    print("   - Focus rejection on obvious non-incidents")

    print("\n3. EXPECTED IMPACT:")
    print("   - Should increase events passing LLM from 2 to 8-12")
    print("   - Will capture valid incidents currently being filtered out")
    print("   - Maintains quality by still rejecting summaries and reports")

def main():
    """Main function to fix the LLM prompt."""

    print("FIXING LLM PROMPT AGGRESSIVENESS")
    print("=" * 60)

    print("\nPROBLEM IDENTIFIED:")
    print("- 18 raw events for January 2020")
    print("- Random Forest keeps ~10-14 events")
    print("- LLM only accepts 2 events (too strict!)")
    print("- Valid incidents like 'Toll Group Ransomware Attack' being rejected")

    print("\nSOLUTION:")
    print("- Update LLM prompt to be less aggressive")
    print("- Bias toward accepting specific incidents")
    print("- Maintain rejection of obvious summaries/reports")

    # Create backup
    backup_original_file()

    # Show what we're going to change
    show_recommended_changes()

    # Ask for confirmation
    response = input("\nProceed with updating the LLM classifier? (y/n): ")
    if response.lower() != 'y':
        print("Update cancelled.")
        return

    # Update the classifier
    if update_llm_classifier():
        print("\n✅ SUCCESS!")
        print("LLM classifier has been updated with less aggressive prompt.")
        print("\nNext steps:")
        print("1. Test with: python quick_filter_test.py")
        print("2. Run full pipeline to see improved results")
        print("3. Monitor for better event acceptance rates")
    else:
        print("\n❌ FAILED!")
        print("Could not update LLM classifier automatically.")
        print("Manual update required.")

if __name__ == "__main__":
    main()