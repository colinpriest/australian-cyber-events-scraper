"""Probe the page 9 chiclet DOM structure. Selects Jul-Dec 2024,
navigates to page 9, then dumps every visible element whose inner text
is exactly 'All' along with its tag, attributes, classes, ancestor
chain (4 deep), bounding box, and computed selection state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from scripts.oaic.OAIC_dashboard_scraper import OAICDashboardController

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("probe")
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


PROBE_JS = r"""
() => {
  const out = [];
  const all = document.querySelectorAll('*');
  for (const el of all) {
    const t = (el.innerText || '').trim();
    if (t.toLowerCase() !== 'all') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;

    const ancestors = [];
    let cur = el;
    for (let i = 0; i < 5 && cur; i++) {
      ancestors.push({
        tag: cur.tagName,
        class: (cur.className || '').toString(),
        id: cur.id || '',
        role: cur.getAttribute && cur.getAttribute('role'),
        ariaChecked: cur.getAttribute && cur.getAttribute('aria-checked'),
        ariaSelected: cur.getAttribute && cur.getAttribute('aria-selected'),
        ariaPressed: cur.getAttribute && cur.getAttribute('aria-pressed'),
        ariaLabel: cur.getAttribute && cur.getAttribute('aria-label'),
        title: cur.getAttribute && cur.getAttribute('title'),
      });
      cur = cur.parentElement;
    }

    out.push({
      text: t,
      box: {x: r.x, y: r.y, w: r.width, h: r.height},
      ancestors: ancestors,
    });
  }
  return out;
}
"""


async def main() -> int:
    load_dotenv()
    out_dir = Path("oaic_screenshots") / "_probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled',
                  '--start-maximized', '--window-size=2560,1440'],
        )
        controller = OAICDashboardController(
            headless=True, screenshot_dir=str(out_dir), browser=browser,
        )
        await controller.launch_browser()
        await controller.navigate_to_dashboard()
        await controller.navigate_to_page(2)
        await asyncio.sleep(2)
        await controller._maximize_powerbi_view()

        if not await controller.select_semester("Jul-Dec 2024"):
            logger.error("select_semester failed")
            return 2

        await controller.navigate_to_page(9)
        await asyncio.sleep(3)
        await controller._maximize_powerbi_view()
        await asyncio.sleep(2)

        results = await controller.powerbi_frame.evaluate(PROBE_JS)
        logger.info(f"Found {len(results)} elements with text='All'")
        for i, r in enumerate(results):
            print(f"\n=== Match #{i+1} ===")
            print(f"box: {r['box']}")
            print("ancestors:")
            for j, a in enumerate(r['ancestors']):
                print(f"  [{j}] tag={a['tag']!r} role={a['role']!r} "
                      f"aria-checked={a['ariaChecked']!r} "
                      f"aria-selected={a['ariaSelected']!r} "
                      f"aria-pressed={a['ariaPressed']!r} "
                      f"class={a['class'][:80]!r} "
                      f"aria-label={a['ariaLabel']!r}")

        (out_dir / "all_probe.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        screenshot = await controller.page.screenshot()
        (out_dir / "page9_probe.png").write_bytes(screenshot)

        await controller.close()
        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
