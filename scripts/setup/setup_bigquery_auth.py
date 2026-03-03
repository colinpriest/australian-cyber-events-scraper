#!/usr/bin/env python3
"""
Google BigQuery Authentication Setup for GDELT
This script helps you set up authentication for BigQuery to access GDELT data.
"""

import os
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.credentials import Credentials
from google.cloud import bigquery
import json

# BigQuery scopes needed to read public datasets like GDELT
SCOPES = [
    'https://www.googleapis.com/auth/bigquery',
    'https://www.googleapis.com/auth/cloud-platform.read-only'
]

def setup_user_credentials():
    """Set up user credentials using OAuth flow."""
    print("Setting up Google BigQuery authentication...")
    print("You'll need to:")
    print("1. Create a Google Cloud Project (free)")
    print("2. Enable the BigQuery API")
    print("3. Create OAuth 2.0 credentials")
    print()

    # Check if we already have credentials
    creds = None
    token_file = 'bigquery_token.json'

    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
            print("[SUCCESS] Found existing credentials")
        except Exception as e:
            print(f"[WARNING] Existing credentials invalid: {e}")

    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                print("Refreshing expired credentials...")
                creds.refresh(Request())
                print("[SUCCESS] Credentials refreshed")
            except Exception as e:
                print(f"[ERROR] Failed to refresh credentials: {e}")
                creds = None

        if not creds:
            print("Starting OAuth flow...")
            print("You need to create OAuth 2.0 credentials first:")
            print()
            print("1. Go to: https://console.cloud.google.com/")
            print("2. Create a new project or select existing one")
            print("3. Enable BigQuery API: https://console.cloud.google.com/apis/library/bigquery.googleapis.com")
            print("4. Go to: https://console.cloud.google.com/apis/credentials")
            print("5. Click 'Create Credentials' > 'OAuth client ID'")
            print("6. Choose 'Desktop application'")
            print("7. Download the JSON file and save it as 'client_secrets.json' in this directory")
            print()

            credentials_file = 'client_secrets.json'
            if not os.path.exists(credentials_file):
                print(f"[ERROR] Please create {credentials_file} with your OAuth credentials first!")
                print("Download it from Google Cloud Console and save it here.")
                return None, None

            try:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
                print("[SUCCESS] Authentication successful!")
            except Exception as e:
                print(f"[ERROR] Authentication failed: {e}")
                return None, None

        # Save the credentials for the next run
        try:
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
            print(f"[SUCCESS] Credentials saved to {token_file}")
        except Exception as e:
            print(f"[WARNING] Could not save credentials: {e}")

    return creds, extract_project_id(creds)

def extract_project_id(creds):
    """Extract project ID from credentials or ask user."""
    # Try to get project ID from token
    if hasattr(creds, 'project_id') and creds.project_id:
        return creds.project_id

    # Ask user for project ID
    print()
    project_id = input("Enter your Google Cloud Project ID: ").strip()
    if not project_id:
        print("[ERROR] Project ID is required!")
        return None

    return project_id

def test_bigquery_access(creds, project_id):
    """Test BigQuery access with GDELT dataset."""
    try:
        print(f"Testing BigQuery access with project: {project_id}")
        client = bigquery.Client(project=project_id, credentials=creds)

        # Test query - count recent GDELT events
        query = """
        SELECT COUNT(*) as event_count
        FROM `gdelt-bq.gdeltv2.events`
        WHERE DATEADDED >= '20250920000000'
        LIMIT 1
        """

        print("Running test query...")
        query_job = client.query(query)
        results = query_job.result()

        for row in results:
            print(f"[SUCCESS] BigQuery test successful! Found {row.event_count} recent events")
            return True

    except Exception as e:
        print(f"[ERROR] BigQuery test failed: {e}")
        print()
        print("Common issues and solutions:")
        print("1. Authentication scope issue:")
        print("   - Delete bigquery_token.json and run this script again")
        print("   - This will re-authenticate with broader scopes")
        print()
        print("2. Project setup issues:")
        print("   - Make sure BigQuery API is enabled in your project")
        print("   - Verify your project ID is correct")
        print("   - Ensure you have billing enabled (required but GDELT queries are free)")
        print()
        print("3. If the error mentions 'insufficient scopes':")
        print("   - The authentication token needs to be refreshed with new scopes")
        print("   - Simply delete bigquery_token.json and run the script again")
        return False

def create_env_file(project_id):
    """Create or update .env file with BigQuery settings."""
    env_file = '.env'
    env_vars = {}

    # Read existing .env file
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    key, value = line.strip().split('=', 1)
                    env_vars[key] = value

    # Update with BigQuery settings
    env_vars['GOOGLE_CLOUD_PROJECT'] = project_id
    env_vars['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.abspath('bigquery_token.json')

    # Write updated .env file
    with open(env_file, 'w') as f:
        for key, value in env_vars.items():
            f.write(f"{key}={value}\n")

    print(f"[SUCCESS] Updated {env_file} with BigQuery configuration")

def main():
    print("Google BigQuery Setup for GDELT Data Access")
    print("=" * 50)

    # Set up authentication
    creds, project_id = setup_user_credentials()
    if not creds or not project_id:
        print("[ERROR] Setup failed. Please try again.")
        return

    # Test access
    if not test_bigquery_access(creds, project_id):
        print("[ERROR] BigQuery test failed. Check your setup.")
        return

    # Create environment configuration
    create_env_file(project_id)

    print()
    print("[SUCCESS] BigQuery setup completed successfully!")
    print()
    print("Next steps:")
    print("1. Your GDELT data source will now use BigQuery (no rate limits!)")
    print("2. Run your tests to see the improvement")
    print("3. BigQuery queries to public datasets like GDELT are free")
    print()
    print("Files created:")
    print(f"- bigquery_token.json (your authentication token)")
    print(f"- .env (updated with BigQuery settings)")

if __name__ == "__main__":
    main()