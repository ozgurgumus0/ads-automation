"""
Lists every Meta ad account the current META_ACCESS_TOKEN can see.
Read-only — does not modify anything.
"""
import os
from dotenv import load_dotenv
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.user import User

load_dotenv()

token = os.getenv("META_ACCESS_TOKEN")
api_version = os.getenv("META_API_VERSION", "v21.0")
current_account = os.getenv("META_AD_ACCOUNT_ID", "")

if not token:
    raise SystemExit("META_ACCESS_TOKEN missing in .env")

FacebookAdsApi.init(access_token=token, api_version=api_version)
me = User(fbid="me")

fields = [
    "id",
    "account_id",
    "name",
    "account_status",
    "currency",
    "timezone_name",
    "business_country_code",
    "business_name",
]

print(f"\nFetching accessible ad accounts (current default in .env: {current_account})\n")

accounts = list(me.get_ad_accounts(fields=fields))
if not accounts:
    print("No ad accounts visible to this token.")
    raise SystemExit(0)

print(f"{len(accounts)} ad account(s) visible:\n")
print(f"{'ID':<22} {'Country':<8} {'Currency':<8} {'Status':<8} {'Business':<30} Name")
print("-" * 110)

STATUS_MAP = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RV", 8: "PENDING_S", 9: "IN_GRACE", 100: "PENDING_C", 101: "TEMP_UNAV", 102: "CLOSED"}

for acc in accounts:
    aid = acc.get("id", "")
    country = acc.get("business_country_code") or "—"
    currency = acc.get("currency") or "—"
    status_code = acc.get("account_status")
    status = STATUS_MAP.get(status_code, str(status_code))
    biz = (acc.get("business_name") or "—")[:28]
    name = acc.get("name") or "—"
    marker = " <- current default" if aid == current_account else ""
    print(f"{aid:<22} {country:<8} {currency:<8} {status:<8} {biz:<30} {name}{marker}")

print()
de_accounts = [a for a in accounts if (a.get("business_country_code") or "").upper() == "DE"]
if de_accounts:
    print(f"Found {len(de_accounts)} German (DE) account(s) — Germany IDs to use in .env:")
    for a in de_accounts:
        print(f"  {a['id']}  ->  {a.get('name')}")
else:
    print("No German (DE) accounts visible. You'll need someone in DE to grant your token / System User access.")
