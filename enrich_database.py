from __future__ import annotations

import sys
import concurrent.futures

if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer)

from cyber_event_data import CyberEventData
from entity_scraper import SeleniumScraper
from llm_extractor import extract_event_details_with_llm


def process_url(url: str, db: CyberEventData):
    """
    Worker function to scrape a single URL, analyze it, and update the database.
    """
    scraper = None
    try:
        print(f"Starting to process URL: {url}")
        scraper = SeleniumScraper()
        
        text_content = scraper.get_page_text(url)
        if not text_content:
            db.mark_url_as_processed(url)
            return

        enriched_data = extract_event_details_with_llm(text_content)
        if not enriched_data:
            db.mark_url_as_processed(url)
            return

        db.update_event_with_enriched_data(url, enriched_data.model_dump())

    except Exception as e:
        print(f"An error occurred while processing {url}: {e}")
        # In case of a fatal error, still try to mark as processed to avoid retries
        if db and url:
            db.mark_url_as_processed(url)
    finally:
        if scraper:
            scraper.close()

def main():
    """
    Main orchestration function to enrich the cyber events database using multiple threads.
    """
    db = None
    try:
        db = CyberEventData()
        urls_to_process = db.get_unenriched_urls(limit=200)
        if not urls_to_process:
            print("No new URLs to enrich. Database is up-to-date.")
            return

        print(f"Found {len(urls_to_process)} URLs to enrich. Starting 10 concurrent threads...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Pass the shared db instance to each worker
            list(executor.map(lambda url: process_url(url, db), urls_to_process))

        print("\nEnrichment process complete.")

    except Exception as e:
        print(f"An error occurred during the main enrichment process: {e}")
    finally:
        if db:
            db.close()

if __name__ == "__main__":
    main()
