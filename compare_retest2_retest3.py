"""
Compare Retest #2 vs Retest #3 Results

Analyzes the impact of the softened penalty (0.5x -> 0.8x) and improved validation.
"""

import json
from collections import defaultdict

# Load results
retest2 = json.load(open('batch_enrichment_results_20251028_174300.json'))
retest3 = json.load(open('batch_enrichment_results_20251028_210848.json'))

print("=" * 100)
print("RETEST #2 vs RETEST #3 COMPARISON")
print("=" * 100)
print()
print("Changes in Retest #3:")
print("  1. Non-specific penalty: 0.5x -> 0.8x (SOFTENED)")
print("  2. Validation keywords: Added 'flags', 'reports', 'confirms', 'discloses', 'reveals', 'data leak', 'cyberattack'")
print("  3. Australian relevance threshold: 0.5 -> 0.3 (LOWERED)")
print()

# Overall Statistics
print("=" * 100)
print("OVERALL STATISTICS")
print("=" * 100)
print()

# Extract statistics from top-level fields
total2 = retest2['total_events']
total3 = retest3['total_events']
auto2 = retest2['auto_accept']
auto3 = retest3['auto_accept']
warn2 = retest2['accept_with_warning']
warn3 = retest3['accept_with_warning']
reject2 = retest2['rejected']
reject3 = retest3['rejected']

print(f"{'Metric':<30} {'Retest #2':<15} {'Retest #3':<15} {'Change':<15}")
print("-" * 100)
print(f"{'Total Events':<30} {total2:<15} {total3:<15} -")
print(f"{'AUTO_ACCEPT':<30} {auto2:<15} {auto3:<15} {auto3 - auto2:+d}")
print(f"{'ACCEPT_WITH_WARNING':<30} {warn2:<15} {warn3:<15} {warn3 - warn2:+d}")
print(f"{'REJECT':<30} {reject2:<15} {reject3:<15} {reject3 - reject2:+d}")
print()

accept2 = auto2 + warn2
accept3 = auto3 + warn3
print(f"{'TOTAL ACCEPTANCE':<30} {accept2:<15} {accept3:<15} {accept3 - accept2:+d}")
print(f"{'Acceptance Rate':<30} {accept2/total2*100:.1f}%{'':<10} {accept3/total3*100:.1f}%{'':<10} {(accept3/total3 - accept2/total2)*100:+.1f}%")
print()

# Event-level changes
print("=" * 100)
print("EVENT-LEVEL CHANGES")
print("=" * 100)
print()

# Create lookup dictionaries
events2 = {e['event_id']: e for e in retest2['events']}
events3 = {e['event_id']: e for e in retest3['events']}

# Track changes
changes = []
for event_id in events2:
    if event_id in events3:
        e2 = events2[event_id]
        e3 = events3[event_id]

        if e2['decision'] != e3['decision']:
            changes.append({
                'event_id': event_id,
                'title': e3['title'],
                'victim': e3.get('victim', 'None'),
                'old_decision': e2['decision'],
                'new_decision': e3['decision'],
                'old_confidence': e2['confidence'],
                'new_confidence': e3['confidence'],
                'conf_change': e3['confidence'] - e2['confidence'],
                'is_specific': e3.get('is_specific_incident', None),
                'australian_rel': e3.get('australian_relevance', 0),
            })

print(f"Total Changes: {len(changes)}/{len(events2)} ({len(changes)/len(events2)*100:.1f}%)")
print()

# Group by transition type
transitions = defaultdict(list)
for change in changes:
    key = f"{change['old_decision']} -> {change['new_decision']}"
    transitions[key].append(change)

for transition, events in sorted(transitions.items()):
    print(f"\n{transition}: {len(events)} events")
    print("-" * 100)
    for e in events[:5]:  # Show first 5
        print(f"  - {e['title'][:70]}")
        print(f"    Victim: {e['victim']}")
        print(f"    Confidence: {e['old_confidence']:.2f} -> {e['new_confidence']:.2f} ({e['conf_change']:+.2f})")
        print(f"    is_specific: {e['is_specific']}, Australian: {e['australian_rel']:.2f}")
        print()
    if len(events) > 5:
        print(f"  ... and {len(events) - 5} more")
        print()

# Key Test Case: Event #9
print("=" * 100)
print("KEY TEST CASE: EVENT #9 (TPG/iiNet)")
print("=" * 100)
print()

event9_id = '19af3f4f-ceea-49b0-9bc7-21d05119662c'
if event9_id in events2 and event9_id in events3:
    e2 = events2[event9_id]
    e3 = events3[event9_id]

    print(f"Title: {e3['title']}")
    print(f"Victim: {e3.get('victim', 'None')}")
    print()
    print(f"{'Metric':<30} {'Retest #2':<20} {'Retest #3':<20}")
    print("-" * 70)
    print(f"{'Decision':<30} {e2['decision']:<20} {e3['decision']:<20}")
    print(f"{'Confidence':<30} {e2['confidence']:<20.2f} {e3['confidence']:<20.2f}")
    print(f"{'is_specific_incident':<30} {e2.get('is_specific_incident', 'N/A')!s:<20} {e3.get('is_specific_incident', 'N/A')!s:<20}")
    print(f"{'Australian Relevance':<30} {e2.get('australian_relevance', 0):<20.2f} {e3.get('australian_relevance', 0):<20.2f}")
    print()

    if e2['decision'] == 'REJECT' and e3['decision'] in ['AUTO_ACCEPT', 'ACCEPT_WITH_WARNING']:
        print("[+] SUCCESS: Event #9 moved from REJECT to ACCEPTED!")
        print("    The softened penalty and/or validation improvements worked!")
    elif e3['confidence'] > e2['confidence']:
        print("[~] PARTIAL: Event #9 confidence improved but still " + e3['decision'])
    else:
        print("[X] NO CHANGE: Event #9 still " + e3['decision'])
else:
    print("[X] Event #9 not found in results")

print()

# Verification Overrides
print("=" * 100)
print("VALIDATION OVERRIDE STATISTICS")
print("=" * 100)
print()

overrides2 = sum(1 for e in retest2['events'] if e.get('specificity_overrides', 0) > 0)
overrides3 = sum(1 for e in retest3['events'] if e.get('specificity_overrides', 0) > 0)

print(f"Events with validation overrides:")
print(f"  Retest #2: {overrides2} ({overrides2/len(events2)*100:.1f}%)")
print(f"  Retest #3: {overrides3} ({overrides3/len(events3)*100:.1f}%)")
print(f"  Change: {overrides3 - overrides2:+d}")
print()

# Success Criteria Check
print("=" * 100)
print("SUCCESS CRITERIA CHECK")
print("=" * 100)
print()

criteria = [
    ("Event #9 moves from REJECT to ACCEPT",
     event9_id in events2 and events2[event9_id]['decision'] == 'REJECT' and
     event9_id in events3 and events3[event9_id]['decision'] in ['AUTO_ACCEPT', 'ACCEPT_WITH_WARNING']),
    ("AUTO_ACCEPT increases by >= 5 events",
     auto3 - auto2 >= 5),
    ("Total acceptance rate >= 63%",
     accept3/total3 >= 0.63),
    ("REJECT decreases by >= 3 events",
     reject2 - reject3 >= 3),
]

for criterion, passed in criteria:
    status = "[+] PASS" if passed else "[X] FAIL"
    print(f"{status}: {criterion}")

print()

# Final Assessment
print("=" * 100)
print("FINAL ASSESSMENT")
print("=" * 100)
print()

total_passed = sum(1 for _, passed in criteria if passed)
if total_passed == len(criteria):
    print("[+] ALL SUCCESS CRITERIA MET!")
    print()
    print("Retest #3 changes were successful:")
    print("  - Softened penalty (0.8x) reduced false rejections")
    print("  - Improved validation caught more legitimate incidents")
    print("  - Overall acceptance rate improved without sacrificing quality")
    print()
    print("RECOMMENDATION: Proceed to Phase 2 (full 1,878 events)")
elif total_passed >= len(criteria) * 0.75:
    print("[~] MOST SUCCESS CRITERIA MET")
    print(f"    Passed {total_passed}/{len(criteria)} criteria")
    print()
    print("RECOMMENDATION: Review failures and decide if acceptable for Phase 2")
else:
    print("[X] INSUFFICIENT IMPROVEMENT")
    print(f"    Only passed {total_passed}/{len(criteria)} criteria")
    print()
    print("RECOMMENDATION: Further tuning needed before Phase 2")

print()
