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
from openai import OpenAI
from playwright.async_api import async_playwright, Page, Frame
from tenacity import retry, stop_after_attempt, wait_exponential

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('oaic_dashboard_scraper.log')
    ]
)
logger = logging.getLogger(__name__)


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

    def __init__(self, headless: bool = True, screenshot_dir: Optional[str] = None):
        """
        Initialize the dashboard controller.

        Args:
            headless: Run browser in headless mode (default True)
            screenshot_dir: Directory to save screenshots (default: oaic_screenshots/<timestamp>)
        """
        self.headless = headless
        self.playwright = None
        self.browser = None
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
        """Initialize Playwright and launch the browser with maximized window."""
        logger.info("Launching browser...")
        self.playwright = await async_playwright().start()

        # Launch with maximum window size for best screenshot quality
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--start-maximized',  # Start maximized
                '--window-size=2560,1440',  # Large window size
            ]
        )

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

        logger.info("Browser launched successfully with maximized window")

    async def close(self):
        """Close browser and cleanup resources."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser closed")

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

    async def select_semester(self, semester: str) -> bool:
        """
        Select a semester from the dropdown filter.
        Based on debug: div.slicer-dropdown-menu with aria-label="Semester"

        Args:
            semester: Semester string (e.g., "Jan-Jun 2025")

        Returns:
            True if selection successful
        """
        logger.info(f"Selecting semester: {semester}")

        if not self.powerbi_frame:
            logger.error("Power BI frame not available")
            return False

        try:
            # The semester dropdown is in a slicer-dropdown-menu div
            # First click the dropdown to open it
            dropdown = await self.powerbi_frame.query_selector('div.slicer-dropdown-menu[aria-label="Semester"]')

            if not dropdown:
                # Try alternative selectors
                dropdown = await self.powerbi_frame.query_selector('div.slicer-dropdown-menu')

            if not dropdown:
                dropdown = await self.powerbi_frame.query_selector('[class*="slicer-dropdown"]')

            if dropdown:
                # Click to open dropdown
                await dropdown.click()
                logger.info("Clicked semester dropdown to open it")
                await asyncio.sleep(1)

                # Now look for the semester option in the opened dropdown
                # Options are typically in a list that appears after clicking
                option_selectors = [
                    f'span.slicerText:has-text("{semester}")',
                    f'div.slicerItemContainer:has-text("{semester}")',
                    f'[class*="slicer"] :text("{semester}")',
                    f'text="{semester}"',
                ]

                for opt_selector in option_selectors:
                    try:
                        option = await self.powerbi_frame.query_selector(opt_selector)
                        if option:
                            is_visible = await option.is_visible()
                            if is_visible:
                                await option.click()
                                logger.info(f"Selected semester: {semester}")
                                await asyncio.sleep(2)  # Wait for dashboard to update
                                return True
                    except Exception:
                        continue

                logger.warning(f"Could not find option for semester: {semester}")
                # Click elsewhere to close dropdown
                await self.powerbi_frame.click('body')

            else:
                logger.warning("Could not find semester dropdown")

            # If the current semester is already Jan-Jun 2025 (default), that's OK
            current_text = await self.powerbi_frame.query_selector('div.slicer-dropdown-menu')
            if current_text:
                text = await current_text.inner_text()
                if semester in text:
                    logger.info(f"Semester {semester} is already selected")
                    return True

            return False

        except Exception as e:
            logger.error(f"Failed to select semester {semester}: {e}")
            return False

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

            # Create variations to handle plural/singular differences
            # e.g., "Cyber incidents" -> ["cyber incidents", "cyber incident", "cyber"]
            variations = [filter_lower]
            if filter_lower.endswith('s'):
                variations.append(filter_lower[:-1])  # Remove trailing 's'
            if filter_lower.endswith('es'):
                variations.append(filter_lower[:-2])  # Remove trailing 'es'
            if not filter_lower.endswith('s'):
                variations.append(filter_lower + 's')  # Add trailing 's'

            # Also add the main keyword (first word or after "by")
            words = filter_lower.split()
            if len(words) > 1:
                variations.append(words[-1])  # Last word
                if 'by' in words:
                    idx = words.index('by')
                    if idx + 1 < len(words):
                        variations.append(' '.join(words[idx+1:]))

            logger.debug(f"Searching for filter with variations: {variations}")

            # First, try exact match with original text
            exact_selectors = [
                f'text="{filter_text}"',
                f'[aria-label="{filter_text}"]',
            ]

            for selector in exact_selectors:
                try:
                    element = await self.powerbi_frame.query_selector(selector)
                    if element:
                        is_visible = await element.is_visible()
                        if is_visible:
                            await element.scroll_into_view_if_needed()
                            await element.click()
                            await asyncio.sleep(1.5)
                            logger.info(f"Clicked filter option (exact): {filter_text}")
                            return True
                except Exception:
                    continue

            # Try partial/contains matching with variations
            partial_selector_templates = [
                '[aria-label*="{}"]',
                'button:has-text("{}")',
                '[role="radio"]:has-text("{}")',
                '[role="button"]:has-text("{}")',
                '[role="option"]:has-text("{}")',
                '[role="menuitemradio"]:has-text("{}")',
                'text="{}"',
                'span:has-text("{}")',
                'div.slicerText:has-text("{}")',
                # Power BI chiclet slicer buttons
                'div[class*="chiclet"]:has-text("{}")',
                'div[class*="slicer"]:has-text("{}")',
                # General clickable elements with text
                'div[class*="visual"] span:has-text("{}")',
                'div[class*="visual"] div:has-text("{}")',
            ]

            for variation in variations:
                for template in partial_selector_templates:
                    selector = template.format(variation)
                    try:
                        elements = await self.powerbi_frame.query_selector_all(selector)
                        for element in elements:
                            try:
                                is_visible = await element.is_visible()
                                if is_visible:
                                    # Verify the text actually matches our intent
                                    text = await element.inner_text()
                                    text_lower = text.lower().strip()

                                    # Check if any variation matches the element text
                                    if any(v in text_lower or text_lower in v for v in variations):
                                        await element.scroll_into_view_if_needed()
                                        await element.click()
                                        await asyncio.sleep(1.5)
                                        logger.info(f"Clicked filter option (flexible): '{text}' for '{filter_text}'")
                                        return True
                            except Exception:
                                continue
                    except Exception:
                        continue

            # Last resort: search all visible elements for matching text
            logger.info(f"Trying last resort search for filter: {filter_text}")
            all_elements = await self.powerbi_frame.query_selector_all('*')
            for element in all_elements:
                try:
                    is_visible = await element.is_visible()
                    if not is_visible:
                        continue

                    text = await element.inner_text()
                    if not text:
                        continue

                    text_lower = text.lower().strip()

                    # Check if element text matches any variation exactly or closely
                    for v in variations:
                        if v == text_lower or (len(v) > 5 and v in text_lower and len(text_lower) < len(v) + 10):
                            # Check if element is clickable (has reasonable size)
                            box = await element.bounding_box()
                            if box and box['width'] > 20 and box['height'] > 10:
                                await element.click()
                                await asyncio.sleep(1.5)
                                logger.info(f"Clicked filter option (last resort): '{text}' for '{filter_text}'")
                                return True
                except Exception:
                    continue

            logger.warning(f"Could not find filter option: {filter_text} (tried variations: {variations})")
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
        Capture screenshots of all data pages for a semester.
        Includes multiple filter states for pages with interactive options.

        Args:
            semester: Semester being captured

        Returns:
            Dictionary mapping page_key to screenshot bytes
            Keys are like "page_2", "page_6_all", "page_6_cyber", etc.
        """
        screenshots = {}

        # Note: We assume we're already on page 2 after semester selection
        # current_page should already be set from navigate_to_page(2) call

        # Define filter options for pages that have them
        # These are clickable filter buttons on each page
        page_filters = {
            6: ["All breaches", "Malicious or criminal attacks", "Cyber incidents", "Human error", "System faults"],
            7: ["By time taken only", "By top industry sectors", "By source of breach"],
            8: ["By time taken only", "By top industry sectors", "By source of breach"],
            9: ["All", "Cyber incidents", "Malicious or criminal attacks", "Human error", "System fault"],
        }

        for page_num in sorted(self.DATA_PAGES.keys()):
            logger.info(f"Processing page {page_num}: {self.DATA_PAGES.get(page_num, 'Unknown')}")

            # Navigate to the page (will skip if already there)
            await self.navigate_to_page(page_num)
            await asyncio.sleep(2)  # Wait for visualizations to render

            # Re-expand after each page navigation (expand mode is lost on page change)
            await self._maximize_powerbi_view()
            await asyncio.sleep(1)

            if page_num in page_filters:
                # Use predefined filters
                filters = page_filters[page_num]
                await self._capture_with_filters(screenshots, page_num, semester, filters)
            else:
                # Simple page - just capture one screenshot
                screenshots[f"page_{page_num}"] = await self.capture_page_screenshot(page_num, semester)

        return screenshots

    async def _capture_with_filters(self, screenshots: Dict, page_num: int, semester: str, filters: List[str]):
        """Helper to capture screenshots for each filter state on a page."""
        for i, filter_text in enumerate(filters):
            # Click the filter option
            filter_clicked = await self.click_filter_option(filter_text)
            await asyncio.sleep(1.5)  # Wait for visualization to update

            # Re-expand the Power BI view after clicking filter (clicking filter exits fullscreen)
            await self._maximize_powerbi_view()
            await asyncio.sleep(1)

            # Create a safe key for this screenshot
            filter_safe = filter_text.lower().replace(' ', '_').replace('/', '_')[:20]
            key = f"page_{page_num}_{filter_safe}"

            # Capture screenshot
            screenshots[key] = await self.capture_page_screenshot(page_num, semester, filter_safe)

            if not filter_clicked and i == 0:
                # If first filter didn't work, just take one screenshot
                logger.warning(f"Page {page_num} filters not working, capturing single screenshot")
                screenshots[f"page_{page_num}"] = screenshots[key]
                break


class DashboardVisionExtractor:
    """Extracts structured data from dashboard screenshots using GPT-4o-mini vision."""

    def __init__(self, api_key: str):
        """
        Initialize the vision extractor.

        Args:
            api_key: OpenAI API key
        """
        if not api_key:
            raise ValueError("OpenAI API key required for vision extraction")
        self.client = OpenAI(api_key=api_key)
        self.model = "gpt-4o-mini"  # Cost-effective with vision capability

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
    def _call_vision_api(self, image_bytes: bytes, prompt: str) -> Dict:
        """
        Call the OpenAI Vision API with retry logic.

        Args:
            image_bytes: Screenshot image
            prompt: Extraction prompt

        Returns:
            Parsed JSON response
        """
        messages = self._create_vision_message(image_bytes, prompt)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=4096,
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content
        return json.loads(content)

    def extract_snapshot_data(self, image_bytes: bytes) -> Dict:
        """
        Extract data from Page 2: Snapshot.

        This page contains:
        - Total notifications KPI
        - Source of breaches donut chart
        - Cyber incident breakdown bar chart
        - Top 5 sectors table
        - Human error causes
        """
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing the "Snapshot" page.
Extract ALL visible data into a structured JSON format.

Look for and extract:
1. Total notifications received (large number, may have % change indicator)
2. Source of data breaches donut chart:
   - "Malicious or criminal attack" count and percentage
   - "Human error" count and percentage
   - "System fault" count and percentage
3. Cyber incident breakdown horizontal bar chart showing percentages for:
   - Phishing (compromised credentials)
   - Compromised or stolen credentials (method unknown)
   - Ransomware
   - Hacking
   - Brute-force attack
   - Malware
4. Top 5 sectors to notify breaches (table with sector names)
5. Human error causes with percentages:
   - PI sent to wrong recipient (email)
   - Unauthorised disclosure
   - Failure to use BCC

Return ONLY valid JSON with this exact structure (use null for values you cannot read):
{
    "total_notifications": <int or null>,
    "change_from_previous": <string like "-10%" or null>,
    "malicious_attacks": <int or null>,
    "malicious_attacks_pct": <float or null>,
    "human_error": <int or null>,
    "human_error_pct": <float or null>,
    "system_faults": <int or null>,
    "system_faults_pct": <float or null>,
    "cyber_incidents": {
        "phishing_pct": <float or null>,
        "compromised_credentials_pct": <float or null>,
        "ransomware_pct": <float or null>,
        "hacking_pct": <float or null>,
        "brute_force_pct": <float or null>,
        "malware_pct": <float or null>
    },
    "top_sectors": [
        {"sector": "<name>", "rank": <int>},
        ...
    ],
    "human_error_causes": {
        "wrong_recipient_email_pct": <float or null>,
        "unauthorised_disclosure_pct": <float or null>,
        "failure_to_use_bcc_pct": <float or null>
    },
    "data_breaches_affecting_100_or_fewer_pct": <float or null>
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract snapshot data: {e}")
            return {}

    def extract_notifications_data(self, image_bytes: bytes) -> Dict:
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
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract notifications data: {e}")
            return {}

    def extract_individuals_affected(self, image_bytes: bytes) -> Dict:
        """Extract data from Page 4: Number of individuals affected by breaches."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Number of individuals affected by breaches".

Extract the distribution of breaches by number of individuals affected:
- 1
- 2-10
- 11-100
- 101-1,000
- 1,001-5,000
- 5,001-10,000
- 10,001-50,000
- 50,001-100,000
- 100,001-250,000
- 250,001-500,000
- 500,001-1,000,000
- 1,000,001-5,000,000
- Unknown

Return ONLY valid JSON:
{
    "individuals_affected_distribution": [
        {"range": "1", "count": <int or null>},
        {"range": "2-10", "count": <int or null>},
        {"range": "11-100", "count": <int or null>},
        {"range": "101-1,000", "count": <int or null>},
        {"range": "1,001-5,000", "count": <int or null>},
        {"range": "5,001-10,000", "count": <int or null>},
        {"range": "10,001-50,000", "count": <int or null>},
        {"range": "50,001-100,000", "count": <int or null>},
        {"range": "100,001-250,000", "count": <int or null>},
        {"range": "250,001-500,000", "count": <int or null>},
        {"range": "500,001-1,000,000", "count": <int or null>},
        {"range": "1,000,001-5,000,000", "count": <int or null>},
        {"range": "Unknown", "count": <int or null>}
    ],
    "percentage_100_or_fewer": <float or null>
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract individuals affected data: {e}")
            return {}

    def extract_personal_info_types(self, image_bytes: bytes) -> Dict:
        """Extract data from Page 5: Kinds of personal information involved."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Kinds of personal information involved in breaches".

Extract counts for each type of personal information:
- Contact information
- Identity information
- Financial details
- Health information
- Tax file Numbers
- Other sensitive information
- Consumer Data Right data
- Digital ID information documents

Return ONLY valid JSON:
{
    "personal_info_types": {
        "contact_information": <int or null>,
        "identity_information": <int or null>,
        "financial_details": <int or null>,
        "health_information": <int or null>,
        "tax_file_numbers": <int or null>,
        "other_sensitive_information": <int or null>,
        "consumer_data_right": <int or null>,
        "digital_id": <int or null>
    }
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract personal info types data: {e}")
            return {}

    def extract_breach_sources(self, image_bytes: bytes) -> Dict:
        """Extract data from Page 6: Source of breaches."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Source of data breaches".

Extract the comparison data showing current vs previous period:
- Human error (current and previous period counts)
- Malicious or criminal attack (current and previous period counts)
- System fault (current and previous period counts)

Return ONLY valid JSON:
{
    "breach_sources": {
        "human_error": {
            "current_period": <int or null>,
            "previous_period": <int or null>
        },
        "malicious_attack": {
            "current_period": <int or null>,
            "previous_period": <int or null>
        },
        "system_fault": {
            "current_period": <int or null>,
            "previous_period": <int or null>
        }
    },
    "current_period_label": "<e.g. Jan-Jun 2025 or null>",
    "previous_period_label": "<e.g. Jul-Dec 2024 or null>"
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract breach sources data: {e}")
            return {}

    def extract_time_to_identify(self, image_bytes: bytes) -> Dict:
        """Extract data from Page 7: Time taken to identify breaches."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Time taken to identify breaches".

Extract the distribution of time taken to identify breaches:
- Unknown
- Less than 1 hour
- 1-24 hours
- 1-7 days
- 8-30 days
- More than 30 days

Return ONLY valid JSON:
{
    "time_to_identify": {
        "unknown": <int or null>,
        "less_than_1_hour": <int or null>,
        "1_to_24_hours": <int or null>,
        "1_to_7_days": <int or null>,
        "8_to_30_days": <int or null>,
        "more_than_30_days": <int or null>
    }
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract time to identify data: {e}")
            return {}

    def extract_time_to_notify(self, image_bytes: bytes) -> Dict:
        """Extract data from Page 8: Time taken to notify the OAIC."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Time taken to notify the OAIC of breaches".

Extract the distribution of time taken to notify:
- Unknown
- Less than 10 days
- 10-30 days
- 31-60 days
- More than 30 days

Return ONLY valid JSON:
{
    "time_to_notify": {
        "unknown": <int or null>,
        "less_than_10_days": <int or null>,
        "10_to_30_days": <int or null>,
        "31_to_60_days": <int or null>,
        "more_than_30_days": <int or null>
    }
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract time to notify data: {e}")
            return {}

    def extract_top_sectors(self, image_bytes: bytes) -> Dict:
        """Extract data from Page 9: Top 5 sectors by source of breaches."""
        prompt = """Analyze this OAIC Power BI dashboard screenshot showing "Top 5 sectors by source of breaches".

Extract the top sectors with their notification counts and breakdown by source:
- Australian Government
- Education
- Finance (incl. superannuation)
- Health service providers
- Legal, accounting & management services

For each sector, try to extract:
- Total notifications
- Breakdown by cyber incidents, human error, system fault (if visible)

Return ONLY valid JSON:
{
    "top_sectors": [
        {
            "sector": "<sector name>",
            "total_notifications": <int or null>,
            "cyber_incidents": <int or null>,
            "human_error": <int or null>,
            "system_fault": <int or null>
        },
        ...
    ]
}"""

        try:
            return self._call_vision_api(image_bytes, prompt)
        except Exception as e:
            logger.error(f"Failed to extract top sectors data: {e}")
            return {}

    def extract_page(self, page_num: int, image_bytes: bytes) -> Dict:
        """
        Extract data from a specific page.

        Args:
            page_num: Page number (2-9)
            image_bytes: Screenshot image bytes

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
            9: self.extract_top_sectors
        }

        extractor = extractors.get(page_num)
        if extractor:
            return extractor(image_bytes)
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
        result["malicious_attacks"] = snapshot.get("malicious_attacks")
        result["human_error"] = snapshot.get("human_error")
        result["system_faults"] = snapshot.get("system_faults")

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

        # Top sectors
        sectors_data = extractions.get(9, {})
        result["top_sectors"] = []
        for sector_info in sectors_data.get("top_sectors", []):
            result["top_sectors"].append({
                "sector": sector_info.get("sector"),
                "notifications": sector_info.get("total_notifications")
            })

        # Also use snapshot sectors if available
        if not result["top_sectors"]:
            for sector_info in snapshot.get("top_sectors", []):
                result["top_sectors"].append({
                    "sector": sector_info.get("sector"),
                    "notifications": None
                })

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
        year = int(period_key.split()[0])

        if year >= 2025:
            # Use dashboard data for 2025+
            if period_key in new_by_period:
                merged.append(new_by_period[period_key])
                logger.info(f"Using dashboard data for {period_key}")
        else:
            # Use PDF data for historical, but save comparison
            if period_key in pdf_by_period:
                merged.append(pdf_by_period[period_key])
            if period_key in new_by_period:
                validation_comparison.append({
                    'period': period_key,
                    'pdf_data': pdf_by_period.get(period_key),
                    'dashboard_data': new_by_period[period_key]
                })
                logger.info(f"Created comparison for historical period {period_key}")

    return merged, validation_comparison


async def main():
    """Main entry point for OAIC dashboard scraper."""
    parser = argparse.ArgumentParser(
        description='Scrape OAIC Power BI dashboard for data breach statistics',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--semester', type=str, default=None,
                        help='Scrape specific semester only (e.g., "Jan-Jun 2025")')
    parser.add_argument('--from-2025', action='store_true',
                        help='Only scrape 2025 and later semesters')
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

    args = parser.parse_args()

    # Load environment variables
    load_dotenv()
    api_key = os.getenv('OPENAI_API_KEY')

    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment")
        sys.exit(1)

    # Initialize components
    headless = not args.headful
    browser_controller = OAICDashboardController(headless=headless)
    vision_extractor = DashboardVisionExtractor(api_key)

    try:
        # Launch browser
        await browser_controller.launch_browser()

        # Navigate to dashboard
        if not await browser_controller.navigate_to_dashboard():
            logger.error("Failed to load dashboard")
            sys.exit(1)

        # Debug mode - explore elements and exit
        if args.debug:
            logger.info("Running in debug mode...")
            await browser_controller.debug_iframe_elements()

            # Also take a screenshot of the initial state
            screenshot_path = browser_controller.screenshot_dir / "debug_initial_state.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)

            iframe_element = await browser_controller.page.query_selector('iframe[src*="powerbi"]')
            if iframe_element:
                screenshot_bytes = await iframe_element.screenshot()
                with open(screenshot_path, 'wb') as f:
                    f.write(screenshot_bytes)
                logger.info(f"Debug screenshot saved: {screenshot_path}")

            logger.info("Debug mode complete. Check logs for element information.")
            return

        # Get available semesters
        semesters = await browser_controller.get_available_semesters()

        # Filter semesters based on arguments
        if args.semester:
            semesters = [args.semester]
        elif args.from_2025:
            semesters = [s for s in semesters if '2025' in s or '2026' in s]

        logger.info(f"Will scrape semesters: {semesters}")

        all_period_data = []

        for semester_idx, semester in enumerate(semesters):
            logger.info(f"\n{'='*50}")
            logger.info(f"Processing semester: {semester} ({semester_idx + 1}/{len(semesters)})")
            logger.info('='*50)

            # Reset to page 1 before starting each semester (except the first)
            # This ensures we have a clean navigation state
            if semester_idx > 0:
                await browser_controller.reset_to_page_one()
                await asyncio.sleep(1)

            # First navigate to page 2 (Snapshot) where semester dropdown is visible
            # The semester dropdown is not visible on the Home page (page 1)
            await browser_controller.navigate_to_page(2)
            await asyncio.sleep(2)

            # Maximize the Power BI view after navigating (in case it reset)
            await browser_controller._maximize_powerbi_view()

            # Now select semester in dashboard
            await browser_controller.select_semester(semester)

            # Capture screenshots of all data pages
            screenshots = await browser_controller.capture_all_pages(semester)

            # Extract data from each screenshot
            # Keys are like "page_2", "page_6_all", "page_6_cyber", etc.
            extractions = {}
            for key, image_bytes in screenshots.items():
                # Parse page number from key (e.g., "page_2" -> 2, "page_6_cyber" -> 6)
                parts = key.split('_')
                page_num = int(parts[1]) if len(parts) >= 2 else 0

                logger.info(f"Extracting data from {key} (page {page_num})...")

                # Only extract from main pages (not filter variants for now)
                # For filter variants, we'll enhance extraction later
                if page_num in OAICDashboardController.DATA_PAGES:
                    extracted = vision_extractor.extract_page(page_num, image_bytes)
                    # Store by key to preserve filter variants
                    extractions[key] = extracted
                    # Also store by page_num for consolidation (use first/main extraction)
                    if page_num not in extractions:
                        extractions[page_num] = extracted

            # Consolidate into period data structure
            period_data = vision_extractor.consolidate_period_data(extractions, semester)

            # Validate
            is_valid, errors = validate_extracted_data(period_data)
            if not is_valid:
                logger.warning(f"Validation warnings for {semester}: {errors}")

            all_period_data.append(period_data)

        # Merge with existing data
        merged_data, comparison = merge_with_existing_oaic_data(
            all_period_data,
            args.existing_data
        )

        # Save output
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = args.output or f'oaic_cyber_statistics_{timestamp}.json'

        with open(output_file, 'w') as f:
            json.dump(merged_data, f, indent=2, default=str)
        logger.info(f"Saved merged data to: {output_file}")

        # Save comparison file if any historical data was scraped
        if comparison:
            comparison_file = f'oaic_pdf_vs_dashboard_comparison_{timestamp}.json'
            with open(comparison_file, 'w') as f:
                json.dump(comparison, f, indent=2, default=str)
            logger.info(f"Saved comparison data to: {comparison_file}")

        # Save session metadata
        metadata_file = browser_controller.screenshot_dir / 'metadata.json'
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        with open(metadata_file, 'w') as f:
            json.dump({
                'scraped_at': datetime.now().isoformat(),
                'semesters': semesters,
                'output_file': output_file,
                'comparison_file': comparison_file if comparison else None
            }, f, indent=2)

        logger.info(f"\nScraping complete!")
        logger.info(f"  Semesters processed: {len(semesters)}")
        logger.info(f"  Output file: {output_file}")
        logger.info(f"  Screenshots: {browser_controller.screenshot_dir}")

    except Exception as e:
        logger.error(f"Scraping failed: {e}", exc_info=True)
        sys.exit(1)

    finally:
        await browser_controller.close()


if __name__ == '__main__':
    asyncio.run(main())
