"""Re-extract page 7 (Time-to-identify) ONLY from saved screenshots,
using the corrected bucket scheme (<= 10 days / 11-20 days / 21-30 days
/ > 30 days). Replaces the bogus mixed-scheme data in time_to_identify_pct.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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

EXPECTED_BUCKETS = {"Unknown", "<= 10 days", "11-20 days", "21-30 days", "> 30 days"}

PROMPT = """\
OAIC Notifiable Data Breaches dashboard "Time taken to identify breaches"
page (page 7), "By time taken only" tab.

Top-left: "Show results for: <Jan-Jun YYYY>" - return EXACTLY in 'displayed_semester'.

The chart has 5 bucket categories. Pairs of bars (previous vs current
semester) per bucket. Each bar labelled with a percentage. Buckets are
ALWAYS exactly these five, in this order:
  Unknown / <= 10 days / 11-20 days / 21-30 days / > 30 days

Use ASCII "<= 10 days" and "> 30 days" - do NOT use unicode "≤" or any
variant. Use the bucket names EXACTLY as listed above.

Return ONLY valid JSON:
{
  "displayed_semester": "Jan-Jun 2025",
  "time_to_identify_pct": [
    {"bucket": "Unknown",     "current_pct": 5,  "previous_pct": 4},
    {"bucket": "<= 10 days",  "current_pct": 56, "previous_pct": 53},
    {"bucket": "11-20 days",  "current_pct": 8,  "previous_pct": 9},
    {"bucket": "21-30 days",  "current_pct": 4,  "previous_pct": 4},
    {"bucket": "> 30 days",   "current_pct": 27, "previous_pct": 30}
  ]
}

DO NOT invent other bucket names. The chart only has these 5 buckets.
"""


def _norm(s: str) -> str:
    return ''.join(c.lower() for c in (s or '') if c.isalnum())


async def call_vision(client: AsyncOpenAI, image_path: Path) -> Optional[Dict]:
    img_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }],
            max_tokens=1024,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error(f"  vision API error: {e}")
        return None


async def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    client = AsyncOpenAI(api_key=api_key)
    extracted: Dict[tuple, List[Dict]] = {}
    rejected: List[str] = []

    sem = asyncio.Semaphore(4)

    async def process(folder, year, period, expected):
        path = SCREENSHOT_DIR / folder / "page_7_Time_to_identify.png"
        if not path.exists():
            logger.warning(f"  [{expected}] missing: {path}")
            return
        async with sem:
            data = await call_vision(client, path)
        if not data:
            rejected.append(f"{expected}: vision failed")
            return
        if _norm(data.get("displayed_semester") or "") != _norm(expected):
            rejected.append(f"{expected}: showed {data.get('displayed_semester')!r}")
            return
        ttip = data.get("time_to_identify_pct") or []
        bucket_names = {e.get("bucket") for e in ttip}
        # Allow either ASCII or unicode form for ≤ / > but normalize to ASCII
        normalized = []
        ok = True
        for e in ttip:
            b = (e.get("bucket") or "").strip()
            # Normalize unicode variants to ASCII
            b = b.replace("≤", "<=").replace("–", "-").replace("—", "-")
            b = b.replace("≤", "<=")
            normalized.append({"bucket": b,
                               "current_pct": e.get("current_pct"),
                               "previous_pct": e.get("previous_pct")})
        # Filter to only expected buckets
        normalized = [e for e in normalized if e["bucket"] in EXPECTED_BUCKETS]
        if len(normalized) < 4:
            rejected.append(f"{expected}: only {len(normalized)} valid buckets ({bucket_names!r})")
            return
        extracted[(year, period)] = normalized
        logger.info(f"  [{expected}] OK: {[(b['bucket'], b['current_pct']) for b in normalized]}")

    await asyncio.gather(*(process(*s) for s in SEMESTERS))

    if rejected:
        logger.warning("REJECTED:")
        for r in rejected:
            logger.warning(f"  {r}")

    if not extracted:
        sys.exit("Nothing extracted")

    # Patch JSON
    files = sorted(glob.glob("oaic_cyber_statistics_*.json"),
                   key=os.path.getmtime, reverse=True)
    target = Path(files[0])
    data = json.loads(target.read_text(encoding="utf-8"))
    n = 0
    for r in data:
        key = (r.get("year"), r.get("period"))
        if key in extracted:
            r["time_to_identify_pct"] = extracted[key]
            n += 1
    target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info(f"Patched {n} period(s) in {target}")


if __name__ == "__main__":
    asyncio.run(main())
