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

**The bound for "different" (added 2026-10-29).** The merge cut-off measured only
the precision of merging, so a score just under it is not evidence that two
events are different incidents (the labelled pairs between 0.78 and 0.87 are 3
the same incident, 3 different). `calibrate` therefore also records
`same_incident.different_cutoff`: the largest score below which, weighted,
at least 95% of the labelled pairs are labelled `different`, with at least 5 of
them **and none labelled `same`** (a label is never acted on the other way; the
weighted precision alone ran to 0.91, past the merge cut-off, because the many
low-scoring pairs outweigh the few near it). Measured on the same stored
judgements and labels: **0.78** (27 of 27 labelled pairs below it are
different, weighted recall 0.98, bootstrap out-of-sample precision mean 0.995 /
p05 0.979). A model score between 0.78 and 0.87, or any model score when a
calibration has no `different_cutoff`, leaves the pair unresolved. `python
scripts/calibrate_supplier_victims.py calibrate --different-only` adds the field
to an existing calibration, after checking the stored judgements still
reproduce its merge cut-off.

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
