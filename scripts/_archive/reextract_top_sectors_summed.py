"""Re-extract OAIC page-9 'Top 5 sectors by source of breaches' from the
already-saved 'All filter' screenshots, this time summing the per-cause
sub-counts (Human Error + Malicious or Criminal Attack + System Fault) to
produce the real per-sector total.

The page is laid out as three columns (one per cause), each showing five
icons (one per top-5 sector) with a small count bar/label above each icon.
Per-cause counts add up to the sector's true total notification count.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


SCREENSHOT_DIR = Path("oaic_screenshots/page9_rescrape_2026-05-03_210038")

SEMESTERS = [
    ("Jan_Jun_2022", 2022, "H1"),
    ("Jul_Dec_2022", 2022, "H2"),
    ("Jan_Jun_2023", 2023, "H1"),
    ("Jul_Dec_2023", 2023, "H2"),
    ("Jan_Jun_2024", 2024, "H1"),
    ("Jul_Dec_2024", 2024, "H2"),
    ("Jan_Jun_2025", 2025, "H1"),
]

# The five top-5 sector positions, left-to-right within each cause column.
# Order matches the legend in the screenshot's top-left:
SECTOR_ICONS_LEFT_TO_RIGHT = [
    "Australian Government",
    "Education",
    "Finance (incl. superannuation)",
    "Health service providers",
    "Legal, accounting & management services",
]

PROMPT = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Top 5
sectors by source of breaches" page (page 9), with the "All" filter
selected.

The chart is split into THREE COLUMNS (left to right):
  1. "Human error"
  2. "Malicious or criminal attack"
  3. "System fault"

Each column shows the SAME 5 sector icons in the SAME left-to-right order:
  position 1: Australian Government (map of Australia icon)
  position 2: Education (graduation cap icon)
  position 3: Finance (incl. superannuation) (coin stack icon)
  position 4: Health service providers (heart icon)
  position 5: Legal, accounting & management services (scales icon)

Above each icon there is a small bar with a NUMERIC LABEL - the count
of notifications for that sector under that cause for the semester.
Sometimes a sector has NO bar in a column (count is 0); in that case
return 0 (NOT null) for that cell.

Extract the per-cell counts. The sector's TRUE TOTAL for the semester is
the SUM across the three columns; you don't need to compute the sum -
just return all 15 cells (5 sectors x 3 causes).

CRITICAL:
- Real cell values are typically 0-100. If you see numbers like 200+ for
  a single (sector, cause) cell, double-check - that may be the total
  across all sectors not just one.
- If a count is genuinely missing (no bar shown), return 0, not null.
- Read EACH NUMBER LITERALLY - do not infer or estimate.

Return ONLY valid JSON (no commentary):
{
  "sectors": [
    {
      "sector": "Australian Government",
      "human_error":              <int or 0>,
      "malicious_or_criminal":    <int or 0>,
      "system_fault":             <int or 0>
    },
    {"sector": "Education", "human_error": ..., "malicious_or_criminal": ..., "system_fault": ...},
    {"sector": "Finance (incl. superannuation)", ...},
    {"sector": "Health service providers", ...},
    {"sector": "Legal, accounting & management services", ...}
  ]
}
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
        logger.error(f"vision API error for {image_path}: {e}")
        return None


def build_top_sectors_payload(per_cell: Dict) -> List[Dict]:
    """Sum the 3 causes per sector and produce the JSON we patch into the
    OAIC stats file.
    """
    out = []
    for entry in per_cell.get("sectors") or []:
        sector = entry.get("sector")
        if not sector:
            continue
        he = int(entry.get("human_error") or 0)
        mc = int(entry.get("malicious_or_criminal") or 0)
        sf = int(entry.get("system_fault") or 0)
        total = he + mc + sf
        out.append({
            "sector": sector,
            "notifications": total,
            "_breakdown": {
                "human_error": he,
                "malicious_or_criminal": mc,
                "system_fault": sf,
            },
        })
    return out


async def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    if not SCREENSHOT_DIR.exists():
        sys.exit(f"Screenshot dir missing: {SCREENSHOT_DIR}")

    client = AsyncOpenAI(api_key=api_key)

    extracted: Dict[tuple, List[Dict]] = {}

    print("=== Re-extracting per-cause counts and summing ===")
    sem = asyncio.Semaphore(3)

    async def process(folder: str, year: int, period: str):
        path = SCREENSHOT_DIR / folder / "page_9_Top_sectors_all_filter.png"
        if not path.exists():
            print(f"  [{year} {period}] missing screenshot: {path}")
            return
        async with sem:
            data = await call_vision(client, path)
        if not data:
            return
        sectors = build_top_sectors_payload(data)
        extracted[(year, period)] = sectors
        print(f"  [{year} {period}]:")
        for s in sectors:
            b = s["_breakdown"]
            print(f"    {s['sector']!r:50s}  HE={b['human_error']:3d}  MC={b['malicious_or_criminal']:3d}  SF={b['system_fault']:3d}  -> total {s['notifications']}")

    await asyncio.gather(*(process(f, y, p) for f, y, p in SEMESTERS))

    if not extracted:
        sys.exit("No semesters extracted - aborting JSON patch")

    # Patch most recent stats file
    files = sorted(glob.glob("oaic_cyber_statistics_*.json"),
                   key=os.path.getmtime, reverse=True)
    target = Path(files[0])
    data = json.loads(target.read_text(encoding="utf-8"))
    n_patched = 0
    for r in data:
        key = (r.get("year"), r.get("period"))
        if key in extracted:
            r["top_sectors"] = [
                {"sector": s["sector"], "notifications": s["notifications"]}
                for s in extracted[key]
            ]
            n_patched += 1
    target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"\nPatched {n_patched} period(s) in {target}")


if __name__ == "__main__":
    asyncio.run(main())
