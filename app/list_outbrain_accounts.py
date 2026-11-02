"""
Lists every Outbrain Amplify marketer account the current OUTBRAIN_API_KEY can see.
Read-only.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

OB_BASE_URL = "https://api.outbrain.com/amplify/v0.1"
token = os.getenv("OUTBRAIN_API_KEY")
current_id = os.getenv("OUTBRAIN_ACCOUNT_ID", "")

if not token:
    raise SystemExit("OUTBRAIN_API_KEY missing in .env")

headers = {"OB-TOKEN-V1": token}


def fetch(path):
    r = requests.get(f"{OB_BASE_URL}{path}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


print(f"\nOutbrain discovery (current account in .env: {current_id})\n")

# Step 1: who is this token? (user info)
try:
    me = fetch("/users/me")
    print("Token belongs to:")
    print(f"  email:  {me.get('email')}")
    print(f"  name:   {me.get('firstName','')} {me.get('lastName','')}".strip())
    print(f"  userId: {me.get('id')}")
    print()
except Exception as e:
    print(f"Could not fetch /users/me: {e}\n")

# Step 2: every marketer this user can access
try:
    data = fetch("/marketers")
    marketers = data.get("marketers", [])
except Exception as e:
    raise SystemExit(f"Failed to list marketers: {e}")

if not marketers:
    print("No marketer accounts visible.")
    raise SystemExit(0)

print(f"{len(marketers)} marketer account(s) visible:\n")
print(f"{'Marketer ID':<36} {'Currency':<8} {'Country':<8} Name")
print("-" * 100)

de_accounts = []
for m in marketers:
    mid = m.get("id", "")
    currency = m.get("currency", "—")
    country = m.get("country", "—")
    name = m.get("name", "—")
    marker = "  <- current" if mid == current_id else ""
    print(f"{mid:<36} {currency:<8} {country:<8} {name}{marker}")
    nm = (name or "").lower()
    if (country or "").upper() == "DE" or "german" in nm or "deutschland" in nm or " de" in nm or nm.endswith(" de"):
        de_accounts.append(m)

print()
if de_accounts:
    print(f"Likely Germany account(s):")
    for a in de_accounts:
        print(f"  {a['id']}  ->  {a.get('name')}  ({a.get('country')})")
else:
    print("No clearly-German marketer accounts found. Ask the DE team for their Outbrain marketer ID.")
