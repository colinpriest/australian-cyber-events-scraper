#!/usr/bin/env python3
"""
Direct BigQuery test for GDELT access
"""

import os
import json
from google.oauth2.credentials import Credentials
from google.cloud import bigquery

def test_bigquery():
    project_id = "gdelt-cyber-data"
    token_file = 'bigquery_token.json'

    print("Testing BigQuery GDELT access...")
    print(f"Project ID: {project_id}")

    try:
        # Load credentials
        if not os.path.exists(token_file):
            print(f"[ERROR] {token_file} not found. Run setup_bigquery_auth.py first.")
            return False

        # Load credentials from JSON file
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        creds = Credentials(
            token=token_data.get('token'),
            refresh_token=token_data.get('refresh_token'),
            token_uri=token_data.get('token_uri'),
            client_id=token_data.get('client_id'),
            client_secret=token_data.get('client_secret')
        )
        client = bigquery.Client(project=project_id, credentials=creds)

        # Test query - count recent Australian cyber events from GDELT
        query = """
        SELECT
            COUNT(*) as event_count,
            COUNT(DISTINCT Actor1Name) as unique_actors
        FROM `gdelt-bq.gdeltv2.events`
        WHERE CAST(DATEADDED AS STRING) >= '20250920000000'
          AND (
            ActionGeo_CountryCode = 'AS' OR
            Actor1CountryCode = 'AS' OR
            Actor2CountryCode = 'AS'
          )
        LIMIT 1
        """

        print("Running BigQuery test query...")
        query_job = client.query(query)
        results = query_job.result()

        for row in results:
            print(f"[SUCCESS] BigQuery test successful!")
            print(f"Found {row.event_count} recent Australian events")
            print(f"Found {row.unique_actors} unique actors")

        # Update .env file
        env_file = '.env'
        env_vars = {}

        # Read existing .env
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                for line in f:
                    if '=' in line and not line.strip().startswith('#'):
                        key, value = line.strip().split('=', 1)
                        env_vars[key] = value

        # Update with BigQuery settings
        env_vars['GOOGLE_CLOUD_PROJECT'] = project_id
        env_vars['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.abspath(token_file)

        # Write .env
        with open(env_file, 'w') as f:
            for key, value in env_vars.items():
                f.write(f"{key}={value}\n")

        print(f"[SUCCESS] Updated .env with BigQuery configuration")
        return True

    except Exception as e:
        print(f"[ERROR] BigQuery test failed: {e}")
        return False

if __name__ == "__main__":
    success = test_bigquery()
    if success:
        print("\n[SUCCESS] BigQuery setup completed! GDELT rate limiting is now solved.")
    else:
        print("\n[ERROR] BigQuery setup failed.")