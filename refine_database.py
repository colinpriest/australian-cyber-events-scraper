from __future__ import annotations

import sys

if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer)

from cyber_event_data import CyberEventData
from entity_scraper import SeleniumScraper
from llm_extractor import extract_event_details_with_llm
from entity_resolver import is_entity_australian

def enrich_events_with_missing_details(db: CyberEventData, scraper: SeleniumScraper):
    """Finds events with poor descriptions and enriches them by scraping their source URL."""
    print("\n--- Starting enrichment for events with missing details ---")
    events_to_enrich = db.get_events_missing_details(limit=50)
    if not events_to_enrich:
        print("No events with missing details found.")
        return

    print(f"Found {len(events_to_enrich)} events to enrich.")
    for event in events_to_enrich:
        print(f"Enriching event {event['event_id']} from URL: {event['url']}")
        text_content = scraper.get_page_text(event['url'])
        if text_content:
            enriched_data = extract_event_details_with_llm(text_content)
            if enriched_data:
                db.update_event_details(event['event_id'], enriched_data.model_dump())
        db.mark_url_as_processed(event['url'])

def review_and_filter_events(db: CyberEventData):
    """Reviews enriched events, filters them, and verifies entity nationality."""
    print("\n--- Starting review and filtering of enriched events ---")
    events_to_review = db.get_events_to_review(limit=200)
    if not events_to_review:
        print("No new events to review.")
        return

    print(f"Found {len(events_to_review)} events to review.")
    deleted_count = 0
    verified_count = 0

    for event in events_to_review:
        event_id = event['event_id']
        is_specific = event['is_specific_event']
        is_australian = event['is_australian_event']

        # Filter out non-specific or non-Australian events
        if not is_specific or not is_australian:
            db.delete_event(event_id)
            deleted_count += 1
        else:
            # If the event seems valid, verify its entities
            print(f"Verifying entities for event: {event['title']}")
            entities = db.get_entities_for_event(event_id)
            is_still_valid = True
            if not entities:
                print(f"  ⚠️ Warning: No entities found for this Australian event.")
                is_still_valid = False # Or handle as needed

            for entity_name in entities:
                if not is_entity_australian(entity_name):
                    print(f"  ⚠️ Warning: Non-Australian entity '{entity_name}' found for event.")
                    # Depending on policy, we could disassociate the entity or delete the event
                    # For now, we just warn.
            
            if is_still_valid:
                 verified_count += 1

        # Mark as reviewed so it's not processed again
        db.mark_event_as_reviewed(event_id)

    print(f"Review complete. Verified {verified_count} events. Deleted {deleted_count} events.")

def main():
    """
    Main function to run the database refinement process.
    """
    db = None
    scraper = None
    try:
        db = CyberEventData()
        scraper = SeleniumScraper() # Scraper is needed for enrichment part

        # 1. Enrich events that were stored with minimal details
        enrich_events_with_missing_details(db, scraper)

        # 2. Review events based on LLM enrichment and filter out bad data
        review_and_filter_events(db)

    except Exception as e:
        print(f"An error occurred during the refinement process: {e}")
    finally:
        if scraper:
            scraper.close()
        if db:
            db.close()
        print("\nRefinement process finished.")

if __name__ == "__main__":
    main()