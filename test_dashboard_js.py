#!/usr/bin/env python3
"""
Test script to check what JavaScript console output we would see.
This simulates the JavaScript execution to debug the dashboard issues.
"""

import requests
import json
import sys

def test_dashboard_api(base_url="http://127.0.0.1:5002"):
    """Test all dashboard API endpoints and simulate JavaScript behavior."""

    print("=== Dashboard JavaScript Simulation ===")

    # Simulate the dateRange being set
    date_range = {
        'start': '2020-01-01',
        'end': '2024-09-28'  # Current date
    }
    print(f"Initial date range set: {date_range}")

    def build_api_url(endpoint):
        url = f"{base_url}/api/v1/dashboard/{endpoint}"
        if date_range:
            url += f"?start_date={date_range['start']}&end_date={date_range['end']}"
        print(f"Building API URL: {url}")
        return url

    def test_api_endpoint(endpoint_name, endpoint):
        print(f"\n--- Testing {endpoint_name} ---")
        try:
            url = build_api_url(endpoint)
            print(f"Fetching from URL: {url}")

            response = requests.get(url)
            print(f"Response received: {response.status_code} {response.reason}")

            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}: {response.reason}")

            data = response.json()
            print(f"Data received: {json.dumps(data, indent=2)[:200]}...")
            print(f"SUCCESS {endpoint_name} loaded successfully")
            return data

        except Exception as e:
            print(f"FAILED to load {endpoint_name}: {e}")
            return None

    # Test all endpoints
    endpoints = [
        ("Monthly Trends", "monthly-event-counts"),
        ("Severity Trends", "monthly-severity-trends"),
        ("Records Affected", "monthly-records-affected"),
        ("Event Type Mix", "monthly-event-type-mix"),
        ("Entity Types", "entity-type-distribution"),
        ("Records Histogram", "records-affected-histogram")
    ]

    results = {}
    for name, endpoint in endpoints:
        results[name] = test_api_endpoint(name, endpoint)

    # Summary
    print(f"\n=== SUMMARY ===")
    successful = sum(1 for result in results.values() if result is not None)
    total = len(results)
    print(f"Successfully loaded: {successful}/{total} endpoints")

    if successful == total:
        print("All endpoints working - the issue is likely in the frontend JavaScript")
    else:
        print("Some endpoints failing - backend issues detected")

    return results

if __name__ == "__main__":
    if len(sys.argv) > 1:
        base_url = sys.argv[1]
    else:
        base_url = "http://127.0.0.1:5002"

    test_dashboard_api(base_url)