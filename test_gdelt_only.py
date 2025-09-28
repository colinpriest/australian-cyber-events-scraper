#!/usr/bin/env python3
"""Simple GDELT API test"""

import asyncio
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

from cyber_data_collector.datasources.gdelt import GDELTDataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.utils import RateLimiter

async def test_gdelt():
    # Load environment
    load_dotenv()

    # Create config with enhanced rate limiting settings
    config = DataSourceConfig(
        name="gdelt",
        enabled=True,
        timeout=60,
        custom_config={
            "max_records": 10,
            "query": "Australia AND (cyber OR ransomware OR hacking OR \"data breach\")",
            "access_method": "auto"  # Try all methods
        }
    )

    # Create rate limiter with conservative settings
    rate_limiter = RateLimiter()
    rate_limiter.set_limit("gdelt", per_minute=10, per_second=0.2)

    # Create GDELT data source with environment config
    env_config = {
        "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT"),
        "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    }
    gdelt_source = GDELTDataSource(config, rate_limiter, env_config)

    # Validate config and show available methods
    if gdelt_source.validate_config():
        source_info = gdelt_source.get_source_info()
        print(f"Available access methods: {source_info.get('access_methods', [])}")
    else:
        print("Failed to validate GDELT configuration")

    # Define date range (last 7 days)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    date_range = DateRange(start_date=start_date, end_date=end_date)

    print(f"Testing GDELT DOC 2.0 API for period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    # Test API call
    try:
        events = await gdelt_source.collect_events(date_range)
        print(f"Successfully collected {len(events)} events from GDELT DOC 2.0 API")

        # Print first few events for verification
        for i, event in enumerate(events[:3]):
            print(f"\nEvent {i+1}:")
            print(f"  Title: {event.title}")
            print(f"  Type: {event.event_type}")
            print(f"  Date: {event.event_date}")
            print(f"  Australian: {event.australian_relevance}")
            print(f"  URL: {event.data_sources[0].url}")

        return True

    except Exception as exc:
        print(f"GDELT API test failed: {exc}")
        return False

if __name__ == "__main__":
    asyncio.run(test_gdelt())