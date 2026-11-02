"""
One-time helper: generate a Google Ads refresh token via the OAuth consent flow.

WHY: the Google Ads API call only works if the refresh token belongs to a Google
account that has access to the target Ads customer (__GOOGLE_ADS_CUSTOMER_ID__). This opens a
browser, lets you log in as THAT account, and prints the refresh token.

HOW TO RUN:
  1. In Google Cloud Console -> APIs & Services -> Credentials, open the OAuth
     client you want to use (ideally a "Desktop app" client). Click "Download JSON".
  2. Save that file next to this script as:  client_secret.json
  3. Run:   python generate_refresh_token.py
  4. A browser opens. **Log in as the Google account that can see Ads account
     __GOOGLE_ADS_CUSTOMER_ID__** (the one used in Google Ads UI), and approve.
  5. Copy the printed refresh token into .env as GOOGLE_ADS_REFRESH_TOKEN,
     and set GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET to match that client.

Fallback: if you don't have client_secret.json but DO have the client_id/secret,
set GADS_CLIENT_ID and GADS_CLIENT_SECRET as env vars before running.
"""
import os
import json

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/adwords"]
HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(HERE, "client_secret.json")


def build_flow():
    if os.path.exists(CLIENT_SECRET_FILE):
        print(f"Using {CLIENT_SECRET_FILE}")
        return InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes=SCOPES)

    cid = os.getenv("GADS_CLIENT_ID")
    csecret = os.getenv("GADS_CLIENT_SECRET")
    if not (cid and csecret):
        raise SystemExit(
            "No client_secret.json found and GADS_CLIENT_ID / GADS_CLIENT_SECRET not set.\n"
            "Download the OAuth client JSON from Google Cloud Console and save it as "
            f"{CLIENT_SECRET_FILE}, or export GADS_CLIENT_ID and GADS_CLIENT_SECRET."
        )
    client_config = {
        "installed": {
            "client_id": cid,
            "client_secret": csecret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    print("Using GADS_CLIENT_ID / GADS_CLIENT_SECRET from environment")
    return InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)


def main():
    flow = build_flow()
    # access_type=offline + prompt=consent guarantees a refresh_token is returned.
    try:
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    except Exception as e:
        print(f"\nLocal-server flow failed ({e}); falling back to console flow.")
        creds = flow.run_console(access_type="offline", prompt="consent")

    print("\n" + "=" * 70)
    print("SUCCESS. Put this in ads_automation/.env :\n")
    print(f"GOOGLE_ADS_REFRESH_TOKEN={creds.refresh_token}")
    print("\n(and make sure GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET match")
    print(" the OAuth client you just used.)")
    print("=" * 70)


if __name__ == "__main__":
    main()
