# Labelling AI-event pairs: the same incident, or two?

For `scripts/calibrate_ai_duplicates.py packet`. Each item is two stored events (`event_a`, `event_b`) in which AI played a
part. You see their titles, dates (and how each date was determined), recorded victim, and every record's page text and
research. You do **not** see any model's score, the sampling stratum, or the AI category a model gave. Search the web for
the incident as needed; the evidence stored is often a privacy policy, a menu page or another story.

Output: a CSV with the columns `pair_id,label,rationale,corroboration,labeller`, one row per item.

| Label | Meaning |
|---|---|
| `same` | One real-world incident reported again: follow-up coverage, commentary or analysis of it, a government, regulator or company response, an explainer, a later disclosure of more detail. This holds even if the reports name a missing, vague or different organisation as the victim. One supplier or tool breach reported once per customer is one incident (project convention). |
| `different` | Separate occasions, victims, systems or actors. This includes a second incident disclosed later that involves the same AI system, vendor or operator, and a report of a different matter altogether. |
| `unsure` | After searching, the evidence does not settle it. `unsure` pairs are left out of the calibration. |

Conventions, as for `evaluation/duplicate_labels.csv`:

* Judge the incident the records describe, not the recorded victim: it may be missing, vague (a sector or "an agency"), a
  regulator, the AI company, or wrong.
* A date built from a publication date, a "found" date or a month-only placeholder says little about when the incident
  happened; a day stated in the records or in reliable reporting says a lot.
* The same AI vendor or product in both events is **not** enough for `same`; say what connects the two (the same target
  system, the same occasion, the same disclosure).
* Write the decisive reason in `rationale` (one or two sentences), put `packet` in `corroboration` unless a web search settled
  it (then the URL), and name yourself and the date in `labeller`.
* Do not look at, or ask for, any model score or any list of expected answers.
