# Ads automation and creative dashboard

A small Flask app that pulls daily ad performance from Meta, Google Ads,
Outbrain and TikTok into one Google Sheet (`main_metrics`), plus the static
creative-performance dashboard that reads from that sheet.

Built for the performance-marketing team of an Italian residential solar
installer. All company names, IDs and figures have been removed.

## Layout

| Folder | What it is |
|---|---|
| `app/` | Flask app (`app.py`, UI in `templates/`), one fetcher per platform in `fetchers/`, Google Sheets writer, daily runners |
| `app/fetchers/seo*.py`, `app/seo_*.py` | Separate SEO / GEO dashboard: GA4, Search Console, Peec AI and warehouse lead counts into their own sheet |
| `app/fetchers/whatsapp.py` | WhatsApp mode: GA4 WhatsApp events into a separate sheet (counts only) |
| `dashboard/creative-ad-performance.html` | Single-file creative dashboard (reads the sheet via the gviz endpoint) |
| `dashboard/apps-script/` | Deploys the dashboard as a Google Apps Script web app (`deploy.ps1`, clasp) |
| `dashboard/static-web-app/` | Alternative Azure Static Web App deploy |
| `scripts/creative-analysis/` | One-off scripts: download Meta creatives, enrich ad details, LLM creative analysis |
| `scripts/legend-merge/` | Weekly merge of the GTM export with the creative legend workbook |

## Setup

```bash
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in the platform credentials you use
python app/app.py       # UI on http://localhost:5000
```

Helpers to find your account IDs: `app/list_meta_accounts.py`,
`app/list_google_accounts.py`, `app/list_outbrain_accounts.py`.
`app/generate_refresh_token.py` creates the Google Ads refresh token.

Scheduled runs: `app/run_daily.bat` (ads) and `app/run_seo_daily.ps1` (SEO),
e.g. from Windows Task Scheduler.

## Notes

- Platforms with no credentials in `.env` are skipped.
- The TikTok fetcher has only been tested against mocked responses.
- Nothing in this repo contains real data. CSV, XLSX, logs and `.env` are gitignored.
