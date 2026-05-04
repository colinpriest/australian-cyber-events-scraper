"""Targeted re-scrape of OAIC dashboard page 9 (Top sectors) for 2022 H1 -
2025 H2, this time clicking the 'All breaches' filter before screenshotting
so we capture the actual unfiltered notification counts (not System Fault
or rank-only views).

Reuses the existing OAICDashboardController for navigation; explicitly
clicks the 'All' filter chiclet on page 9 before each capture, then routes
the screenshot through the same vision extractor.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from openai import AsyncOpenAI
from playwright.async_api import async_playwright

from scripts.oaic.OAIC_dashboard_scraper import (
    OAICDashboardController,
    generate_known_semesters,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


PAGE9_PROMPT = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's
"Top 5 sectors by source of breaches" page (page 9), with the "All breaches"
filter selected so unfiltered totals are visible.

The chart shows the top 5 industry sectors by total notifications. Each row has:
- a sector NAME (e.g. "Australian Government", "Education",
  "Finance (incl. superannuation)", "Health service providers",
  "Legal, accounting & management services");
- a horizontal bar (or stacked bar by sub-category);
- a NUMERIC COUNT printed against the bar - this is the actual notification
  count (typically 20-200 for a top-5 sector in one semester).

Extract for each visible sector its NOTIFICATION COUNT (the displayed total
number), NOT its rank position or its sub-category counts.

CRITICAL:
- Real top-5 sector counts are usually 20+. If you find yourself returning
  values of 1-5, you have either read a rank position or extracted from
  a filtered sub-view. Look harder for the larger total number.
- If the chart is stacked (cyber incidents / human error / system fault per
  sector), the SECTOR'S TOTAL is what you want - sum the stacks if needed.
- If a count is genuinely missing/illegible, return null. Do NOT guess.

Return ONLY valid JSON (no commentary):
{
  "top_sectors": [
    {
      "sector": "<exact sector name>",
      "total_notifications": <int total count or null>
    },
    ...up to 5 entries...
  ]
}
"""


async def click_all_breaches_filter(controller: OAICDashboardController) -> bool:
    """Click the 'All' / 'All breaches' filter chiclet visible on page 9."""
    if not controller.powerbi_frame:
        return False
    candidates = ('All breaches', 'All')
    for label in candidates:
        try:
            ok = await controller.click_filter_option(label)
            if ok:
                logger.info(f"  Filter clicked: {label!r}")
                await asyncio.sleep(2)
                # Re-expand in case clicking the filter exited fullscreen
                await controller._maximize_powerbi_view()
                await asyncio.sleep(1)
                return True
        except Exception as e:
            logger.debug(f"Filter click {label!r} failed: {e}")
    logger.warning("  Could not click 'All breaches' filter - capturing whatever is visible")
    return False


async def call_vision(client: AsyncOpenAI, image_bytes: bytes) -> Dict:
    img_b64 = base64.b64encode(image_bytes).decode('utf-8')
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PAGE9_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
        }],
        max_tokens=2048,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def patch_json(target_file: Path, updates: Dict[tuple, List[Dict]]) -> int:
    data = json.loads(target_file.read_text(encoding='utf-8'))
    n = 0
    for record in data:
        key = (record.get('year'), record.get('period'))
        if key in updates and updates[key]:
            old_top = record.get('top_sectors')
            new_top = updates[key]
            if old_top != new_top:
                record['top_sectors'] = new_top
                n += 1
    if n:
        target_file.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')
    return n


async def process_semester(browser, semester: str, ai: AsyncOpenAI,
                           shot_dir: Path) -> Optional[List[Dict]]:
    """Open dashboard, select semester, go to page 9, click 'All', capture, extract."""
    controller = OAICDashboardController(headless=True, screenshot_dir=str(shot_dir),
                                         browser=browser)
    try:
        await controller.launch_browser()
        if not await controller.navigate_to_dashboard():
            logger.error(f"[{semester}] navigate_to_dashboard failed")
            return None
        await controller.navigate_to_page(2)
        await asyncio.sleep(2)
        await controller._maximize_powerbi_view()

        if not await controller.select_semester(semester):
            logger.error(f"[{semester}] select_semester failed")
            return None

        await controller.navigate_to_page(9)
        await asyncio.sleep(3)
        await controller._maximize_powerbi_view()
        await asyncio.sleep(1)

        # Click 'All breaches' filter so unfiltered counts are visible
        await click_all_breaches_filter(controller)

        # Save the screenshot for audit
        shot_bytes = await controller.capture_page_screenshot(9, semester, suffix='all_filter')

        # Extract via vision
        try:
            result = await call_vision(ai, shot_bytes)
        except Exception as e:
            logger.error(f"[{semester}] vision API error: {e}")
            return None

        sectors = []
        for entry in (result.get('top_sectors') or []):
            sectors.append({
                'sector': entry.get('sector'),
                'notifications': entry.get('total_notifications'),
            })
        logger.info(f"[{semester}] extracted {len(sectors)} sectors")
        for s in sectors:
            logger.info(f"   {s['sector']!r} -> {s['notifications']}")
        return sectors
    finally:
        await controller.close()


async def main():
    load_dotenv()
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    # Process all semesters from 2022 onwards (the broken set).
    semesters = [s for s in generate_known_semesters() if int(s.split()[-1]) >= 2022]
    logger.info(f"Will rescrape page 9 for: {semesters}")

    ai = AsyncOpenAI(api_key=api_key)
    ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    shot_dir = Path('oaic_screenshots') / f'page9_rescrape_{ts}'

    extracted: Dict[tuple, List[Dict]] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--start-maximized',
                '--window-size=2560,1440',
            ],
        )
        try:
            sem = asyncio.Semaphore(3)

            async def run_one(semester: str):
                async with sem:
                    sectors = await process_semester(browser, semester, ai, shot_dir)
                    if sectors:
                        # Parse year + period from "Jan-Jun 2025" / "Jul-Dec 2024"
                        period = 'H1' if semester.startswith('Jan-Jun') else 'H2'
                        year = int(semester.split()[-1])
                        extracted[(year, period)] = sectors

            await asyncio.gather(*(run_one(s) for s in semesters))
        finally:
            await browser.close()

    if not extracted:
        sys.exit("No semesters processed successfully - aborting JSON patch")

    # Patch the most-recent stats file
    import glob
    files = sorted(glob.glob('oaic_cyber_statistics_*.json'),
                   key=os.path.getmtime, reverse=True)
    if not files:
        sys.exit("No oaic_cyber_statistics_*.json found")
    target = Path(files[0])
    patched = patch_json(target, extracted)
    logger.info(f"Patched {patched} period(s) in {target}")
    logger.info("Now run: python scripts/build_static_dashboard.py")


if __name__ == '__main__':
    asyncio.run(main())
