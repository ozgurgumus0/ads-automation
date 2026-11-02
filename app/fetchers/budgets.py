"""Today's campaign budgets as set in Meta and Google Ads. READ-ONLY.

Only list/select calls: nothing here creates, edits or pauses anything. Used by
the agent control center's budget-pacing check (does each campaign spend what
it was configured to spend?). The platforms only return the CURRENT budget, so
the history is built by taking one snapshot per day from now on.

Channel names follow fetchers/google_ads.py ("YT" in the name -> Youtube, "DS"
-> Display, SEARCH -> Google Search) plus PERFORMANCE_MAX -> PMax, so budgets
join to main_metrics spend by campaign id and channel.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Explicit path: this module is also imported from the agent control center,
# whose working directory is not ads_automation.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _eur_from_minor(value) -> float | None:
    """Meta returns budgets in the account currency's minor unit (cents)."""
    try:
        return round(int(value) / 100, 2) if value not in (None, "", "0") else None
    except (TypeError, ValueError):
        return None


def meta_budgets() -> list[dict]:
    """Active Meta campaigns and ad sets with their budgets (CBO on the campaign, ABO on the ad sets)."""
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.api import FacebookAdsApi

    FacebookAdsApi.init(access_token=os.getenv("META_ACCESS_TOKEN"), api_version=os.getenv("META_API_VERSION", "v21.0"))
    account = AdAccount(os.getenv("META_AD_ACCOUNT_ID"))
    active = {"effective_status": ["ACTIVE"]}
    rows = []
    campaigns = account.get_campaigns(fields=["id", "name", "effective_status", "daily_budget", "lifetime_budget"], params=active)
    for c in campaigns:
        rows.append({"channel": "Meta", "level": "campaign", "campaign_id": c.get("id"), "campaign": c.get("name"),
                     "adset_id": "", "adset": "", "status": c.get("effective_status"),
                     "daily_budget_eur": _eur_from_minor(c.get("daily_budget")),
                     "lifetime_budget_eur": _eur_from_minor(c.get("lifetime_budget")),
                     "shared_budget_id": "", "budget_limited": None})
    adsets = account.get_ad_sets(fields=["id", "name", "campaign_id", "effective_status", "daily_budget", "lifetime_budget"],
                                 params=active)
    for a in adsets:
        daily, lifetime = _eur_from_minor(a.get("daily_budget")), _eur_from_minor(a.get("lifetime_budget"))
        if daily is None and lifetime is None:
            continue                     # CBO: the budget lives on the campaign
        rows.append({"channel": "Meta", "level": "adset", "campaign_id": a.get("campaign_id"), "campaign": "",
                     "adset_id": a.get("id"), "adset": a.get("name"), "status": a.get("effective_status"),
                     "daily_budget_eur": daily, "lifetime_budget_eur": lifetime,
                     "shared_budget_id": "", "budget_limited": None})
    return rows


def _google_channel(name: str, channel_type: str) -> str | None:
    if "YT" in name:
        return "Youtube"
    if "DS" in name:
        return "Display"
    if channel_type == "SEARCH":
        return "Google Search"
    if channel_type == "PERFORMANCE_MAX":
        return "PMax"
    return None


def google_budgets() -> list[dict]:
    """Enabled Google Ads campaigns with their daily budget, shared-budget id and 'limited by budget' status."""
    from fetchers.google_ads import _build_client

    client = _build_client()
    service = client.get_service("GoogleAdsService")
    customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID").replace("-", "")
    query = """
        SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
               campaign.primary_status_reasons, campaign_budget.id, campaign_budget.amount_micros,
               campaign_budget.explicitly_shared, campaign_budget.period
        FROM campaign
        WHERE campaign.status = 'ENABLED'"""
    rows = []
    for batch in service.search_stream(customer_id=customer_id, query=query):
        for r in batch.results:
            name = r.campaign.name
            channel = _google_channel(name, r.campaign.advertising_channel_type.name)
            if channel is None:
                continue
            reasons = {getattr(x, "name", str(x)) for x in r.campaign.primary_status_reasons}
            rows.append({"channel": channel, "level": "campaign", "campaign_id": str(r.campaign.id), "campaign": name,
                         "adset_id": "", "adset": "", "status": r.campaign.status.name,
                         "daily_budget_eur": round(r.campaign_budget.amount_micros / 1_000_000, 2) if r.campaign_budget.amount_micros else None,
                         "lifetime_budget_eur": None,
                         "shared_budget_id": str(r.campaign_budget.id) if r.campaign_budget.explicitly_shared else "",
                         "budget_limited": "BUDGET_CONSTRAINED" in reasons})
    return rows


def fetch_budgets() -> tuple[list[dict], list[str]]:
    """Every platform's budgets; a platform that fails is reported, not fatal."""
    rows, errors = [], []
    for name, fetch in (("Meta", meta_budgets), ("Google Ads", google_budgets)):
        try:
            rows += fetch()
        except Exception as error:                              # noqa: BLE001
            errors.append(f"{name}: {type(error).__name__}: {str(error)[:200]}")
    return rows, errors
