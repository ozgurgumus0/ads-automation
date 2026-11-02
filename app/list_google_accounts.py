"""
Lists every Google Ads customer the current refresh token can see.
Walks the customer hierarchy: starts from listAccessibleCustomers(),
then for each one checks whether it's an MCC (manager) and lists its children.
Read-only.
"""
import os
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient

load_dotenv()


def _build_client(login_customer_id=None):
    cfg = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus": True,
    }
    if login_customer_id:
        cfg["login_customer_id"] = str(login_customer_id).replace("-", "")
    return GoogleAdsClient.load_from_dict(cfg)


def _list_accessible(client):
    svc = client.get_service("CustomerService")
    res = svc.list_accessible_customers()
    return [r.split("/")[-1] for r in res.resource_names]


def _customer_info(client, customer_id):
    """Run a small GAQL query to learn whether the customer is an MCC and its name."""
    ga = client.get_service("GoogleAdsService")
    query = """
        SELECT
            customer.id,
            customer.descriptive_name,
            customer.currency_code,
            customer.time_zone,
            customer.manager,
            customer.test_account,
            customer.status
        FROM customer
        LIMIT 1
    """
    try:
        resp = ga.search(customer_id=customer_id, query=query)
        for row in resp:
            c = row.customer
            return {
                "id": str(c.id),
                "name": c.descriptive_name,
                "currency": c.currency_code,
                "tz": c.time_zone,
                "manager": c.manager,
                "test": c.test_account,
                "status": c.status.name if hasattr(c.status, "name") else str(c.status),
            }
    except Exception as e:
        return {"id": customer_id, "error": str(e)[:200]}
    return {"id": customer_id, "error": "no rows returned"}


def _list_children(client, manager_id):
    """List every customer linked under this manager (one level deep)."""
    ga = client.get_service("GoogleAdsService")
    query = """
        SELECT
            customer_client.id,
            customer_client.descriptive_name,
            customer_client.currency_code,
            customer_client.time_zone,
            customer_client.manager,
            customer_client.level,
            customer_client.status
        FROM customer_client
        WHERE customer_client.level <= 1
    """
    children = []
    try:
        resp = ga.search(customer_id=manager_id, query=query)
        for row in resp:
            cc = row.customer_client
            children.append({
                "id": str(cc.id),
                "name": cc.descriptive_name,
                "currency": cc.currency_code,
                "tz": cc.time_zone,
                "manager": cc.manager,
                "level": cc.level,
                "status": cc.status.name if hasattr(cc.status, "name") else str(cc.status),
            })
    except Exception as e:
        print(f"    error listing children of {manager_id}: {str(e)[:200]}")
    return children


def main():
    current_id = (os.getenv("GOOGLE_ADS_CUSTOMER_ID") or "").replace("-", "")
    current_login = (os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "").replace("-", "")
    print(f"\nGoogle Ads discovery (current customer={current_id}, login={current_login})\n")

    client = _build_client()
    top_level = _list_accessible(client)
    print(f"listAccessibleCustomers returned {len(top_level)} entry(ies):")
    for cid in top_level:
        print(f"  - {cid}")
    print()

    for cid in top_level:
        info = _customer_info(_build_client(login_customer_id=cid), cid)
        marker = "  <- current" if cid == current_id else ""
        print(f"Customer {cid}{marker}")
        if "error" in info:
            print(f"  error: {info['error']}")
            continue
        kind = "MCC (manager)" if info["manager"] else "ad account"
        print(f"  name:     {info['name']}")
        print(f"  type:     {kind}")
        print(f"  currency: {info['currency']}")
        print(f"  tz:       {info['tz']}")
        print(f"  status:   {info['status']}")
        if info["manager"]:
            kids = _list_children(_build_client(login_customer_id=cid), cid)
            print(f"  children ({len(kids)}):")
            for k in kids:
                kkind = "MCC" if k["manager"] else "account"
                kmark = "  <- current" if k["id"] == current_id else ""
                print(f"    - {k['id']:<12} {kkind:<7} {k['currency']:<4} {k['status']:<12} {k['name']}{kmark}")
        print()


if __name__ == "__main__":
    main()
