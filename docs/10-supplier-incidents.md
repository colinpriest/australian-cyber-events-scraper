# Supplier incidents: every affected organisation, one incident

A supplier or third-party breach is **one incident with several co-equal
victims**: ProctorU (2020) exposed students of ten Australian universities,
Frontier Software's payroll breach exposed South Australian public servants,
the Canvas/Instructure breach reached universities, TAFEs and state education
departments, HWL Ebsworth's ransomware exposed dozens of government clients.
The schema has always allowed several `victim` roles on one event
(`DeduplicatedEventEntities.relationship_type`), but nothing looked for the
other victims, and the same supplier incident was often stored as separate
events, one per customer that reported it.

`python scripts/dedup_admin.py find-supplier-victims [--research] [--apply]`
fixes both, in `cyber_data_collector/dedup/supplier_victims.py`.

## 1. Detection

An active event is a *supplier incident* when any of these hold:

* it has a vendor - `DeduplicatedEvents.vendor_organization_name` or a
  `vendor` role (descriptions such as "third-party IT provider" are not
  vendors);
* its own title, description or summary describes a third-party, supplier,
  vendor, service-provider, contractor, outsourcing or supply-chain compromise;
* one or more of its member records' titles or descriptions do.

Detection is recall-oriented: it only decides which events get candidates
gathered and judged. Only the first two (`searchable`) get a supplier search.

## 2. Candidates

**Victim candidates** - organisations that may have been affected and are not
yet recorded victims (or variants of them, or the supplier):

* organisations the event's records link as `victim`, `affected_customer` or
  `primary_target`. Not `affected`: extraction gives every unclassified link
  that role, so it names journalists, regulators and dates as well;
* the organisation Perplexity's research named on each member record
  (`EnrichedEvents.perplexity_enrichment_data.formal_entity_name`);
* with `--research`, the organisations named by one Perplexity search per
  searchable incident: *organisations - especially Australian ones - whose
  data or systems were affected by this incident, with how and a source*. A
  live search cannot be replayed, so the query, answer, citations and model are
  stored in `SupplierIncidentSearches` and reused (a new search is made only
  when the query changes - a changed title or vendor - or the model does).

Victims are organisations: names whose recorded `entity_kind` is product,
person, threat actor or other are never candidates, nor are collectives
("Australian universities"), sector labels ("Queensland education sector"),
regulators and descriptive labels. A named candidate is resolved to an existing
entity by canonical key or the strict same-organisation test below; a new
entity is created only when an accepted search name matches none.

**Event candidates** - other active events that may be the same supplier
incident recorded under another customer, within 365 days:

* a shared non-hub article or a corpus-rare subject word within 60 days (the
  multi-key candidate generation of `dedup/candidates.py`);
* an event naming the same supplier (vendor role, victim, affected customer or
  vendor field);
* a victim candidate's own event.

Pairs that share a victim are left to `check-duplicates`.

## 3. Judgement

Two Jev Noul questions, answered over stored evidence and stored in
`ModelJudgements` (reused unless the evidence, question or model changes):

| Question | Subject | Version | Evidence |
|---|---|---|---|
| Is the candidate an organisation whose data, systems or customers were affected by THIS incident? | `event_organisation`, `<event>|<entity id or name key>` | `supplier-victim-v1` | the incident's records (page text + Perplexity research), corroboration search, supplier search, the candidate and where it was found |
| Is the other event the same supplier incident? | `event_pair` | `same-supplier-incident-v1` | both events' records and corroboration, gap in days, why they were paired |

The victim question tells Jev that a regulator, responder, outlet, product,
threat actor, the supplier itself, an organisation hit by a different incident,
and **another name for a recorded victim** are not additional victims.

Scores in the band `[0.2, 0.8)` - fixed before any labelling - are re-judged
by the reasoning model (`utils/escalation.py`, `OPENAI_MODEL`, reasoning
effort high). If an escalation fails the item is **undecided**: it is reported
and never acted on, and Jev's raw score is not used in its place.

## 4. Calibration

`scripts/calibrate_supplier_victims.py`:

1. `sample` - per question, a seeded sample of the current candidates
   stratified on **Jev's** stored score (low < 0.2, mid 0.2-0.8, high >= 0.8),
   which exists before any escalation, so the sample cannot depend on the
   final score being calibrated; each stratum is weighted back to its band,
   in `evaluation/supplier_victim_sample.csv`.
2. `packet --out DIR` - the evidence each sampled judgement saw, **with no
   score**, shuffled so the order does not reveal the stratum.
3. Labels in `evaluation/supplier_victim_labels.csv`
   (`kind,subject_id,label,rationale,labeller`). Victim labels: `affected`,
   `not_affected`, `already_recorded` (a recorded victim under another name -
   attaching it would count the organisation twice), `unsure`. Pair labels:
   `same`, `different`, `unsure`.
4. `calibrate` - per question, the lowest final score at which the items it
   would act on are, weighted, at least 95% labelled positive with at least 5
   labelled acceptances; a stratified bootstrap of the whole
   choose-then-test procedure is the out-of-sample precision. Written to
   `evaluation/supplier_victim_calibration.json` with the question versions
   and models. A question with no cut-off reaching 95% stays report-only.
5. `extend --kind K --stratum S --add N --round R --seed X [--judge]` - when a
   cut-off rests on too few labelled actions for the deployment rule, adds up
   to N items to one stratum: a seeded random draw from the current
   candidates that Jev's own score (on their current evidence) puts in the
   stratum and that are not sampled yet (`--judge` asks Jev for candidates
   with no such score; without it the step refuses, since placing only the
   scored ones would bias the draw). Old and new items of the stratum are one
   random sample of it and all weigh `population / items` (population: the
   stratum's size at sampling, or more if it now holds more); other strata
   are unchanged. The new items are judged in full (final scores stored) and
   written with `round` R; `packet --round R` is the blind packet of those
   items only.

`--apply` reads the labels and the calibration first and **refuses (exit 2)
and says why** when the labels are missing, unreadable or malformed, or the
calibration is missing or was measured on other models or question versions.
Labels are ground truth: a labelled `not_affected` / `already_recorded` /
`different` item is never acted on; a labelled `affected` / `same` item always
is.

### Labelling (2026-10-27)

187 items (115 victim, 72 same-incident) were labelled by five Claude Opus
5.5 subagents from the blind packet plus web searches, following a written
guide. Conventions worth reviewing:

* **Parent, brand or unit of a recorded victim** (Thrifty for Hertz, iiNet for
  TPG, a Partnered Health clinic, Libraries Tasmania for DECYP) is
  `already_recorded`: counting it would count one organisation twice.
* **The event's own scalar victim with no victim role** (`recorded_victims`
  empty) is `affected`: attaching it adds the missing role. Labellers split on
  this; it was harmonised when the files were merged (2 labels changed, noted
  in their rationales), from the packet alone.
* **Mass exploitation of one product** (Accellion FTA 2020-21, MOVEit 2023,
  Cleo 2024) was labelled one supplier incident (`same` / `affected`);
  Exchange ProxyLogon was split between labellers. This is the convention
  most worth reviewing: it merges customers who each ran their own appliance.
* **Knock-on disruption** (JLR's suppliers idled by its shutdown) is
  `not_affected`; disruption of an organisation's own supplier-hosted systems
  (Evolution Mining's ERP) is `affected`.
* Claimed but unconfirmed victims (IntelBroker's Cisco list, banks that "were
  checking" in HWL Ebsworth) are `unsure` and excluded.

### Calibration (2026-10-27, `evaluation/supplier_victim_calibration.json`)

Measured on a copy of the database taken 2026-10-27 (`work.db`), Jev
`jev-1.14.0` + `gpt-5.5-2026-04-23`; 443 victim and 213 pair candidates, of
which 145 and 21 were escalated (none failed).

| Question | Labelled used (positive) | Cut-off | Accepted at cut-off | Weighted precision | Weighted recall | Bootstrap out-of-sample precision (mean / p05) |
|---|---|---|---|---|---|---|
| victim | 103 (62) + 12 unsure | **0.96** | 7, all right | 1.00 | 0.11 | 0.90 / 0.73 |
| same incident | 71 (38) + 1 unsure | **0.87** | 31, 30 right | 0.97 | 0.79 | 0.94 / 0.80 |

The victim question is weak: weighted precision sits at 0.85-0.91 for every
cut-off from 0.80 to 0.95, so only the top scores clear 95% and the bootstrap
says even that is fragile. Its high-scoring errors are mostly the
`already_recorded` convention - brands and units of a recorded victim (iiNet
for TPG, Partnered Health's clinics, Thrifty for Hertz, ImLive's sister
sites) - which the evidence shown to Jev does not reveal, plus unnamed
descriptions ("Australian airport") and adjacent incidents (OpenAI's June
Medicare-portal access beside its July incident). Most attached victims
therefore come from the labels themselves (ground truth), not the model; a
better question (telling Jev about parent/brand relations) needs new labels to
measure, because these labels have now been seen.

The same-incident cut-off is solid; its one labelled error above the cut-off is
a contaminated event (the NSW Department of Education's July 2021 attack
holding NSW Health Accellion records).

### Recalibration on current evidence (2026-10-29)

`dedup/calibration_guard.py` makes a calibration apply only to the evidence
processing it was measured on, so the 2026-10-27 file (no evidence fingerprint)
stopped applying and `find-supplier-victims --apply` refused. The step is now
recalibrated by `dedup_admin.py recalibrate --step supplier-victims` (or
`--if-stale`, which the refresh runs): it copies the labelling-time snapshot
named in `evaluation/calibration_snapshots.json`
(`instance/calibration_supplier_victims.db`, the 2026-10-27 23:21 backup, which
holds every labelled event and pair), carries the live database's stored
`SupplierIncidentSearches` and timelines in, re-scores the labelled victim
candidates and pairs with today's readers (Jev, the middle band escalated; answers are
keyed on a hash of the evidence, so a repeat run buys nothing), and runs
`calibrate_supplier_victims.py calibrate`, which records the evidence
fingerprint.

Coverage is partial and stated. The working copy re-scored **70 of 103**
labelled victim candidates and **69 of 71** labelled pairs (139 of 174; 139
Jev calls and 37 GPT-5.5 escalations). The 33 missing victims were named only by
the supplier searches of 2026-10-27, which were not stored (the working database
`work.db` is gone) and cannot be replayed; the searches stored on 2026-10-28 name
others. The step fails below 65% coverage. The labels are the same ones, now
measured on today's scores:

| Question | Labelled used (positive) | Cut-off | Accepted at cut-off | Weighted precision | Weighted recall | Bootstrap out-of-sample precision (mean / p05) |
|---|---|---|---|---|---|---|
| victim | 70 (42) | **0.87** (was 0.96) | 26, 25 right | 0.96 | 0.58 (was 0.11) | 0.94 / 0.76 |
| same incident, merge | 69 (36) | **0.89** (was 0.87) | 27, all right | 1.00 | 0.75 | 0.95 / 0.79 |
| same incident, "different" below | 69 (36) | **0.09** (was 0.78) | 19, all right | 1.00 | 0.92 | 0.99 / 0.97 |

The victim bootstrap p05 of 0.76 says that cut-off is fragile; the "different"
bound is low because two labelled-same pairs now score 0.11 and 0.19 (the bound
stops below every labelled-same pair), so a model-only "different" is rare and
most unlabelled pairs between 0.09 and 0.89 are held.

**The bound for "different" (added 2026-10-29).** The merge cut-off measured only
the precision of merging, so a score just under it is not evidence that two
events are different incidents (the labelled pairs between 0.78 and 0.87 are 3
the same incident, 3 different). `calibrate` therefore also records
`same_incident.different_cutoff`: the largest score below which, weighted,
at least 95% of the labelled pairs are labelled `different`, with at least 5 of
them **and none labelled `same`** (a label is never acted on the other way; the
weighted precision alone ran to 0.91, past the merge cut-off, because the many
low-scoring pairs outweigh the few near it). Measured first on the 2026-10-27
scores: **0.78** (27 of 27 labelled pairs below it are
different, weighted recall 0.98, bootstrap out-of-sample precision mean 0.995 /
p05 0.979); the 2026-10-29 recalibration above replaced it. A model score between the two bounds, or any model score when a
calibration has no `different_cutoff`, leaves the pair unresolved. `python
scripts/calibrate_supplier_victims.py calibrate --different-only` adds the field
to an existing calibration, after checking the stored judgements still
reproduce its merge cut-off.

### More labels for the deployment rule (2026-10-30)

The deployment rule (`dedup/deployment.py`) lets an action run only when the
exact one-sided 95% lower bound on its precision is at least 0.80 on all labels
and on 5-fold held-out labels, with at least 15 labelled actions. Re-scored on
current evidence (FinalDecisions; a copy of the labelling snapshot with the live
stored searches, timelines and supplier answers carried in, 134 Jev calls and
39 escalations), the 2026-10-27 labels gave usable scores for 73 of 103 victim
candidates and 69 of 71 pairs. Every action then failed on the held-out bound
alone: victims 22/24 held out (0.76), merges 30/33 (0.78), "different" 24/26
(0.78).

`extend` added 59 items (round 2, seed 20261030), every current candidate first
scored by Jev on its current evidence (about 540 Jev calls): victim high +30 of
125 eligible (weight 2.83 -> 2.06), same-incident high +4 and mid +5 (both
strata are now a census), same-incident low +20 of 150 (weight 10.4 -> 4.71).
They were labelled blind (same packet format and conventions, 2026-10-30): 27
affected, 1 already recorded, 1 not affected, 1 unsure; 6 same, 22 different, 1
unsure. Calibrated on the copy (`instance/recalibration/supplier-victims.db`),
no further calls:

| Question | Labelled used (positive) | Cut-off | Labelled at cut-off (lower bound) | Held out (lower bound) | Weighted recall | Bootstrap (mean / p05) |
|---|---|---|---|---|---|---|
| victim | 102 (71) | **0.85** | 52 of 54 right (0.888) | 53 of 56 (0.854) | 0.73 | 0.94 / 0.81 |
| same incident, merge | 97 (42) | **0.87** | 33 of 34 (0.868) | 34 of 37 (0.804) | 0.79 | 0.95 / 0.81 |
| same incident, "different" below | 97 (42) | **0.09** | 44 of 44 (0.925 at n_eff 38.5) | 46 of 49 (0.849) | 0.94 | 0.99 / 0.96 |

All three pass the rule and act. The merge action passes by the smallest
margin (held-out 0.804): its candidate population is exhausted (every high and
mid pair is labelled), so one more wrong merge in a later recalibration would
switch it off until new pairs exist to label.

## 5. Applying

In order:

1. **Merge** each accepted event candidate into the supplier incident (the
   supplier incident with most records survives) through
   `DedupLedger.merge_events`, which refuses across a cached `different` ruling
   - refusals are reported. The merged event's victims are carried to the
   surviving event (except the supplier itself), each with an audit row.
   Merges change membership, so roles are refreshed (`DeferredRoleRefresher`)
   before step 2.
2. **Attach** each accepted victim candidate as a `victim` role on the
   incident: `DeduplicatedEventEntities` (inserted or re-typed) and the member
   records' `EnrichedEventEntities` links. The scalar
   `victim_organization_name` is not changed and no victim is demoted: the
   victims are co-equal.
   A candidate that is **another name for a victim the event already has**
   (`victim_counting.same_within_event`: "Australian Red Cross Society" beside
   "Australian Red Cross", "Block, Inc. (Cash App Investing)" beside "Block
   (ASX:SQ2)") is reported as a variant and not attached; carried victims
   are checked the same way, and descriptions ("Sydney radiologist") are not
   carried.
   A victim that still has **its own active event within 365 days** of the
   incident (or an undated one) would be counted twice if that event were the
   same incident, so the pair is *ruled on* first (`rule_on_own_event`), in
   order of authority:

   1. a **pair label** (`evaluation/supplier_victim_labels.csv`, kind
      `same_incident`; `unsure` is no ruling) and an active `DedupOverrides`
      ruling between any member records of the two events; a label and an
      override that disagree leave the pair **unresolved**;
   2. the calibrated **same-incident judgement** of the pair: at or above the
      merge cut-off the same incident, below the calibrated `different_cutoff`
      a different one, and in between neither (held, the band named).

   *Same incident:* the own event is merged into the supplier incident in step 1
   (also when only a label or override says so, and the pair was not a model
   candidate), and the ledger carries the customer across as a victim.
   *Different incident:* the customer is attached as a victim of the supplier
   incident; its own event stays a separate incident, and the findings'
   `attached[].own_events_ruled_different` give the event, the ground and the
   judgement. *Unresolved* (no stored or fresh judgement of the pair - undated,
   over 365 days apart, or sharing a victim with the incident, so
   `check-duplicates` decides - a failed or un-escalated judgement, no
   cut-off, conflicting rulings, or a ruled-same merge the ledger refused): the
   victim is **held**, reported in `held[]` with `why`, and decided on a later
   run once something rules on the pair. A `different` ruling cached as an
   override makes the ledger refuse the merge, so it reads as different here.

Every attachment is recorded in `SupplierVictimAttachments` (event, entity,
previous role, decided by `jev` / `escalation` / `label` / `merge`,
probability, cut-off, question version, merged-from event, snapshot batch)
after a ledger snapshot. The command is idempotent: a second run finds the
victims already recorded and the merged events gone. Findings are written to
`instance/supplier_victim_findings.json`.

Failures are loud: a missing `TYPESAFE_API_KEY` or `OPENAI_API_KEY` (or
`PERPLEXITY_API_KEY` with `--research`) refuses to run; a failed Jev call or
search is reported and the command exits 1.

## 6. Counting once per organisation

The repeat-timing analysis (`scripts/analyze_recurrent_timing.py`) and the
by-entity dashboard (`scripts/build_entity_dashboard.py`) attribute events by
`victim` role, so a multi-victim incident counts once for each victim
organisation. They now also count it **never twice for one organisation**:
`dedup/victim_counting.py` joins victim entities that are one organisation
under two names and keys every row on one representative entity (the variant
with most victim roles), whose size and kind the rows then carry; duplicate
(organisation, event) rows are dropped. A variant pair on one event no longer
lists the incident twice, and a variant pair across two events is now one
organisation with a repeat rather than two single-event organisations.

Within one event the test is looser (`same_within_event`): inside one
incident, two victims whose identifying words are a subset of one another
("Australian Red Cross" / "Australian Red Cross Society") are one
organisation unless the extra words name a place, jurisdiction or another line
of business, and only the variant with most victim roles is counted.

Across events the test is strict, because a false join loses a victim: the same canonical
key or learned alias; the same words apart from "the" and legal forms; the
canonicaliser's structural test; or `EntityResolver.same_organisation` **and**
an identifying word left after place, portfolio and institution words are
removed, leading the longer name, with no line-of-business or jurisdiction
difference. Groups use complete linkage, so "Federal Group" cannot chain
"Federal Court" to the Australian Federal Police. On the 575 victim entities
of 2026-10-27 the resolver alone produced 18 joins, 9 of them wrong (Service
NSW / NSW Government, Football Australia / Fremantle Football Club, Sharp Motor
Group / Sharp Office Systems ...); the strict test makes 8, all right
(Medibank / Medibank Private Limited, Meta / Meta Platforms, Inc., BlueScope /
BlueScope Steel, ANU / The ANU, Bunnings, JBS, Novati, youX).

**Acronyms are candidates, not proof** (`victim_counting.AcronymPolicy`). "AP"
is the initialism of Advance Press and of Associated Press, so the structural
test that joins "FRV" to "Fire Rescue Victoria" cannot also join it to both.
A name and an acronym that abbreviates it are one organisation only when

1. a confirmed global alias (`EntityAliases` reviewed or model-confirmed, or
   `EXPLICIT_ALIASES`) says so - then it is the organisation the alias names,
   and no other it happens to fit; or
2. inside one event, that event's own stored resolution
   (`EntityMentionResolutions`, link table `DeduplicatedEventEntities`, with
   an organisation named) resolved the acronym to it; or
3. the acronym fits exactly one organisation among the stored entity names and
   is not registered as a contextual alias - the case that needs no model.

An acronym that fits several organisations, or is registered in
`ContextualEntityAliases`, and has neither 1 nor 2 stays unresolved and
distinct, so ten co-equal universities that include "UC" do not collapse into
the University of Canberra or of California. A resolver without a policy
accepts no acronym. On the 2026-10-29 copy no victim count or recurrence repeat
changed. Regression tests: `tests/test_victim_acronyms.py`.

## Refresh position

After `check-duplicates` (same-victim merges) and before
`check-victim-attribution`:

```
classify-pages, check-sources, check-events, classify-entities --stale-only,
check-victims, check-duplicates, find-supplier-victims --research --apply,
check-victim-attribution
```

`check-victims` deliberately skips events with several victims, so it never
undoes co-victims attached here.
