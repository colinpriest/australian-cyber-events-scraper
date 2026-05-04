#!/usr/bin/env python3
"""
OAIC Dashboard Scraper

Scrapes the OAIC Notifiable Data Breach Statistics Power BI dashboard using
browser automation (Playwright) and extracts data using OpenAI Vision API.

Usage:
    python OAIC_dashboard_scraper.py                    # Scrape all available semesters
    python OAIC_dashboard_scraper.py --semester "Jan-Jun 2025"  # Specific semester
    python OAIC_dashboard_scraper.py --from-2025        # Only 2025 onwards
    python OAIC_dashboard_scraper.py --headful          # Show browser window
"""

import argparse
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
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import AsyncOpenAI
from playwright.async_api import async_playwright, Browser, Page, Frame
from tenacity import retry, stop_after_attempt, wait_exponential

# Make project root importable when run as `python scripts/oaic/OAIC_dashboard_scraper.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cyber_data_collector.utils import setup_logging
from scripts.oaic.oaic_validators import (
    OAICValidationError,
    quarantine_extraction,
    validate_cross_page_totals,
    validate_displayed_semester,
    validate_inter_semester_delta,
    validate_page_payload,
)

# Configure logging
setup_logging(log_file="logs/oaic_dashboard_scraper.log")
logger = logging.getLogger(__name__)

QUARANTINE_DIR = Path("instance/oaic_debug")


# Run-summary infra (log-capture + end-of-run replay) is shared with
# pipeline.py / run_full_pipeline.py so all three entry points present
# WARNINGs and ERRORs the same way.
from cyber_data_collector.utils.run_summary import (
    install_run_summary,
    print_run_summary as _print_run_summary,
)
install_run_summary()


class OAICDashboardController:
    """Controls Playwright browser for Power BI dashboard navigation."""

    BASE_URL = "https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard"
    TOTAL_PAGES = 11

    # Pages to scrape (skip Home=1, Data Notes=10, Glossary=11)
    DATA_PAGES = {
        2: "Snapshot",
        3: "Notifications_received",
        4: "Individuals_affected",
        5: "Personal_information_types",
        6: "Source_of_breaches",
        7: "Time_to_identify",
        8: "Time_to_notify",
        9: "Top_sectors"
    }

    def __init__(
        self,
        headless: bool = True,
        screenshot_dir: Optional[str] = None,
        browser: Optional[Browser] = None,
    ):
        """
        Initialize the dashboard controller.

        Args:
            headless: Run browser in headless mode (default True)
            screenshot_dir: Directory to save screenshots (default: oaic_screenshots/<timestamp>)
            browser: Optional pre-launched Playwright Browser to share across
                controllers. When provided, this controller creates its own
                BrowserContext+Page but does NOT close the browser or stop
                Playwright on cleanup.
        """
        self.headless = headless
        self.playwright = None
        self.browser = browser
        self._external_browser = browser is not None
        self.context = None
        self.page = None
        self.powerbi_frame: Optional[Frame] = None

        # Setup screenshot directory
        if screenshot_dir:
            self.screenshot_dir = Path(screenshot_dir)
        else:
            timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
            self.screenshot_dir = Path('oaic_screenshots') / timestamp

    async def launch_browser(self):
        """Initialize Playwright (if needed) and create a fresh context+page."""
        if not self._external_browser:
            logger.info("Launching browser...")
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--start-maximized',
                    '--window-size=2560,1440',
                ]
            )
        else:
            logger.info("Reusing shared browser; creating new context...")

        # Use viewport=None with no_viewport=True to use full window size
        # This allows the browser to use the actual maximized window dimensions
        self.context = await self.browser.new_context(
            viewport=None,  # No fixed viewport - use full window
            no_viewport=True,  # Important: allows window to determine size
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            screen={'width': 2560, 'height': 1440}  # Declare a large screen
        )
        self.page = await self.context.new_page()

        # Maximize the window if running headful
        if not self.headless:
            try:
                # Get the CDP session to maximize window
                cdp = await self.context.new_cdp_session(self.page)
                await cdp.send('Browser.setWindowBounds', {
                    'windowId': 1,
                    'bounds': {'windowState': 'maximized'}
                })
            except Exception as e:
                logger.debug(f"Could not maximize via CDP: {e}")
                # Alternative: set a large viewport
                await self.page.set_viewport_size({'width': 2560, 'height': 1440})
        else:
            # For headless, use a large fixed viewport
            await self.page.set_viewport_size({'width': 2560, 'height': 1440})

        logger.info("Browser ready (context+page created)")

    async def close(self):
        """Close context (and browser+playwright if owned by this controller)."""
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                logger.debug(f"Context close error: {e}")
            self.context = None
        if not self._external_browser:
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            logger.info("Browser closed")
        else:
            logger.info("Context closed (shared browser left running)")

    async def navigate_to_dashboard(self) -> bool:
        """
        Navigate to the OAIC dashboard and wait for Power BI to load.

        Returns:
            True if navigation successful, False otherwise
        """
        logger.info(f"Navigating to dashboard: {self.BASE_URL}")
        try:
            await self.page.goto(self.BASE_URL, wait_until='networkidle', timeout=60000)
            await self._wait_for_powerbi_load()
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to dashboard: {e}")
            return False

    async def _wait_for_powerbi_load(self):
        """Wait for Power BI iframe and visualizations to fully load."""
        logger.info("Waiting for Power BI dashboard to load...")

        # Wait for iframe - Power BI uses various iframe selectors
        iframe_selectors = [
            'iframe[title*="Power BI"]',
            'iframe[src*="powerbi"]',
            'iframe.powerbi-frame',
            'iframe[name*="powerbi"]'
        ]

        iframe = None
        for selector in iframe_selectors:
            try:
                iframe = await self.page.wait_for_selector(selector, timeout=15000)
                if iframe:
                    logger.info(f"Found Power BI iframe with selector: {selector}")
                    break
            except Exception:
                continue

        if not iframe:
            # Try to find any iframe
            iframes = await self.page.query_selector_all('iframe')
            logger.info(f"Found {len(iframes)} iframes on page")
            for i, frame in enumerate(iframes):
                src = await frame.get_attribute('src') or ''
                title = await frame.get_attribute('title') or ''
                logger.info(f"  iframe[{i}]: src={src[:80]}... title={title}")
                if 'powerbi' in src.lower() or 'powerbi' in title.lower():
                    iframe = frame
                    break

        if iframe:
            self.powerbi_frame = await iframe.content_frame()
            if self.powerbi_frame:
                logger.info("Switched to Power BI iframe context")
                # Wait for visualization containers
                try:
                    await self.powerbi_frame.wait_for_selector('.visualContainer, .visual, [class*="visual"]', timeout=20000)
                except Exception:
                    logger.warning("Could not find visual containers, continuing anyway")

        # Additional wait for charts to render
        await asyncio.sleep(3)
        logger.info("Power BI dashboard loaded")

        # Initialize page tracking
        self.current_page = 1

        # Maximize the Power BI visualization within the iframe
        await self._maximize_powerbi_view()

    async def _ensure_frame_context(self):
        """Ensure we have a valid Power BI frame context, refresh if needed."""
        try:
            # Test if frame is still valid by trying a simple query
            if self.powerbi_frame:
                try:
                    await self.powerbi_frame.query_selector('body')
                    return True
                except Exception:
                    logger.warning("Power BI frame context lost, re-acquiring...")

            # Re-acquire frame reference
            iframe_selectors = [
                'iframe[src*="powerbi"]',
                'iframe[title*="Power BI"]',
            ]

            for selector in iframe_selectors:
                try:
                    iframe = await self.page.query_selector(selector)
                    if iframe:
                        self.powerbi_frame = await iframe.content_frame()
                        if self.powerbi_frame:
                            logger.info("Re-acquired Power BI frame context")
                            return True
                except Exception:
                    continue

            logger.error("Could not re-acquire Power BI frame context")
            return False

        except Exception as e:
            logger.error(f"Frame context check failed: {e}")
            return False

    async def _maximize_powerbi_view(self):
        """Click the expand button to maximize the Power BI visualization within the page."""
        # Ensure we have valid frame context first
        await self._ensure_frame_context()

        if not self.powerbi_frame:
            return

        try:
            # Look for the expand/fullscreen button in the bottom right corner
            # Common aria-labels for expand buttons in Power BI
            expand_selectors = [
                'button[aria-label="Expand"]',
                'button[aria-label="Enter Full Screen"]',
                'button[aria-label="Full screen"]',
                'button[aria-label="Fullscreen"]',
                'button[aria-label="Enter full screen mode"]',
                'button[aria-label="Maximize"]',
                'button[title="Expand"]',
                'button[title="Full screen"]',
                # Icon-based buttons (often have specific classes)
                'button.enterFullScreen',
                'button[class*="fullscreen"]',
                'button[class*="expand"]',
                # Generic expand icon
                'button i.glyphicon-fullscreen',
                'button i[class*="expand"]',
                'button i[class*="fullscreen"]',
            ]

            expand_button = None
            for selector in expand_selectors:
                try:
                    expand_button = await self.powerbi_frame.query_selector(selector)
                    if expand_button:
                        is_visible = await expand_button.is_visible()
                        if is_visible:
                            logger.info(f"Found expand button with selector: {selector}")
                            break
                        expand_button = None
                except Exception:
                    continue

            # If not found by selector, try to find by position (bottom right corner)
            if not expand_button:
                # Look for buttons and check their position
                all_buttons = await self.powerbi_frame.query_selector_all('button')
                for btn in all_buttons:
                    try:
                        is_visible = await btn.is_visible()
                        if not is_visible:
                            continue

                        # Check aria-label for expand-related text
                        aria = await btn.get_attribute('aria-label') or ''
                        title = await btn.get_attribute('title') or ''
                        btn_class = await btn.get_attribute('class') or ''

                        if any(keyword in (aria + title + btn_class).lower() for keyword in ['expand', 'full', 'maximize', 'enlarge']):
                            expand_button = btn
                            logger.info(f"Found expand button by keyword: aria='{aria}' title='{title}' class='{btn_class}'")
                            break
                    except Exception:
                        continue

            if expand_button:
                await expand_button.click()
                logger.info("Clicked expand button to maximize Power BI visualization")
                await asyncio.sleep(2)  # Wait for expansion animation
            else:
                logger.warning("Could not find expand button - listing available buttons for debugging")
                # Debug: list all buttons
                all_buttons = await self.powerbi_frame.query_selector_all('button')
                for i, btn in enumerate(all_buttons[:15]):
                    try:
                        aria = await btn.get_attribute('aria-label') or ''
                        title = await btn.get_attribute('title') or ''
                        btn_class = await btn.get_attribute('class') or ''
                        is_visible = await btn.is_visible()
                        if is_visible:
                            logger.info(f"  Button[{i}]: aria='{aria[:30]}' title='{title[:20]}' class='{btn_class[:30]}'")
                    except Exception:
                        continue

        except Exception as e:
            logger.warning(f"Could not maximize Power BI view: {e}")

    async def discover_filter_options(self) -> List[str]:
        """
        Discover available filter/radio button options on the current page.
        Returns list of visible filter option texts.
        """
        if not self.powerbi_frame:
            return []

        found_options = []

        try:
            # Look for common filter element patterns in Power BI
            # Power BI uses various element types for filters/slicers
            filter_selectors = [
                # Radio buttons and options
                '[role="radio"]',
                '[role="option"]',
                '[role="menuitemradio"]',
                # Slicer elements
                'div.slicer-restatement',
                'span.slicerText',
                'div.slicerItemContainer span',
                # Chiclet slicer (button-style filters)
                '[class*="chiclet"] span',
                'div.chicletSlicer span',
                # General clickable text in filter areas
                '[class*="slicer"] [class*="text"]',
                '[class*="visual"] [role="listbox"] span',
                # Button-style filters
                'button[class*="slicer"]',
                'div[class*="slicer"] button',
            ]

            for selector in filter_selectors:
                try:
                    elements = await self.powerbi_frame.query_selector_all(selector)
                    for element in elements:
                        try:
                            is_visible = await element.is_visible()
                            if is_visible:
                                text = await element.inner_text()
                                text = text.strip()
                                # Filter out empty, very short, or very long texts
                                # Allow shorter texts like "All" (3 chars)
                                if text and 2 < len(text) < 60 and text not in found_options:
                                    # Exclude obvious non-filter items
                                    if not any(skip in text for skip in ['\n', '\t', 'Show keyboard', 'Skip to']):
                                        found_options.append(text)
                        except Exception:
                            continue
                except Exception:
                    continue

            if found_options:
                logger.info(f"Discovered {len(found_options)} filter options: {found_options[:10]}{'...' if len(found_options) > 10 else ''}")

        except Exception as e:
            logger.debug(f"Filter discovery failed: {e}")

        return found_options

    async def debug_iframe_elements(self):
        """Debug helper to list available elements in the Power BI iframe."""
        if not self.powerbi_frame:
            logger.warning("No Power BI frame available for debugging")
            return

        logger.info("=== DEBUG: Exploring Power BI iframe elements ===")

        # Look for navigation elements
        nav_patterns = [
            'button',
            '[role="button"]',
            '[class*="nav"]',
            '[class*="page"]',
            '[aria-label]',
            '[class*="slicer"]',
            '[class*="filter"]',
        ]

        for pattern in nav_patterns:
            try:
                elements = await self.powerbi_frame.query_selector_all(pattern)
                if elements:
                    logger.info(f"\n{pattern}: {len(elements)} elements")
                    for i, el in enumerate(elements[:10]):  # Limit to first 10
                        try:
                            tag = await el.evaluate('el => el.tagName')
                            classes = await el.get_attribute('class') or ''
                            aria = await el.get_attribute('aria-label') or ''
                            text = await el.inner_text()
                            text = text[:50] if text else ''
                            is_visible = await el.is_visible()
                            logger.info(f"  [{i}] {tag} | visible={is_visible} | class={classes[:40]} | aria={aria[:30]} | text={text[:30]}")
                        except Exception:
                            continue
            except Exception as e:
                logger.debug(f"Pattern {pattern} failed: {e}")

        logger.info("=== END DEBUG ===")

    async def navigate_to_page_by_clicking(self, target_page: int) -> bool:
        """
        Navigate using the visible Next Page / Previous Page buttons in Power BI.
        Based on debug output: aria-label="Next Page" and aria-label="Previous Page"
        """
        # Ensure we have valid frame context
        await self._ensure_frame_context()

        if not self.powerbi_frame:
            return False

        current = getattr(self, 'current_page', 1)
        clicks_needed = target_page - current

        if clicks_needed == 0:
            return True

        logger.info(f"Navigating from page {current} to page {target_page} ({clicks_needed} clicks)")

        try:
            # Use exact aria-label from debug output
            if clicks_needed > 0:
                selector = 'button[aria-label="Next Page"]'
                direction = "Next"
            else:
                selector = 'button[aria-label="Previous Page"]'
                direction = "Previous"
                clicks_needed = abs(clicks_needed)

            for click_num in range(clicks_needed):
                button = await self.powerbi_frame.query_selector(selector)

                if not button:
                    logger.error(f"Could not find {direction} Page button")
                    return False

                is_visible = await button.is_visible()
                is_enabled = await button.is_enabled()

                if not is_visible or not is_enabled:
                    logger.warning(f"{direction} Page button not clickable (visible={is_visible}, enabled={is_enabled})")
                    # May have reached the end/start
                    break

                # Click the button
                await button.click()
                logger.info(f"Clicked {direction} Page ({click_num + 1}/{clicks_needed})")

                # Wait for page transition
                await asyncio.sleep(2)

            self.current_page = target_page
            logger.info(f"Now on page {target_page}")
            return True

        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return False

    async def navigate_to_page_by_name(self, page_name: str) -> bool:
        """
        Navigate by opening the page flyout and clicking the page name.
        The flyout contains buttons like: Home, Snapshot, Notifications received, etc.
        """
        if not self.powerbi_frame:
            return False

        logger.info(f"Attempting to navigate to page: {page_name}")

        try:
            # First, try to open the page navigation flyout
            # Look for the navigation wrapper that shows "1 of 11"
            nav_wrapper = await self.powerbi_frame.query_selector('span.navigation-wrapper')
            if nav_wrapper:
                await nav_wrapper.click()
                await asyncio.sleep(0.5)

            # Now look for the page button in the flyout
            # These have class="sectionItem" and aria-label matching the page name
            page_button = await self.powerbi_frame.query_selector(f'button.sectionItem[aria-label*="{page_name}"]')

            if page_button:
                await page_button.click()
                await asyncio.sleep(2)
                logger.info(f"Clicked page button: {page_name}")
                return True
            else:
                logger.warning(f"Could not find page button for: {page_name}")
                return False

        except Exception as e:
            logger.error(f"Navigation by name failed: {e}")
            return False

    async def get_available_semesters(self) -> List[str]:
        """
        Get list of available semesters from the dropdown filter.

        Returns:
            List of semester strings (e.g., ["Jan-Jun 2025", "Jul-Dec 2024", ...])
        """
        logger.info("Detecting available semesters from dropdown...")

        # Known semesters available in OAIC dashboard (from Jan 2020 onwards)
        known_semesters = [
            "Jan-Jun 2025",
            "Jul-Dec 2024",
            "Jan-Jun 2024",
            "Jul-Dec 2023",
            "Jan-Jun 2023",
            "Jul-Dec 2022",
            "Jan-Jun 2022",
            "Jul-Dec 2021",
            "Jan-Jun 2021",
            "Jul-Dec 2020",
            "Jan-Jun 2020",
        ]

        # Try to read from actual dropdown
        try:
            frame = self.powerbi_frame or self.page

            # Look for dropdown/slicer elements
            dropdown_selectors = [
                '[class*="slicer"]',
                '[class*="dropdown"]',
                '[role="listbox"]',
                '[aria-label*="Semester"]'
            ]

            for selector in dropdown_selectors:
                try:
                    elements = await frame.query_selector_all(selector)
                    if elements:
                        logger.info(f"Found {len(elements)} elements matching {selector}")
                        break
                except Exception:
                    continue

        except Exception as e:
            logger.warning(f"Could not read dropdown, using known semesters: {e}")

        logger.info(f"Available semesters: {known_semesters}")
        return known_semesters

    @staticmethod
    def _normalize_semester(s: str) -> str:
        """Lowercase, alphanumeric-only fingerprint used for fuzzy semester matching.

        Handles whitespace differences, en-dash vs hyphen, and casing so
        ``"Jul - Dec 2025"``, ``"jul-dec 2025"`` and ``"Jul-Dec  2025"`` all
        compare equal.
        """
        return ''.join(c.lower() for c in s if c.isalnum())

    @staticmethod
    def _semester_aliases(semester: str) -> List[str]:
        """Return alternative texts the dashboard might use for the same semester.

        OAIC's dashboard has been seen to label the period slicer in several
        ways depending on year: full month names, year-included or not, etc.
        Each alias is normalised before comparison.
        """
        s = semester.strip()
        aliases = [s]

        # Common substitutions for month abbreviations / full names.
        substitutions = (
            ('Jan-Jun', ['January-June', 'Jan - Jun', 'January - June', '1 January - 30 June',
                         '1-Jan to 30-Jun', 'H1', '1 January to 30 June']),
            ('Jul-Dec', ['July-December', 'Jul - Dec', 'July - December', '1 July - 31 December',
                         '1-Jul to 31-Dec', 'H2', '1 July to 31 December']),
        )
        for src, dsts in substitutions:
            if src in s:
                year = s.replace(src, '').strip()
                for dst in dsts:
                    aliases.append(f'{dst} {year}'.strip())
                    aliases.append(dst)  # year-less variant for nested year+period slicers
        return aliases

    def _all_target_norms(self, semester: str) -> List[str]:
        return [self._normalize_semester(a) for a in self._semester_aliases(semester) if a]

    async def _find_semester_dropdown(self):
        """Locate the Semester slicer dropdown trigger element."""
        for sel in (
            'div.slicer-dropdown-menu[aria-label="Semester"]',
            'div.slicer-dropdown-menu[aria-label*="emester"]',
            'div.slicer-dropdown-menu[aria-label*="eriod"]',
            'div.slicer-dropdown-menu',
            '[class*="slicer-dropdown"]',
        ):
            try:
                el = await self.powerbi_frame.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def _verify_dashboard_shows_semester(
        self,
        semester: str,
        max_attempts: int = 8,
        wait_between: float = 0.6,
    ) -> bool:
        """Confirm the dashboard's slicer is currently showing `semester`.

        The OAIC dashboard layout has two stacked elements top-left:
        a tiny "Show results for" LABEL (which never contains the value),
        and a DROPDOWN TRIGGER below it whose text shows the currently
        selected semester (e.g. "Jan-Jun 2020"). The trigger is the
        authoritative source of truth: it updates the moment the slicer
        click takes effect, even before the visuals re-render.

        Earlier versions of this helper searched for the label text and
        always returned False, which incorrectly rejected even valid
        selections. We now:
          1. Read the dropdown trigger's inner text (primary).
          2. Fall back to scanning page text for the period label.
          3. Poll a few times so a slow re-render doesn't false-fail.
        """
        if not self.powerbi_frame:
            return False
        target_norms = set(self._all_target_norms(semester))

        async def _read_trigger_text() -> Optional[str]:
            """Read the slicer trigger directly. This is the single most
            reliable signal of the slicer's current selection.
            """
            try:
                dropdown = await self._find_semester_dropdown()
                if dropdown:
                    txt = (await dropdown.inner_text() or '').strip()
                    return txt or None
            except Exception:
                pass
            return None

        async def _scan_page_for_period_label() -> Optional[str]:
            """Last-resort: search the iframe for any visible text node
            that looks like a semester label (Jan-Jun YYYY / Jul-Dec YYYY
            / January-June YYYY / July-December YYYY).
            """
            try:
                hits = await self.powerbi_frame.evaluate(
                    """
                    () => {
                      const out = [];
                      const re = /(jan(uary)?|jul(y)?)\\s*[-–]\\s*(jun(e)?|dec(ember)?)\\s+\\d{4}/i;
                      const all = document.querySelectorAll('*');
                      for (const el of all) {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 80) continue;
                        const m = t.match(re);
                        if (m) out.push(m[0]);
                      }
                      return out;
                    }
                    """
                )
            except Exception:
                hits = []
            for h in hits or []:
                if self._normalize_semester(h) in target_norms:
                    return h
            return hits[0] if hits else None

        for attempt in range(max_attempts):
            trigger = await _read_trigger_text()
            if trigger and self._normalize_semester(trigger) in target_norms:
                logger.debug(
                    f"[{semester}] verified via dropdown trigger: {trigger!r}"
                )
                return True

            page_label = await _scan_page_for_period_label()
            if page_label and self._normalize_semester(page_label) in target_norms:
                logger.debug(
                    f"[{semester}] verified via page label: {page_label!r}"
                )
                return True

            logger.debug(
                f"[{semester}] attempt {attempt + 1}: trigger={trigger!r}, "
                f"page_label={page_label!r} (need one of {target_norms})"
            )
            await asyncio.sleep(wait_between)
        return False

    async def select_semester(self, semester: str) -> bool:
        """Select a semester from the dashboard, trying multiple UI patterns.

        Power BI slicers come in several flavours (dropdown, chiclet, list,
        native HTML select). We try each in order and return True on the first
        successful selection AND a DOM-level post-condition check confirming
        the dashboard now displays that semester.

        Without the post-condition the dropdown silently falls back to the
        default period for older semesters (those no longer in OAIC's
        published-list), and we'd scrape the wrong-period screenshots.
        """
        logger.info(f"Selecting semester: {semester}")

        if not self.powerbi_frame:
            logger.error("Power BI frame not available")
            return False

        target_norms = set(self._all_target_norms(semester))
        logger.debug(f"[{semester}] Will accept normalized matches: {target_norms}")

        strategies = (
            ("already_selected", self._sem_strat_already_selected),
            ("native_select",    self._sem_strat_native_select),
            # Search-typing handles older semesters that live deep in the
            # virtualised list far more reliably than scroll-and-click.
            ("dropdown_search",  self._sem_strat_dropdown_search),
            ("dropdown",         self._sem_strat_dropdown),
            ("text_scan",        self._sem_strat_text_scan),
        )

        for name, strat in strategies:
            try:
                if not await strat(semester, target_norms):
                    continue
            except Exception as e:
                logger.debug(f"[{semester}] Strategy '{name}' raised: {e}")
                continue
            # Strategy reported success - confirm via DOM probe before
            # accepting it. Power BI takes ~1-2s to re-render the visuals.
            await asyncio.sleep(2)
            if await self._verify_dashboard_shows_semester(semester):
                logger.info(f"[{semester}] Selected via strategy '{name}' (verified)")
                return True
            logger.warning(
                f"[{semester}] Strategy '{name}' clicked an option but the "
                "dashboard still shows a different period; trying next strategy."
            )

        logger.error(
            f"[{semester}] No strategy could put the dashboard into this period. "
            "Likely the semester is not (or no longer) in the dropdown."
        )
        await self._dump_dropdown_failure(semester)
        return False

    async def _sem_strat_already_selected(self, semester: str, target_norms: set) -> bool:
        """Strategy 0: trigger already shows the requested semester."""
        dropdown = await self._find_semester_dropdown()
        if not dropdown:
            return False
        try:
            trigger_text = (await dropdown.inner_text() or '').strip()
        except Exception:
            return False
        if trigger_text and self._normalize_semester(trigger_text) in target_norms:
            logger.info(f"[{semester}] Already the active selection (trigger text: {trigger_text!r})")
            return True
        return False

    async def _sem_strat_native_select(self, semester: str, target_norms: set) -> bool:
        """Strategy 1: a native HTML <select> element somewhere in the iframe."""
        try:
            selects = await self.powerbi_frame.query_selector_all('select')
        except Exception:
            return False
        for sel in selects:
            try:
                option_texts = await sel.evaluate(
                    '(s) => Array.from(s.options).map(o => o.text)'
                )
            except Exception:
                continue
            for opt_text in option_texts:
                if self._normalize_semester(opt_text or '') in target_norms:
                    try:
                        await sel.select_option(label=opt_text)
                        logger.info(f"[{semester}] Selected via <select> option {opt_text!r}")
                        return True
                    except Exception:
                        pass
        return False

    async def _sem_strat_dropdown_search(self, semester: str, target_norms: set) -> bool:
        """Strategy 2a: open the dropdown, type into the slicer's search input,
        and click the only remaining option. This is the most reliable way to
        reach options buried deep in a virtualised list (e.g. semesters from
        2020/2021 that live below the default scroll viewport).

        Power BI slicer dropdowns include a "Search" input by default. Typing
        into it instantly filters the options client-side, so we never have
        to scroll a virtual list at all.
        """
        dropdown = await self._find_semester_dropdown()
        if not dropdown:
            return False

        try:
            await dropdown.click()
        except Exception as e:
            logger.debug(f"[{semester}] dropdown_search: opening dropdown failed: {e}")
            return False
        await asyncio.sleep(1.0)

        # Find the search input. Power BI slicer popups render the search
        # input as either a child of `slicer-dropdown-popup` or a sibling
        # right above it - both layouts have shipped over the years.
        search_selectors = (
            'div.slicer-dropdown-popup input[type="text"]',
            'div.slicer-dropdown-popup input[type="search"]',
            'div.slicer-dropdown-popup input',
            '[role="listbox"] input',
            'div.searchbox input',
            'input[placeholder*="earch" i]',
            'input[aria-label*="earch" i]',
            'input[type="search"]',
            'input[role="searchbox"]',
        )
        search_input = None
        for sel in search_selectors:
            try:
                el = await self.powerbi_frame.query_selector(sel)
                if el and await el.is_visible():
                    search_input = el
                    break
            except Exception:
                continue

        # Last resort: find ANY visible text-like input in the iframe that
        # is reasonably small (search boxes are typically 150-400px wide
        # and 20-40px tall). Skip giant text-area-style fields.
        if not search_input:
            try:
                inputs = await self.powerbi_frame.query_selector_all('input')
            except Exception:
                inputs = []
            for el in inputs:
                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                    if not box:
                        continue
                    if 80 <= box['width'] <= 500 and 16 <= box['height'] <= 60:
                        # Skip checkboxes etc.
                        type_attr = (await el.get_attribute('type')) or 'text'
                        if type_attr.lower() not in ('text', 'search', ''):
                            continue
                        search_input = el
                        break
                except Exception:
                    continue

        if not search_input:
            logger.debug(f"[{semester}] dropdown_search: no search input visible inside popup")
            try:
                await self.powerbi_frame.click('body')
            except Exception:
                pass
            return False

        # The slicer matches on the START of the option label, so type the
        # canonical "Jan-Jun YYYY" / "Jul-Dec YYYY" form. Try a couple of
        # fingerprints in case OAIC changed labels for older years.
        candidate_queries = [semester]
        # If our prefix-based aliases produce a unique 'Jan-Jun YYYY'
        # variant, add the year-only fragment as a fallback.
        m = re.match(r"(Jan-Jun|Jul-Dec)\s+(\d{4})", semester)
        if m:
            candidate_queries.append(m.group(2))  # year only
            candidate_queries.append(f"{m.group(1)} {m.group(2)}")  # canonical

        async def _click_visible_option_matching() -> Optional[str]:
            option_selectors = (
                'div.slicer-dropdown-popup [role="option"]',
                'div.slicer-dropdown-popup span.slicerText',
                'div.slicer-dropdown-popup div.slicerItemContainer',
                '[role="listbox"] [role="option"]',
                '[role="listbox"] span',
            )
            for sel in option_selectors:
                try:
                    elements = await self.powerbi_frame.query_selector_all(sel)
                except Exception:
                    continue
                for el in elements:
                    try:
                        if not await el.is_visible():
                            continue
                        text = (await el.inner_text() or '').strip()
                        if not text:
                            continue
                        if self._normalize_semester(text) in target_norms:
                            await el.scroll_into_view_if_needed()
                            await el.click()
                            return text
                    except Exception:
                        continue
            return None

        for query in candidate_queries:
            try:
                # Triple-click to select existing text, then type new query.
                await search_input.click(click_count=3)
                await search_input.fill('')  # clear
                await search_input.type(query, delay=15)
                await asyncio.sleep(0.7)  # let the slicer filter
                clicked = await _click_visible_option_matching()
                if clicked:
                    logger.info(
                        f"[{semester}] dropdown_search matched after typing "
                        f"{query!r}: clicked option {clicked!r}"
                    )
                    return True
                logger.debug(
                    f"[{semester}] dropdown_search: no match after typing {query!r}"
                )
            except Exception as e:
                logger.debug(
                    f"[{semester}] dropdown_search: search-input interaction "
                    f"failed for {query!r}: {e}"
                )
                continue

        # Search bar didn't help - close the popup so other strategies aren't
        # confused by leftover state, then signal failure to the caller.
        try:
            await self.powerbi_frame.click('body')
        except Exception:
            pass
        return False

    async def _sem_strat_dropdown(self, semester: str, target_norms: set) -> bool:
        """Strategy 2: open dropdown, then scroll the (virtualized) popup until the option is found.

        Power BI dropdown popups use virtual scrolling: only ~8 options live in
        the DOM at once. To find an option that's outside the current viewport
        we have to scroll the popup container and re-scan after each step.
        Newest semesters appear at the top of OAIC's list, older ones at the
        bottom, so we explicitly walk both directions from the default
        viewport position.
        """
        dropdown = await self._find_semester_dropdown()
        if not dropdown:
            return False
        try:
            trigger_text = (await dropdown.inner_text() or '').strip()
        except Exception:
            trigger_text = ''
        await dropdown.click()
        logger.info(f"[{semester}] Opened dropdown (was showing: {trigger_text!r})")
        await asyncio.sleep(1.2)

        scroll_container = await self._find_dropdown_scroll_container()
        if not scroll_container:
            logger.debug(f"[{semester}] No scroll container located - falling back to single scan")

        all_seen: set = set()

        async def scan_and_try_click() -> Optional[str]:
            """Scan the iframe for visible option-like elements, click first match."""
            option_selectors = (
                'div.slicer-dropdown-popup [role="option"]',
                'div.slicer-dropdown-popup span.slicerText',
                'div.slicer-dropdown-popup div.slicerItemContainer',
                '[role="listbox"] [role="option"]',
                '[role="listbox"] span',
                'div.slicerItemContainer',
                'span.slicerText',
                '[role="option"]',
                '[role="treeitem"]',
                '[role="menuitemradio"]',
            )
            local_seen: Dict[str, Any] = {}
            for sel in option_selectors:
                try:
                    elements = await self.powerbi_frame.query_selector_all(sel)
                except Exception:
                    continue
                for el in elements:
                    try:
                        if not await el.is_visible():
                            continue
                        text = (await el.inner_text() or '').strip()
                        if not text or text in local_seen:
                            continue
                        local_seen[text] = el
                    except Exception:
                        continue
            for text, el in local_seen.items():
                all_seen.add(text)
                if self._normalize_semester(text) in target_norms:
                    try:
                        await el.scroll_into_view_if_needed()
                        await el.click()
                        return text
                    except Exception as e:
                        logger.debug(f"Click failed for {text!r}: {e}")
            return None

        # Pass 1: current viewport
        match = await scan_and_try_click()
        if match:
            logger.info(f"[{semester}] Dropdown matched (initial viewport): {match!r}")
            return True

        if not scroll_container:
            try:
                await self.powerbi_frame.click('body')
            except Exception:
                pass
            logger.warning(
                f"[{semester}] Dropdown strategy: no match and no scroll container. "
                f"Visible options: {sorted(all_seen)[:30]}"
            )
            return False

        # Pass 2: jump to top (newest semesters live there for OAIC's reverse-chrono list)
        try:
            await scroll_container.evaluate('(el) => { el.scrollTop = 0; }')
            await asyncio.sleep(0.4)
        except Exception:
            pass
        match = await scan_and_try_click()
        if match:
            logger.info(f"[{semester}] Dropdown matched (after scroll-to-top): {match!r}")
            return True

        # Pass 3: walk down progressively until we hit the bottom or find a match
        last_top = -1
        for step in range(30):
            try:
                info = await scroll_container.evaluate(
                    '(el) => ({top: el.scrollTop, max: el.scrollHeight - el.clientHeight, ch: el.clientHeight})'
                )
            except Exception:
                break
            if info['max'] <= 0 or info['top'] >= info['max']:
                break
            new_top = min(info['max'], info['top'] + max(60, int(info['ch']) - 30))
            if new_top == last_top:
                break
            try:
                await scroll_container.evaluate(f'(el) => {{ el.scrollTop = {new_top}; }}')
            except Exception:
                break
            await asyncio.sleep(0.35)
            last_top = new_top
            match = await scan_and_try_click()
            if match:
                logger.info(f"[{semester}] Dropdown matched (after scroll step {step + 1}): {match!r}")
                return True

        # No match anywhere
        try:
            await self.powerbi_frame.click('body')
        except Exception:
            pass
        preview = sorted(all_seen)[:40]
        logger.warning(
            f"[{semester}] Dropdown strategy exhausted scroll: {len(all_seen)} unique options "
            f"seen. All: {preview}"
        )
        return False

    async def _find_dropdown_scroll_container(self):
        """Find the scrollable element inside the open dropdown popup."""
        candidates = (
            'div.slicer-dropdown-popup div.scroll-region',
            'div.slicer-dropdown-popup [class*="scroll"]',
            'div.slicer-dropdown-popup',
            'div.scrollRegion',
            'div[role="listbox"][class*="scroll"]',
            'div[role="listbox"]',
            'div.slicer-content-wrapper',
        )
        for sel in candidates:
            try:
                el = await self.powerbi_frame.query_selector(sel)
                if not el or not await el.is_visible():
                    continue
                # Confirm it's actually scrollable
                info = await el.evaluate(
                    '(n) => ({scroll: n.scrollHeight, client: n.clientHeight})'
                )
                if info and info.get('scroll', 0) > info.get('client', 0):
                    return el
            except Exception:
                continue
        # Last-ditch: any element on the page that scrolls and contains slicerText
        try:
            return await self.powerbi_frame.evaluate_handle("""
                () => {
                    const all = Array.from(document.querySelectorAll('*'));
                    return all.find(n => {
                        const cs = getComputedStyle(n);
                        return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                            && n.scrollHeight > n.clientHeight + 5
                            && n.querySelector('.slicerText, [role="option"]');
                    }) || null;
                }
            """)
        except Exception:
            return None

    async def _sem_strat_text_scan(self, semester: str, target_norms: set) -> bool:
        """Strategy 3: scan visible elements for matching text and click the smallest one.

        Handles chiclet slicers (always-visible buttons) and any case where the
        option lives outside our known dropdown selectors.
        """
        candidate_selectors = (
            '[role="option"]', '[role="radio"]', '[role="treeitem"]',
            '[role="menuitemradio"]', '[role="button"]',
            'span.slicerText', 'div.slicerItemContainer',
            '[class*="chiclet"]', 'div[class*="chicletSlicer"]',
            'button', 'a',
        )
        # Pick the smallest matching clickable - large ancestors usually
        # contain the option but aren't the option itself.
        best = None
        best_area = float('inf')
        seen_texts: set = set()

        for sel in candidate_selectors:
            try:
                elements = await self.powerbi_frame.query_selector_all(sel)
            except Exception:
                continue
            for el in elements:
                try:
                    if not await el.is_visible():
                        continue
                    text = (await el.inner_text() or '').strip()
                    if not text or len(text) > 80:
                        continue
                    seen_texts.add(text)
                    if self._normalize_semester(text) not in target_norms:
                        continue
                    box = await el.bounding_box()
                    if not box or box['width'] < 5 or box['height'] < 5:
                        continue
                    area = box['width'] * box['height']
                    if area < best_area:
                        best, best_area = (el, text), area
                except Exception:
                    continue

        if best:
            el, text = best
            try:
                await el.scroll_into_view_if_needed()
                await el.click()
                logger.info(f"[{semester}] Text-scan matched and clicked: {text!r}")
                return True
            except Exception as e:
                logger.warning(f"[{semester}] Text-scan click failed: {e}")
        else:
            # log a sample of texts that were *similar* (share at least 2 chars)
            target_one = next(iter(target_norms), '')
            similar = sorted({t for t in seen_texts
                              if any(c in self._normalize_semester(t) for c in (target_one[:4],))})[:30]
            logger.warning(
                f"[{semester}] Text-scan: no exact match. "
                f"Texts containing target prefix: {similar}"
            )
        return False

    async def _dump_dropdown_failure(self, semester: str) -> None:
        """Save iframe HTML + screenshot so the failure can be diagnosed offline."""
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe = ''.join(c for c in semester if c.isalnum())
            dump_dir = self.screenshot_dir / 'dropdown_failure'
            dump_dir.mkdir(parents=True, exist_ok=True)

            if self.powerbi_frame:
                try:
                    html = await self.powerbi_frame.content()
                    html_path = dump_dir / f'{safe}_{ts}.html'
                    html_path.write_text(html, encoding='utf-8')
                    logger.info(f"[{semester}] Saved iframe HTML for diagnosis: {html_path}")
                except Exception as e:
                    logger.debug(f"HTML dump failed: {e}")

            try:
                iframe_el = await self.page.query_selector('iframe[src*="powerbi"], iframe[title*="Power BI"]')
                if iframe_el:
                    shot_path = dump_dir / f'{safe}_{ts}.png'
                    await iframe_el.screenshot(path=str(shot_path))
                    logger.info(f"[{semester}] Saved iframe screenshot: {shot_path}")
            except Exception as e:
                logger.debug(f"Screenshot dump failed: {e}")
        except Exception as e:
            logger.error(f"Diagnostic dump failed entirely: {e}")

    async def navigate_to_page(self, page_num: int) -> bool:
        """
        Navigate to a specific dashboard page using Power BI internal navigation.

        Args:
            page_num: Page number (1-11)

        Returns:
            True if navigation successful
        """
        page_name = self.DATA_PAGES.get(page_num, 'Unknown')
        logger.info(f"Navigating to page {page_num}: {page_name}")

        # CRITICAL: Must use powerbi_frame for all interactions inside the dashboard
        if not self.powerbi_frame:
            logger.error("Power BI frame not initialized")
            return False

        current = getattr(self, 'current_page', 1)
        if page_num == current:
            logger.info(f"Already on page {page_num}")
            return True

        try:
            clicks_needed = page_num - current

            # Use the robust clicking method
            return await self.navigate_to_page_by_clicking(page_num)

        except Exception as e:
            logger.error(f"Failed to navigate to page {page_num}: {e}")
            return False

    async def _is_filter_already_selected(self, text: str, max_y: int = 600) -> bool:
        """Return True if a filter chiclet/tab/radio with EXACT inner text
        `text` is currently in the selected state.

        This exists because on fresh BrowserContext loads, OAIC's chiclet
        filters (page 6/9 'All breaches' / 'All', page 7/8 'By time taken
        only') are already in their default-correct state. Clicking them
        a second time toggles them off and triggers Power BI's "reset
        all slicers" behaviour - which CLEARS the period slicer back to
        default and ruins every subsequent screenshot.

        We probe by scanning the same selectors `click_button_by_exact_text`
        uses, but instead of clicking we examine the element AND its
        ancestors for any of the standard "this is selected" signals
        (aria-pressed/checked/selected = "true", or class list containing
        "selected"/"is-selected"/"chiclet--selected"/"checked").
        """
        if not self.powerbi_frame:
            return False
        target_norm = text.strip().casefold()

        async def _looks_selected(el) -> bool:
            try:
                state = await el.evaluate(
                    """
                    (n) => {
                      // walk up to 4 ancestors checking selection signals
                      let cur = n;
                      for (let i = 0; i < 5 && cur; i++) {
                        const aria = (cur.getAttribute && (
                          cur.getAttribute('aria-checked') === 'true'
                          || cur.getAttribute('aria-pressed') === 'true'
                          || cur.getAttribute('aria-selected') === 'true'
                        ));
                        const cls = (cur.className || '').toString().toLowerCase();
                        const flagged = /(^|\\s)(selected|is-selected|isselected|active|chiclet--selected|checked)(\\s|$)/.test(cls);
                        if (aria || flagged) return true;
                        cur = cur.parentElement;
                      }
                      return false;
                    }
                    """
                )
                return bool(state)
            except Exception:
                return False

        candidate_selectors = (
            'button', '[role="button"]', '[role="tab"]', '[role="radio"]',
            '[role="menuitemradio"]', '[role="option"]', '[role="checkbox"]',
            '[role="link"]',
            'span.slicerText', 'div[class*="chiclet"]', 'div.slicerItemContainer',
            'div[class*="visual-actionButton"]',
        )
        for sel in candidate_selectors:
            try:
                elements = await self.powerbi_frame.query_selector_all(sel)
            except Exception:
                continue
            for el in elements:
                try:
                    if not await el.is_visible():
                        continue
                    text_actual = (await el.inner_text() or '').strip()
                    if text_actual.casefold() != target_norm:
                        continue
                    box = await el.bounding_box()
                    if not box or box['y'] > max_y:
                        continue
                    if await _looks_selected(el):
                        return True
                except Exception:
                    continue
        return False

    async def click_button_by_exact_text(self, text: str, max_y: int = 250) -> bool:
        """Click a button whose inner text EXACTLY equals `text`, restricted
        to elements near the top of the iframe (max_y px).

        This is the right call for chiclet-style filter buttons (e.g. the
        "All" / "Cyber Incident" / etc. row on page 9, or "By time taken only"
        tabs on pages 7-8). The looser ``click_filter_option`` does fuzzy
        matching which routinely picks the wrong element on these pages.

        Rank 4 (post-condition verification): after clicking, the same
        element is re-queried and its aria-pressed/aria-checked/aria-selected
        attribute or `is-selected` CSS class is checked. If the post-state
        does not indicate selection, the click is retried once with a fresh
        element handle. This catches stale-DOM and mouse-coordinate-drift
        failures that previously left the dashboard on a different filter.
        """
        if not self.powerbi_frame:
            return False
        target = text.strip()
        target_norm = target.casefold()

        async def _verify_selected(el) -> bool:
            """Return True when we have positive evidence the element is
            in the selected state. Returns True for ELEMENTS WITHOUT A
            RECOGNIZABLE SELECTION ATTRIBUTE - such elements (e.g. Power
            BI bookmark action buttons) are click-once-fire-action
            controls where the click itself is the signal; there's no
            post-state to verify.
            """
            saw_selection_attr = False
            for attr in ("aria-pressed", "aria-checked", "aria-selected"):
                try:
                    val = await el.get_attribute(attr)
                except Exception:
                    val = None
                if val is not None:
                    saw_selection_attr = True
                    if val.lower() == "true":
                        return True
            try:
                cls = (await el.get_attribute("class")) or ""
            except Exception:
                cls = ""
            cls_low = cls.lower()
            if any(token in cls_low for token in (
                "selected", "is-selected", "active",
                "isselected", "chiclet--selected"
            )):
                return True
            # No recognizable selection-state attribute or class. This is
            # a fire-and-forget control (e.g. a Power BI bookmark action
            # button) where the click itself is the only signal we get.
            # Trust it; drift detection downstream will catch a miswire.
            if not saw_selection_attr:
                return True
            return False

        async def _try_click(el) -> bool:
            try:
                if not await el.is_visible():
                    return False
                box = await el.bounding_box()
                if not box or box["y"] > max_y:
                    return False
                await el.scroll_into_view_if_needed()
                await el.click(timeout=3000)
            except Exception:
                return False
            # Post-condition: small wait for the dashboard to repaint, then
            # check the element's selected state. One retry on miss.
            for attempt in (1, 2):
                await asyncio.sleep(0.6)
                try:
                    if await _verify_selected(el):
                        return True
                except Exception:
                    pass
                if attempt == 1:
                    try:
                        await el.click(timeout=3000)
                    except Exception:
                        return False
            logger.debug(
                f"click_button_by_exact_text({text!r}): clicked but selected "
                "post-condition never satisfied"
            )
            return False

        # Try Playwright role+name match first (most semantic).
        try:
            for role in ('button', 'tab', 'radio', 'menuitemradio', 'menuitem', 'option'):
                loc = self.powerbi_frame.get_by_role(role, name=target, exact=True)
                count = await loc.count()
                for i in range(count):
                    el = loc.nth(i)
                    try:
                        if await _try_click(el):
                            return True
                    except Exception:
                        continue
        except Exception:
            pass

        # Fall back to enumerating button-like AND slicer/chiclet elements
        # and matching exact text. Power BI chiclet slicers (e.g. page 9's
        # 'All / Cyber Incident / Malicious...' row) don't carry standard
        # ARIA roles - they're span.slicerText / div[class*="chiclet"] /
        # role="checkbox" instead, which is why a button-only sweep misses
        # them and the loose fallback ends up clicking the page wrapper.
        candidate_selectors = (
            'button', '[role="button"]', '[role="tab"]', '[role="radio"]',
            '[role="menuitemradio"]', '[role="option"]',
            '[role="checkbox"]', '[role="link"]',
            'span.slicerText',
            'div[class*="chiclet"]',
            'div.slicerItemContainer',
            'div[class*="visual-actionButton"]',
        )
        for sel in candidate_selectors:
            try:
                elements = await self.powerbi_frame.query_selector_all(sel)
            except Exception:
                continue
            for el in elements:
                try:
                    text_actual = (await el.inner_text() or '').strip()
                    if text_actual.casefold() != target_norm:
                        continue
                    if await _try_click(el):
                        return True
                except Exception:
                    continue
        return False

    async def click_filter_option(self, filter_text: str) -> bool:
        """
        Click a filter/radio button option within the Power BI dashboard.
        Uses flexible matching to handle plural/singular variations.

        Args:
            filter_text: Text of the filter option to click (e.g., "All breaches", "Cyber incidents")

        Returns:
            True if click successful
        """
        # Ensure we have valid frame context
        await self._ensure_frame_context()

        if not self.powerbi_frame:
            return False

        try:
            # Normalize the filter text for flexible matching
            filter_lower = filter_text.lower().strip()

            # Variations are STRICTLY plural/singular morphology of the
            # full phrase. We deliberately do NOT add per-word fallbacks
            # ("breaches" alone, etc.) because generic words like
            # "breaches" appear inside large page-wrapper elements, and
            # matching "v in text_lower" with such variations causes the
            # selector to click the entire dashboard body. Page 9's
            # filter chiclet only says "All", so the loose matcher is
            # really only meant to handle morphology drift, not
            # keyword-extraction.
            variations = [filter_lower]
            if filter_lower.endswith('es'):
                variations.append(filter_lower[:-2])
            elif filter_lower.endswith('s'):
                variations.append(filter_lower[:-1])
            else:
                variations.append(filter_lower + 's')

            logger.debug(f"Searching for filter with variations: {variations}")

            # Hard limits on what counts as a clickable filter chiclet/tab.
            # Anything bigger than this is almost certainly a wrapper
            # element, not the actual control.
            MAX_W, MAX_H, MAX_TEXT_LEN = 400, 80, 80

            def _ok_size(box) -> bool:
                if not box:
                    return False
                return (10 <= box['width'] <= MAX_W
                        and 10 <= box['height'] <= MAX_H)

            async def _consider(el) -> Optional[Tuple[float, Any, str]]:
                """Return (area, element, text) if the candidate is a
                plausible filter control, otherwise None.
                """
                try:
                    if not await el.is_visible():
                        return None
                    text = (await el.inner_text() or '').strip()
                    if not text or len(text) > MAX_TEXT_LEN:
                        return None
                    text_lower = text.lower()
                    if not any(text_lower == v for v in variations):
                        # Allow only EXACT lowercased equality - never
                        # substring containment, which is what triggered
                        # the page-wrapper bug on page 9.
                        return None
                    box = await el.bounding_box()
                    if not _ok_size(box):
                        return None
                    return (box['width'] * box['height'], el, text)
                except Exception:
                    return None

            # First, try exact match by text/aria-label. Power BI generally
            # wires these as radio/button so this lands directly on the
            # chiclet on most pages.
            exact_selectors = [
                f'text="{filter_text}"',
                f'[aria-label="{filter_text}"]',
            ]
            for selector in exact_selectors:
                try:
                    element = await self.powerbi_frame.query_selector(selector)
                    if not element:
                        continue
                    if not await element.is_visible():
                        continue
                    box = await element.bounding_box()
                    if not _ok_size(box):
                        # Don't click giant wrappers even when they were
                        # the result of a "text=" match.
                        continue
                    await element.scroll_into_view_if_needed()
                    await element.click()
                    await asyncio.sleep(1.5)
                    logger.info(f"Clicked filter option (exact): {filter_text}")
                    return True
                except Exception:
                    continue

            # Constrained partial-match sweep. Selectors are intentionally
            # narrow (no `div[class*="visual"] div:has-text(...)` which
            # matches the entire chart body). Pick the SMALLEST candidate
            # whose inner_text equals one of the variations exactly.
            partial_selector_templates = [
                '[aria-label*="{}"]',
                'button:has-text("{}")',
                '[role="radio"]:has-text("{}")',
                '[role="checkbox"]:has-text("{}")',
                '[role="button"]:has-text("{}")',
                '[role="option"]:has-text("{}")',
                '[role="menuitemradio"]:has-text("{}")',
                '[role="tab"]:has-text("{}")',
                'span.slicerText:has-text("{}")',
                'div.slicerItemContainer:has-text("{}")',
                'div[class*="chiclet"]:has-text("{}")',
            ]

            best: Optional[Tuple[float, Any, str]] = None
            for variation in variations:
                for template in partial_selector_templates:
                    selector = template.format(variation)
                    try:
                        elements = await self.powerbi_frame.query_selector_all(selector)
                    except Exception:
                        continue
                    for element in elements:
                        candidate = await _consider(element)
                        if candidate is None:
                            continue
                        if best is None or candidate[0] < best[0]:
                            best = candidate

            if best:
                _, el, text = best
                try:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await asyncio.sleep(1.5)
                    logger.info(f"Clicked filter option (smallest match): {text!r} for {filter_text!r}")
                    return True
                except Exception as e:
                    logger.debug(f"Click on smallest match failed: {e}")

            logger.warning(
                f"Could not find filter option: {filter_text} "
                f"(tried variations: {variations})"
            )
            return False

        except Exception as e:
            logger.error(f"Failed to click filter {filter_text}: {e}")
            return False

    async def capture_page_screenshot(self, page_num: int, semester: str, suffix: str = "") -> bytes:
        """
        Capture screenshot of the current dashboard page.

        Args:
            page_num: Page number for naming
            semester: Semester string for organizing screenshots
            suffix: Optional suffix for filter variant screenshots

        Returns:
            Screenshot as bytes
        """
        # Create directory structure
        semester_safe = semester.replace(' ', '_').replace('-', '_')
        page_name = self.DATA_PAGES.get(page_num, f'page_{page_num}')
        suffix_safe = f"_{suffix}" if suffix else ""
        screenshot_path = self.screenshot_dir / semester_safe / f'page_{page_num}_{page_name}{suffix_safe}.png'
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)

        # Take screenshot - capture the iframe element specifically
        try:
            if self.powerbi_frame:
                # Find the iframe element and screenshot it
                iframe_element = await self.page.query_selector('iframe[src*="powerbi"], iframe[title*="Power BI"]')
                if iframe_element:
                    screenshot_bytes = await iframe_element.screenshot()
                else:
                    # Fallback to full page
                    screenshot_bytes = await self.page.screenshot(full_page=False)
            else:
                screenshot_bytes = await self.page.screenshot(full_page=False)
        except Exception as e:
            logger.warning(f"Screenshot capture issue: {e}, using full page")
            screenshot_bytes = await self.page.screenshot(full_page=False)

        # Save screenshot
        with open(screenshot_path, 'wb') as f:
            f.write(screenshot_bytes)

        logger.info(f"Screenshot saved: {screenshot_path}")
        return screenshot_bytes

    async def reset_to_page_one(self) -> bool:
        """
        Reset navigation back to page 1 (Home) by clicking Previous Page until we reach page 1.
        This ensures a clean state before starting a new semester.

        Returns:
            True if successfully reset to page 1
        """
        if not self.powerbi_frame:
            return False

        current = getattr(self, 'current_page', 1)
        if current == 1:
            logger.info("Already on page 1")
            return True

        logger.info(f"Resetting from page {current} back to page 1...")

        try:
            # Click Previous Page repeatedly until we reach page 1
            for _ in range(current - 1):
                prev_button = await self.powerbi_frame.query_selector('button[aria-label="Previous Page"]')
                if not prev_button:
                    logger.warning("Previous Page button not found during reset")
                    break

                is_visible = await prev_button.is_visible()
                is_enabled = await prev_button.is_enabled()

                if not is_visible or not is_enabled:
                    logger.info("Previous Page button not clickable, assuming at page 1")
                    break

                await prev_button.click()
                await asyncio.sleep(0.5)

            self.current_page = 1
            logger.info("Reset to page 1 complete")
            return True

        except Exception as e:
            logger.error(f"Failed to reset to page 1: {e}")
            # Force reset the counter anyway
            self.current_page = 1
            return False

    async def capture_all_pages(self, semester: str) -> Dict[str, bytes]:
        """
        Capture one screenshot per data page for a semester.

        Page 9 ("Top sectors") is special-cased: this view has multiple filter
        chiclets (All breaches / Cyber Incident / Malicious / Human Error /
        System Fault). The default view varies between sessions, and a
        filtered view exposes only sub-counts (1-5 range) instead of the
        unfiltered totals (20-200) we want. We explicitly click 'All breaches'
        before screenshotting page 9 so the captured numbers are real
        notification counts.

        Args:
            semester: Semester being captured

        Returns:
            Dictionary mapping page_key (e.g. "page_2") to screenshot bytes.
        """
        screenshots = {}

        # Note: We assume we're already on page 2 after semester selection.
        # current_page should already be set from navigate_to_page(2) call.

        for page_num in sorted(self.DATA_PAGES.keys()):
            logger.info(f"Processing page {page_num}: {self.DATA_PAGES.get(page_num, 'Unknown')}")

            await self.navigate_to_page(page_num)
            await asyncio.sleep(2)  # Wait for visualizations to render

            # Re-expand after each page navigation (expand mode is lost on page change)
            await self._maximize_powerbi_view()
            await asyncio.sleep(1)

            # PER-PAGE BOOKMARK HANDLING.
            #
            # The "All" / "Cyber Incident" / "Malicious or Criminal Attack"
            # / "Human Error" / "System Fault" elements on page 9 (and
            # similar on page 6) are NOT slicer chiclets - they are Power
            # BI BOOKMARK ACTION BUTTONS. A live DOM probe confirmed it:
            # the ancestor div carries `class="visual visual-actionButton"`
            # and `aria-label="Bookmark . Click here to follow"`. Clicking
            # a bookmark applies a saved report-state snapshot, which on
            # this dashboard ALSO RESETS the period slicer back to the
            # bookmark's saved default (Jan-Jun 2025).
            #
            # Pages 6/7/8 default to the bookmark we want ("All breaches"
            # for page 6, "By time taken only" for pages 7/8) - confirmed
            # by direct visual inspection. So we do nothing for those
            # pages and rely on validators as a safety net.
            #
            # Page 9 limitation: when the slicer is on a NON-default
            # period (e.g. Jul-Dec 2024), Power BI auto-applies a sub-
            # bookmark on page-9 entry (e.g. "System Fault"), giving us
            # a 5x2 sub-view instead of the 5x3 matrix. Clicking the
            # "All" bookmark to fix this RELIABLY resets the period
            # slicer back to default, AND the popup that opens for re-
            # selection then renders empty (Power BI client-side state
            # is corrupted by the bookmark transition). We've exhausted
            # both Playwright click strategies and search-typing for the
            # re-selection. Conclusion: page 9 cannot be reliably
            # captured for non-default periods through this scraper.
            #
            # We let it through with whatever bookmark is currently
            # active. The page-9 Pydantic validator detects rank values
            # / sub-view shapes and quarantines bad output, so the worst
            # outcome is a missing page-9 record for a non-default
            # period - the other 7 pages still capture correctly.
            if page_num == 9:
                slicer_ok = await self._verify_dashboard_shows_semester(
                    semester, max_attempts=2, wait_between=0.4
                )
                if not slicer_ok:
                    logger.warning(
                        f"  Page 9: slicer not on {semester!r} - capturing "
                        "anyway (validator will quarantine if wrong)."
                    )
            # Pre-screenshot slicer-drift check. With filter-bookmark clicks
            # removed this should rarely fire, but it's a cheap insurance
            # against any future Power BI behaviour change. If the slicer
            # drifted off the requested period since the last page, re-
            # select once before screenshotting.
            if not await self._verify_dashboard_shows_semester(
                semester, max_attempts=3, wait_between=0.4
            ):
                logger.warning(
                    f"  Page {page_num}: slicer drifted off {semester!r}; "
                    "re-selecting before screenshot."
                )
                if await self.select_semester(semester):
                    await self.navigate_to_page(page_num)
                    await asyncio.sleep(2)
                    await self._maximize_powerbi_view()
                    await asyncio.sleep(1)
                    if not await self._verify_dashboard_shows_semester(
                        semester, max_attempts=3, wait_between=0.4
                    ):
                        logger.warning(
                            f"  Page {page_num}: slicer remained drifted after "
                            "re-selection; screenshot will likely be quarantined."
                        )
                else:
                    logger.warning(
                        f"  Page {page_num}: re-selection of {semester!r} failed; "
                        "screenshot will likely be quarantined."
                    )

            screenshots[f"page_{page_num}"] = await self.capture_page_screenshot(page_num, semester)

        return screenshots


class DashboardVisionExtractor:
    """Extracts structured data from dashboard screenshots using GPT-4o vision.

    Different model tiers per page:
      - Pages 2-5: gpt-4o-mini is sufficient (KPI numbers, simple bars).
      - Pages 6/7/8/9: gpt-4o (full model) - these have side-by-side
        bar pairs and tiny percentage labels that the mini model
        consistently misreads (current/previous swaps, offset-by-one
        bucket alignment).
    """

    # Per-page model selection. gpt-4o costs ~25x more per call than
    # mini but eliminates the misread errors on multi-bar charts.
    # 7 semesters x 4 chart-heavy pages = 28 extra full-model calls per
    # full-history scrape, which is ~$0.20 - cheap insurance.
    PAGE_MODELS = {
        2: "gpt-4o-mini",
        3: "gpt-4o-mini",
        4: "gpt-4o-mini",
        5: "gpt-4o-mini",
        6: "gpt-4o",
        7: "gpt-4o",
        8: "gpt-4o",
        9: "gpt-4o",
    }

    def __init__(self, api_key: str):
        """
        Initialize the vision extractor.

        Args:
            api_key: OpenAI API key
        """
        if not api_key:
            raise ValueError("OpenAI API key required for vision extraction")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = "gpt-4o-mini"  # Default; per-page override via PAGE_MODELS

    def _encode_image(self, image_bytes: bytes) -> str:
        """Encode image bytes to base64 for API."""
        return base64.b64encode(image_bytes).decode('utf-8')

    def _create_vision_message(self, image_bytes: bytes, prompt: str) -> list:
        """Create a vision API message with image and prompt."""
        base64_image = self._encode_image(image_bytes)
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{base64_image}"
                        }
                    }
                ]
            }
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _call_vision_api(self, image_bytes: bytes, prompt: str, page: Optional[int] = None) -> Dict:
        """
        Call the OpenAI Vision API with retry logic (async). Uses
        PAGE_MODELS[page] when page is given; falls back to self.model.
        """
        messages = self._create_vision_message(image_bytes, prompt)
        model = self.PAGE_MODELS.get(page, self.model) if page else self.model

        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=4096,
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content
        return json.loads(content)

    async def _validated_extract(
        self,
        page: int,
        prompt: str,
        image_bytes: bytes,
        semester: Optional[str] = None,
    ) -> Dict:
        """Run vision extraction for `page` with full validation pipeline.

        Pipeline (in order):
          1. Call vision API.
          2. Validate via per-page Pydantic schema.
          3. Verify displayed_semester echo matches `semester`.
          4. On failure, retry up to 2 more times. The retry is INFORMED:
             we tell the LLM exactly what was wrong and what it produced
             last time, so the retry is a correction pass rather than a
             blind re-call. (A blind re-call on the same image would
             usually produce the same misread.)
          5. After retries exhausted, quarantine and return {}.
        """
        last_payload: Dict = {}
        last_error: Optional[OAICValidationError] = None
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                # Build the prompt: blind on attempt 1, correction-aware
                # on attempts 2+.
                effective_prompt = prompt
                if attempt > 1 and last_error is not None and last_payload:
                    effective_prompt = self._build_correction_prompt(
                        prompt, last_payload, last_error.errors,
                    )

                last_payload = await self._call_vision_api(
                    image_bytes, effective_prompt, page=page,
                )
                validated = validate_page_payload(page, last_payload, semester or "")
                if semester:
                    validate_displayed_semester(page, validated, semester)
                if attempt > 1:
                    logger.info(
                        f"[page {page}] [{semester}] passed on attempt {attempt} "
                        "after correction-feedback retry."
                    )
                return validated
            except OAICValidationError as exc:
                last_error = exc
                is_semester_mismatch = any(
                    "displayed_semester" in m for m in exc.errors
                )
                # Wrong-period screenshots can't be fixed by retrying -
                # quarantine immediately.
                if is_semester_mismatch:
                    quarantine_extraction(
                        QUARANTINE_DIR, semester or "unknown_semester", page,
                        image_bytes, last_payload, exc.errors,
                    )
                    logger.error(
                        f"[page {page}] [{semester}] screenshot shows wrong "
                        f"period ({exc.errors[0]}); page skipped (no retry "
                        "would help - same image)."
                    )
                    return {}
                if attempt < max_attempts:
                    # INFO level: only the FINAL outcome of the retry chain
                    # is actionable. A failed attempt followed by a passing
                    # retry is a non-event from the user's perspective, so
                    # this stays out of the end-of-run WARNING/ERROR summary.
                    logger.info(
                        f"[page {page}] [{semester}] validation failed "
                        f"(attempt {attempt}/{max_attempts}): {exc.errors}. "
                        "Retrying with correction feedback."
                    )
                    continue
                # All attempts exhausted.
                quarantine_extraction(
                    QUARANTINE_DIR, semester or "unknown_semester", page,
                    image_bytes, last_payload, exc.errors,
                )
                logger.error(
                    f"[page {page}] [{semester}] validation failed all "
                    f"{max_attempts} attempts; page skipped. Errors: {exc.errors}"
                )
                return {}
            except json.JSONDecodeError as exc:
                if attempt < max_attempts:
                    # INFO: same rationale as the validation-failure retry
                    # above - a transient JSON decode failure that gets
                    # rescued by retry isn't worth alerting on.
                    logger.info(
                        f"[page {page}] [{semester}] vision JSON decode failed "
                        f"(attempt {attempt}/{max_attempts}): {exc}. Retrying."
                    )
                    last_error = OAICValidationError(
                        page=page, semester=semester or "",
                        errors=[f"JSONDecodeError: {exc}"],
                    )
                    continue
                quarantine_extraction(
                    QUARANTINE_DIR, semester or "unknown_semester", page,
                    image_bytes, last_payload,
                    [f"JSONDecodeError: {exc}"],
                )
                return {}
            except Exception as exc:
                # Network / API errors: retry, otherwise log and skip.
                if attempt < max_attempts:
                    # INFO: transient API errors that get retried successfully
                    # don't need to bubble up as WARNINGs.
                    logger.info(
                        f"[page {page}] [{semester}] vision API error "
                        f"(attempt {attempt}/{max_attempts}): {exc}. Retrying."
                    )
                    continue
                logger.error(
                    f"[page {page}] [{semester}] vision API error all "
                    f"{max_attempts} attempts: {exc}"
                )
                return {}
        return {}

    @staticmethod
    def _build_correction_prompt(
        original_prompt: str,
        previous_payload: Dict,
        errors: List[str],
    ) -> str:
        """Build a correction-aware retry prompt that includes the LLM's
        previous (wrong) answer and the specific validator errors. This
        converts the retry from a blind re-call - which on the same
        image at low temperature usually yields the same misread - into
        a focused correction pass.
        """
        try:
            prev_json = json.dumps(previous_payload, indent=2, ensure_ascii=False)
        except Exception:
            prev_json = str(previous_payload)
        error_list = "\n".join(f"  - {e}" for e in errors) if errors else "  (none)"

        return (
            f"{original_prompt}\n\n"
            "===============================================================\n"
            "RETRY: your previous extraction was rejected by validators.\n"
            "===============================================================\n\n"
            "Your previous JSON output was:\n"
            f"```json\n{prev_json}\n```\n\n"
            "The validator rejected it with these errors:\n"
            f"{error_list}\n\n"
            "RE-EXAMINE THE SCREENSHOT, focusing on the specific bars / "
            "buckets that produced wrong sums or values. Common failure "
            "modes:\n"
            "  - Misreading a single digit (e.g. '64' read as '84' - they "
            "look similar at small font sizes).\n"
            "  - Reading the wrong bar of a side-by-side pair (LEFT bar = "
            "previous semester, RIGHT bar = current semester).\n"
            "  - Skipping a tiny bar (0% / 1% bars often missed).\n\n"
            "Return a CORRECTED JSON in the SAME schema as before. Before "
            "returning, verify the sums match expectations. Output ONLY "
            "the JSON, no commentary."
        )

    async def extract_snapshot_data(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """
        Extract data from Page 2: Snapshot. Page 2 is the AUTHORITATIVE source
        for per-semester counts (the page-9 'Top 5 sectors by source' view does
        NOT show real totals - it shows source-breakdowns by category).

        Page 2 contains:
        - "Show results for: <semester>" label (top-left) - we read this back
          to verify the dashboard actually applied the requested semester
          filter (silent-selection-failure protection)
        - Total notifications KPI ('532', with % change vs previous semester)
        - Notifications by month bar chart (one bar per Jan/Feb/.../Dec/etc.)
        - "Sources of data breaches" donut (Human error % / Malicious % / System fault %)
        - Cyber incident breakdown horizontal bars (% per attack type)
        - "Top 5 sectors to notify data breaches, by notifications received" -
          5 bars with COUNTS above each bar and a sector icon below it
        - "% of data breaches affected 100 people or fewer" KPI
        - Top causes of human error breaches (3 icons with %)
        """
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Snapshot"
page (page 2). Read the entire page carefully.

Top-left: "Show results for: <Jan-Jun YYYY> | <Jul-Dec YYYY>" - this tells
you which semester this snapshot is for. Return that label EXACTLY in
'displayed_semester' so we can verify the screenshot is for the expected
period (silent-semester-selection-failure protection).

Center-top: big number "Notifications received: <N>" plus "Down -10% compared
to <previous semester>" or similar.

Center-top: small bar chart "Notifications received by month" with one bar
per calendar month showing a labelled count (Jan=60, Feb=83, etc.).

Top-right: donut chart "Sources of data breaches" with three slices:
Human error %, Malicious or criminal attack %, System fault %.

Middle-right: "Top 5 sectors to notify data breaches, by notifications
received" - 5 bars with the COUNT printed ABOVE each bar and a sector
icon BELOW. Icons map as follows:
  map of Australia  -> Australian Government
  graduation cap    -> Education
  coin stack        -> Finance (incl. superannuation)
  heart             -> Health service providers
  scales of justice -> Legal, accounting & management services
A non-standard sector (e.g. Insurance, Recruitment Agencies, Retail) may
appear if it's actually in the top 5 - read its label too.

Lower-left: "Cyber incident breakdown" horizontal bars with % labels for:
phishing, compromised credentials, ransomware, hacking, brute-force, malware.

Lower-middle: "<XX>% of data breaches affected 100 people or fewer".

Lower-right: "Top causes of human error breaches" - 3 icons with %.

Return ONLY valid JSON in this shape (use null when truly unreadable):

{
  "displayed_semester": "Jan-Jun 2025",
  "total_notifications": 532,
  "change_from_previous": "-10%",
  "monthly_notifications": [
    {"month": "Jan", "count": 60},
    {"month": "Feb", "count": 83}
  ],
  "human_error_pct": 37,
  "malicious_attacks_pct": 59,
  "system_faults_pct": 3,
  "cyber_incidents": {
    "phishing_pct": 28,
    "compromised_credentials_pct": 21,
    "ransomware_pct": 21,
    "hacking_pct": 17,
    "brute_force_pct": 6,
    "malware_pct": 4
  },
  "top_sectors": [
    {"sector": "Health service providers",                "notifications": 96},
    {"sector": "Finance (incl. superannuation)",          "notifications": 73},
    {"sector": "Australian Government",                   "notifications": 67},
    {"sector": "Education",                               "notifications": 38},
    {"sector": "Legal, accounting & management services", "notifications": 37}
  ],
  "small_breaches_100_or_fewer_pct": 67,
  "human_error_causes": {
    "wrong_recipient_email_pct":   44,
    "unauthorised_disclosure_pct": 22,
    "failure_to_use_bcc_pct":       9
  }
}

CRITICAL:
- Numbers are LITERAL - read them off the page, don't infer.
- top_sectors counts are real notification counts (15-200 range typically).
  NEVER return 1-5 for a top-5 sector count - that would be a rank position
  from a different (filtered) view, not the count.
- If a value is genuinely missing/illegible, return null - do not guess.
"""

        try:
            return await self._validated_extract(2, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract snapshot data: {e}")
            return {}

    async def extract_notifications_data(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 3: Notifications received during reporting period."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Notifications received during the reporting period".

Extract ALL visible data including:
1. Monthly notification counts (bar chart showing Jan-Jun or Jul-Dec)
2. Notification breakdown by type:
   - Malicious attack
   - Human error
   - System fault
3. Any trend comparisons with previous period

Return ONLY valid JSON:
{
    "monthly_notifications": [
        {"month": "<month name>", "count": <int or null>},
        ...
    ],
    "by_type": {
        "malicious_attack": <int or null>,
        "human_error": <int or null>,
        "system_fault": <int or null>
    },
    "trend_comparison": "<description or null>"
}"""

        try:
            return await self._validated_extract(3, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract notifications data: {e}")
            return {}

    async def extract_individuals_affected(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 4: Number of individuals affected by breaches.

        The dashboard's actual buckets are listed below. Note the SEPARATE
        25,001-50,000 bucket (older versions of this prompt rolled it into
        10,001-50,000), and the 1,000,001-10,000,000 upper bucket (older
        versions used 1,000,001-5,000,000 which is the wrong upper bound).
        """
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's
"Number of individuals affected by breaches" page (page 4).

Top-left: "Show results for: <Jan-Jun YYYY>" - return that label EXACTLY in
'displayed_semester' for verification.

Left side: horizontal bar chart "Number of individuals worldwide affected
by breaches" with one bar per range, count printed at the right end of
each bar. The exact buckets shown are (in this order, top to bottom):

  1
  2-10
  11-100
  101-1,000
  1,001-5,000
  5,001-10,000
  10,001-25,000
  25,001-50,000
  50,001-100,000
  100,001-250,000
  250,001-500,000
  1,000,001-10,000,000
  Unknown

Right side: a small table "Large-scale data breaches affecting Australians"
showing breakdown for the previous and current semester at three large
buckets (100,001-250,000 / 250,001-500,000 / 500,001-1,000,000). Capture
this too if visible.

Return ONLY valid JSON:
{
  "displayed_semester": "Jan-Jun 2025",
  "individuals_affected_distribution": [
    {"range": "1",                     "count": 151},
    {"range": "2-10",                  "count": 94},
    {"range": "11-100",                "count": 109},
    {"range": "101-1,000",             "count": 102},
    {"range": "1,001-5,000",           "count": 32},
    {"range": "5,001-10,000",          "count": 10},
    {"range": "10,001-25,000",         "count": 9},
    {"range": "25,001-50,000",         "count": 4},
    {"range": "50,001-100,000",        "count": 5},
    {"range": "100,001-250,000",       "count": 1},
    {"range": "250,001-500,000",       "count": 3},
    {"range": "1,000,001-10,000,000",  "count": 2},
    {"range": "Unknown",               "count": 10}
  ],
  "large_scale_australians": [
    {"range": "100,001-250,000",     "previous_semester": 3, "current_semester": 3},
    {"range": "250,001-500,000",     "previous_semester": 3, "current_semester": 3},
    {"range": "500,001-1,000,000",   "previous_semester": 1, "current_semester": null}
  ]
}

CRITICAL:
- Read counts LITERALLY. If a bucket shows "151" return 151, not 1 or 5.
- If a bucket has no bar visible, return 0 (not null) for that bucket.
- Use null only when text is genuinely unreadable.
"""

        try:
            return await self._validated_extract(4, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract individuals affected data: {e}")
            return {}

    async def extract_personal_info_types(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 5: Kinds of personal information involved."""
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Kinds of
personal information involved in breaches" page (page 5).

Top-left: "Show results for: <Jan-Jun YYYY>" - return that label EXACTLY
in 'displayed_semester' for verification.

The page shows TWO horizontal-bar charts:

1. Top: "Kinds of personal information involved in breaches" - 6 categories
   with COUNT printed at the right of each bar:
     - Contact information       (e.g. 456)
     - Identity information      (e.g. 303)
     - Financial details         (e.g. 194)
     - Health information        (e.g. 161)
     - Tax File Numbers          (e.g. 116)
     - Other sensitive information (e.g. 105)

2. Bottom: "Data breaches involving Digital ID and CDR data" - 2 small bars:
     - Consumer Data Right data  (e.g. 0)
     - Digital ID information/documents (e.g. 0)

Return ONLY valid JSON:
{
  "displayed_semester": "Jan-Jun 2025",
  "personal_info_types": {
    "contact_information":       456,
    "identity_information":      303,
    "financial_details":         194,
    "health_information":        161,
    "tax_file_numbers":          116,
    "other_sensitive_information": 105,
    "consumer_data_right":       0,
    "digital_id":                0
  }
}

CRITICAL: Read the displayed counts LITERALLY. They are typically 50-500
for the main 6 categories. Use null only if a count is genuinely unreadable.
"""

        try:
            return await self._validated_extract(5, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract personal info types data: {e}")
            return {}

    async def extract_breach_sources(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 6: Source of breaches."""
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Source of
breaches" page (page 6).

CRITICAL ORIENTATION RULES:
- The slicer dropdown at top-left labelled "Show results for" shows the
  CURRENT semester being displayed. Return its text EXACTLY in
  'displayed_semester'. Both halves are valid: "Jan-Jun YYYY" or
  "Jul-Dec YYYY". DO NOT read this from the chart legend - the chart
  legend lists BOTH the previous AND current period.
- 'displayed_semester' MUST equal 'current_period_label'. They are the
  same thing - the period currently visible.
- In the chart's legend (above the bars), the OLDER period is listed
  FIRST and the NEWER period is listed SECOND. The slicer always shows
  the NEWER (current) period.
- For each category there are TWO bars side-by-side. The LEFT bar in
  each pair is the PREVIOUS (older) semester. The RIGHT bar is the
  CURRENT (newer) semester - the one shown in the slicer.

The chart "Source of data breaches - all" has three categories, each with
a previous-bar and a current-bar:
  Human error
  Malicious or criminal attack
  System fault

Each bar has a numeric label printed above it. Counts are typically 5-500.

Return ONLY valid JSON:
{
  "displayed_semester":     "<text from slicer dropdown - e.g. Jul-Dec 2024>",
  "current_period_label":   "<same as displayed_semester>",
  "previous_period_label":  "<the OTHER period from the legend>",
  "breach_sources": {
    "human_error":      {"current_period": <RIGHT bar>, "previous_period": <LEFT bar>},
    "malicious_attack": {"current_period": <RIGHT bar>, "previous_period": <LEFT bar>},
    "system_fault":     {"current_period": <RIGHT bar>, "previous_period": <LEFT bar>}
  }
}

Self-check before returning: 'current_period_label' MUST exactly match
'displayed_semester'. If it doesn't, you've swapped current and previous
- swap them before returning.
"""

        try:
            return await self._validated_extract(6, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract breach sources data: {e}")
            return {}

    async def extract_time_to_identify(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 7: Time taken to identify breaches."""
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Time taken
to identify breaches" page (page 7), with the "By time taken only" tab
selected.

CRITICAL ORIENTATION RULES:
- The slicer dropdown at top-left labelled "Show results for" shows the
  CURRENT semester. Return its text EXACTLY in 'displayed_semester'.
  Both halves are valid: "Jan-Jun YYYY" or "Jul-Dec YYYY". DO NOT read
  this from the chart legend.
- 'displayed_semester' MUST equal 'current_period_label'.
- Chart legend (above bars): OLDER period FIRST, NEWER (current) SECOND.
- For each time bucket there are TWO bars side-by-side. LEFT bar in
  each pair = PREVIOUS semester. RIGHT bar in each pair = CURRENT
  semester (the slicer's value).

Time buckets are EXACTLY these five (in this order, left to right):
  Unknown / <= 10 days / 11-20 days / 21-30 days / > 30 days

(Use ASCII "<= 10 days" and "> 30 days" in your output - do NOT use the
unicode characters ≤ or > variants).

Return ONLY valid JSON:
{
  "displayed_semester":     "<text from slicer - e.g. Jul-Dec 2024>",
  "current_period_label":   "<same as displayed_semester>",
  "previous_period_label":  "<the OTHER period from the legend>",
  "time_to_identify_pct": [
    {"bucket": "Unknown",     "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "<= 10 days",  "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "11-20 days",  "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "21-30 days",  "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "> 30 days",   "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>}
  ]
}

Self-checks BEFORE returning:
1. 'current_period_label' MUST exactly match 'displayed_semester'. If
   not, swap current and previous everywhere.
2. The 5 'current_pct' values MUST sum to 100 ±2. If not, you have
   misread bars - re-examine the RIGHT bar of each pair carefully.
3. The 5 'previous_pct' values MUST sum to 100 ±2. Same check for the
   LEFT bar of each pair.
4. Read EVERY bar's printed percentage label. Even tiny bars (e.g. 0%
   or 1% for Unknown) have a label - don't skip them.
"""

        try:
            return await self._validated_extract(7, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract time to identify data: {e}")
            return {}

    async def extract_time_to_notify(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 8: Time taken to notify the OAIC."""
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Time taken
to notify the OAIC of breaches" page (page 8), with the "By time taken only"
tab selected.

CRITICAL ORIENTATION RULES:
- The slicer dropdown at top-left labelled "Show results for" shows the
  CURRENT semester. Return its text EXACTLY in 'displayed_semester'.
  Both halves are valid: "Jan-Jun YYYY" or "Jul-Dec YYYY". DO NOT read
  this from the chart legend.
- 'displayed_semester' MUST equal 'current_period_label'.
- Chart legend (above bars): OLDER period FIRST, NEWER (current) SECOND.
- For each time bucket there are TWO bars side-by-side. LEFT bar in
  each pair = PREVIOUS semester. RIGHT bar in each pair = CURRENT
  semester (the slicer's value).

Time buckets are EXACTLY these five (in this order, left to right):
  Unknown / <= 10 days / 11-20 days / 21-30 days / > 30 days

(Use ASCII "<= 10 days" and "> 30 days" - do NOT use ≤ / > unicode.)

Return ONLY valid JSON:
{
  "displayed_semester":     "<text from slicer - e.g. Jul-Dec 2024>",
  "current_period_label":   "<same as displayed_semester>",
  "previous_period_label":  "<the OTHER period from the legend>",
  "time_to_notify_pct": [
    {"bucket": "Unknown",     "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "<= 10 days",  "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "11-20 days",  "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "21-30 days",  "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>},
    {"bucket": "> 30 days",   "current_pct": <RIGHT bar %>, "previous_pct": <LEFT bar %>}
  ]
}

Self-checks BEFORE returning:
1. 'current_period_label' MUST exactly match 'displayed_semester'. If
   not, swap current and previous everywhere.
2. The 5 'current_pct' values MUST sum to 100 ±2. If not, re-read the
   RIGHT bar of each pair.
3. The 5 'previous_pct' values MUST sum to 100 ±2. Re-read the LEFT
   bar of each pair if not.
4. Read EVERY bar's printed percentage label. Tiny bars (0% / 1%) ALSO
   have labels - don't skip them.
"""

        try:
            return await self._validated_extract(8, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract time to notify data: {e}")
            return {}

    async def extract_top_sectors(self, image_bytes: bytes, semester: Optional[str] = None) -> Dict:
        """Extract data from Page 9 with the 'All' filter selected.

        Page 9 with 'All' shows a 5-sectors x 3-causes matrix:
          rows:    Australian Government / Education / Finance / Health / Legal
          columns: Human error / Malicious or criminal attack / System fault
        Each cell has a count above its bar (or no bar if 0).
        Per-sector total = sum across the 3 causes.
        """
        prompt = """\
You are looking at the OAIC Notifiable Data Breaches dashboard's "Top 5
sectors by source of breaches" page (page 9), with the 'All' filter selected.

Top-left: "Show results for: <Jan-Jun YYYY>" - return that label EXACTLY
in 'displayed_semester' for verification.

The chart is split into THREE COLUMNS (left to right): "Human error",
"Malicious or criminal attack", "System fault". Each column shows the SAME
five sector icons in the SAME left-to-right order:
  1. Australian Government (map of Australia icon)
  2. Education (graduation cap icon)
  3. Finance (incl. superannuation) (coin stack icon)
  4. Health service providers (heart icon)
  5. Legal, accounting & management services (scales icon)

Above each icon is a small bar with a numeric label (count). When there
is no bar above an icon the count is 0.

Return ONLY valid JSON. ALWAYS include all 5 sectors and all 3 causes,
using 0 (NOT null) for empty cells:
{
  "displayed_semester": "Jan-Jun 2025",
  "sector_by_source": [
    {"sector": "Australian Government",                  "human_error": 32, "malicious_or_criminal": 32, "system_fault": 3},
    {"sector": "Education",                              "human_error": 28, "malicious_or_criminal": 6,  "system_fault": 2},
    {"sector": "Finance (incl. superannuation)",         "human_error": 22, "malicious_or_criminal": 48, "system_fault": 1},
    {"sector": "Health service providers",               "human_error": 47, "malicious_or_criminal": 42, "system_fault": 3},
    {"sector": "Legal, accounting & management services","human_error": 5,  "malicious_or_criminal": 32, "system_fault": 0}
  ]
}

CRITICAL:
- Read counts LITERALLY off the bars. Real per-cell values are 0-150.
- If you see ranks (1, 2, 3, 4, 5) you are looking at a different filtered view -
  return null for the affected cells.
"""

        try:
            return await self._validated_extract(9, prompt, image_bytes, semester)
        except Exception as e:
            logger.error(f"Failed to extract top sectors data: {e}")
            return {}

    async def extract_page(
        self,
        page_num: int,
        image_bytes: bytes,
        semester: Optional[str] = None,
    ) -> Dict:
        """
        Extract data from a specific page.

        Args:
            page_num: Page number (2-9)
            image_bytes: Screenshot image bytes
            semester: Requested semester string, used for displayed_semester
                echo verification (rank 2). Optional but strongly recommended.

        Returns:
            Extracted data dictionary
        """
        extractors = {
            2: self.extract_snapshot_data,
            3: self.extract_notifications_data,
            4: self.extract_individuals_affected,
            5: self.extract_personal_info_types,
            6: self.extract_breach_sources,
            7: self.extract_time_to_identify,
            8: self.extract_time_to_notify,
            9: self.extract_top_sectors,
        }

        extractor = extractors.get(page_num)
        if extractor:
            return await extractor(image_bytes, semester=semester)
        else:
            logger.warning(f"No extractor for page {page_num}")
            return {}

    def consolidate_period_data(self, extractions: Dict[int, Dict], semester: str) -> Dict:
        """
        Consolidate extracted data from all pages into final period structure.

        Args:
            extractions: Dictionary mapping page_num to extracted data
            semester: Semester string (e.g., "Jan-Jun 2025")

        Returns:
            Consolidated data matching oaic_cyber_statistics JSON format
        """
        # Parse semester
        semester_match = re.match(r'(Jan|Jul)-(Jun|Dec)\s+(\d{4})', semester)
        if semester_match:
            start_month = 1 if semester_match.group(1) == 'Jan' else 7
            end_month = 6 if semester_match.group(2) == 'Jun' else 12
            year = int(semester_match.group(3))
            period = 'H1' if start_month == 1 else 'H2'
        else:
            # Fallback
            year = datetime.now().year
            period = 'H1'
            start_month = 1
            end_month = 6

        # Start with base structure
        result = {
            "title": f"Notifiable Data Breaches Report: {semester}",
            "url": OAICDashboardController.BASE_URL,
            "year": year,
            "period": period,
            "quarter": period,
            "start_month": start_month,
            "end_month": end_month,
            "source": "dashboard",
            "scraped_at": datetime.now().isoformat()
        }

        # Extract from snapshot (page 2)
        snapshot = extractions.get(2, {})
        result["total_notifications"] = snapshot.get("total_notifications")

        # Source counts: prefer page 6 (dedicated "Source of breaches" view with
        # integer counts) over page 2's snapshot donut, which the LLM has to
        # derive from percentages (lossy and prone to ~30% rounding gaps).
        breach_sources = (extractions.get(6, {}) or {}).get("breach_sources") or {}

        def _from_p6(key: str):
            entry = breach_sources.get(key) or {}
            v = entry.get("current_period")
            return v if isinstance(v, (int, float)) and v > 0 else None

        result["malicious_attacks"] = (
            _from_p6("malicious_attack") or snapshot.get("malicious_attacks")
        )
        result["human_error"] = (
            _from_p6("human_error") or snapshot.get("human_error")
        )
        result["system_faults"] = (
            _from_p6("system_fault") or snapshot.get("system_faults")
        )

        # Calculate cyber incidents from percentages
        cyber = snapshot.get("cyber_incidents", {})
        if result["total_notifications"] and result["malicious_attacks"]:
            # Cyber incidents are subset of malicious attacks
            result["cyber_incidents_total"] = result["malicious_attacks"]
            if result["malicious_attacks"] and result["total_notifications"]:
                result["cyber_incidents_percentage"] = round(
                    (result["malicious_attacks"] / result["total_notifications"]) * 100, 1
                )

        # Attack type breakdowns - calculate counts from percentages
        # The cyber_incidents dict contains percentages of malicious attacks
        malicious_count = result.get("malicious_attacks") or 0
        if malicious_count and cyber:
            # Calculate absolute counts from percentages
            phishing_pct = cyber.get("phishing_pct")
            result["phishing"] = round(malicious_count * phishing_pct / 100) if phishing_pct else None

            ransomware_pct = cyber.get("ransomware_pct")
            result["ransomware"] = round(malicious_count * ransomware_pct / 100) if ransomware_pct else None

            hacking_pct = cyber.get("hacking_pct")
            result["hacking"] = round(malicious_count * hacking_pct / 100) if hacking_pct else None

            brute_force_pct = cyber.get("brute_force_pct")
            result["brute_force"] = round(malicious_count * brute_force_pct / 100) if brute_force_pct else None

            malware_pct = cyber.get("malware_pct")
            result["malware"] = round(malicious_count * malware_pct / 100) if malware_pct else None

            compromised_pct = cyber.get("compromised_credentials_pct")
            result["compromised_credentials"] = round(malicious_count * compromised_pct / 100) if compromised_pct else None
        else:
            result["phishing"] = None
            result["ransomware"] = None
            result["hacking"] = None
            result["brute_force"] = None
            result["malware"] = None
            result["compromised_credentials"] = None

        # Top sectors. Page 2's snapshot is the AUTHORITATIVE source -
        # it lists the top-5 sectors with real per-sector notification
        # counts read from the bar labels. Page 9's "sector_by_source"
        # is a different shape (5x3 matrix of source-causes per sector)
        # and the per-sector total can only be derived by summing its
        # three source columns - which we do as a fallback when page 9
        # is available AND page 2's snapshot didn't yield counts.
        result["top_sectors"] = []
        for sector_info in snapshot.get("top_sectors", []):
            result["top_sectors"].append({
                "sector": sector_info.get("sector"),
                # FIX: was `None`. The snapshot DOES have a real
                # notifications integer per sector - the previous code
                # threw it away, leaving every entry useless and breaking
                # the dashboard's "OAIC: Top Affected Sectors" chart.
                "notifications": sector_info.get("notifications"),
            })

        # Page-9 fallback: when the snapshot didn't supply counts (e.g.
        # the LLM missed those bars) AND we have a clean page-9 matrix,
        # sum its 3 source columns per sector to derive a total.
        if result["top_sectors"] and any(
            entry["notifications"] is None for entry in result["top_sectors"]
        ):
            p9_totals: Dict[str, int] = {}
            for row in (extractions.get(9, {}).get("sector_by_source") or []):
                sector_name = row.get("sector")
                if not sector_name:
                    continue
                total = 0
                for col in ("human_error", "malicious_or_criminal", "system_fault"):
                    v = row.get(col)
                    if isinstance(v, (int, float)):
                        total += int(v)
                if total > 0:
                    p9_totals[sector_name] = total
            for entry in result["top_sectors"]:
                if entry["notifications"] is None and entry["sector"] in p9_totals:
                    entry["notifications"] = p9_totals[entry["sector"]]

        # Individuals affected distribution
        individuals = extractions.get(4, {})
        result["individuals_affected_distribution"] = individuals.get("individuals_affected_distribution", [])

        # Key findings
        result["key_findings"] = []

        # PDF-related fields (not applicable for dashboard)
        result["pdf_url"] = None
        result["pdf_parsed"] = False

        return result


def validate_extracted_data(data: Dict) -> Tuple[bool, List[str]]:
    """
    Validate extracted data meets expected structure and ranges.

    Args:
        data: Extracted period data

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []

    # Required fields
    required = ['year', 'period']
    for field in required:
        if field not in data or data[field] is None:
            errors.append(f"Missing required field: {field}")

    # Range validations
    total = data.get('total_notifications')
    if total is not None:
        if total < 50 or total > 2000:
            errors.append(f"Suspicious total_notifications: {total} (expected 50-2000)")

    # Percentage validations
    for pct_field in ['cyber_incidents_percentage']:
        pct = data.get(pct_field)
        if pct is not None and not (0 <= pct <= 100):
            errors.append(f"Invalid percentage for {pct_field}: {pct}")

    # Source counts should sum approximately to total
    malicious = data.get('malicious_attacks') or 0
    human = data.get('human_error') or 0
    system = data.get('system_faults') or 0
    source_sum = malicious + human + system

    if total and source_sum > 0:
        # Allow 10% tolerance
        if source_sum < total * 0.9 or source_sum > total * 1.1:
            errors.append(f"Source sum ({source_sum}) doesn't match total ({total})")

    return len(errors) == 0, errors


def drop_duplicated_h1_h2(records: List[Dict]) -> List[Dict]:
    """Drop H2 records that are byte-for-byte identical to the same year's H1.

    This catches the silent-mislabel bug where Power BI's slicer didn't actually
    change semester selection (e.g. ``select_semester`` reported success but the
    dashboard kept showing H1 numbers): the H2 record then carries pixels from
    H1 and is therefore wrong by definition. Real H1 vs H2 numbers always
    differ on at least one of the canonical counts, so equality is a reliable
    signal of mislabel rather than coincidence.
    """
    by_year_period: Dict[Tuple[int, str], Dict] = {}
    for r in records:
        y, p = r.get('year'), r.get('period')
        if isinstance(y, int) and p in ('H1', 'H2'):
            by_year_period[(y, p)] = r

    canonical_fields = (
        'total_notifications',
        'malicious_attacks',
        'human_error',
        'system_faults',
        'phishing',
        'ransomware',
        'hacking',
        'malware',
        'brute_force',
    )

    drop_ids = set()
    for (year, period), rec in list(by_year_period.items()):
        if period != 'H2':
            continue
        h1 = by_year_period.get((year, 'H1'))
        if not h1:
            continue
        # Both must have at least one non-None canonical count for the comparison
        # to be meaningful; an all-empty pair is its own (separate) problem.
        h1_vals = tuple(h1.get(f) for f in canonical_fields)
        h2_vals = tuple(rec.get(f) for f in canonical_fields)
        if any(v is not None for v in h1_vals) and h1_vals == h2_vals:
            logger.error(
                f"[{year} H2] Identical to H1 across all canonical counts "
                f"({h1_vals}). Discarding H2 - likely a silent semester-selection "
                f"failure. Re-run after the dropdown fix to capture real H2 data."
            )
            drop_ids.add(id(rec))

    return [r for r in records if id(r) not in drop_ids]


def merge_with_existing_oaic_data(new_data: List[Dict], existing_file: str = None) -> Tuple[List[Dict], List[Dict]]:
    """
    Merge dashboard-scraped data with existing PDF-scraped data.

    Priority rules:
    1. For periods where PDF data exists (pre-2025), use PDF data as authoritative
    2. For 2025+ periods, use dashboard-scraped data
    3. Dashboard data for historical periods is saved separately for validation

    Args:
        new_data: Newly scraped dashboard data
        existing_file: Path to existing OAIC data file (optional)

    Returns:
        Tuple of (merged_data, validation_comparison)
    """
    # Load existing PDF-scraped data
    pdf_data = []
    if existing_file and os.path.exists(existing_file):
        with open(existing_file) as f:
            pdf_data = json.load(f)
    else:
        # Find most recent oaic_cyber_statistics file
        files = sorted(glob.glob('oaic_cyber_statistics_*.json'))
        for fpath in reversed(files):
            try:
                with open(fpath) as f:
                    content = json.load(f)
                    # Check if it's from PDF scraper (no 'source' field or source != 'dashboard')
                    if content and isinstance(content, list):
                        if content[0].get('source') != 'dashboard':
                            pdf_data = content
                            logger.info(f"Using existing PDF data from: {fpath}")
                            break
            except Exception:
                continue

    # Index by period
    pdf_by_period = {f"{d['year']} {d['period']}": d for d in pdf_data}
    new_by_period = {f"{d['year']} {d['period']}": d for d in new_data}

    merged = []
    validation_comparison = []

    all_periods = sorted(set(pdf_by_period.keys()) | set(new_by_period.keys()))

    for period_key in all_periods:
        pdf_rec = pdf_by_period.get(period_key)
        new_rec = new_by_period.get(period_key)

        # Field-level merge: dashboard data wins where present, PDF fills nulls.
        # This replaces the old "PDF authoritative for years <2025" rule, which
        # silently discarded fresh dashboard scrapes for 2022-2024 even when
        # the PDF data had null attack-type counts.
        if new_rec and pdf_rec:
            combined = dict(pdf_rec)
            for k, v in new_rec.items():
                if v is not None and v != [] and v != {}:
                    combined[k] = v
            combined.setdefault('source', 'dashboard')
            merged.append(combined)
            validation_comparison.append({
                'period': period_key,
                'pdf_data': pdf_rec,
                'dashboard_data': new_rec,
            })
            logger.info(f"Merged dashboard+PDF data for {period_key} (dashboard preferred)")
        elif new_rec:
            merged.append(new_rec)
            logger.info(f"Using dashboard data for {period_key}")
        elif pdf_rec:
            merged.append(pdf_rec)
            logger.info(f"Using PDF data for {period_key} (no dashboard scrape)")

    return merged, validation_comparison


# Earliest semester present on the OAIC dashboard.
EARLIEST_SEMESTER_YEAR = 2020


async def _discover_available_semesters(browser: Browser, headless: bool) -> set:
    """Open the dashboard once, query the semester dropdown's actual
    options, and return the set of semester strings present. Used as a
    pre-flight filter so we don't log ERRORs for semesters that simply
    aren't published yet on OAIC's side.

    Returns an empty set on any failure - caller treats that as
    "discovery skipped" and proceeds with the unfiltered request.
    """
    controller = OAICDashboardController(
        headless=headless,
        screenshot_dir="oaic_screenshots/_discovery",
        browser=browser,
    )
    try:
        await controller.launch_browser()
        if not await controller.navigate_to_dashboard():
            return set()
        await controller.navigate_to_page(2)
        await asyncio.sleep(2)
        await controller._maximize_powerbi_view()

        # Read dropdown options via the existing get_available_semesters
        # path (which opens the dropdown and scrapes option text).
        options = await controller.get_available_semesters()
        # Normalise to canonical "Jan-Jun YYYY" / "Jul-Dec YYYY".
        canonical: set = set()
        for opt in options:
            if not opt:
                continue
            s = re.sub(r"\s+", " ", str(opt).strip())
            if re.match(r"^(Jan-Jun|Jul-Dec)\s+\d{4}$", s):
                canonical.add(s)
        logger.info(f"Pre-flight discovered {len(canonical)} semesters on dashboard")
        return canonical
    finally:
        try:
            await controller.close()
        except Exception:
            pass


def generate_known_semesters(today: Optional[datetime] = None) -> List[str]:
    """Return the list of OAIC semesters expected to be live on the dashboard.

    OAIC publishes H1 (Jan-Jun) reports in August-September of the same year,
    and H2 (Jul-Dec) reports in February-March of the following year. We use
    the end of each publication window (Sep for H1, Mar for H2) as a
    conservative availability cutoff so we don't waste a browser session
    trying to select a semester that hasn't been published yet.

    Returns semesters newest-first.
    """
    today = today or datetime.now()

    def h1_available(y: int) -> bool:
        # H1 of year y published Aug-Sep of year y.
        return today.year > y or (today.year == y and today.month >= 9)

    def h2_available(y: int) -> bool:
        # H2 of year y published Feb-Mar of year y+1.
        return today.year > y + 1 or (today.year == y + 1 and today.month >= 3)

    semesters: List[str] = []
    for year in range(today.year, EARLIEST_SEMESTER_YEAR - 1, -1):
        if h2_available(year):
            semesters.append(f"Jul-Dec {year}")
        if h1_available(year):
            semesters.append(f"Jan-Jun {year}")
    return semesters


async def process_semester(
    browser: Browser,
    semester: str,
    vision_extractor: DashboardVisionExtractor,
    screenshot_dir: Path,
    headless: bool,
) -> Optional[Dict]:
    """Navigate, capture, and extract one semester end-to-end.

    Uses its own BrowserContext + Page so multiple semesters can run
    concurrently against the same shared Browser instance.
    Vision API calls for the semester's pages are issued in parallel.
    """
    controller = OAICDashboardController(
        headless=headless,
        screenshot_dir=str(screenshot_dir),
        browser=browser,
    )

    try:
        await controller.launch_browser()

        if not await controller.navigate_to_dashboard():
            logger.error(f"[{semester}] Failed to load dashboard")
            return None

        # The semester dropdown isn't visible on the Home page; go to page 2 first.
        await controller.navigate_to_page(2)
        await asyncio.sleep(2)
        await controller._maximize_powerbi_view()

        # If we can't actually select the requested semester, bail. Otherwise the
        # dashboard would still be on the previous/default semester, and we'd
        # silently capture screenshots attributed to the wrong period.
        if not await controller.select_semester(semester):
            logger.warning(
                f"[{semester}] Could not select this semester in the dashboard - "
                f"skipping (likely not yet published or dropdown widget changed)."
            )
            return None

        # Sequentially capture screenshots (Power BI nav is order-dependent).
        screenshots = await controller.capture_all_pages(semester)

        # Parallel vision-API extraction across pages for this semester.
        async def extract_one(key: str, image_bytes: bytes):
            parts = key.split('_')
            try:
                page_num = int(parts[1]) if len(parts) >= 2 else 0
            except ValueError:
                return None
            if page_num not in OAICDashboardController.DATA_PAGES:
                return None
            logger.info(f"[{semester}] Extracting data from {key} (page {page_num})...")
            extracted = await vision_extractor.extract_page(
                page_num, image_bytes, semester=semester
            )
            return page_num, key, extracted

        extraction_results = await asyncio.gather(
            *(extract_one(k, v) for k, v in screenshots.items())
        )

        extractions: Dict = {}
        for result in extraction_results:
            if result is None:
                continue
            page_num, key, extracted = result
            extractions[key] = extracted
            if page_num not in extractions:
                extractions[page_num] = extracted

        period_data = vision_extractor.consolidate_period_data(extractions, semester)

        is_valid, errors = validate_extracted_data(period_data)
        if not is_valid:
            logger.warning(f"[{semester}] Validation warnings: {errors}")

        # Rank 7: cross-page total_notifications consistency. Soft-warns
        # at 10% deviation; raises at 25%, in which case we drop the
        # period rather than persist garbage.
        try:
            cross_warnings = validate_cross_page_totals(period_data, semester)
            for w in cross_warnings:
                logger.warning(f"[{semester}] cross-page check: {w}")
        except OAICValidationError as exc:
            logger.error(
                f"[{semester}] cross-page consistency failed: {exc.errors}; "
                "discarding period."
            )
            quarantine_extraction(
                QUARANTINE_DIR, semester, page=0,
                image_bytes=None, payload=period_data,
                errors=exc.errors,
            )
            return None

        # Rank 10: inter-semester delta soft-warning. Compare to the
        # previous period currently stored in the consolidated JSON file
        # so we don't re-query a DB the scraper doesn't own.
        try:
            prior = _load_prior_period_for_compare(period_data)
            for w in validate_inter_semester_delta(period_data, prior):
                logger.warning(f"[{semester}] inter-semester delta: {w}")
        except Exception as exc:
            logger.debug(f"[{semester}] inter-semester compare skipped: {exc}")

        return period_data

    except Exception as e:
        logger.error(f"[{semester}] Processing failed: {e}", exc_info=True)
        return None
    finally:
        await controller.close()


def _load_prior_period_for_compare(new_period: Dict) -> Optional[Dict]:
    """Load the immediately-prior semester's record from the most recent
    consolidated `oaic_dashboard_data_*.json` file. Returns None if no
    prior period is available.
    """
    year = new_period.get("year")
    period = new_period.get("period")
    if not (isinstance(year, int) and period in ("H1", "H2")):
        return None
    if period == "H1":
        prior_year, prior_period = year - 1, "H2"
    else:
        prior_year, prior_period = year, "H1"

    candidates = sorted(glob.glob("oaic_dashboard_data_*.json"))
    if not candidates:
        return None
    try:
        with open(candidates[-1], "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return None
    for r in records:
        if (isinstance(r, dict)
                and r.get("year") == prior_year
                and r.get("period") == prior_period):
            return r
    return None


async def _run_debug_mode(browser: Browser, screenshot_dir: Path, headless: bool) -> None:
    """One-off debug mode: explore iframe elements and capture an initial screenshot."""
    controller = OAICDashboardController(
        headless=headless,
        screenshot_dir=str(screenshot_dir),
        browser=browser,
    )
    try:
        await controller.launch_browser()
        if not await controller.navigate_to_dashboard():
            logger.error("Failed to load dashboard")
            sys.exit(1)

        logger.info("Running in debug mode...")
        await controller.debug_iframe_elements()

        screenshot_path = controller.screenshot_dir / "debug_initial_state.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        iframe_element = await controller.page.query_selector('iframe[src*="powerbi"]')
        if iframe_element:
            screenshot_bytes = await iframe_element.screenshot()
            with open(screenshot_path, 'wb') as f:
                f.write(screenshot_bytes)
            logger.info(f"Debug screenshot saved: {screenshot_path}")

        logger.info("Debug mode complete. Check logs for element information.")
    finally:
        await controller.close()


async def main():
    """Main entry point for OAIC dashboard scraper."""
    parser = argparse.ArgumentParser(
        description='Scrape OAIC Power BI dashboard for data breach statistics',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--semester', type=str, default=None,
                        help='Scrape specific semester only (e.g., "Jan-Jun 2025")')
    parser.add_argument('--from-year', type=int, default=None, metavar='YEAR',
                        help='Only scrape semesters from YEAR onwards (e.g., --from-year 2024)')
    parser.add_argument('--from-2025', action='store_true',
                        help='Deprecated alias for --from-year 2025')
    parser.add_argument('--headful', action='store_true',
                        help='Show browser window (for debugging)')
    parser.add_argument('--no-screenshots', action='store_true',
                        help='Skip saving screenshots (not recommended)')
    parser.add_argument('--output', type=str, default=None,
                        help='Output filename (default: oaic_cyber_statistics_<timestamp>.json)')
    parser.add_argument('--existing-data', type=str, default=None,
                        help='Path to existing OAIC data file to merge with')
    parser.add_argument('--debug', action='store_true',
                        help='Run in debug mode - explore iframe elements and exit')
    parser.add_argument(
        '--max-concurrent', type=int, default=1,
        help=(
            'Max semesters scraped in parallel (default: 1). Power BI does '
            'not reliably render slicer popups across multiple BrowserContexts '
            'sharing one Chromium process - dropdowns silently render empty, '
            'and slicers drift back to the dashboard default mid-scrape. Per-'
            'semester vision API calls are still issued in parallel within '
            'each semester, so the time-dominant phase is unaffected. Only '
            'raise this above 1 if you accept that some semester pages will '
            'be quarantined and re-scraped manually.'
        )
    )

    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment")
        sys.exit(1)

    headless = not args.headful
    vision_extractor = DashboardVisionExtractor(api_key)

    # Single shared screenshot dir so parallel semesters write under one timestamp.
    timestamp_dir = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    screenshot_dir = Path('oaic_screenshots') / timestamp_dir

    # Resolve semester list dynamically from today's date + OAIC publication schedule.
    known_semesters = generate_known_semesters()
    from_year = args.from_year if args.from_year is not None else (2025 if args.from_2025 else None)
    if args.semester:
        semesters = [args.semester]
    elif from_year is not None:
        # Each semester string ends with the 4-digit year, e.g. "Jan-Jun 2025".
        semesters = [s for s in known_semesters if int(s.split()[-1]) >= from_year]
    else:
        semesters = known_semesters

    logger.info(f"Will scrape semesters: {semesters}")

    all_period_data: List[Dict] = []
    output_file = None
    comparison_file = None

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
            if args.debug:
                await _run_debug_mode(browser, screenshot_dir, headless)
                return

            # Pre-flight: query the dashboard once to learn which semesters
            # are actually published, then drop any requested semester
            # that's not in the dropdown. Avoids the noisy "ERROR:
            # No strategy could put the dashboard into this period"
            # cascade when the user requests a future or unpublished
            # period (e.g. Jul-Dec 2025 isn't on the dashboard yet even
            # though our publication-schedule heuristic says it should be).
            try:
                available = await _discover_available_semesters(browser, headless)
            except Exception as e:
                logger.warning(
                    f"Pre-flight semester discovery failed: {e}. "
                    "Proceeding with the unfiltered request list."
                )
                available = set()
            if available:
                missing = [s for s in semesters if s not in available]
                semesters = [s for s in semesters if s in available]
                if missing:
                    logger.info(
                        f"Skipping {len(missing)} semester(s) not yet "
                        f"published on the dashboard: {missing}"
                    )
                if not semesters:
                    logger.error(
                        "No requested semesters are available on the dashboard. "
                        "Try a different --from-year or --semester."
                    )
                    return

            max_concurrent = max(1, min(args.max_concurrent, len(semesters)))
            if max_concurrent > 1:
                logger.warning(
                    f"--max-concurrent={max_concurrent} requested. Power BI "
                    "iframes do not isolate cleanly across BrowserContexts; "
                    "expect intermittent empty-dropdown failures and drifted "
                    "slicers. Use 1 for reliable scraping."
                )
            logger.info(
                f"Processing {len(semesters)} semester(s) with up to "
                f"{max_concurrent} in parallel"
            )

            sem_lock = asyncio.Semaphore(max_concurrent)

            async def run_one(semester: str) -> Optional[Dict]:
                async with sem_lock:
                    logger.info(f"\n{'='*50}\nStarting semester: {semester}\n{'='*50}")
                    return await process_semester(
                        browser=browser,
                        semester=semester,
                        vision_extractor=vision_extractor,
                        screenshot_dir=screenshot_dir,
                        headless=headless,
                    )

            results = await asyncio.gather(*(run_one(s) for s in semesters))
            # Pair each requested semester with its result so we can report which
            # ones actually came back with data and which were skipped/failed.
            semester_outcomes = list(zip(semesters, results))
            all_period_data = [r for s, r in semester_outcomes if r is not None]
            # Self-check: drop any H2 record byte-identical to the same year's H1
            # (silent semester-selection-failure signature).
            before = len(all_period_data)
            all_period_data = drop_duplicated_h1_h2(all_period_data)
            if len(all_period_data) < before:
                # Mark the dropped semester(s) as failed in the outcome list so
                # the end-of-run summary reports them as skipped, not scraped.
                kept_ids = {id(r) for r in all_period_data}
                semester_outcomes = [
                    (s, r) if (r is None or id(r) in kept_ids) else (s, None)
                    for s, r in semester_outcomes
                ]
        finally:
            await browser.close()

    # Merge and persist outside the browser lifetime to keep cleanup fast.
    merged_data, comparison = merge_with_existing_oaic_data(
        all_period_data,
        args.existing_data,
    )

    timestamp_out = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = args.output or f'oaic_cyber_statistics_{timestamp_out}.json'

    with open(output_file, 'w') as f:
        json.dump(merged_data, f, indent=2, default=str)
    logger.info(f"Saved merged data to: {output_file}")

    if comparison:
        comparison_file = f'oaic_pdf_vs_dashboard_comparison_{timestamp_out}.json'
        with open(comparison_file, 'w') as f:
            json.dump(comparison, f, indent=2, default=str)
        logger.info(f"Saved comparison data to: {comparison_file}")

    metadata_file = screenshot_dir / 'metadata.json'
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    with open(metadata_file, 'w') as f:
        json.dump({
            'scraped_at': datetime.now().isoformat(),
            'semesters': semesters,
            'output_file': output_file,
            'comparison_file': comparison_file,
        }, f, indent=2)

    succeeded = [s for s, r in semester_outcomes if r is not None]
    failed = [s for s, r in semester_outcomes if r is None]

    logger.info("\nScraping complete!")
    logger.info(f"  Semesters requested ({len(semesters)}): {semesters}")
    logger.info(f"  Semesters scraped   ({len(succeeded)}): {succeeded}")
    if failed:
        logger.warning(f"  Semesters skipped   ({len(failed)}): {failed}")
        logger.warning("  (Likely not yet published on the OAIC dashboard, "
                       "or the dropdown widget changed - see earlier "
                       "'Visible options' log lines.)")
    logger.info(f"  Output file: {output_file}")
    logger.info(f"  Screenshots: {screenshot_dir}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Run interrupted by user (Ctrl+C)")
        raise
    finally:
        # Replay every WARNING/ERROR captured during the run so they don't
        # get lost in the Playwright/HTTP log noise. Always fires - success,
        # exception, or Ctrl+C.
        _print_run_summary()
