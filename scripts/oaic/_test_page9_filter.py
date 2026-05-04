"""Focused live test: navigate the OAIC dashboard, select a non-default
semester (Jul-Dec 2024), navigate to page 9, and verify that the period
slicer DOES NOT drift after the per-page filter handling. Exits 0 on
success, non-zero on failure. Saves the page-9 screenshot for visual
inspection.

Usage:
    python scripts/oaic/_test_page9_filter.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from scripts.oaic.OAIC_dashboard_scraper import OAICDashboardController

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("page9_test")

# Quiet the noisy modules.
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> int:
    load_dotenv()
    target_semester = "Jul-Dec 2024"
    headless = True

    out_dir = Path("oaic_screenshots") / "page9_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--start-maximized',
                '--window-size=2560,1440',
            ],
        )
        try:
            controller = OAICDashboardController(
                headless=headless,
                screenshot_dir=str(out_dir),
                browser=browser,
            )
            await controller.launch_browser()
            if not await controller.navigate_to_dashboard():
                logger.error("Failed to load dashboard")
                return 2

            await controller.navigate_to_page(2)
            await asyncio.sleep(2)
            await controller._maximize_powerbi_view()

            if not await controller.select_semester(target_semester):
                logger.error(f"Could not select {target_semester!r}")
                return 3

            # Verify slicer is on Jul-Dec 2024 right after select.
            if not await controller._verify_dashboard_shows_semester(target_semester):
                logger.error("Slicer didn't land on requested period after select_semester")
                return 4
            logger.info(f"Slicer confirmed on {target_semester!r} after select")

            # Navigate to page 9 and run the per-page filter handling.
            screenshots = await controller.capture_all_pages(target_semester)

            # Final verification: slicer is still on Jul-Dec 2024 at end.
            ok = await controller._verify_dashboard_shows_semester(target_semester)
            logger.info(
                f"Slicer state after capture_all_pages: "
                f"{'OK on ' + target_semester if ok else 'DRIFTED'}"
            )

            # Visual evidence: save just the page-9 screenshot to a known path.
            p9 = screenshots.get("page_9")
            if p9:
                target = out_dir / "_page9_after_capture.png"
                target.write_bytes(p9)
                logger.info(f"Saved page-9 screenshot: {target}")

            await controller.close()
            await browser.close()
            return 0 if ok else 5
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
