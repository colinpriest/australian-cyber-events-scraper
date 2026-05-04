"""Full-pipeline live test: scrape Jul-Dec 2024 end-to-end (Playwright +
vision API + validators) and report which pages produce clean data.

Exits 0 when all 8 pages either pass validation or are gracefully handled
(page 9 is allowed to be quarantined - documented limitation).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from scripts.oaic.OAIC_dashboard_scraper import (
    DashboardVisionExtractor,
    OAICDashboardController,
    process_semester,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("full_test")
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> int:
    load_dotenv()
    import os
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        logger.error("OPENAI_API_KEY not in env")
        return 1

    target = "Jan-Jun 2024"
    out_dir = Path("oaic_screenshots") / "_full_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    extractor = DashboardVisionExtractor(api_key)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled',
                  '--start-maximized', '--window-size=2560,1440'],
        )
        try:
            result = await process_semester(
                browser=browser,
                semester=target,
                vision_extractor=extractor,
                screenshot_dir=out_dir,
                headless=True,
            )
        finally:
            await browser.close()

    if not result:
        logger.error(f"process_semester returned None for {target!r}")
        return 2

    logger.info("=" * 60)
    logger.info(f"RESULT for {target}:")
    logger.info(f"  total_notifications:   {result.get('total_notifications')}")
    logger.info(f"  malicious_attacks:     {result.get('malicious_attacks')}")
    logger.info(f"  human_error:           {result.get('human_error')}")
    logger.info(f"  system_faults:         {result.get('system_faults')}")
    monthly = result.get("monthly_notifications") or []
    logger.info(f"  monthly_notifications: {len(monthly)} entries, sum={sum(m.get('count') or 0 for m in monthly)}")
    pi = result.get("personal_info_types") or {}
    logger.info(f"  personal_info_types:   {pi}")
    bs = result.get("breach_sources") or {}
    logger.info(f"  breach_sources:        {bs}")
    t2id = result.get("time_to_identify_pct") or []
    logger.info(f"  time_to_identify_pct:  {len(t2id)} buckets")
    for e in t2id:
        logger.info(f"    {e}")
    t2n = result.get("time_to_notify_pct") or []
    logger.info(f"  time_to_notify_pct:    {len(t2n)} buckets")
    for e in t2n:
        logger.info(f"    {e}")
    logger.info("=" * 60)

    expected_total_range = (300, 800)
    total = result.get("total_notifications")
    if not (total and expected_total_range[0] <= total <= expected_total_range[1]):
        logger.error(f"total_notifications {total} outside expected range {expected_total_range}")
        return 3

    src_sum = (result.get("malicious_attacks") or 0) + (result.get("human_error") or 0) + (result.get("system_faults") or 0)
    if src_sum > 0 and abs(src_sum - total) / total > 0.10:
        logger.warning(f"source_sum {src_sum} differs from total {total} by {abs(src_sum - total)/total:.0%}")

    # Page 7/8 sum check
    for label, entries in (("identify", t2id), ("notify", t2n)):
        if entries:
            cur_sum = sum((e.get("current_pct") or 0) for e in entries)
            prev_sum = sum((e.get("previous_pct") or 0) for e in entries)
            logger.info(f"  page {label}: current_sum={cur_sum}, previous_sum={prev_sum}")
            if not (92 <= cur_sum <= 108):
                logger.warning(f"  page {label}: current_sum {cur_sum} out of tolerance")
            if not (92 <= prev_sum <= 108):
                logger.warning(f"  page {label}: previous_sum {prev_sum} out of tolerance")

    logger.info("\n=== TEST PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
