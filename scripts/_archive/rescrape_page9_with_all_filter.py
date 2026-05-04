"""Re-scrape OAIC page 9 with the 'All' filter actually clicked.

The previous attempt failed because click_filter_option() didn't reliably
activate the 'All' button at the top of page 9. This script uses Playwright
locators directly to find the All button by exact text and verify it became
active before screenshotting.

Then re-extracts per-cause counts (Human Error / Malicious or Criminal Attack
/ System Fault) and sums them per sector.
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
from playwright.async_api import async_playwright

from scripts.oaic.OAIC_dashboard_scraper import (
    OAICDashboardController,
    generate_known_semesters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


PROMPT = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Top 5
sectors by source of breaches" page (page 9), with the 'All' filter selected.

The page shows THREE COLUMNS (left to right): "Human error",
"Malicious or criminal attack", "System fault". Each column shows the SAME
five sector icons (Australian Government, Education, Finance,
Health service providers, Legal/accounting/management services) in the same
left-to-right order. Above each icon is a small bar with a numeric count.

Read EVERY visible numeric label. Real cell values are 0-100. Where there's
no bar above an icon, the count is 0.

Return ONLY valid JSON:
{
  "sectors": [
    {"sector": "Australian Government", "human_error": <int>, "malicious_or_criminal": <int>, "system_fault": <int>},
    {"sector": "Education", ...},
    {"sector": "Finance (incl. superannuation)", ...},
    {"sector": "Health service providers", ...},
    {"sector": "Legal, accounting & management services", ...}
  ]
}

CRITICAL: extract numbers AS DISPLAYED. If you see "47" above the Health
icon under Human error, return human_error=47 for Health, not 1 or 5.
"""


async def click_all_filter_robust(controller: OAICDashboardController) -> bool:
    """Click the 'All' button at the top of page 9 using direct Playwright
    locators. Verify the filter changed by re-querying the active state.
    """
    frame = controller.powerbi_frame
    if not frame:
        return False

    # Try several specific selectors targeting the 'All' button on page 9
    selectors = [
        'button:has-text("All"):not(:has-text("Cyber")):not(:has-text("Malicious")):not(:has-text("Human")):not(:has-text("Fault"))',
        '[role="button"]:has-text("All"):not(:has-text("Cyber"))',
        # Power BI sometimes wraps button text
        '*:has-text("All"):not(:has-text("Cyber")):not(:has-text("Malicious")):not(:has-text("Human")):not(:has-text("Fault"))',
    ]

    for sel in selectors:
        try:
            elements = await frame.query_selector_all(sel)
        except Exception:
            continue
        for el in elements:
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text() or '').strip()
                if text != 'All':
                    continue
                box = await el.bounding_box()
                # The filter buttons sit near the top of the page (y < 200 typically)
                if not box or box['y'] > 250:
                    continue
                await el.scroll_into_view_if_needed()
                await el.click()
                logger.info(f"  Clicked 'All' filter button at y={box['y']:.0f}")
                await asyncio.sleep(2.5)
                return True
            except Exception as e:
                logger.debug(f"  Click attempt failed: {e}")
                continue

    logger.warning("  Could not locate 'All' filter button")
    return False


async def call_vision(client: AsyncOpenAI, image_bytes: bytes) -> Optional[Dict]:
    img_b64 = base64.b64encode(image_bytes).decode('utf-8')
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


async def verify_semester_applied(controller: OAICDashboardController,
                                   target: str, attempts: int = 6) -> bool:
    """After select_semester, the dashboard takes a moment to actually
    refresh. Verify by re-reading the slicer trigger text and confirming it
    shows the target. Retry-with-sleep if not. Returns False if it never
    converges - in which case the caller MUST NOT capture data from the
    page (silent semester-selection failure pattern).
    """
    target_norm = controller._normalize_semester(target)
    for attempt in range(attempts):
        await asyncio.sleep(1.5)
        try:
            dropdown = await controller._find_semester_dropdown()
            if not dropdown:
                continue
            text = (await dropdown.inner_text() or '').strip()
            if controller._normalize_semester(text) == target_norm:
                logger.info(f"  Semester verified: trigger now shows {text!r}")
                return True
        except Exception:
            continue
    logger.error(f"  Semester NEVER updated to {target!r} - aborting capture")
    return False


async def process_semester(browser, semester: str, ai: AsyncOpenAI,
                           shot_dir: Path) -> Optional[List[Dict]]:
    controller = OAICDashboardController(headless=True, screenshot_dir=str(shot_dir),
                                         browser=browser)
    try:
        await controller.launch_browser()
        if not await controller.navigate_to_dashboard():
            return None
        await controller.navigate_to_page(2)
        await asyncio.sleep(2)
        await controller._maximize_powerbi_view()
        if not await controller.select_semester(semester):
            logger.error(f"[{semester}] select_semester failed")
            return None

        # CRITICAL: verify the dashboard ACTUALLY applied the semester filter
        # before we capture. Without this, every parallel context can sit on
        # the default Jan-Jun 2025 view and produce identical (wrong) numbers
        # for every semester.
        if not await verify_semester_applied(controller, semester):
            return None

        await controller.navigate_to_page(9)
        await asyncio.sleep(3)
        await controller._maximize_powerbi_view()
        await asyncio.sleep(1)

        # CRITICAL: explicitly click the 'All' filter button using a robust selector
        if not await click_all_filter_robust(controller):
            logger.error(f"[{semester}] could not click 'All' filter - aborting this semester")
            return None
        await controller._maximize_powerbi_view()
        await asyncio.sleep(1)

        shot_bytes = await controller.capture_page_screenshot(9, semester, suffix='ALL_filter_v2')
        result = await call_vision(ai, shot_bytes)
        if not result:
            return None

        # Sum per-cause to get per-sector totals
        sectors = []
        for entry in result.get('sectors') or []:
            name = entry.get('sector')
            if not name:
                continue
            he = int(entry.get('human_error') or 0)
            mc = int(entry.get('malicious_or_criminal') or 0)
            sf = int(entry.get('system_fault') or 0)
            total = he + mc + sf
            sectors.append({'sector': name, 'notifications': total,
                            '_breakdown': {'he': he, 'mc': mc, 'sf': sf}})
        logger.info(f"[{semester}]:")
        for s in sectors:
            b = s['_breakdown']
            logger.info(f"  {s['sector']!r:50s}  HE={b['he']:3d}  MC={b['mc']:3d}  SF={b['sf']:3d}  -> total {s['notifications']}")
        return sectors
    finally:
        await controller.close()


async def main():
    load_dotenv()
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        sys.exit("OPENAI_API_KEY missing")

    # Only the 6 semesters whose top_sectors are still wrong. 2025 H1 is
    # already correct on disk (we verified its numbers match the user's
    # screenshot) so we skip it to avoid overwriting on a verification miss.
    semesters = [s for s in generate_known_semesters()
                 if int(s.split()[-1]) >= 2022 and s != "Jan-Jun 2025"]
    logger.info(f"Will rescrape: {semesters}")

    ai = AsyncOpenAI(api_key=api_key)
    ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    shot_dir = Path('oaic_screenshots') / f'page9_all_v2_{ts}'

    extracted: Dict[tuple, List[Dict]] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled',
                  '--start-maximized', '--window-size=2560,1440'],
        )
        try:
            sem = asyncio.Semaphore(3)

            async def run_one(semester: str):
                async with sem:
                    sectors = await process_semester(browser, semester, ai, shot_dir)
                    if sectors:
                        period = 'H1' if semester.startswith('Jan-Jun') else 'H2'
                        year = int(semester.split()[-1])
                        extracted[(year, period)] = sectors

            await asyncio.gather(*(run_one(s) for s in semesters))
        finally:
            await browser.close()

    if not extracted:
        sys.exit("Nothing extracted - aborting")

    # Patch newest stats file
    files = sorted(glob.glob('oaic_cyber_statistics_*.json'),
                   key=os.path.getmtime, reverse=True)
    target = Path(files[0])
    data = json.loads(target.read_text(encoding='utf-8'))
    n = 0
    for r in data:
        key = (r.get('year'), r.get('period'))
        if key in extracted:
            r['top_sectors'] = [
                {'sector': s['sector'], 'notifications': s['notifications']}
                for s in extracted[key]
            ]
            n += 1
    target.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')
    logger.info(f"Patched {n} period(s) in {target}")
    logger.info(f"Saved screenshots to {shot_dir}")
    logger.info("Now run: python scripts/build_static_dashboard.py")


if __name__ == '__main__':
    asyncio.run(main())
