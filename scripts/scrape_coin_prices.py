#!/usr/bin/env python3
"""
Scrape daily price history for 14 crypto assets (BTC already handled separately).

Sources:
- Binance 1d klines: ETH, BNB, XRP, SOL, TRX, DOGE, ZEC (full history from listing date)
- CoinGecko 365d: USDT, USDC, USDS, HYPE, FIGR_HELOC, RAIN, LEO (newer / stablecoins)

Output: data/{symbol}-price.json (one file per coin)
        Format mirrors data/btc-price.json
        {source, scrapedAt, lastUpdated, rows: [{date, price}, ...]}
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Per-coin config
COINS = [
    # (coinId, source, param, filename)
    # Binance coins
    {"id": "ethereum",     "source": "binance",     "symbol": "ETHUSDT",  "file": "eth-price.json"},
    {"id": "binancecoin",  "source": "binance",     "symbol": "BNBUSDT",  "file": "bnb-price.json"},
    {"id": "ripple",       "source": "binance",     "symbol": "XRPUSDT",  "file": "xrp-price.json"},
    {"id": "solana",       "source": "binance",     "symbol": "SOLUSDT",  "file": "sol-price.json"},
    {"id": "tron",         "source": "binance",     "symbol": "TRXUSDT",  "file": "trx-price.json"},
    {"id": "dogecoin",     "source": "binance",     "symbol": "DOGEUSDT", "file": "doge-price.json"},
    {"id": "zcash",        "source": "binance",     "symbol": "ZECUSDT",  "file": "zec-price.json"},
    # CoinGecko 365d coins (newer or stablecoins)
    {"id": "tether",       "source": "coingecko",   "symbol": "USDT",      "file": "usdt-price.json"},
    {"id": "usd-coin",     "source": "coingecko",   "symbol": "USDC",      "file": "usdc-price.json"},
    {"id": "figure-heloc", "source": "coingecko",   "symbol": "FIGR_HELOC", "file": "figure-heloc-price.json"},
    {"id": "hyperliquid",  "source": "coingecko",   "symbol": "HYPE",      "file": "hype-price.json"},
    {"id": "usds",         "source": "coingecko",   "symbol": "USDS",      "file": "usds-price.json"},
    {"id": "rain",         "source": "coingecko",   "symbol": "RAIN",      "file": "rain-price.json"},
    {"id": "leo-token",    "source": "coingecko",   "symbol": "LEO",       "file": "leo-price.json"},
]

DATA_DIR = Path(__file__).parent.parent / "data"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def fetch_json(url, timeout=30, max_retries=3):
    """Fetch JSON with retry on rate limits (CoinGecko 429)."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait = 30 * (attempt + 1)  # 30s, 60s, 90s
                print(f"    ⏳ 429 rate limit, sleeping {wait}s...")
                time.sleep(wait)
                continue
            raise
    return None


def fetch_binance_klines(symbol):
    """Fetch all 1d klines for a symbol. Returns list of {date, price}."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&startTime=0&limit=1000"
    all_rows = []
    while url:
        data = fetch_json(url)
        if not data:
            break
        for k in data:
            # kline: [openTime, open, high, low, close, volume, closeTime, ...]
            ts = k[0]
            close = float(k[4])
            date_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            all_rows.append({"date": date_str, "price": close})
        if len(data) < 1000:
            break
        # Paginate: next batch starts after last closeTime
        last_close_time = data[-1][6]
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&startTime={last_close_time + 1}&limit=1000"
        time.sleep(0.3)
    return all_rows


def fetch_coingecko_365d(coin_id):
    """Fetch 365d daily prices for a coin from CoinGecko public API."""
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=365"
    data = fetch_json(url)
    prices = data.get("prices", [])
    rows = []
    for ts_ms, price in prices:
        date_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append({"date": date_str, "price": price})
    return rows


def save_json(filename, source, rows):
    """Save with same format as btc-price.json."""
    if not rows:
        print(f"  ⚠️  No rows for {filename}, skipping")
        return False
    # Deduplicate by date (keep last occurrence)
    seen = {}
    for r in rows:
        seen[r["date"]] = r["price"]
    deduped = [{"date": d, "price": seen[d]} for d in sorted(seen.keys())]

    payload = {
        "source": source,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "lastUpdated": deduped[-1]["date"] if deduped else None,
        "rows": deduped,
    }
    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / filename
    out_path.write_text(json.dumps(payload, indent=2))
    return True


def main():
    print(f"Scraping {len(COINS)} coins → {DATA_DIR}\n")
    results = []
    for i, coin in enumerate(COINS, 1):
        cid = coin["id"]
        src = coin["source"]
        sym = coin["symbol"]
        fname = coin["file"]
        try:
            if src == "binance":
                rows = fetch_binance_klines(sym)
                source = f"binance {sym} 1d klines"
            elif src == "coingecko":
                rows = fetch_coingecko_365d(cid)
                source = f"coingecko /coins/{cid}/market_chart days=365"
            else:
                print(f"  [{i:2}/{len(COINS)}] {cid:18} ❌ unknown source: {src}")
                continue

            if save_json(fname, source, rows):
                last = rows[-1] if rows else {}
                first = rows[0] if rows else {}
                # Get first non-zero for older stablecoins that might start at 0
                first_nz = next((r for r in rows if r["price"] > 0), first)
                print(f"  [{i:2}/{len(COINS)}] {cid:18} {src:10} → {fname}  {first.get('date','?'):10} -> {last.get('date','?'):10}  {len(rows):4} pts  ${last.get('price',0):.4g}  (first>0: {first_nz.get('date','?')} ${first_nz.get('price',0):.4g})")
                results.append((cid, fname, len(rows)))
            else:
                print(f"  [{i:2}/{len(COINS)}] {cid:18} ❌ no data")
        except urllib.error.HTTPError as e:
            print(f"  [{i:2}/{len(COINS)}] {cid:18} ❌ HTTP {e.code}: {e.read().decode()[:80]}")
        except Exception as e:
            print(f"  [{i:2}/{len(COINS)}] {cid:18} ❌ {type(e).__name__}: {str(e)[:80]}")
        # Rate limit protection (CoinGecko free tier is strict: ~10-15 calls/min)
        if src == "coingecko":
            time.sleep(7)  # safe margin
        else:
            time.sleep(0.3)

    print(f"\n✅ {len(results)}/{len(COINS)} coins scraped")
    return 0 if len(results) == len(COINS) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
