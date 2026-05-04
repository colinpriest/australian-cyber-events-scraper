"""LLM enrichment pass: add OAIC-aligned 'personal_info_types' and
'breach_source_category' columns to DeduplicatedEvents and populate them
from each event's title + description + summary.

This makes the OAIC vs Database comparison charts meaningful:
  - Personal info types (Contact / Identity / Financial / Health / TFN / Other / CDR / Digital ID)
  - Source category (Cyber Incident / Malicious or Criminal Attack / Human Error / System Fault / Other)

Schema migration is idempotent. Re-runnable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


PII_KEYS = {
    "contact_information", "identity_information", "financial_details",
    "health_information", "tax_file_numbers", "other_sensitive_information",
    "consumer_data_right", "digital_id",
}
SOURCE_CATEGORIES = {
    "Cyber Incident",
    "Malicious or Criminal Attack",
    "Human Error",
    "System Fault",
    "Other",
}


PROMPT = """\
You are reading a brief summary of a cyber-security incident. Classify it
two ways, returning ONLY valid JSON.

1. personal_info_types - which OAIC-aligned categories of personal
   information were exposed/accessed/leaked in THIS incident? Return one
   bool per category (true if mentioned, false if not). Be EVIDENCE-BASED:
   only set true if the article text indicates that category was involved.
     - contact_information           (name, email, phone, address)
     - identity_information          (DOB, driver licence, passport, ID number)
     - financial_details             (bank acct, credit card, payment info)
     - health_information            (medical records, treatment info)
     - tax_file_numbers              (TFN)
     - other_sensitive_information   (other sensitive PII not above)
     - consumer_data_right           (CDR data under the CDR regime)
     - digital_id                    (Digital ID/MyGov-style IDs)

2. breach_source_category - one of:
     - "Cyber Incident"               (Phishing, ransomware, hacking, malware,
                                       compromised credentials, brute-force,
                                       any malicious cyber attack)
     - "Malicious or Criminal Attack" (Non-cyber malicious - theft of paperwork,
                                       insider data theft, social engineering
                                       not via cyber means)
     - "Human Error"                  (PI sent to wrong recipient, disclosure
                                       without consent, lost paperwork)
     - "System Fault"                 (Software bug, misconfiguration causing
                                       unintended exposure)
     - "Other"                        (Genuinely cannot determine)

Most cyber-news incidents will be "Cyber Incident". Use other categories
only when the article clearly indicates a different cause.

Output schema:
{
  "personal_info_types": {
    "contact_information":         true,
    "identity_information":        false,
    "financial_details":           true,
    "health_information":          false,
    "tax_file_numbers":            false,
    "other_sensitive_information": false,
    "consumer_data_right":         false,
    "digital_id":                  false
  },
  "breach_source_category": "Cyber Incident"
}
"""


def ensure_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")]
    added = 0
    if "personal_info_types_json" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN personal_info_types_json TEXT")
        added += 1
    if "breach_source_category" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN breach_source_category VARCHAR(40)")
        added += 1
    conn.commit()
    conn.close()
    if added:
        logger.info(f"Schema migration: added {added} column(s) to DeduplicatedEvents")


def load_unclassified(db_path: str, limit: Optional[int], force: bool) -> List[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = "status='Active'"
    if not force:
        where += " AND (personal_info_types_json IS NULL OR breach_source_category IS NULL)"
    q = f"""
        SELECT deduplicated_event_id, title, description, summary
        FROM DeduplicatedEvents WHERE {where} ORDER BY event_date DESC
    """
    if limit:
        q += f" LIMIT {limit}"
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    return rows


async def classify(client: AsyncOpenAI, ev: dict) -> Optional[dict]:
    title = (ev.get("title") or "")[:200]
    description = (ev.get("description") or "")[:1500]
    summary = (ev.get("summary") or "")[:1000]
    user_text = f"Title: {title}\n\nDescription: {description}\n\nSummary: {summary}"
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user",   "content": user_text},
            ],
            max_tokens=512,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        pii = data.get("personal_info_types") or {}
        cat = data.get("breach_source_category")
        if cat not in SOURCE_CATEGORIES:
            cat = "Other"
        pii_complete = {k: bool(pii.get(k, False)) for k in PII_KEYS}
        return {
            "deduplicated_event_id": ev["deduplicated_event_id"],
            "personal_info_types_json": json.dumps(pii_complete),
            "breach_source_category": cat,
        }
    except Exception as e:
        logger.debug(f"  enrich error for {ev['deduplicated_event_id']}: {e}")
        return None


async def enrich(db_path: str, limit: Optional[int], concurrency: int, force: bool) -> int:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    ensure_schema(db_path)
    events = load_unclassified(db_path, limit=limit, force=force)
    if not events:
        logger.info("No events need enrichment - already up to date.")
        return 0
    logger.info(f"Will enrich {len(events)} event(s) with concurrency={concurrency}")

    client = AsyncOpenAI(api_key=api_key)
    sem = asyncio.Semaphore(concurrency)
    results: List[dict] = []
    progress = {"done": 0}

    async def run_one(ev):
        async with sem:
            r = await classify(client, ev)
            progress["done"] += 1
            if progress["done"] % 50 == 0:
                logger.info(f"  ...{progress['done']}/{len(events)} processed")
            if r:
                results.append(r)

    await asyncio.gather(*(run_one(e) for e in events))

    conn = sqlite3.connect(db_path)
    n_updated = 0
    for r in results:
        cur = conn.execute(
            "UPDATE DeduplicatedEvents "
            "SET personal_info_types_json = ?, breach_source_category = ? "
            "WHERE deduplicated_event_id = ?",
            (r["personal_info_types_json"], r["breach_source_category"],
             r["deduplicated_event_id"]),
        )
        if cur.rowcount:
            n_updated += 1
    conn.commit()
    conn.close()
    logger.info(f"Updated {n_updated}/{len(events)} rows")
    return n_updated


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="instance/cyber_events.db")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    n = asyncio.run(enrich(args.db, args.limit, args.concurrency, args.force))
    print(f"Enriched {n} events.")


if __name__ == "__main__":
    main()
