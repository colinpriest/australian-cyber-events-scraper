"""Re-extract OAIC dashboard page 2 (Snapshot) from saved screenshots.

Page 2 has the AUTHORITATIVE per-semester counts that earlier scrape attempts
were failing to find on page 9. It contains:

  * Total notifications received                     (e.g. 532)
  * Notifications by month bar chart                 (Jan=60, Feb=83 ...)
  * Source-of-breaches donut % (Human/Malicious/System)
  * Cyber incident breakdown % (Phishing 28%, Ransomware 21% ...)
  * Top 5 sectors with COUNTS (96, 73, 67, 38, 37)
  * Top human-error causes
  * '% of data breaches affecting ≤100 people' (eg 67%)

We re-extract from EACH semester's saved page-2 screenshot. To prevent the
silent-semester-selection failure, we ALSO ask the LLM to read back the
'Show results for ...' label in the top-left and reject the data if it
doesn't match the folder label.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Most recent screenshot run that actually has all 7 page_2 captures
SCREENSHOT_DIR = Path("oaic_screenshots/2026-05-03_173023")

SEMESTERS = [
    ("Jan_Jun_2022", 2022, "H1", "Jan-Jun 2022"),
    ("Jul_Dec_2022", 2022, "H2", "Jul-Dec 2022"),
    ("Jan_Jun_2023", 2023, "H1", "Jan-Jun 2023"),
    ("Jul_Dec_2023", 2023, "H2", "Jul-Dec 2023"),
    ("Jan_Jun_2024", 2024, "H1", "Jan-Jun 2024"),
    ("Jul_Dec_2024", 2024, "H2", "Jul-Dec 2024"),
    ("Jan_Jun_2025", 2025, "H1", "Jan-Jun 2025"),
]

PROMPT = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Snapshot"
page (page 2). Read the entire page carefully.

Top-left: "Show results for: <Jan-Jun YYYY> | <Jul-Dec YYYY>" - this tells
you which semester this snapshot is for. Return that label EXACTLY in
'displayed_semester' - we use it to verify the screenshot is for the
expected period.

Center-top has a big number "Notifications received: <N>" and a small
bar chart "Notifications received by month" with one bar per calendar
month showing a labelled count.

Top-right has a donut chart "Sources of data breaches" with three slices:
Human error %, Malicious or criminal attack %, System fault %.

Middle-right has "Top 5 sectors to notify data breaches, by notifications
received" - this shows 5 horizontal/vertical bars, each with a count
ABOVE the bar AND a sector icon BELOW. The icons identify the sector:
- map of Australia = Australian Government
- graduation cap   = Education
- coin stack       = Finance (incl. superannuation)
- heart            = Health service providers
- scales of justice = Legal, accounting & management services
There may be additional icons (e.g. recruitment) if a non-standard sector is in the top-5.

Lower-left has "Cyber incident breakdown" - horizontal bars with % labels for:
phishing, compromised credentials, ransomware, hacking, brute-force, malware.

Lower-middle says "<XX>% of data breaches affected 100 people or fewer".

Lower-right has "Top causes of human error breaches" - 3 icons with %.

Return ONLY valid JSON in this shape (use null when truly unreadable):

{
  "displayed_semester": "Jan-Jun 2025",
  "total_notifications": 532,
  "monthly_notifications": [
    {"month": "Jan", "count": 60},
    {"month": "Feb", "count": 83},
    ...
  ],
  "sources_donut_pct": {
    "human_error":               37,
    "malicious_or_criminal":     59,
    "system_fault":               3
  },
  "cyber_incident_breakdown_pct": {
    "phishing":                  28,
    "compromised_credentials":   21,
    "ransomware":                21,
    "hacking":                   17,
    "brute_force":                6,
    "malware":                    4
  },
  "top_sectors": [
    {"sector": "Health service providers",                "notifications": 96},
    {"sector": "Finance (incl. superannuation)",          "notifications": 73},
    {"sector": "Australian Government",                   "notifications": 67},
    {"sector": "Education",                               "notifications": 38},
    {"sector": "Legal, accounting & management services", "notifications": 37}
  ],
  "small_breaches_100_or_fewer_pct": 67,
  "top_human_error_causes_pct": {
    "pi_sent_to_wrong_recipient_email":  44,
    "unauthorised_disclosure":            22,
    "failure_to_use_bcc":                  9
  }
}

CRITICAL:
- Numbers are LITERAL counts/percentages displayed on the page. Read them.
- top_sectors counts are real notification counts (15-200). NEVER return 1-5.
- If a value is genuinely missing/illegible, return null - do not guess.
"""


async def call_vision(client: AsyncOpenAI, image_path: Path) -> Optional[Dict]:
    img_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }],
            max_tokens=2048,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error(f"  vision API error: {e}")
        return None


def _norm_sem(s: str) -> str:
    return ''.join(c.lower() for c in (s or '') if c.isalnum())


async def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    if not SCREENSHOT_DIR.exists():
        sys.exit(f"Missing screenshot dir: {SCREENSHOT_DIR}")

    client = AsyncOpenAI(api_key=api_key)
    extracted: Dict[Tuple[int, str], Dict] = {}
    rejected: List[str] = []

    sem = asyncio.Semaphore(4)

    async def process(folder: str, year: int, period: str, expected_label: str):
        path = SCREENSHOT_DIR / folder / "page_2_Snapshot.png"
        if not path.exists():
            logger.warning(f"  [{expected_label}] missing: {path}")
            return
        async with sem:
            data = await call_vision(client, path)
        if not data:
            rejected.append(f"{expected_label}: vision API failed")
            return
        # SAFETY: verify the displayed semester matches the folder
        displayed = (data.get("displayed_semester") or "").strip()
        if _norm_sem(displayed) != _norm_sem(expected_label):
            rejected.append(
                f"{expected_label}: screenshot showed {displayed!r} - REJECTED"
            )
            return
        # Sanity: total_notifications must be in [50, 2000]
        total = data.get("total_notifications")
        if not (isinstance(total, (int, float)) and 50 <= total <= 2000):
            rejected.append(
                f"{expected_label}: total_notifications={total} outside [50, 2000] - REJECTED"
            )
            return
        # Sanity: top_sectors entries must have notification counts in [10, 250]
        ts = data.get("top_sectors") or []
        bad = [s for s in ts
               if isinstance(s.get("notifications"), (int, float))
               and not (10 <= s["notifications"] <= 250)]
        if bad:
            rejected.append(
                f"{expected_label}: {len(bad)}/{len(ts)} top_sectors out of range - REJECTED"
            )
            return
        extracted[(year, period)] = data
        ts_str = ", ".join(f"{s['sector'][:15]}={s['notifications']}" for s in ts)
        logger.info(f"  [{expected_label}] OK total={total} sectors=[{ts_str}]")

    await asyncio.gather(*(process(*s) for s in SEMESTERS))

    if rejected:
        logger.warning("REJECTED extractions:")
        for r in rejected:
            logger.warning(f"  {r}")

    if not extracted:
        sys.exit("Nothing extracted - aborting JSON patch")

    # Patch the newest stats file with all the new fields
    files = sorted(glob.glob("oaic_cyber_statistics_*.json"),
                   key=os.path.getmtime, reverse=True)
    target = Path(files[0])
    data = json.loads(target.read_text(encoding="utf-8"))
    n = 0
    for r in data:
        key = (r.get("year"), r.get("period"))
        if key not in extracted:
            continue
        ex = extracted[key]
        r["top_sectors"] = ex.get("top_sectors") or r.get("top_sectors")
        r["total_notifications"] = ex.get("total_notifications") or r.get("total_notifications")
        # Save additional fields we now have for the cross-comparison plots
        if ex.get("monthly_notifications"):
            r["monthly_notifications"] = ex["monthly_notifications"]
        if ex.get("sources_donut_pct"):
            r["sources_donut_pct"] = ex["sources_donut_pct"]
        if ex.get("cyber_incident_breakdown_pct"):
            r["cyber_incident_breakdown_pct"] = ex["cyber_incident_breakdown_pct"]
        if ex.get("small_breaches_100_or_fewer_pct"):
            r["small_breaches_100_or_fewer_pct"] = ex["small_breaches_100_or_fewer_pct"]
        if ex.get("top_human_error_causes_pct"):
            r["top_human_error_causes_pct"] = ex["top_human_error_causes_pct"]
        n += 1

    target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info(f"Patched {n} period(s) in {target}")


if __name__ == "__main__":
    asyncio.run(main())
