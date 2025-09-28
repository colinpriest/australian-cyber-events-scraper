import asyncio
import argparse
from entity_scraper import PlaywrightScraper
from llm_extractor import extract_event_details_with_llm, ExtractedEventDetails
import json

async def main():
    parser = argparse.ArgumentParser(description="Debug the LLM extractor on a single URL.")
    parser.add_argument("url", help="The URL of the article to process.")
    args = parser.parse_args()

    print(f"Scraping content from: {args.url}")
    async with PlaywrightScraper() as scraper:
        content = await scraper.get_page_text(args.url)

    if not content:
        print("Failed to scrape content from the URL.")
        return

    print("\n--- Scraped Content (first 500 chars) ---")
    print(content[:500])
    print("-----------------------------------------\n")

    print("Processing content with LLM extractor...")
    llm_details = extract_event_details_with_llm(content)

    if not llm_details:
        print("LLM extraction failed.")
        return

    print("\n--- LLM Extraction Results ---")
    # Pretty print the Pydantic model
    print(json.dumps(llm_details.model_dump(), indent=2))
    print("------------------------------\n")

    # Explicitly check the fields and print a summary
    print("--- Analysis Summary ---")
    print(f"Is Australian Event? {llm_details.is_australian_event}")
    print(f"Is Specific Event?   {llm_details.is_specific_event}")
    print(f"Primary Entity:      {llm_details.primary_entity}")
    print("------------------------\n")


if __name__ == "__main__":
    asyncio.run(main())
