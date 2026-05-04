"""Comprehensive re-extract of OAIC dashboard pages 2, 4, 5, 6, 7, 8 from
saved screenshots, with displayed-semester verification on every page.
Populates the most-recent oaic_cyber_statistics_*.json file with all
fields needed by the dashboard's comparison charts.

Page 9 (sector x source matrix) is NOT included here because the saved
screenshots from earlier runs are mostly under the wrong filter (System
Fault). To re-extract page 9 properly, run rescrape_page9_with_all_filter.py
after this completes.
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
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# Use the most recent run that has full page 2-9 captures for all 7 semesters
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

PAGES = [
    ("page_2_Snapshot.png",                "snapshot",
     "Extract everything from the Snapshot page (page 2)."),
    ("page_4_Individuals_affected.png",    "individuals_affected",
     "Extract individuals-affected distribution (page 4)."),
    ("page_5_Personal_information_types.png", "personal_info",
     "Extract kinds of personal information (page 5)."),
    ("page_6_Source_of_breaches.png",      "breach_sources",
     "Extract source of breaches (page 6)."),
    ("page_7_Time_to_identify.png",        "time_to_identify",
     "Extract time-to-identify distribution (page 7)."),
    ("page_8_Time_to_notify.png",          "time_to_notify",
     "Extract time-to-notify distribution (page 8)."),
]


def _norm(s: str) -> str:
    return ''.join(c.lower() for c in (s or '') if c.isalnum())


# ---- Per-page prompts (mirror what's in OAIC_dashboard_scraper.py) ---------

PROMPTS: Dict[str, str] = {

    "snapshot": """\
OAIC dashboard "Snapshot" page (page 2).

Top-left: "Show results for: <Jan-Jun YYYY>" - return EXACTLY in 'displayed_semester'.

Center-top: big number Notifications received and small bar chart by month.
Top-right: donut Sources of data breaches with three slices.
Middle-right: Top 5 sectors with COUNT printed ABOVE each bar and sector
icon below (Aus Gov / Education / Finance / Health / Legal/acct/mgmt).
Lower-left: Cyber incident breakdown horizontal bars (%).
Lower-middle: "<XX>% of data breaches affected 100 people or fewer".
Lower-right: Top causes of human error breaches (3 % values).

Return ONLY valid JSON. Include all fields; use null only when truly unreadable:
{
  "displayed_semester": "Jan-Jun 2025",
  "total_notifications": 532,
  "change_from_previous": "-10%",
  "monthly_notifications": [{"month":"Jan","count":60}, ...],
  "human_error_pct": 37, "malicious_attacks_pct": 59, "system_faults_pct": 3,
  "cyber_incidents": {"phishing_pct":28,"compromised_credentials_pct":21,"ransomware_pct":21,"hacking_pct":17,"brute_force_pct":6,"malware_pct":4},
  "top_sectors": [{"sector":"Health service providers","notifications":96}, ...],
  "small_breaches_100_or_fewer_pct": 67,
  "human_error_causes": {"wrong_recipient_email_pct":44,"unauthorised_disclosure_pct":22,"failure_to_use_bcc_pct":9}
}
top_sectors counts are real notification counts (15-200). NEVER 1-5.
""",

    "individuals_affected": """\
OAIC dashboard "Number of individuals affected by breaches" page (page 4).

Top-left: "Show results for ..." - return EXACTLY in 'displayed_semester'.

Left horizontal-bar chart shows counts per range:
  1 / 2-10 / 11-100 / 101-1,000 / 1,001-5,000 / 5,001-10,000 /
  10,001-25,000 / 25,001-50,000 / 50,001-100,000 / 100,001-250,000 /
  250,001-500,000 / 1,000,001-10,000,000 / Unknown
Each row shows the count at the right end of the bar (or no bar = 0).

Right table "Large-scale data breaches affecting Australians" shows
previous_semester vs current_semester counts at three large buckets.

Return ONLY valid JSON:
{
  "displayed_semester": "Jan-Jun 2025",
  "individuals_affected_distribution": [
    {"range":"1","count":151}, {"range":"2-10","count":94}, ...
  ],
  "large_scale_australians": [
    {"range":"100,001-250,000","previous_semester":3,"current_semester":3},
    ...
  ]
}
Read counts LITERALLY (not ranks). 0 for empty bars; null only if illegible.
""",

    "personal_info": """\
OAIC dashboard "Kinds of personal information involved in breaches" page (page 5).

Top-left: "Show results for ..." - return EXACTLY in 'displayed_semester'.

Top horizontal-bar chart with counts at right ends of bars for these 6 categories:
  Contact information / Identity information / Financial details /
  Health information / Tax File Numbers / Other sensitive information

Bottom small chart "Data breaches involving Digital ID and CDR data":
  Consumer Data Right data / Digital ID information/documents

Return ONLY valid JSON:
{
  "displayed_semester": "Jan-Jun 2025",
  "personal_info_types": {
    "contact_information":456,"identity_information":303,"financial_details":194,
    "health_information":161,"tax_file_numbers":116,"other_sensitive_information":105,
    "consumer_data_right":0,"digital_id":0
  }
}
Numbers are typically 50-500 for the main categories.
""",

    "breach_sources": """\
OAIC dashboard "Source of breaches" page (page 6) with "All breaches" radio selected.

Top-left: "Show results for ..." - return EXACTLY in 'displayed_semester'.

Chart "Source of data breaches - all" shows 3 categories x 2 bars
(previous semester vs current semester). Numeric label above each bar:
  Human error                    (e.g. previous=171, current=193)
  Malicious or criminal attack   (e.g. previous=410, current=308)
  System fault                   (e.g. previous=12, current=17)

Return ONLY valid JSON:
{
  "displayed_semester": "Jan-Jun 2025",
  "current_period_label":"Jan-Jun 2025","previous_period_label":"Jul-Dec 2024",
  "breach_sources": {
    "human_error": {"current_period":193,"previous_period":171},
    "malicious_attack": {"current_period":308,"previous_period":410},
    "system_fault": {"current_period":17,"previous_period":12}
  }
}
""",

    "time_to_identify": """\
OAIC dashboard "Time taken to identify breaches" page (page 7), "By time taken only" tab.

Top-left: "Show results for ..." - return EXACTLY in 'displayed_semester'.

Chart shows pairs of bars (previous vs current semester) for each time bucket.
Each bar labelled with PERCENTAGE.

Time buckets typically: Unknown / Less than 1 hour / 1-24 hours /
1-7 days / 8-30 days / More than 30 days.

Return ONLY valid JSON:
{
  "displayed_semester":"Jan-Jun 2025",
  "current_period_label":"Jan-Jun 2025","previous_period_label":"Jul-Dec 2024",
  "time_to_identify_pct":[
    {"bucket":"Unknown","current_pct":1,"previous_pct":1},
    {"bucket":"Less than 1 hour","current_pct":0,"previous_pct":0},
    {"bucket":"1-24 hours","current_pct":5,"previous_pct":4},
    {"bucket":"1-7 days","current_pct":32,"previous_pct":28},
    {"bucket":"8-30 days","current_pct":30,"previous_pct":31},
    {"bucket":"More than 30 days","current_pct":32,"previous_pct":36}
  ]
}
""",

    "time_to_notify": """\
OAIC dashboard "Time taken to notify the OAIC of breaches" page (page 8), "By time taken only" tab.

Top-left: "Show results for ..." - return EXACTLY in 'displayed_semester'.

Chart shows pairs of bars (previous vs current semester) for each time bucket.
Each bar labelled with PERCENTAGE.

Time buckets typically: Unknown / <= 10 days / 11-20 days / 21-30 days / > 30 days.

Return ONLY valid JSON:
{
  "displayed_semester":"Jan-Jun 2025",
  "current_period_label":"Jan-Jun 2025","previous_period_label":"Jul-Dec 2024",
  "time_to_notify_pct":[
    {"bucket":"Unknown","current_pct":0,"previous_pct":1},
    {"bucket":"<= 10 days","current_pct":33,"previous_pct":29},
    {"bucket":"11-20 days","current_pct":17,"previous_pct":16},
    {"bucket":"21-30 days","current_pct":27,"previous_pct":27},
    {"bucket":"> 30 days","current_pct":23,"previous_pct":28}
  ]
}
""",
}


async def call_vision(client: AsyncOpenAI, prompt: str, image_path: Path) -> Optional[Dict]:
    img_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
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
        logger.error(f"  vision API error ({image_path.name}): {e}")
        return None


async def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    if not SCREENSHOT_DIR.exists():
        sys.exit(f"Missing {SCREENSHOT_DIR}")

    client = AsyncOpenAI(api_key=api_key)
    sem = asyncio.Semaphore(6)
    rejected: List[str] = []
    extracted: Dict[Tuple[int, str], Dict] = {(y, p): {} for _, y, p, _ in SEMESTERS}

    async def process(folder: str, year: int, period: str, expected_label: str,
                      page_filename: str, key: str):
        path = SCREENSHOT_DIR / folder / page_filename
        if not path.exists():
            logger.warning(f"  [{expected_label}/{key}] missing: {path}")
            return
        async with sem:
            data = await call_vision(client, PROMPTS[key], path)
        if not data:
            rejected.append(f"{expected_label}/{key}: vision API failed")
            return
        ds = data.get("displayed_semester") or ""
        if _norm(ds) != _norm(expected_label):
            rejected.append(
                f"{expected_label}/{key}: screenshot showed {ds!r} - REJECTED"
            )
            return
        extracted[(year, period)][key] = data
        logger.info(f"  [{expected_label}/{key}] OK")

    tasks = []
    for folder, year, period, label in SEMESTERS:
        for page_filename, key, _desc in PAGES:
            tasks.append(process(folder, year, period, label, page_filename, key))
    await asyncio.gather(*tasks)

    if rejected:
        logger.warning("REJECTED extractions:")
        for r in rejected:
            logger.warning(f"  {r}")

    # Patch the newest stats file with all fields
    files = sorted(glob.glob("oaic_cyber_statistics_*.json"),
                   key=os.path.getmtime, reverse=True)
    target = Path(files[0])
    data = json.loads(target.read_text(encoding="utf-8"))
    n_patched = 0

    for r in data:
        key = (r.get("year"), r.get("period"))
        if key not in extracted or not extracted[key]:
            continue
        bag = extracted[key]
        # Snapshot fields (highest priority - authoritative for top_sectors etc.)
        snap = bag.get("snapshot")
        if snap:
            for f in ("total_notifications", "change_from_previous", "monthly_notifications",
                      "human_error_pct", "malicious_attacks_pct", "system_faults_pct",
                      "cyber_incidents", "top_sectors", "small_breaches_100_or_fewer_pct",
                      "human_error_causes"):
                if snap.get(f) is not None:
                    r[f] = snap[f]
        # Page 4
        ia = bag.get("individuals_affected")
        if ia:
            if ia.get("individuals_affected_distribution"):
                r["individuals_affected_distribution"] = ia["individuals_affected_distribution"]
            if ia.get("large_scale_australians"):
                r["large_scale_australians"] = ia["large_scale_australians"]
        # Page 5
        pi = bag.get("personal_info")
        if pi and pi.get("personal_info_types"):
            r["personal_info_types"] = pi["personal_info_types"]
        # Page 6
        bs = bag.get("breach_sources")
        if bs:
            if bs.get("breach_sources"):
                r["breach_sources"] = bs["breach_sources"]
                # Also fold in the absolute counts as primary source/count fields
                src = bs["breach_sources"]
                if isinstance(src.get("human_error"), dict):
                    r["human_error"] = src["human_error"].get("current_period")
                if isinstance(src.get("malicious_attack"), dict):
                    r["malicious_attacks"] = src["malicious_attack"].get("current_period")
                if isinstance(src.get("system_fault"), dict):
                    r["system_faults"] = src["system_fault"].get("current_period")
        # Page 7
        ti = bag.get("time_to_identify")
        if ti and ti.get("time_to_identify_pct"):
            r["time_to_identify_pct"] = ti["time_to_identify_pct"]
        # Page 8
        tn = bag.get("time_to_notify")
        if tn and tn.get("time_to_notify_pct"):
            r["time_to_notify_pct"] = tn["time_to_notify_pct"]
        n_patched += 1

    target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info(f"\nPatched {n_patched} period(s) in {target}")


if __name__ == "__main__":
    asyncio.run(main())
