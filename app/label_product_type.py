"""
Classifies each ad in the Transkripts tab as PV, HP, PV + HP, or Unknown,
then writes the result to a "Product Type" column.

Uses:
  1. Rule-based signals from ad name + campaign name
  2. GPT-4o for any that remain ambiguous after rules
"""

import sys
import time
from dotenv import load_dotenv
import gspread
from openai import OpenAI
import os

load_dotenv(override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPREADSHEET_ID = "__SHEET_ID__"
SA_FILE        = "service_account.json"


# ── Rule-based classification ─────────────────────────────────────────────────

PV_KEYWORDS = [
    "fotovoltaic", "solare", "solar", "pannelli", " pv", "_pv", ".pv",
    "pnrr", "cer", "bollette", "bolletta", "sconto_fattura", "sconto fattura",
    "freebill", "free bill", "energia", "ecobonus", "superbonus",
    "incentivi", "sliderbonus", "slider bonus", "916",
    "risparmio", "luce", "kwh", "kwp",
]
HP_KEYWORDS = [
    "heatpump", "heat pump", "heat_pump", " hp", "_hp", ".hp", "::hp",
    "pompa di calore", "pompa_di_calore", "riscaldamento", "caldaia",
    "climatizzaz", "strappa_h2", "h2", "mercato tutelato",
]
BOTH_NAMES = [
    "BrandOne", "BrandOne", "BrandOne",
]


def rule_classify(ad_name: str, campaign_name: str, transcript: str) -> str | None:
    """Return 'PV', 'HP', 'PV + HP', or None (= needs GPT)."""
    text = (ad_name + " " + campaign_name + " " + transcript).lower()

    for b in BOTH_NAMES:
        if b.lower() in text:
            return "PV + HP"

    has_pv = any(k in text for k in PV_KEYWORDS)
    has_hp = any(k in text for k in HP_KEYWORDS)

    if has_pv and has_hp:
        return "PV + HP"
    if has_pv:
        return "PV"
    if has_hp:
        return "HP"
    return None  # uncertain


# ── GPT-4o batch classification ───────────────────────────────────────────────

def gpt_classify_batch(client: OpenAI, items: list[dict]) -> dict[str, str]:
    """
    items: [{"ad_name": ..., "campaign": ..., "transcript": ...}, ...]
    Returns {ad_name: label} where label in {"PV", "HP", "PV + HP", "Unknown"}
    """
    lines = []
    for i, it in enumerate(items, 1):
        snippet = (it["transcript"] or "")[:300].replace("\n", " ")
        lines.append(
            f'{i}. Ad name: "{it["ad_name"]}" | Campaign: "{it["campaign"]}" | '
            f'Transcript snippet: "{snippet}"'
        )

    prompt = (
        "You are classifying Italian solar/energy ads for an Italian residential solar/energy company.\n"
        "Label each ad as exactly one of: PV, HP, PV + HP, Unknown.\n"
        "  PV  = Photovoltaic / solar panels\n"
        "  HP  = Heat Pump (pompa di calore)\n"
        "  PV + HP = both products together\n"
        "  Unknown = cannot determine\n\n"
        "Context clues:\n"
        "- 'bollette', 'sconto fattura', 'luce', 'CER', 'PNRR', 'SliderBonus', '916', 'Ecobonus' → PV\n"
        "- 'pompa di calore', 'riscaldamento', 'caldaia', 'H2' → HP\n"
        "- 'BrandOne' or 'BrandOne' → PV + HP\n"
        "- 'HP_' prefix in ad name at this advertiser means Heat Pump\n"
        "- 'Younited' = financing partner for HP\n"
        "- 'Strappa' = HP campaign\n"
        "- 'Fiducia', 'Trend' = likely HP brand campaigns\n\n"
        "Respond with ONLY a numbered list matching the input order, one label per line:\n"
        "1. PV\n2. HP\n...\n\n"
        "Ads to classify:\n" + "\n".join(lines)
    )

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip()

    results = {}
    for i, line in enumerate(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        # strip leading "N." or "N) "
        label = line.split(".", 1)[-1].split(")", 1)[-1].strip()
        if i < len(items):
            results[items[i]["ad_name"]] = label
    return results


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    print("Connecting to Google Sheets…")
    gc = gspread.service_account(filename=SA_FILE)
    sp = gc.open_by_key(SPREADSHEET_ID)

    transkripts = sp.worksheet("Transkripts")
    all_values  = transkripts.get_all_values()
    header      = all_values[0]
    rows        = all_values[1:]

    col_name       = header.index("Ad Name")
    col_channel    = header.index("Channel")
    col_media      = header.index("Media Type")
    col_transcript = header.index("Transcript")

    # Add "Product Type" column if missing
    if "Product Type" not in header:
        col_pt = len(header)
        transkripts.update_cell(1, col_pt + 1, "Product Type")
        header.append("Product Type")
        for row in rows:
            row.append("")
        print("Added 'Product Type' column.")
    else:
        col_pt = header.index("Product Type")
        print("'Product Type' column already exists.")

    # Build campaign name lookup from main_metrics
    print("Reading main_metrics for campaign name lookup…")
    src     = sp.worksheet("main_metrics")
    records = src.get_all_records()
    campaign_map = {}
    for r in records:
        name = str(r.get("Ad name", "")).strip()
        if name and name not in campaign_map:
            campaign_map[name] = str(r.get("Campaign name", "")).strip()

    # Classify
    uncertain = []  # items needing GPT
    labels    = {}  # ad_name → label

    for row in rows:
        while len(row) < len(header):
            row.append("")
        ad_name    = row[col_name].strip()
        transcript = row[col_transcript].strip()
        campaign   = campaign_map.get(ad_name, "")
        existing   = row[col_pt].strip()

        if existing:
            labels[ad_name] = existing
            continue

        label = rule_classify(ad_name, campaign, transcript)
        if label:
            labels[ad_name] = label
        else:
            uncertain.append({"ad_name": ad_name, "campaign": campaign, "transcript": transcript})

    print(f"Rule-based: {len(labels)} classified, {len(uncertain)} uncertain → sending to GPT-4o…")

    # GPT in batches of 30
    BATCH = 30
    for i in range(0, len(uncertain), BATCH):
        batch = uncertain[i:i + BATCH]
        print(f"  GPT batch {i//BATCH + 1}/{(len(uncertain)-1)//BATCH + 1} ({len(batch)} items)…")
        gpt_labels = gpt_classify_batch(oai, batch)
        labels.update(gpt_labels)
        time.sleep(1)

    # Write back in one batch
    col_letter = chr(ord("A") + col_pt)
    data = []
    for i, row in enumerate(rows):
        ad_name = row[col_name].strip()
        label   = labels.get(ad_name, "Unknown")
        data.append({"range": f"Transkripts!{col_letter}{i + 2}", "values": [[label]]})

    sp.values_batch_update({"valueInputOption": "RAW", "data": data})
    print(f"\nDone. {len(data)} rows labelled.")

    # Summary
    from collections import Counter
    counts = Counter(labels.values())
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
