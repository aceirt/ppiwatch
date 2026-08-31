# PP Investments Foreclosure Watch — Scraper

Polls judicial auction and foreclosure sites across the FL panhandle every 4-6 hours.
Writes `docs/feed.json` (served via GitHub Pages) and fires CRM alert webhooks
for each watcher whose criteria matches a new listing.

## Sites monitored

| Site | Type | County/Region |
|------|------|---------------|
| bay.realforeclose.com | Judicial foreclosure | Bay FL |
| gulf.realforeclose.com | Judicial foreclosure | Gulf FL |
| walton.realforeclose.com | Judicial foreclosure | Walton FL |
| okaloosa.realforeclose.com | Judicial foreclosure | Okaloosa FL |
| washington.realforeclose.com | Judicial foreclosure | Washington FL |
| holmes.realforeclose.com | Judicial foreclosure | Holmes FL |
| jackson.realforeclose.com | Judicial foreclosure | Jackson FL |
| calhoun.realforeclose.com | Judicial foreclosure | Calhoun FL |
| escambia.realforeclose.com | Judicial foreclosure | Escambia FL |
| auction.com | REO + bank-owned | Multi-county |
| hubzu.com | Bank-owned REO | Multi-county |
| xome.com | Fannie Mae + REO | Multi-county |
| hudhomestore.com | FHA foreclosures | Multi-county |
| govdeals.com | Govt surplus / tax deed | Multi-county |

## Environment variables (secrets)

| Variable | Required | Description |
|----------|----------|-------------|
| `CRM_WEBHOOK_URL` | Yes | Workflow 2 webhook URL from CRM Automations |
| `CRM_API_KEY` | Yes | CRM API key — for fetching active watchers |
| `HUD_API_KEY` | Optional | Improves HUD result reliability |
| `GOVDEALS_API_KEY` | Optional | GovDeals API key |
| `FEED_OUTPUT_PATH` | Optional | Default: `docs/feed.json` |

## Deploy option 1 — GitHub Actions (recommended)

Already configured in `.github/workflows/scrape.yml`. Runs at 6 AM, 12 PM, 6 PM UTC.

```bash
# 1. Go to repo Settings → Secrets and variables → Actions
# 2. Add: CRM_WEBHOOK_URL, CRM_API_KEY
# 3. Go to Settings → Pages → Source: GitHub Actions
# 4. Push to main — first run starts automatically
# 5. feed.json lives at: https://aceirt.github.io/ppiwatch/feed.json
```

## Deploy option 2 — VPS / Linux cron

```bash
git clone https://github.com/aceirt/ppiwatch.git
cd ppiwatch
pip install -r scraper/requirements.txt

export CRM_WEBHOOK_URL="https://services.leadconnectorhq.com/hooks/..."
export CRM_API_KEY="your-crm-api-key"
export FEED_OUTPUT_PATH="/var/www/html/feed.json"

python scraper/scraper.py

# Add to crontab (every 6 hours)
# 0 */6 * * * cd /path/to/ppiwatch && python scraper/scraper.py >> /var/log/ppwatch.log 2>&1
```

## Deploy option 3 — n8n

1. Create a **Schedule trigger** node every 6 hours
2. Add **Execute Command** node: `python /path/scraper/scraper.py`
3. Set environment variables in node settings

## Deploy option 4 — AWS Lambda + EventBridge

```bash
zip -r scraper.zip scraper/
aws lambda create-function \
  --function-name ppwatch-scraper \
  --runtime python3.11 \
  --handler scraper.run_once \
  --zip-file fileb://scraper.zip \
  --environment Variables='{CRM_WEBHOOK_URL=...,CRM_API_KEY=...}'

aws events put-rule --schedule-expression "rate(6 hours)" --name ppwatch-schedule
```

## Wiring the live feed into the watch app

1. Deploy the scraper using any option above
2. Note the public URL of `feed.json`:
   - GitHub Pages: `https://aceirt.github.io/ppiwatch/feed.json`
3. Open `watch.ppinvestments.net/feed`
4. Click **Live data source** panel → paste the feed URL → Save
5. The app polls it every 60 seconds and on tab focus

## Adding new counties or states

Open `scraper.py` and add a tuple to `REALFORECLOSE_COUNTIES`:

```python
("leon",    "Leon FL"),     # Tallahassee
("alachua", "Alachua FL"),  # Gainesville
("duval",   "Duval FL"),    # Jacksonville
```

---
*PP Investments — Panama City, FL — watch.ppinvestments.net*
