# AI involvement - labelling guide

Labels for `evaluation/ai_involvement_labels.csv`, used by
`scripts/calibrate_ai_involvement.py calibrate` to choose the cut-off on
P(AI) = 1 - P(No-AI) and to measure how often the attacker / vulnerability
category is right. Labellers are blind to the model: packets
(`calibrate_ai_involvement.py packet`) carry the evidence Jev saw, never its
probabilities.

## Columns

`deduplicated_event_id,label,rationale,labeller`

- `label`: `ai_attacker`, `ai_vulnerability`, `ai_misuse`, `no_ai` or `unsure`.
- `rationale`: one or two sentences - the decisive evidence, and for AI
  labels whose AI it was. Cite a URL when a web search settled it.
- `labeller`: who labelled (e.g. `claude-opus-5.5 subagent`).

## The decision (same as the question, `ai-involvement-v4`)

1. **Did an AI system play a reported part in THIS incident?** AI means a
   large language model, AI agent, chatbot, generative-AI tool, deepfake or
   machine-learning model. What the reporting says happened counts, including
   a statement by the victim, investigators or authorities that AI was
   (likely) used. A journalist's or commentator's speculation does not; nor
   does AI mentioned only in passing, as a defensive tool, as the victim's
   line of business, or in another story on the same page.
2. **If it did, what part did it play?**
   - `ai_misuse` - there was no attack, manipulation or fault: the incident
     *is* the organisation's own use of AI. An AI system it chose to deploy
     collected, used or disclosed people's personal information unlawfully or
     improperly (in-store facial recognition, scraping photos into its own
     face-recognition tool, running AI on personal data without consent), or
     its staff or contractors disclosed personal or confidential data by
     entering it into an AI tool such as ChatGPT. The AI did what it was
     built (or asked) to do; the wrong is in how it was used.

   Otherwise someone attacked, manipulated or broke something, and the
   question is whose AI it was:
   - `ai_attacker` - on the attacking side: the attacker's own tool
     (AI-written phishing or malware, a deepfake call or video, an AI tool
     that found and exploited an ordinary weakness), or an AI agent that
     itself did the unauthorised access (an AI company's agent that reached
     someone else's portal).
   - `ai_vulnerability` - on the victim's side, and the weak point: a system
     the victim or its supplier owned, ran or had connected to its systems
     was attacked, manipulated or leaked data (prompt injection, a jailbroken
     or tricked chatbot, exposed model, training data, prompts or chat logs,
     data poisoning, a compromised third-party AI tool or integration through
     which attackers got in). This holds even when the attacker also used AI.

## Worked boundaries

| Case | Label |
|---|---|
| OpenAI's research agent bypassed controls on a Services Australia (Medicare statistics) portal | `ai_attacker` |
| A third-party AI tool an employee had connected to the company's Google Workspace was compromised and let attackers in (Vercel, 2026) | `ai_vulnerability` |
| A deepfake video call impersonating the CFO tricked staff into a transfer | `ai_attacker` |
| A customer-service chatbot was tricked into revealing other customers' orders | `ai_vulnerability` |
| A hiring chatbot run by a supplier exposed applicants' records through default credentials (McDonald's / Paradox.ai) | `ai_vulnerability` |
| The Privacy Commissioner found a retailer's in-store facial recognition unlawful (Kmart, Bunnings) | `ai_misuse` |
| A company scraped Australians' photos into its own face-recognition tool (Clearview AI) | `ai_misuse` |
| A contractor uploaded applicants' personal data to ChatGPT (NSW Reconstruction Authority) | `ai_misuse` |
| Users prompted a platform's own image generator to make sexual deepfakes of real people (Grok); no system was attacked | `ai_misuse` (the platform's AI made them on request) |
| An AI company suffered an ordinary credential-stuffing breach of its customer database | `no_ai` |
| Article speculates attackers "may have used AI" with no statement from the victim or investigators | `no_ai` |
| The victim's breach notice says the phishing email was "likely AI-generated" | `ai_attacker` |
| The stored page is about a different incident from the event's title | judge the event named in the title; use a web search |

## When to search

The packet is what Jev saw; it can be thin or about another article. Search
the web for the incident named in the event title when the packet does not
settle the label, and cite what settled it. Use `unsure` only when a search
still leaves it open; `unsure` labels are excluded from calibration and
counted.
