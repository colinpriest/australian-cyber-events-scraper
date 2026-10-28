# Organisation timelines (v2), calibrated victim attribution, and timeline reconciliation

Three steps that build on each other. Each one that changes data acts only at
a cut-off calibrated on labels from this database, made blind to the model's
scores; without a matching calibration it reports and says so. Missing keys,
failed calls and missing labels fail the command (exit 2) - nothing falls back
silently to another model, an older answer or an unprotected run.

## 1. Organisation timelines, question v2 (`dedup/org_timeline.py`)

The timeline question v1 failed three ways on 2026-10-27:

| Failure | Example | v2 remedy |
|---|---|---|
| Aliases were whatever name the records carried, so a misfiled record put a different organisation into the question, and the timeline then vouched for the misattribution | "Australian Federal Police (also recorded as: Aussie Fluid Power)"; "University of Western Australia (also recorded as: UN Women Australia)"; "Ambulance Victoria (also recorded as: Ambulance Tasmania)" - about half of the 51 stored questions named another organisation | An alias must be a spelling of the same name (`is_same_name`: one name's words contain the other's, legal forms and "Australia" aside, and the shorter still identifies something). The looser `victim_agreement.denotes` matched all three pairs above |
| Multinationals refused | Microsoft: "can't reliably produce the requested exhaustive ... list" | Asked about Australia only (incidents affecting its Australian customers, staff, data or operations) when `EntitiesV2.is_australian = 0`, or when the global question is declined - retried in the same call, both answers stored, and the question then stays scoped |
| Supplier breaches missed | UWA's 2020 ProctorU breach | A separate supplier/third-party instruction, naming the suppliers the organisation's events record as vendor ("include each only if reporting confirms it was affected") |

Also: incidents at systems the organisation operates are asked for (ADHA /
My Health Record found nothing under v1); "list what your sources support,
even if incomplete" (a strict wording made Perplexity decline Westpac);
unnumbered `DATE | WHAT | ...` lines are parsed (the Commonwealth Bank's
answer came without numbers and was read as a refusal); and "I can't reliably
... NONE FOUND" counts as a refusal.

**Refreshing.** A timeline is stale when newer evidence exists *or* the stored
question differs from the current one, so the v2 wording re-researches every
timeline once (every version is kept). Organisations that fell to one event but
still have a timeline are kept current too (`organisations_to_research`).

**Failures are visible.** A failed search is recorded in
`OrganisationTimelineFailures` and raises. Judgements read timelines through
`for_judgement`, which adds a `warning` - "This history may be incomplete:
refreshing it failed on <date>; ... Last successful research <date>. An event
that matches nothing in it is NOT evidence against the event." - whenever the
timeline is stale, failed or declined. The warning is added only then, so a
current timeline's input, and every stored answer keyed on it, is unchanged.
Event reality and duplicate judgements read it the same way.

On a copy of the live database (2026-10-27): 49 organisations re-researched
(41 with several events plus 8 that kept a timeline); 6 are asked about
Australia only (Microsoft, Dell, Toyota, Uber, "Clinic" by entity data;
"Government" after declining); no latest timeline is a refusal (v1:
Microsoft) and none carries a misfiled alias. The wording took three rounds
on the copy (two drafts made Perplexity decline Westpac, CBA and UNSW, or drop
real incidents); about 150 Perplexity calls in all. Still imperfect: Perplexity
recall varies run to run, and UWA's timeline still omits ProctorU 2020
although UNSW's now lists it.

## 2. Victim attribution (`dedup/victim_judgement.py`)

`check-victim-attribution` judges **every** active event-victim pair (v1 judged
only events whose victim had a timeline): Jev `is_recorded_victim`
(`victim-attribution-v2`) over the event's records, Perplexity research, any
corroboration search, its other recorded victims and the victim's timeline
(with its warning). Scores in 0.1-0.7 - the band fixed before labelling - are
re-judged by the reasoning model (`victim-attribution-escalation-v1`); the
final score is calibrated.

With `--apply`, for pairs below the calibrated cut-off:

* another recorded victim not itself below the cut-off -> the wrong one is
  demoted to `bystander`;
* else Jev chooses the real victim among the organisations the event's own
  evidence names (`victim-replacement-v1`; each record's Perplexity victim and
  the organisations linked to its records, less collectives, products and
  threat actors), adopted only at or above the calibrated replacement
  threshold and only if the records' text names it, via
  `victim_agreement.apply_victim` (the `check-victims` re-attribution);
* else **flagged** and left unchanged - never guessed.

Labelled pairs are ground truth (`correct` never changed; `wrong` re-attributed
only to the labelled victim). `--apply` refuses if the labels file is missing,
unreadable or empty. Every change is snapshotted by the ledger and audited in
`VictimAttributionActions` with the scores, cut-offs, calibration and the
event's roles before the change.

### Labels and calibration (2026-10-27)

`evaluation/victim_attribution_sample.csv`: 151 pairs, stratified on the final
score - a census of all 61 below 0.5 (weight 1), 45 of 297 in 0.5-0.9 (weight
6.60) and 45 of 278 at 0.9 and above (weight 6.18), seed 20261027.
`evaluation/victim_attribution_labels.csv`: labelled by six Claude Opus 5.5
subagents from packets with no scores (`calibrate_victim_attribution.py
packets`) plus web search, one-line rationale each: 85 `correct`, 60 `wrong`,
3 `not_incident`, 3 `unsure`. Conventions given to the labellers:

* `correct` - the incident happened to the recorded victim (its systems,
  accounts or data, or its customers'/students'/staff's data held by it or a
  supplier). Includes one of several co-equal victims, the breached supplier
  itself, legal/parent/brand names, a collective name ("Australian SMEs") only
  when nothing more specific can be identified, and a whole government when
  the incident targeted it broadly.
* `wrong` - a different organisation (acronym clash, similar name), an
  organisation that only investigated / regulated / sanctioned / commented, a
  sector story or impersonation scam that did not breach it, or a generic,
  collective or parent-government name when the specific body is identified.
  `correct_victim` names the real victim (copied from the packet's candidates
  when there), or `none`.
* `not_incident`, `unsure` - excluded from calibration.

Labellers flagged their borderline calls in the rationales (e.g. NSW Health
vs Northern Sydney LHD; Heritage Bank, which was the US bank; UNSW and
ProctorU, where UNSW said no UNSW records were in the leak).

`evaluation/victim_attribution_calibration.json` (calibrated on the working
copy of 2026-10-27 after the v2 timelines, `work.db`):

| | Cut-off | Labelled acted on | Correct | Weighted precision | Weighted recall | Out-of-sample (bootstrap mean / 5th pct) |
|---|---|---|---|---|---|---|
| Recorded victim wrong | P < **0.15** | 45 | 43 | 0.956 (95% CI 0.85-0.99) | 0.375 | 0.944 / 0.83 |
| Replacement (sole victim) | choice P >= **0.84** | 25 | 23 | 0.957 | - | 0.944 / 0.83 |

The two victims wrongly judged wrong: Epworth HealthCare (its patient data
leaked from an unnamed third party) and DigiGround (a Qilin listing naming
it). The two wrong replacements: Sold RE for a Radar campaign with several
co-equal agencies (labelled `none`), and the Legal Practice Board of WA for a
WA Health statement. Recall is low: 17 labelled wrong victims score above the
cut-off, mostly in the higher strata (NSW Health for a Northern Sydney LHD
breach at 0.92, Heritage Bank at 0.75, Commonwealth Bank in a scam story at
0.72), so a cut-off that caught them would act on many correct victims.
The bootstrap mean (0.944) is just under the 0.95 target - the procedure's
honest precision is about 94%.

### On a copy of the live database

`check-victim-attribution --apply` (after the v2 timelines): 636 event-victim
pairs judged (151 escalated), 47 below the cut-off - **16 re-attributed**
(UN Women Australia, Aussie Fluid Power, TAFE SA, Henning Harders, Cariad,
Cyberhaven, Stonnington City Council, Origin Energy, ...), **13 demoted**
where another victim remains (Ambulance Victoria on the Ambulance Tasmania
pager leak, ANZ in two multi-bank stories, Cabrini on Eastern Health's
ransomware, ...), **18 flagged** (scam stories naming banks, a collective
with no named victim, replacements below the threshold or not named in the
records). No event lost its last victim (150 victimless active events before
and after). A second run changes nothing (idempotent). Of the five known
wrong victims, four are fixed (UN Women, Aussie Fluid Power, Ambulance
Tasmania, TAFE SA); the NSW driver-licence exposure filed under Optus is not
below the cut-off.

### Flags are reassessed; unresolved attributions are marked, not dropped

A flag is a judgement on one input. Every run judges every pair again, and Jev
is asked only where the input changed: its stored answer is keyed on a hash of
the evidence, the organisation's timeline, the event's other victims and so
its membership. Each `VictimAttributionActions` row now records that
`input_hash` and the event's `member_signature` (columns added in place on
older tables). A pair whose latest action is `flagged` ends every calibrated
run as one of:

| Outcome | When | Row written |
|---|---|---|
| **confirmed** (flag cleared) | re-judged at or above the cut-off, or labelled `correct` | `confirmed`, reason says what changed ("event membership changed", "evidence, timeline or co-victims changed", "input unchanged", "flag predates input tracking") |
| **replaced** | still below the cut-off and now a co-victim remains (`demoted`) or a calibrated replacement qualifies (`reattributed`) | as before |
| **still flagged** | still below the cut-off, no trusted replacement | a new `flagged` row only when the reason or the input changed |

A pair labelled `wrong` is never confirmed. Without a calibration for the
current models and questions nothing is confirmed and flags stay. The command
prints and returns a `flag_review` (flags reassessed, how many with changed
input, the three outcomes, unresolved now).

**Unresolved** = an active event-victim pair whose latest action is `flagged`
(`victim_judgement.unresolved_attributions`). The incident is real and kept;
who it happened to is not settled. It is exposed separately:

* `victim_counting.unresolved_rows` marks the (organisation, event) rows,
  including a kept name standing for a flagged spelling of the same
  organisation dropped within the event;
* the by-entity dashboard shows an "attribution unresolved" badge (hover: the
  flag's reason), a filter, and the count in the subtitle;
* the recurrence analysis tags `attribution_unresolved` on each victim row and
  reports, under "unresolved victim attributions" in the data-quality summary,
  how many rows, events and organisations are unresolved and the observed
  repeats per elapsed-time band with them **included** and **excluded**, plus
  how many repeats involve one. `--unresolved-attributions exclude` (both
  `analyze_recurrent_timing.py` and `analyze_recurrence_extensions.py`; default
  `include`) runs the whole analysis without them.

On the 2026-10-28 copy: 43 active pairs flagged (none confirmed yet - every
stored score is still below 0.15), 15 of them with input changed since the
flag, which the next run re-judges. The dashboard marks 43 rows on 43 events.
In the recurrence cohort 41 attributions are unresolved; 19 of 150 observed
repeats involve one (136 repeats with them excluded).

### Roles are refreshed after the last membership change

Roles carry a signature of the members they were classified against
(`role_maintenance`), so staleness is derived. But `classify-entities
--stale-only` ran once, early in data hygiene, and `check-coherence`,
`check-duplicates`, `find-supplier-victims` and the recurrence check change
membership after it. On the 2026-10-28 copy all 145 events with outdated roles
had been merged after their roles were classified: 295 `check-duplicates`
decisions and 16 recurrence merges. The refresh now also runs:

1. after `find-supplier-victims`, before `reconcile-timelines` (which, like
   the recurrence check, reads victims by role);
2. after the recurrence check (`run_full_pipeline.py` `run_role_refresh`):
   `classify-entities --stale-only`, `check-victims`,
   `check-victim-attribution --apply`, then a check that no active event has
   outdated or missing roles. Any left (a failed model call) are recorded as an
   error and the run ends degraded.

## 3. Timeline reconciliation (`dedup/timeline_reconciliation.py`)

`reconcile-timelines` refreshes stale timelines first, then matches each event
to its organisation's timeline. Each mismatch class is resolved by a
calibrated or deterministic step, never a new reject or re-attribute path:

| Class | Resolution |
|---|---|
| `wrong_organisation` | `check-victim-attribution` (calibrated); the finding shows that judgement's score and outcome |
| `not_an_incident` | `check-events` (calibrated event reality); the finding shows its score and outcome |
| `timeline_gap` | the timeline is refreshed before matching; a gap that remains is a real incident the research misses - the event is kept, report only |
| date conflict | re-dated to the incident's date when the incident date is more precise than the stored one (`redate_eligible`: finer precision, or a stated day against a stored publication/discovery day; a day stated in the records is never overruled) and the match probability is at or above a calibrated cut-off (`scripts/calibrate_timeline_redates.py`); audited in `TimelineRedates`, snapshotted, labels required for `--apply` |
| incident with no event | `--queue-gaps` (existing): search once, queue reporting as a raw record |

Incident dates are parsed from the start of the incident's date field, before
any parenthesis or semicolon ("2018-2019 (reported 25 May 2021)" is 2018,
not the reporting date) and including ranges whose start carries no year
("27 February-11 August 2020" is 27 February 2020). Both shapes produced
false conflicts before.

On the copy after re-attribution: 47 organisations, 97 events, 59 matched;
unmatched 25 `timeline_gap` (kept), 9 `wrong_organisation` (2 below the victim
cut-off, 7 confirmed by it), 4 `not_an_incident` (all scored real by the
latest stored event-reality judgement); 86 coverage gaps; 8 date conflicts,
of which **2** have a more precise incident date (UWA's DieNet DDoS claim
stored as 2023, timeline August 2025; a Western Sydney University "10k
students" story stored 2020-07-15, timeline January-February 2025).

**Re-dating stays report-only.** Two eligible conflicts cannot calibrate a
cut-off (the procedure needs at least 5 labelled re-dates at 95% precision),
so they were not labelled; `calibrate_timeline_redates.py` is ready for when
there are enough. Do not pass `--apply` to `reconcile-timelines` until then:
with no labels file it refuses (exit 2).

The "not_an_incident" outcome reads the latest stored event-reality score;
those scores predate the v2 timelines until `check-events` runs again.

## Commands, in refresh order

```bash
python scripts/dedup_admin.py research-timelines                  # v2 questions; raises if Perplexity is unavailable
python scripts/dedup_admin.py check-sources --apply
python scripts/dedup_admin.py classify-pages
python scripts/dedup_admin.py check-events --apply
python scripts/dedup_admin.py check-victims
python scripts/dedup_admin.py check-victim-attribution --apply    # calibrated; needs the labels file
python scripts/dedup_admin.py check-duplicates --apply
python scripts/dedup_admin.py classify-entities --stale-only       # roles after the merges and splits
python scripts/dedup_admin.py reconcile-timelines --queue-gaps     # no --apply until re-dating is calibrated
# after the recurrence check (run_full_pipeline.py does this as run_role_refresh)
python scripts/dedup_admin.py classify-entities --stale-only
python scripts/dedup_admin.py check-victims
python scripts/dedup_admin.py check-victim-attribution --apply    # settles earlier flags
python scripts/dedup_admin.py roles-status                         # expect 0 outdated
```

Recalibrating (new model or question version): `calibrate_victim_attribution.py
--db <labelling snapshot> sample | packets --out <dir> | calibrate`.
