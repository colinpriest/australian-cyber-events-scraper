  🔧 Step-by-Step BigQuery Setup:

1. Create Google Cloud Project (if you don't have one):

- Go to: https://console.cloud.google.com/
- Click "New Project"
- Give it a name (e.g., "gdelt-cyber-data")
- Note the Project ID (you'll need this)

2. Enable BigQuery API:

- Visit: https://console.cloud.google.com/apis/library/bigquery.googleapis.com
- Click "Enable"

3. Create OAuth Credentials:

- Go to: https://console.cloud.google.com/apis/credentials
- Click "Create Credentials" → "OAuth client ID"
- Choose "Desktop application"
- Give it a name (e.g., "GDELT Data Access")
- Click "Create"
- Download the JSON file
- Save it as client_secrets.json in your project directory

4. Run Setup Again:

  Once you have the client_secrets.json file, run:

  python setup_bigquery_auth.py

  This will:

- Open your browser for Google authentication
- Save your credentials securely
- Test BigQuery access with GDELT data
- Update your .env file with the configuration

5. Renewing Authorization When It Expires:

- If the pipeline logs report `Reauthentication is needed. Please run gcloud auth application-default login`, your local Application Default Credentials (ADC) have expired.
- The quickest fix is to rerun `python setup_bigquery_auth.py` and follow the browser prompt.
- Alternatively, you can refresh the ADC token directly with the Google Cloud CLI:
  1. Install the CLI if `gcloud` is not recognised (https://cloud.google.com/sdk/docs/install) and reopen your shell.
  2. Set the correct project: `gcloud config set project gdelt-cyber-data` (replace with your project ID if different).
  3. Run `gcloud auth application-default login`, sign in via the browser, and wait for the success message.
- After renewing, restart `discover_enrich_events.py` and confirm the logs show `BigQuery client initialized successfully`.