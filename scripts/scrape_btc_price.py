"""
Scrape Bitcoin USD price from Blockchain.com historical chart.
Output: data/btc-price.json

Why this approach:
- Blockchain.com charts/market-price endpoint has no daily granularity for
  the FULL all-time range (returns 4-day intervals + duplicates when
  timespan=all is used to cover 2009-now).
- For recent data, timespan=5years returns DAILY values (1826 points).
- We fetch BOTH and merge: prefer daily for last 5y, keep 4-day for older.

Why Blockchain.com (vs CoinGecko/Binance):
- No API key needed (CoinGecko free tier: 365 days max without key)
- Earliest coverage: 2009-01-03 (genesis) - other APIs start 2013/2017
- CORS not exposed → must scrape server-side and serve as static JSON
  (matches the Farside pattern used for ETF data)

The first ~18 months (2009-01-03 → 2010-08-18) have price=0.0 because
no exchanges existed yet. We keep these for completeness.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / 'data'
OUT_DIR.mkdir(exist_ok=True)
OUT_PATH = OUT_DIR / 'btc-price.json'

# All-time: full coverage back to 2009-01-03, but 4-day intervals + dups
URL_ALL = 'https://api.blockchain.info/charts/market-price?timespan=all&format=json'
# Last 5 years: DAILY granularity, more useful for recent analysis
URL_5Y = 'https://api.blockchain.info/charts/market-price?timespan=5years&format=json'


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def values_to_rows(values: list) -> list:
    """Convert Blockchain.com values array to [{date, price}, ...] sorted by date."""
    rows = []
    for v in values:
        ts = v['x']  # unix seconds
        price = v['y']
        date_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        rows.append({'date': date_iso, 'price': price})
    return rows


def main() -> int:
    print('=== BTC PRICE SCRAPER (blockchain.info) ===', flush=True)

    # 1) Fetch all-time (4-day intervals, 2009-now)
    print(f'\n[1/3] Fetching all-time: {URL_ALL}', flush=True)
    try:
        all_data = fetch(URL_ALL)
    except Exception as e:
        print(f'  ERROR: {e}', file=sys.stderr)
        return 1
    if all_data.get('status') != 'ok':
        print(f'  ERROR: status not ok', file=sys.stderr)
        return 1
    all_rows = values_to_rows(all_data.get('values', []))
    print(f'  -> {len(all_rows)} points (4-day interval)', flush=True)

    # 2) Fetch 5-year daily
    print(f'\n[2/3] Fetching 5y daily: {URL_5Y}', flush=True)
    try:
        fy_data = fetch(URL_5Y)
        fy_rows = values_to_rows(fy_data.get('values', []))
        print(f'  -> {len(fy_rows)} points (daily)', flush=True)
    except Exception as e:
        print(f'  WARN: 5y fetch failed ({e}); falling back to all-time only', flush=True)
        fy_rows = []

    # 3) Merge: prefer daily (5y) over 4-day (all) for any overlapping date
    print(f'\n[3/3] Merging (prefer daily)...', flush=True)
    by_date = {}
    for r in all_rows:
        by_date[r['date']] = r  # baseline
    for r in fy_rows:
        by_date[r['date']] = r  # daily overrides
    rows = sorted(by_date.values(), key=lambda r: r['date'])
    print(f'  -> {len(rows)} unique dates after merge', flush=True)

    result = {
        'source': 'https://api.blockchain.info/charts/market-price (all + 5y merge)',
        'scrapedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'lastUpdated': rows[-1]['date'] if rows else None,
        'rows': rows,
    }

    with open(OUT_PATH, 'w') as f:
        json.dump(result, f, indent=2)

    size_kb = OUT_PATH.stat().st_size / 1024
    n_zero = sum(1 for r in rows if r['price'] == 0)
    first_nz = next((r for r in rows if r['price'] > 0), None)
    print(f'\n  -> {OUT_PATH} ({size_kb:.1f} KB)', flush=True)
    print(f'  total rows: {len(rows)}', flush=True)
    print(f'  range: {rows[0]["date"]} → {rows[-1]["date"]}', flush=True)
    print(f'  zero-price days: {n_zero} (no trades, kept for completeness)', flush=True)
    if first_nz:
        print(f'  first non-zero: {first_nz["date"]} @ ${first_nz["price"]:.2f}', flush=True)
    print(f'  latest: ${rows[-1]["price"]:,.2f}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
