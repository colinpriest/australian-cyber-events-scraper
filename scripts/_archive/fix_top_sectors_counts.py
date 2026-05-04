"""Fix the OAIC `top_sectors[].notifications` field across the 2022 H1 -
2025 H1 semesters where the original LLM extraction silently put rank
position (1-5) instead of the actual notification count from the bar chart.

We use the saved page-9 screenshots and a much more explicit prompt that
demands the integer COUNTS displayed against each bar - typically in the
30-90 range per top-5 sector per semester.

Then we patch the master oaic_cyber_statistics_*.json file (newest first
by mtime) so the dashboard's existing aggregation picks up correct values.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI


SCREENSHOT_DIR = Path("oaic_screenshots/2026-05-03_173023")
SEMESTERS = [
    ("Jan_Jun_2022", 2022, "H1"),
    ("Jul_Dec_2022", 2022, "H2"),
    ("Jan_Jun_2023", 2023, "H1"),
    ("Jul_Dec_2023", 2023, "H2"),
    ("Jan_Jun_2024", 2024, "H1"),
    ("Jul_Dec_2024", 2024, "H2"),
    ("Jan_Jun_2025", 2025, "H1"),
]

PROMPT = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Top 5 sectors
to notify breaches" page (page 9). It shows a horizontal bar chart with the
five sectors with the highest notification counts for the current semester.

Each row has:
- a sector NAME on the left axis (e.g. "Australian Government", "Education",
  "Finance (incl. superannuation)", "Health service providers",
  "Legal, accounting & management services");
- a coloured BAR, whose length encodes the notification count;
- a NUMBER displayed at or near the end of each bar - this is the COUNT
  (typically between 10 and 200 per sector per semester).

Your job: extract for each visible sector its EXACT NOTIFICATION COUNT
(the number printed at/near the bar's end), NOT its rank position.

CRITICAL:
- Real counts are usually two-digit (10-200) per sector per semester.
- If you find yourself returning 1, 2, 3, 4, 5 you have read the rank,
  not the count. Look for the larger number printed against each bar.
- Sometimes the chart breaks each sector into stacked sub-categories
  (cyber incidents / human error / system fault). The sector's TOTAL is
  the sum of its sub-bars; report that total in `total_notifications`.
- If the count is genuinely missing/illegible, return null - DO NOT guess.

Return ONLY valid JSON (no commentary, no markdown fence):
{
  "top_sectors": [
    {
      "sector": "<sector name exactly as displayed>",
      "total_notifications": <int or null - the count, NOT the rank>,
      "cyber_incidents": <int or null>,
      "human_error": <int or null>,
      "system_fault": <int or null>
    },
    ...up to 5 entries...
  ]
}
"""


def call_vision(client: OpenAI, image_path: Path) -> Dict:
    img_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    response = client.chat.completions.create(
        model="gpt-4o",  # full GPT-4o for vision quality
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            }
        ],
        max_tokens=2048,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def sanity_check(top_sectors: List[Dict]) -> List[str]:
    """Return a list of warnings if any sector's reported count looks like a rank."""
    warnings = []
    for entry in top_sectors:
        n = entry.get("total_notifications")
        sector = entry.get("sector") or "<no sector>"
        if n is None:
            continue
        if not isinstance(n, (int, float)):
            warnings.append(f"  {sector!r}: notifications is not numeric: {n!r}")
            continue
        if 1 <= n <= 5:
            warnings.append(
                f"  {sector!r}: notifications={n} - looks like a rank position, "
                f"not a count. Real top-5 sector counts are typically 10-200."
            )
    return warnings


def patch_json_file(path: Path, updates: Dict[tuple, List[Dict]]) -> int:
    """Update top_sectors entries in `path` for periods present in `updates`."""
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for record in data:
        key = (record.get("year"), record.get("period"))
        if key in updates:
            old = record.get("top_sectors") or []
            new = updates[key]
            if old != new:
                record["top_sectors"] = new
                n += 1
    if n:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return n


def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    client = OpenAI(api_key=api_key)

    extracted: Dict[tuple, List[Dict]] = {}
    print("=== Re-extracting top_sectors from saved page-9 screenshots ===")
    for folder, year, period in SEMESTERS:
        path = SCREENSHOT_DIR / folder / "page_9_Top_sectors.png"
        if not path.exists():
            print(f"  [{year} {period}] MISSING: {path}")
            continue
        try:
            result = call_vision(client, path)
        except Exception as e:
            print(f"  [{year} {period}] vision API error: {e}")
            continue
        top = []
        for entry in (result.get("top_sectors") or []):
            top.append({
                "sector": entry.get("sector"),
                "notifications": entry.get("total_notifications"),
            })
        extracted[(year, period)] = top
        warnings = sanity_check(result.get("top_sectors") or [])
        print(f"  [{year} {period}] -> {len(top)} sectors")
        for s in top:
            print(f"      {s['sector']!r:55s} notifications={s['notifications']}")
        for w in warnings:
            print(f"    SANITY WARN:{w}")

    if not extracted:
        sys.exit("No screenshots successfully processed - aborting JSON patch.")

    # Patch the most recent oaic_cyber_statistics_*.json file (the one
    # load_oaic_data picks up via newest-first sort)
    files = sorted(glob.glob("oaic_cyber_statistics_*.json"),
                   key=os.path.getmtime, reverse=True)
    if not files:
        sys.exit("No oaic_cyber_statistics_*.json file found")
    target = Path(files[0])
    print(f"\n=== Patching {target} ===")
    n = patch_json_file(target, extracted)
    print(f"  Updated {n} period(s) with corrected top_sectors counts.")
    print(f"  Now `python scripts/build_static_dashboard.py` to regenerate the chart.")


if __name__ == "__main__":
    main()
