# Deduplication v3 — design proposal and implementation

Status: implemented and applied to `instance/cyber_events.db` (August 2026).
Supersedes the matching logic described in [06-deduplication.md](06-deduplication.md).

---

## 1. Why v2 needed replacing

The v2 pipeline grew incrementally and matched events with normalised string
comparison plus a similarity threshold. Auditing the August 2026 refresh
surfaced four structural problems:

| Problem | Evidence found in the live database |
|---|---|
| **Provenance discarded** | `DeduplicatedEventSources` had **0 rows**; `total_data_sources` was **0 on all 1,034 events**, despite being documented as "count of merged sources". |
| **Lineage incomplete** | Only **384 of 1,034** events had any `EventDeduplicationMap` row. `_store_unique_events` never recorded the master, and `_store_merge_groups` returned early for single-event groups. |
| **Under-merging** | One incident stored many times: **8 Qantas**, 6 Optus, 6 MediSecure events. 64 entity stems had multiple legal-name variants ("Optus Pty Limited" / "Singtel Optus Pty Limited"). |
| **No explanation or undo** | A merge recorded a similarity float and nothing else. Corrections were impossible without a full rebuild — and a rebuild regenerated every `deduplicated_event_id`, destroying anything keyed to it. |

The last row is the root cause of the first two. **Dedup ids were not stable**,
so all provenance attached to them was lost on every rebuild.

---

## 2. Design principles

1. **Recall in cheap layers, precision in expensive ones.** Never let a
   cheap heuristic make a final merge decision.
2. **No decision without evidence.** A merge is only valid if a human can
   independently check the reasoning.
3. **Stable keys.** Anything meant to survive a rebuild is keyed on
   `enriched_event_id`, never on `deduplicated_event_id`.
4. **Append-only history.** Decisions are superseded, never overwritten, so
   any state is reconstructable and reversible.
5. **Corrections are training data.** A human fixing a mistake must make the
   same mistake less likely next time.

---

## 3. Pipeline

```
enriched events
      |
      v
[1] entity blocking          free      recall-oriented candidate buckets
      |
      v
[2] embeddings               ~$0.02    semantic ranking; discard unrelated
      |
      v
[3] short-circuits           free      human override / shared URL / entity mismatch
      |
      v
[4] LLM adjudication         ~$0.01/pr GPT-4o verdict + certainty + reasoning
      |
      v
[5] ledger                   free      append-only decision, snapshot, apply
```

On the live corpus this reduced 1,034 events from **534,061 possible pairs to
683 candidates** (blocking), of which **260 were rejected for free** and 412
reached the LLM.

### 3.1 Entity blocking (`entity_resolution.py`)

Identity is deliberately **not** decided here. An earlier draft tried to force
variants together by stripping corporate parents and "noise" words; it reduced
`Nissan Motor Co. (Australia) Pty Ltd` to the key `co`, which would have merged
every unrelated company ending in "Co Pty Ltd". That draft was discarded.

What remains is narrow and safe:

- `canonical_key` — case, punctuation and legal-suffix normalisation only. It
  never reduces a name to a generic fragment.
- `blocking_keys` — deliberately over-generates keys (full key, significant
  tokens, first-two tokens, acronym) so variants collide and get *considered*.
- `fit(names)` — measures token document-frequency across the corpus and only
  allows single-token blocking on tokens rarer than 0.5% of entities. This is
  what stops `university` or `department` bucketing hundreds of unrelated
  organisations, **without a hand-maintained stopword list**.

Measured on the real 1,034-name corpus: 7/7 known variant groups pair up, 8/8
distinct-organisation pairs stay apart, and the largest block holds 4 members.

### 3.2 Embeddings (`adjudicator.py`)

`text-embedding-3-small` over title + organisation + summary. This is what v2
structurally could not do: recognise that *"Qantas contact centre hit by
significant cyber incident"* and *"6 million Qantas customer records exposed"*
are one event. Pairs below 0.55 cosine are dropped; above 0.93 with an entity
match and a close date, the merge is taken without an LLM call.

### 3.3 Short-circuits

Checked before any spend, in order:

1. **Active human override** → decides, certainty 1.0.
2. **Identical source URL** → same event, certainty 0.99.
3. **Entity keys don't match** → separate, no LLM call.

### 3.4 LLM adjudication

GPT-4o with an `instructor`-validated schema returning `is_same_event`,
`certainty`, `reasoning`, `supporting_facts` and `distinguishing_facts`. The
prompt states the rule that broke v2: *two different incidents at the same
organisation are not the same event; follow-up coverage and regulatory action
about one incident are*.

Failures **default to keeping events separate** — a wrong split is visible and
repairable, a wrong merge silently destroys a distinct incident.

### 3.5 Ledger (`ledger.py`)

`DedupDecisions` is append-only. Every row carries who decided, how certain,
by what method, the reasoning, and a JSON evidence blob. `DedupSnapshots`
captures a dedup row plus its membership before any mutation, so
`restore_snapshot` returns the previous state exactly.

---

## 4. Reversibility

Both directions work against stored state only — **no re-scraping, no
re-enrichment, no pipeline rerun**:

```bash
# detach one wrongly merged member into its own event
python scripts/dedup_admin.py split <dedup_id> <enriched_id> --reason "different incident"

# fold one event into another
python scripts/dedup_admin.py override <enr_a> <enr_b> same --reason "same incident"
```

`split_member` snapshots, creates a new dedup row, repoints the membership,
supersedes the affected decisions and recomputes `total_data_sources`.

---

## 5. Learning from overrides

`dedup_admin.py learn` folds corrections back in two inspectable ways:

1. **Entity aliases.** When a reviewer rules that two *differently named*
   records are the same incident and blocking would never have paired them, the
   names are bound in `EntityAliases`. Future runs bucket them automatically,
   so that class of miss does not recur.
2. **Threshold calibration.** Overrides are graded against what the pipeline
   decided. More "you wrongly merged" rulings raise the merge threshold; more
   "you missed one" rulings lower it. Each adjustment is written to
   `DedupCalibration` with its sample size, so it can be audited or rolled back.

Overrides are keyed on `pair_key(enriched_a, enriched_b)` — stable ids — so
they survive a full rebuild. This is directly tested
(`test_override_survives_a_dedup_rebuild`).

---

## 6. Visibility

`python scripts/build_dedup_dashboard.py` → `dashboard/dedup.html`, a
self-contained page showing per event:

- **Ancestry** — every source record folded in, its role (master/merged), date,
  organisation and outbound source link.
- **Why** — each decision with decider, certainty, reasoning and the evidence
  (canonical entity keys, date gap, embedding similarity, supporting and
  distinguishing facts).
- **Correction** — copy-paste `dedup_admin.py` commands to override, split, or
  re-learn.

Filters surface exactly the things worth reviewing: *merged but unexplained*,
*certainty below 0.85*, *no lineage*, *human-corrected*.

Changes route through the CLI rather than direct DB writes, so the page stays
shareable and every change still lands in the audited ledger.

---

## 7. Results on the live database

| Metric | Before | After |
|---|---|---|
| Events with lineage | 384 / 1,034 | **1,034 / 1,034** |
| `DeduplicatedEventSources` rows | 0 | **2,606** |
| Source records linked (members) | 1,814 | **2,788** |
| Events reporting 0 sources | 1,034 | **0** |
| Qantas events for one incident | 8 | **3** |
| Active deduplicated events | 1,034 | 921 |
| Audited decisions | 0 | 739 |
| Restorable snapshots | 0 | 226 |

113 merges applied at certainty ≥ 0.90; 27 findings in the 0.80–0.89 band were
deliberately left for human review in the dashboard. Integrity checks: 12/12.

---

## 8. Operating it

```bash
python scripts/dedup_admin.py migrate                      # idempotent schema
python scripts/dedup_admin.py backfill --dry-run           # repair lineage/provenance
python scripts/dedup_admin.py find-missed --no-llm         # free candidate preview
python scripts/dedup_admin.py find-missed                  # adjudicate
python scripts/dedup_admin.py apply-missed --min-certainty 0.9
python scripts/dedup_admin.py learn                        # fold in corrections
python scripts/build_dedup_dashboard.py
```

---

## 8a. Legacy review (August 2026)

The 261 merges that predated the ledger were re-adjudicated member-by-member
against their group master, in two passes:

| | Groups | Members judged | Confirmed correct | Wrongly merged |
|---|---|---|---|---|
| Pass 1 | 261 | 662 | 403 | 257 |
| Pass 2 | 40 | 348 | 98 | 250 |
| **Total** | **301** | **1,010** | **501** | **507** |

**Roughly half of all legacy merged members were wrong** - the clearest
evidence of how fragile v2's literal matching was. A representative case: the
2015-2019 Optus White Pages breach (41,278 customers) had the separate 2022
Optus breach, its class action and its ACMA coverage all folded into it,
purely because every record mentioned "Optus".

407 corrections at certainty >= 0.90 were applied; 400 succeeded. 335 became
their own events and 65 were reattached to an existing event with the same
title and date. The 100 findings in the 0.80-0.89 band were left for human
review. Active events went 921 -> 1,256, unexplained merges 261 -> **0**.

Three defects were found and fixed during this run, each caught by a smoke test
or a coverage check rather than by the run appearing to succeed:

1. **Regulator-as-victim.** All 23,388 `EnrichedEventEntities` rows are tagged
   `affected`, so the highest-confidence entity is often the regulator
   ("Australian Privacy Commissioner", "Federal Court"). Comparing one name
   apiece declared correct Optus groups to be different organisations at 0.90
   confidence. Fixed by comparing entity *sets*, and by adding
   `require_entity_match=False` so a review of already-grouped events judges on
   content rather than a noisy label.
2. **Cosine overshoot.** Identical vectors returned `1.0000000000000002`,
   violating the `[-1, 1]` bound on `MatchEvidence` and aborting the whole run
   on the first exact duplicate. Now clamped.
3. **Silent group skipping.** A chained merge re-tags an intermediate group's
   master row as `merged`, so 48 groups had no `master`-tagged row and were
   skipped without warning. The master is now resolved from
   `DeduplicatedEvents.master_enriched_event_id`, which is authoritative.

---

## 8b. Resolving the borderline band and the failed corrections

Both items left open by the first legacy pass were closed.

**The 7 failed corrections.** All seven were correct verdicts (Blackbaud vs
Toll Group, RACGP vs Smoke Alarm Solutions, Australian Embassy vs Latitude
Financial) blocked by the partial unique index `idx_dedup_unique_event` on
`(title, event_date) WHERE status='Active'`. Root cause: v2 merges take the
*earliest* member date, so a group's `event_date` is frequently the departing
member's date - the split row would collide with the group it just left.
`split_member` now recomputes the group's title and date from the members that
actually remain, which is required for correctness anyway and frees the
collision. All 7 applied; **0 of 407 remain unapplied**.

**The 100 borderline findings (0.80-0.89).** Each was re-adjudicated twice with
the records in both orders (`resolve-band`). Re-running at temperature 0 in the
same order only repeats the first answer; swapping is a real robustness test.
An initial run was misleading - 136 of 200 verdicts came from the embedding
short-circuit, and cosine similarity is symmetric, so the swap was vacuous for
them. Adding `force_llm=True` produced 200 genuine verdicts and changed 6
dispositions. Outcome: **83 confirmed wrongly merged** (81 applied), **7
confirmed correctly merged**, **10 order-unstable** and left merged with the
instability recorded.

**Over-splitting discovered and corrected.** Auditing the applied band splits
revealed a real failure mode: when one breach generates a long tail of coverage
(regulator statement, class action, "government concludes response"), pairwise
adjudication reads the differing focus as a different incident. HWL Ebsworth
reached **19 separate active events, all dated 2023-04-26**. Pairwise re-checks
upheld those splits - the blind spot is structural, not a prompt problem, since
each pair really does emphasise something different.

The fix looks at the cluster instead of the pair, re-applying the project's own
documented Rule 1 (`consolidate`): same canonical entity + same date is one
incident. Deterministic, free, and exactly the invariant the over-splitting
broke. It merged **203 duplicate events** across three passes (148 exact-date,
41 variant-name, 14 with a 3-day tolerance), including the 2022 Optus breach
which had scattered across **41 events**. Guards: clusters keyed on a victim
with no identifying token (e.g. "Australia") are skipped, and the date
tolerance defaults to 0.

A companion `recheck-splits` command re-examines splits that separated records
about the same organisation; it re-merged 3 and upheld 171.

---

## 8c. Cluster-level adjudication

Pairwise adjudication has a blind spot that no amount of re-checking fixes.
When one breach generates a long tail of coverage - the incident report, the
company statement, the OAIC investigation, the class action, "government
concludes formal response" - each article emphasises something different, so
pair by pair the model keeps answering "different focus, therefore different
incident". It answers that *consistently*, which is why swapping record order
and re-running changed nothing. HWL Ebsworth ended up as 19 separate active
events, all dated 2023-04-26, with every pairwise re-check upholding the split.

`cluster_adjudicator.py` judges a whole cluster at once and returns a
**partition**. Two properties follow directly:

* The model can see that nine records are follow-up coverage of one breach,
  because it is looking at them together.
* A partition is internally consistent by construction. N pairwise verdicts can
  contradict each other (A~B, B~C, A!~C); a partition cannot.

Cost drops too: one call per cluster instead of N(N-1)/2. A 13-member cluster
goes from 78 calls to 1.

### Building clusters

Two independent bucket types, deliberately over-generating:

* **Entity buckets** - records whose organisations resolve as candidates.
* **Rare-title-token buckets** - entity labels are unreliable (the OAIC is
  stored as the victim of the HWL Ebsworth breach), so entity-only clustering
  found 2 of the 13 HWL records. Every one of them says "Ebsworth" in its
  title. Rarity is measured from corpus frequency, so "data" or "breach" can
  never form a bucket.

**Buckets, not a transitive closure.** The first implementation unioned any two
records sharing a distinctive token, which chained **807 of 845 events into one
cluster** - A shares a word with B, B with C, and the corpus collapses. Buckets
are independent, deduplicated against subsets, and capped at twice the
adjudication limit. A regression test (`test_clusters_do_not_chain_transitively`)
pins this.

The rarity cut-off was then found to be too tight in the other direction: at
0.5% of the corpus, "Ebsworth" (11 titles) was not rare enough to form a bucket
at all, so the cluster that motivated the whole feature never existed. It is
now 3%, with the size cap as the guard.

### Safety

Nothing is applied unless the partition covers every record exactly once. A
partition that drops a record would silently delete an event; one that assigns
a record twice would double-count it. Both are rejected, as are LLM failures
and oversized clusters, and every rejection degrades to "every record its own
incident" - which changes nothing when applied.

### Verified behaviour

Given all 13 HWL Ebsworth records together, the adjudicator returns **one
incident at 0.95 certainty**, correctly identifying the OAIC investigation, the
NDIA exposure notice and the "business as usual" follow-up as coverage of the
same April 2023 breach.

It is also conservative in the other direction. Entity buckets built from
mislabelled victims contain genuinely unrelated events, and the adjudicator
separates them rather than merging: one 25-event bucket was returned as 24
distinct incidents.

```bash
python scripts/dedup_admin.py adjudicate-clusters --dry-run
python scripts/dedup_admin.py adjudicate-clusters --entity "Ebsworth"
python scripts/dedup_admin.py adjudicate-clusters --min-certainty 0.8
```

---

## 8d. Identity on immutable keys, and derived titles

### The constraint was on the wrong column

`DeduplicatedEvents` enforced uniqueness with

```sql
CREATE UNIQUE INDEX idx_dedup_unique_event
    ON DeduplicatedEvents(title, event_date) WHERE status = 'Active'
```

That makes a **mutable display field part of a row's identity**, and it caused
two separate failures already documented above:

1. **Titles could not be corrected.** A merged event inherits its master's
   headline, and master selection follows dedup mechanics rather than how well
   a headline describes the incident. The 130-record Qantas breach was called
   *"Scattered Spider Ransomware Attacks"*, the 96-record Medibank breach
   *"Australia Blames Russian Hacker for Major Cyber..."*, and the 43-record
   HWL Ebsworth breach simply *"Untitled Event"* - and rewriting any of them
   risked colliding with another row.
2. **Splits failed.** Undoing a bad merge creates a row whose title and date
   match the group it just left. Seven corrections aborted outright and needed
   a workaround (recomputing the group's identity first) to proceed.

It is also wrong on its own terms: two genuinely distinct incidents can share a
title and a date, which is routine when titles are placeholders.

Identity now keys on `master_enriched_event_id` - assigned once, never
rewritten, verified unique across all active rows before the swap. The
migration **refuses to run** if that key is not unique, rather than dropping
the old guard without a replacement (`test_migration_refuses_when_immutable_key_is_not_unique`).

The `DUPLICATE_EVENT` integrity check moved with it: it now reports two active
rows sharing a master event, and no longer flags two incidents that happen to
share a headline.

### Titles are now derived, not inherited

`title_selection.py` scores every candidate and picks the best. Candidates are
the incident label produced by cluster adjudication - recoverable from the
ledger, so no extra LLM call - plus every member's own title. Scoring rewards
naming the victim organisation and describing an incident; it penalises
placeholders, bare mastheads ("Cyber Daily", "- iTnews") and shouty headlines.
Derivation runs automatically on merge and on split, and `retitle` applies it
to existing rows.

### Results

| Members | Before | After |
|---|---|---|
| 130 | Scattered Spider Ransomware Attacks | Qantas Airways Says Hackers Leaked Data on Its Customers |
| 96 | Australia Blames Russian Hacker for Major Cyber... | Medibank Private Limited cyber attack (August 2022) |
| 58 | External IT service provider cyber incident | Australian Clinical Labs third-party breach |
| 43 | Untitled Event | HWL Ebsworth ransomware breach (April 2023) |

410 events retitled. **84% of merged events now name their own victim** in the
title; placeholder titles fell to 10 and bare-masthead titles to 4, all cases
where no member offers anything better.

---

## 8e. Victim organisation: a propagation bug, not a bad extractor

The victim was copied from the **master record's highest-confidence entity**.
Every row in `EnrichedEventEntities` is tagged `affected` with no
victim/regulator distinction, so the master's top entity was frequently the
wrong organisation - and where the master had none, nothing was stored at all.

An audit settled which of the two possible causes it was:

| | Count |
|---|---|
| Active events with **no victim recorded** | 287 of 952 (30%) |
| ...where entities **do exist** on the members (propagation bug) | **284** |
| ...where no entity was extracted at all (extraction failure) | **3** |

So it was a bug, not a failing extractor. `victim_selection.py` re-derives the
victim from **all** members: mention frequency across the event, corroborated
by the title. Result: missing victims **287 -> 8 (1%)**, and the 106-record
Optus breach moved from "Australian Cyber Security Centre" to
"Singtel Optus Pty Limited".

### Four filters, each added after it broke something

1. **Corpus ubiquity, not name shape.** The first version rejected anything
   matching "Capitalised Capitalised" as a person - which discarded *Oxfam
   Australia*, *Deakin University*, *Compumedics Limited* and *Brydens
   Lawyers*. Bylines and officials are now identified by how many unrelated
   events they appear in, the same measured approach used for entity blocking.
2. **Ubiquity is scaled, not absolute.** Rejecting ubiquitous names outright
   handed the Optus breach to a bystander mentioned once, because the largest
   victims are *also* corpus-common. A name that dominates its own event
   overcomes the penalty.
3. **Regulators are only replaced when they are bystanders.** ASIC was breached
   *through* Accellion; being a regulator is not proof of a mistake. Replacement
   requires the regulator to be barely mentioned by its own members.
4. **Surgical replacement.** Frequency alone re-decided victims that were
   already correct, proposing *Evolution Mining Limited -> Reuters* and
   *Nine Entertainment -> Sydney Morning Herald*. An existing value is now
   replaced only on positive evidence it is wrong, and never traded for a
   shorter form of the same name.

### A precision/recall split in entity matching

`are_candidates` is deliberately loose - its job is blocking recall - and it
folds *University of Technology Sydney* into *University of Sydney* on the
shared token "sydney". Counting mentions needs the opposite, so
`EntityResolver.same_organisation` was added: token equality, or a subset whose
extra words are only corporate qualifiers ("Optus" / "Singtel Optus Pty
Limited"), or a single distinctive brand word ("Qantas" / "Qantas Airways
Limited"). Using the loose test for counting made a wrong victim look
well-supported.

### Industry follows the victim

The industry describes the victim, so it cannot survive a change of victim.
Carrying it over left ProctorU - a technology vendor - tagged "Education", its
universities' sector, which is precisely the defect
`check_industry_known_vendors_correct` exists to catch. It is now re-derived
from the entity record, sibling agreement, or entity type, and left blank when
none of those can justify a value.

---

## 8f. Entity typing: what an entity IS, and what part it played

Victim selection by mention frequency has a ceiling it cannot pass. Counting
mentions cannot distinguish the breached company from the software it was
breached through, so the global Canvas incident was attributed to "Canvas
Learning Management System" - a product, not an organisation. No amount of
better counting fixes that; the missing information is *what each entity is*.

`EnrichedEventEntities.relationship_type` already existed for this, but all
23,388 rows carried the single value `affected`, so the column conveyed
nothing.

### Two levels, because they vary independently

| | Scope | Stored on | Values |
|---|---|---|---|
| **Kind** | Invariant | `EntitiesV2.entity_kind` | organisation, government_body, product, person, threat_actor, other |
| **Role** | Per event | `EnrichedEventEntities.relationship_type` | victim, vendor, affected_customer, regulator, threat_actor, product, bystander |

Kind is a property of the entity: Canvas is always a product. Role is a
property of the entity *in one event*: Instructure is the victim of its own
breach and the vendor in events about the universities it serves. Collapsing
these into one field would make either one wrong.

### Priority

Victim is the primary output, as before. Vendor is now recorded alongside it in
`DeduplicatedEvents.vendor_organization_name`, because a supply-chain breach is
only intelligible when both are known - "University of Sydney, breached through
Instructure" says something that neither name alone does.

A role-labelled victim **outranks** the frequency heuristic: the classifier saw
the event and separated the breached organisation from the product, which
counting cannot. The heuristic remains for events that have not been
classified.

### Safety

* Rules decide only the unambiguous cases (known regulators, threat actors,
  products, dates, domains); everything else goes to the model, which can see
  the event.
* Entities the model invents are discarded - only names that were actually
  supplied may enter the database.
* Entities the model skips fall back to the rule answer, so no link is left
  unlabelled.
* A product can never be returned as the victim, even if mislabelled as one.
* On LLM failure the rules apply, and rules alone never invent a victim.

### Verified

On the Canvas event the classifier returns exactly the intended split:

```
victim='University of Sydney'   vendor='Instructure'
  product        product          Canvas
  victim         organisation     University of Sydney
  vendor         organisation     Instructure
  threat_actor   threat_actor     ShinyHunters
  regulator      government_body  National Office of Cyber Security
```

One bug worth recording: candidate entities were truncated to the first N
*alphabetically*, which cut everything after "Flinders University" - including
both "Instructure" and "University of Sydney". The classifier never saw the
victim or the vendor. Candidates are now ranked by whether they appear in the
title and by mention count before truncation.

```bash
python scripts/dedup_admin.py classify-entities --dry-run
python scripts/dedup_admin.py classify-entities --entity "Canvas" --show-roles
```

---

## 8g. Keeping entity roles in step with membership

Roles are decided per event, against the set of records it contains. Folding
two events together or splitting one apart therefore invalidates them: the
surviving event keeps a victim and vendor that were judged for a different set
of records.

### Staleness is derived, not flagged

The obvious implementation is a "needs reclassification" flag set by every
mutation. That only works while every current *and future* code path remembers
to set it, and this pipeline has at least five that change membership -
`merge_events`, `split_member`, `consolidate`, the legacy review and the
backfill. Missing one fails silently, which is the same class of bug that left
`DeduplicatedEventSources` empty across the whole database.

Instead each event stores `roles_member_signature`: an order-independent hash
of the members it was classified against. Any membership change - by any route,
including routes not yet written - changes the current signature and the event
reports as stale. Nothing has to remember anything.

```bash
python scripts/dedup_admin.py roles-status                     # what is stale
python scripts/dedup_admin.py classify-entities --stale-only   # refresh only those
python scripts/dedup_admin.py roles-status --invalidate-all    # after changing the classifier
```

`--invalidate-all` covers the other reason roles go stale: the *classifier*
changing rather than the membership. That one genuinely cannot be detected from
the data, so it is explicit.

### Verified

On a copy of the live database, a real merge and a real split each flipped the
affected event to stale while leaving untouched events current. Sixteen tests
pin the behaviour, including that invalidation is targeted rather than global
and that merged-away events are not reported as pending work.

Events with no entities at all are marked classified rather than skipped -
there is nothing to classify, so their (empty) roles trivially match, and
leaving them unmarked would re-select them on every run for ever.

The dashboard shows a "roles stale" badge, a stat card, and a filter, so the
condition is visible without running a command.

---

## 8h. Repeat attacks inside 90 days, re-checked

### The failure mode

One breach is covered more than once, and the coverage is published weeks
apart: the initial report, the organisation's statement, the OAIC notification,
the class action, the "what we now know" follow-up six weeks later. Each
article carries its own publication date. Where extraction takes that date as
the *incident* date, one incident becomes two events dated eight weeks apart —
and stored under the same organisation, that reads as **the organisation was
attacked twice in a quarter**, which is not what happened.

Short gaps are where this is both most likely and most damaging:

- **Most likely**, because reporting lag is measured in weeks, so a spurious
  pair lands in exactly this window.
- **Most damaging**, because the short-elapsed-time band is what every
  recurrence model is estimated from (`scripts/analyze_recurrent_timing.py`
  fits a piecewise hazard on elapsed time since the prior event). A false
  duplicate there does not add noise — it manufactures the signal, putting a
  "repeat event" at a gap of a few weeks that never occurred.

### Why the existing passes do not cover it

| Pass | Blind spot |
|---|---|
| `consolidate` | requires the **same date**, so never sees a pair eight weeks apart |
| `adjudicate-clusters` | blocks on entity and rare title tokens; a follow-up with a different headline may never enter the same cluster |
| `reconcile-entities` | judges an organisation's whole history at once, where a short-gap pair is one comparison among many |

`check-recurrences` asks the narrow question directly, over the complete set of
short-gap pairs.

### Runs, not pairs

Within one entity, events are sorted by date and chained while each
*consecutive* gap is under the window — the inter-event time, which is what the
question is about. Three records six weeks apart form one run rather than three
pairs, and the run is partitioned in a single call. A partition is internally
consistent by construction; three pairwise verdicts can contradict each other
(A~B, B~C, A!~C).

Attribution is by the `victim` **role**, so an event with several co-equal
victims is checked under each of them, and an event whose scalar victim was
never populated is still covered.

### Both errors are real

Organisations genuinely are attacked twice in a quarter, and merging two real
incidents destroys information the merged row cannot recover. The adjudicator
is therefore given the distinguishing evidence explicitly — attack method,
record count, data types, source URLs, and the gap in days — and is told that
dates in this dataset are frequently publication dates rather than incident
dates, so a date difference is weak evidence either way.

Safety, in the same shape as every other pass here: an invalid partition, an
LLM failure or an oversized run all degrade to "change nothing"; merges happen
only at or above `--min-certainty` (default 0.85); and an active human
`different` override on any member pair vetoes the merge outright, because that
is a human answer to precisely this question.

```bash
python scripts/dedup_admin.py check-recurrences --dry-run --verbose
python scripts/dedup_admin.py check-recurrences --window-days 120
python scripts/dedup_admin.py check-recurrences --entity "Optus"
```

### Verified on the live database

699 victim-attributed events produced **8 runs** covering 16 events and 8 short
gaps. The adjudicator split them:

| Organisation | Gap | Verdict |
|---|---|---|
| Melbourne International Film Festival | 17d | one incident (0.90) — same 340,000-record claim |
| Swinburne University of Technology | 45d | one incident (0.90) — same 5,000+ individuals, same registration page |
| University of Notre Dame Australia | 5d | one incident (0.90) — second record is follow-up coverage |
| Service NSW | 23d | one incident (0.70) — **below threshold, left alone** |
| Optus | 86d | distinct — September 2022 breach and a separate December event |
| Commonwealth Bank | 59d | distinct |
| ACT Government | 15d | distinct |
| University of Western Australia | 31d | distinct |

Three false repeats merged; four genuine short-gap recurrences preserved. The
Optus result is the one that matters most — 86 days apart and the same
organisation is exactly the shape this pass could over-merge, and it did not.

Findings are written to `instance/recurrence_findings.json` for review, and the
pass runs automatically after every deduplication in `run_full_pipeline.py`
(`--skip-recurrence-check` to opt out).

---

## 8i. Ordinal entity size

`EntitiesV2.employee_count` and `turnover` have existed since v1 and were NULL
on **all 3,133 rows** — nothing ever populated them. Any analysis asking how big
the victim was therefore ran with a single "unknown" level for the entire
dataset; `analyze_recurrent_timing.py` requests `employee_size` and
`turnover_size` as covariates and gets exactly that.

An exact headcount is unobtainable for most of these organisations — private
companies do not publish one, and government bodies do not publish a comparable
one — but the **band** is nearly always recoverable, and a band is what the
analysis needs.

### The bands

| Band | Definition |
|---|---|
| `SMALL` | fewer than 20 employees, or under A$10m revenue |
| `MEDIUM` | 20–199 employees, or A$10m–100m |
| `LARGE` | 200–4,999 employees, or A$100m–1b |
| `HUGE` | 5,000+ employees or over A$1b; also national/state departments, the major universities, ASX-100 |
| `UNKNOWN` | not identifiable, or not an organisation at all |

The three-way cut follows the **ABS definition of business size** (small 0–19,
medium 20–199, large 200+), which is the standard an Australian dataset should
be readable against. `LARGE` is then split at 5,000 because that band otherwise
runs from a mid-sized firm all the way to Telstra, and a 300-person company
versus a 50,000-person one is precisely what a cyber-incident analysis turns on.
Employee count is the primary axis and revenue the tiebreaker, because
government departments and universities have headcounts but no turnover in the
commercial sense.

### Why Perplexity, not recall

The band depends on facts about the real organisation, not on anything in the
event text — a breach report rarely says how big the victim is. An LLM asked
from memory will confidently size an organisation it has never heard of, and
this dataset is full of small Australian businesses that fall exactly into that
gap. Perplexity looks the organisation up first (website, annual report,
LinkedIn, ABN/ASIC, news), proposes a band from what it finds, and GPT-4o turns
that prose into the stored record. Citations are kept in `size_sources`, so a
band can be traced back to the pages it rests on — the same standard every
merge decision is held to.

Where the answer reports a figure and a band that disagree ("about 45,000
staff … LARGE"), the **figure decides**: it is the evidence, the label is an
inference from it, and the thresholds live in one place so the correction
cannot drift from what the prompt asked for.

### UNKNOWN is an answer, not a failure code

A product ("Canvas"), a person, a ransomware group and a collective noun
("Australian hospitals") have no organisational size. Those are decided by rule
at no API cost — 1,379 of 3,125 rows, roughly 44% of the table. A row whose
research could not be *reached* is marked `unavailable` instead and retried on
the next run, so a transient outage never hardens into a settled UNKNOWN.

A `human` method is never overwritten by an automated pass, and lookups are
shared across spelling variants of one name, so "Optus" and "Optus Pty Ltd" cost
one search rather than two.

```bash
python scripts/dedup_admin.py size-entities --dry-run --verbose
python scripts/dedup_admin.py size-entities --linked-only
python scripts/dedup_admin.py size-entities --entity "Optus" --refresh
```

Incremental by default, so a monthly refresh sizes only what it just added; the
pipeline runs it after every deduplication (`--skip-entity-sizing` to opt out).
Bands appear as a badge and a filter on `dashboard/entities.html`, with the
evidence on hover.

---

## 8j. Stray records: a calibrated coherence check (2026-10-27)

`check-coherence` (`dedup/coherence.py`) finds records that sit inside an event
but describe another incident, and splits them out. It used to ask GPT-4o about
each suspect event and split at a hand-set certainty of 0.99; it was not in the
refresh. Labellers then found strays it had left: Western Sydney University
holding two WA Health pager-leak records, the University of Tasmania holding
University of Sydney and Canvas/Instructure records, NT Health holding an
Updoc record.

### How it decides

1. **Suspects (free, unchanged).** A member whose Perplexity organisation
   shares no distinctive name key with the event's victim, vendor or majority
   organisation. 92 of the 2,161 members of the 321 multi-member events.
   Only suspects can be split on a model score.
2. **Jev, per suspect** (`member_same_incident`, version
   `member-coherence-v2`, `subject_type='event_member'`,
   subject `<dedup_id>|<enriched_id>`): the record's title, its enrichment
   summary, its Perplexity research and its stored page; the event's title,
   date, victim, vendor and other records; and the victim's researched timeline
   (`org_timeline.timeline_for_event`). Stored and reused while the input is
   unchanged; a membership change changes the input, so the remaining members
   are re-judged next run.
3. **Escalation.** Jev scores in 0.1-0.7 (fixed before calibration) go to
   GPT-5.5 with reasoning; its score is final. 13 of the 92 suspects.
4. **Decision.** Split when the final P(same) is below the calibrated cut-off;
   labels override in both directions.

### Calibration

`scripts/calibrate_coherence.py` (`sample`, `packet`, `judge`, `calibrate`) on
`calibration_coherence.db`, a verified backup of the live database taken
2026-10-27 22:08.

- **Sample** (`evaluation/coherence_sample.csv`, seed 20261027): stratum S,
  every suspect (92, weight 1) - any cut-off acts only here, so it is a
  census; stratum F, 28 of the 112 non-suspects dated more than 180 days from
  their event (weight 4.0); stratum N, 28 of the other 1,957 non-suspects
  (weight 69.9). F and N measure what the suspect gate keeps; they are never
  acted on by score.
- **Labels** (`evaluation/coherence_labels.csv`, 148 rows: 122 same, 25
  different, 1 unsure): four Claude Opus 5.5 subagents, each given a quarter of
  a packet built without scores or strata, labelling from the stored evidence
  plus web searches, each row with a rationale. Conventions given: the event's
  incident is the one its victim suffered that its title and most records
  describe (titles can be wrong); misdated copies, follow-ups and the
  supplier-customer convention are `same`; a record whose title or research
  points to the incident is `same` even when its stored page is unhelpful.
  The `incident` column, naming where a `different` record belongs, was
  transcribed from the rationales afterwards and is used only to keep strays
  about one incident together.
- **v1 failed.** Question v1 read the stored page as the record's subject. On
  the 92 suspects no cut-off reached 0.95: 4/5 at 0.02, 10/14 at 0.03,
  bootstrap mean 0.32. Most of its confident "strays" were records whose page
  is a *different article stored by mistake* (Scouts Victoria's notice with a
  UTAS page; Suncorp Bank's, HWL Ebsworth's and Optus/Huawei's with
  unrelated pages) while title and research name the event's incident - which
  the labelling convention, fixed before any score was seen, keeps.
- **v2** states that convention (title, summary and research say what a record
  is about; the page is supporting evidence) and adds the record's own
  summary. Result, precision target 0.95, at least 5 splits:

  | Cut-off | Splits (labelled) | Correct | Precision | Recall | Wilson 95% CI | Bootstrap out-of-sample |
  |---|---|---|---|---|---|---|
  | 0.15 | 17 | 17 | 1.00 | 0.94 | 0.82-1.00 | mean 0.985, median 1.00, p05 1.00 |

  No labelled `same` scores below 0.70, so the margin is wide; the one stray
  missed (OpenAI's Hugging Face episode filed in the Services Australia /
  OpenAI event) scored 0.74. Two strays score exactly 0.15, at the cut-off:
  Jev's sampling variation can put them on either side on a fresh database
  (they are labelled, so they leave regardless).
- **Caveat.** v2 was written after v1 was scored against these labels, so the
  bootstrap (which resamples labels, not question wordings) does not cover
  that choice. The change restates a convention rather than fitting items,
  and the score gap is large, but the next labelling round should check v2 on
  fresh suspects.

Calibration identity: `member-coherence-v2+member-coherence-escalation-v2`,
`jev-1.14.0+gpt-5.5-2026-04-23` (`evaluation/coherence_calibration.json`).
A different model or question version makes model-judged splits report-only
(logged at WARNING as `REPORT ONLY`); labelled strays still split.

### Guards: kept, and what the labels say about them

| Guard | Past error it encodes | Labels |
|---|---|---|
| Only suspects leave on a score (same organisation never does) | misdated copies, follow-ups (Partnered Health, Latitude, OpenAI) | Keeps real strays: 7 of 28 far-dated non-suspects are labelled `different` (Optus's 2019 White Pages record in its 2022 breach, AFP's HWL Ebsworth exposure in its 2022 leak, Okta's 2023 support breach in its 2022 Sitel event, a Hudson Rock bank-credential finding in two events) - about 28 of 112 far-dated non-suspects when weighted, 1.4% of all non-suspects. 21 of the 28 far-dated ones are `same` (misdated copies), so dropping the gate would expose those to the score unmeasured. **Kept**; the misses are a different-incident-at-the-same-organisation problem (see `docs/TODO.md`). Labelled ones are split regardless. |
| An organisation named in the event title stays | youX / Vroom by YouX | Correct in principle but wrong on the mislabelled UTas event titled "University of Sydney third-party Canvas cyber incident": it would hold both University of Sydney strays. Labels override it there. Kept. |
| Majority of strays naming one organisation = mislabelled event, split nothing | 12 youX records judged foreign to "Vroom by YouX" | Not triggered on current data. The youX event is now labelled the other way: most records are the Feb 2026 MongoDB breach and the one Vroom (March 2025) record is the stray. Kept. |
| Supplier-customer rule in the question; vendor counts as the event's organisation | Frontier / NSW Health, Canvas customers | About 20 supplier-customer suspects labelled `same` (Canvas: nine universities, DECYP, Queensland education; Pareto Phone: Amnesty, ACF; Latitude / Coles; Frontier: SA Government, NSW Health; OracleCMS / Ballarat) all score >= 0.70. Kept. |
| Never strip an event bare | - | Kept. |

Dropped: folding a split-off record into "the single event for its
organisation within 90 days". That merge was never measured; the calibrated
`check-duplicates`, run straight after, decides re-homing instead.

Grouping: labelled strays naming one incident leave as one event; model strays
group only when they share an organisation key **and** lie within 90 days,
because one organisation can have two incidents among one event's strays (the
University of Sydney's December 2025 code-library breach and its 2026 Canvas
exposure, both in the UTas event). Groups leaving separately get no ruling
between them, so `check-duplicates` can still join them.

### On a copy of the live database (2026-10-27)

`check-coherence --apply` on a verified copy: 92 suspects judged in 77 events
(13 escalated); 25 stray records split out of 19 events into 21 new events;
305 `different` rulings cached; 36 events' roles refreshed automatically.
Every split was a labelled stray - the score splits nothing more on current
data, because every current suspect is labelled. A second run judged the 76
suspects of the changed events (all >= 0.30) and split nothing.

### Failure behaviour (no fallback)

- Missing `TYPESAFE_API_KEY`, `typesafe-sdk` or `OPENAI_API_KEY`: exit 2,
  nothing judged.
- A Jev error after the SDK's retries: `CoherenceJudgementError`, exit 1,
  nothing split (judging finishes before any split).
- A failed escalation: that member is *undecided* - not split on Jev's
  un-escalated score, not kept as decided; model-judged splits in its event are
  deferred; the run exits 1 and retries next time.
- Under `--apply`, a missing, unreadable or empty labels or calibration file:
  `CoherenceConfigError`, exit 1, nothing split. Without `--apply` the run
  reports and logs why.

### In the refresh

After `check-victims` (suspects are found against the corrected victim) and
before `check-duplicates` (which re-homes the split-off records):

```
recalibrate --if-stale, check-sources --apply, check-events --apply, check-scope --apply,
classify-entities --stale-only, check-victims, check-victim-attribution --apply,
check-coherence --apply, check-same-organisation --apply, check-duplicates --apply,
find-supplier-victims --apply, resolve-label-contradictions
```

Split-off events get their entity roles from the ledger's automatic refresh,
and their reality judgement from the next refresh's `check-events`.

---

## 8k. Separate incidents under one organisation (2026-10-29)

`check-coherence` deliberately never splits a same-organisation member on a
score (section 8j: that guard exists because misdated copies of one incident
were once split apart). The labels showed the cost: 7 of 28 far-dated
non-suspects were different incidents, and on 2026-10-28 the recurrence check
merged two labelled-different pairs at certainty 0.90 / 0.98 - Western Sydney
University's August 2024 and January 2025 breaches, youX's March 2025 S3
exposure and February 2026 MongoDB theft - which the quality gate counted as
`labels.duplicates.contradictions` for a day without anything acting on them.

`check-same-organisation` (`dedup/coherence_same_org.py`) closes that without
reopening the old wound.

**Candidates (free)** - a non-suspect member, only on evidence of a different
incident:

| Evidence | Definition |
|---|---|
| verified far date | the member's incident date was stated in the record, researched by Perplexity, or established by its source (`RawEvents.event_date_precision`) - not a publication date, a discovery date, or unsupported - and its interval lies more than 90 days from the event's date interval (`processing/incident_dates.py` intervals; the heuristic month/year shape of the stored date otherwise) |
| timeline | the organisation's researched timeline lists two or more dated incidents and the member's date falls within tolerance of a different one than the event's does (`timeline_reconciliation.incident_date`, `TOLERANCE_DAYS`) |

A date gap on its own - an unverified date, a publication date - never makes a
candidate; neither signal ever splits anything by itself.

**Judgement** - Jev answers `member_same_incident_same_org`
(`member-coherence-sameorg-v1`, `subject_type='event_member_same_org'`) over the
stray question's evidence plus the member's and the event's date provenance and
`flagged_because`. The question says the flags are hints, that a record's date
is often wrong, and that follow-ups and misdated copies are the same incident.
0.1-0.7 goes to the reasoning model (`member-coherence-sameorg-escalation-v1`).
Failures behave as in 8j: a Jev error raises, a failed escalation leaves the
member undecided.

**Repair - only with its own calibration.** `scripts/calibrate_coherence_same_org.py`
`sample` (stratum C: every candidate; N: a seeded sample of the other
same-organisation members, which measures what candidate selection misses),
`packet` (blind: no score, stratum or flag), `judge`, `calibrate` (largest
cut-off with >= 95% of the candidates it splits labelled `different`, bootstrap,
evidence fingerprint). Until `evaluation/coherence_same_org_calibration.json`
matches the current models, questions and evidence, the step reports and splits
only members labelled `different`; `same` never leaves. Guards: the departing
records must be fewer than those that stay; a split goes through
`DedupLedger.split_members` (snapshot, `different` rulings cached).

On the 2026-10-29 live copy the candidates are 196 members of 82 events (103
timeline-only, 75 date-only, 18 both). Both contradicted pairs' mixed records
are among them (WSU: the January 2025 SSO record, "167 days from the event's date
interval" and matching timeline incident 5, not 3; youX: five March 2025
records, 321 days away).

### Labelled-different pairs and the ledger

`DedupLedger.merge_events` refuses, as for a cached ruling, to join two events
when a pair labelled `different` in `evaluation/duplicate_labels.csv` resolves
(through merges already made) to one on each side. `merge --force` overrules.
`resolve-label-contradictions` (`dedup/label_contradictions.py`, no API calls)
undoes the merges that predate this: for each labelled-different pair that
resolves to one Active event it reads the members of each labelled event from
the labelling-time snapshot (`duplicates` in `evaluation/calibration_snapshots.json`),
intersects with the event's active records, and splits the smaller side out
together (ties: the pair's second event), with `different` rulings cached
between the sides. Limits: a labelled event may itself have held a stray or
misdated copy, which travels with its side (WSU's second event held a record
dated August 2024) and is re-homed afterwards; records that arrived after
labelling belong to neither side and stay; a side with no active record left
and a missing snapshot are listed as unresolved. Idempotent.

---

## 9. Known limitations

- **HWL Ebsworth remains in 13 active events, Regis Resources in 3.** Not a
  deduplication failure: those records carry *different recorded victims* (the
  OAIC is stored as the victim of the HWL breach), so no entity-based rule can
  group them. Fixing it means correcting entity extraction, not matching.
- **10 order-unstable pairs stay merged.** Their verdict flipped on record
  order, which is the honest signal that the evidence does not support acting.
  They are recorded as AMBIGUOUS and filterable in the dashboard.
- **10 events keep a placeholder title and 4 a bare masthead.** Every member
  of those events carries the same unusable headline and no cluster label
  exists, so there is nothing better to promote. Fixing them needs better
  titles upstream, not better selection.
- **8 events still have no victim.** Five are multi-victim roundups
  ("Hacker offers the personal details of 25m Aussies", "Cyber security: a
  month in retrospect") where no single victim exists; three have no entities
  extracted at all. Leaving these blank is correct.
- **Refreshing stale roles is a separate step, not automatic.** A merge marks
  the affected event stale immediately, but reclassifying costs an LLM call per
  event, so it is left to `classify-entities --stale-only` rather than run
  inline. `roles-status` and the dashboard both surface what is pending.
- **Pairwise adjudication cannot see a cluster.** `consolidate` compensates for
  the specific same-entity/same-date case, but a long tail of coverage spread
  across *different* dates still risks fragmenting. Cluster-level adjudication
  (judge all members of a group at once) is the real fix and is not built.
- **Event dates are still unreliable upstream.** Qantas 2025 coverage carries
  `2022-09-07`; MediSecure's May-2024 breach appears under 2019/2020 dates.
  Combined with "earliest date wins" on merge, one mis-dated article drags a
  merged event backwards. v3 records the date gap as evidence but does not fix
  the extraction — that belongs in enrichment.
- **Calibration is a coarse ±0.02 nudge**, not a fitted model. It needs on the
  order of dozens of overrides before it means much.
- **Cost.** A full pass over 1,034 events was ~412 GPT-4o calls. Incremental
  runs adjudicate only new candidates.
