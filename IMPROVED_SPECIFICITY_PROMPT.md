# Improved is_specific_incident Prompt

## Current Problem

GPT-4o is confusing:
- "Analysis of a specific breach" → marks as non-specific ❌
- "Primary announcement of a breach" → marks as specific ✅

Both should be marked as specific if they describe a concrete incident.

## Proposed Replacement (Lines 304-318 in gpt4o_enricher.py)

```
═══════════════════════════════════════════════════════════════════════════════
5. EVENT SPECIFICITY ASSESSMENT
═══════════════════════════════════════════════════════════════════════════════

CRITICAL QUESTION: Does this article describe a SPECIFIC, CONCRETE cyber incident?

⚠️ IMPORTANT: Focus on WHETHER an incident is described, NOT on the article's style or purpose

SPECIFIC INCIDENT (is_specific_incident: true):

The article must describe a CONCRETE cyber incident with:
✓ An identified victim organization (who was attacked)
✓ A specific type of attack (breach, ransomware, DDoS, etc.)
✓ Concrete details about what happened

THESE ARE ALL SPECIFIC INCIDENTS (even if not primary sources):
✓ "Medibank confirms October 2022 data breach affecting 9.7M customers" (official announcement)
✓ "Qantas cyber security breach: What personal details were exposed" (investigation/analysis)
✓ "Timeline: How the Optus breach unfolded" (retrospective analysis)
✓ "Experts analyze the iiNet credential stuffing attack" (expert commentary on specific incident)
✓ "Lessons from the Toll Group ransomware attack" (using specific incident as case study)

NOT SPECIFIC (is_specific_incident: false):

✗ General cybersecurity news without specific victim ("Ransomware threats increase in 2024")
✗ Educational content without specific incident ("How to protect your business from phishing")
✗ Industry trends without specific incident ("Healthcare sector faces growing cyber risks")
✗ Opinion pieces about cybersecurity in general ("Why Australia needs better cyber laws")
✗ Multiple separate incidents in one article ("5 major breaches this quarter" - UNLESS title focuses on one)
✗ Aggregate blog/news roundups with generic titles ("Weekly Cyber News Roundup")

KEY DECISION RULE:

Ask yourself: "After reading this article, can I name:
  1. Which specific organization was attacked?
  2. What type of attack it was?
  3. When it happened (approximately)?

If YES to all 3 → is_specific_incident: TRUE
If NO to any → is_specific_incident: FALSE"

IMPORTANT CLARIFICATIONS:

✓ Analysis, commentary, or investigation OF a specific incident = SPECIFIC
✓ Follow-up reporting on a known incident = SPECIFIC
✓ Lessons/implications from a specific incident = SPECIFIC
✗ General advice not tied to a specific incident = NOT SPECIFIC
✗ Multiple incidents without focus on one = NOT SPECIFIC

EXAMPLES WITH REASONING:

Example 1:
Title: "The Qantas Breach: A Wake-Up Call on Third-Party Privacy Risks"
Content: Analyzes the Qantas 2022 breach, discusses implications for third-party risk
→ is_specific_incident: TRUE
→ reasoning: "Article focuses on analyzing the specific Qantas 2022 breach, even though it's an opinion piece about implications"

Example 2:
Title: "Third-Party Privacy Risks in the Cloud Era"
Content: General discussion of privacy risks, mentions Qantas breach as one example among many
→ is_specific_incident: FALSE
→ reasoning: "General discussion of privacy risks; Qantas mentioned only as an example, not the primary focus"

Example 3:
Title: "Weekly Cybersecurity Roundup - March 2025"
Content: Covers 10 different breaches across various companies
→ is_specific_incident: FALSE
→ reasoning: "Aggregate roundup covering multiple separate incidents without focusing on one"

Example 4:
Title: "Australia's TPG Telecom flags cyber incident in its iiNet system"
Content: Reports on iiNet breach, includes company statement, customer impact, investigation status
→ is_specific_incident: TRUE
→ reasoning: "Reports on a specific iiNet cyber incident with concrete details about the attack"

Example 5:
Title: "How Supply Chain Attacks Work"
Content: Educational content explaining supply chain attacks, uses generic examples
→ is_specific_incident: FALSE
→ reasoning: "Educational content without describing a specific real-world incident"

EXTRACT:
- is_specific_incident: Boolean
- specificity_reasoning: String (cite which details from the article confirm this is/isn't a specific incident)
```

## Why This Is Better

1. **Clearer definition**: "Does this describe a concrete incident?" vs "Is this a primary source?"
2. **Explicit examples** showing analysis/commentary CAN be specific
3. **Decision rule**: 3 simple questions to determine specificity
4. **Edge case handling**: "Lessons from X breach" → specific, "Lessons about cybersecurity" → not specific

## Implementation

Replace lines 304-318 in `gpt4o_enricher.py` with the above text.
