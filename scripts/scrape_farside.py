"""
Scrape Farside Investors ETF flow data for BTC, ETH, HYPE.
Output: etf-data/{btc,eth,hyp}.json

Uses Playwright (headless Chromium) to bypass Cloudflare JS challenge.

Farside has TWO data sources per page:
1. HTML table (visible): ~14 most recent days, per-ETF daily NET FLOWS
2. Chart.js config (embedded): full history, per-ETF CUMULATIVE AUM
   - BTC: 625 days since 11 Jan 2024, 12 ETFs
   - ETH: 489 days since 23 Jul 2024, 10 ETFs
   - HYP: 24 days since 12 May 2026, 3 ETPs

We use #2 for full history. Daily net flow is computed by differencing
consecutive cumulative AUM values.
"""
import asyncio
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / 'etf-data'
OUT_DIR.mkdir(exist_ok=True)

SOURCES = {
    'btc': {'url': 'https://farside.co.uk/btc/', 'inception': date(2024, 1, 11)},
    'eth': {'url': 'https://farside.co.uk/eth/', 'inception': date(2024, 7, 23)},
    'hyp': {'url': 'https://farside.co.uk/hyp/', 'inception': date(2026, 5, 12)},
}

# US stock market holidays (rough list, 2024-2026)
US_HOLIDAYS = {
    date(2024, 1, 1),   date(2024, 1, 15),  date(2024, 2, 19),
    date(2024, 3, 29),  date(2024, 5, 27),  date(2024, 6, 19),
    date(2024, 7, 4),   date(2024, 9, 2),   date(2024, 11, 28),
    date(2024, 12, 25), date(2025, 1, 1),   date(2025, 1, 20),
    date(2025, 2, 17),  date(2025, 4, 18),  date(2025, 5, 26),
    date(2025, 6, 19),  date(2025, 7, 4),   date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1),
    date(2026, 1, 19),  date(2026, 2, 16),  date(2026, 4, 3),
    date(2026, 5, 25),  date(2026, 6, 19),  date(2026, 7, 3),
    date(2026, 9, 7),   date(2026, 11, 26), date(2026, 12, 25),
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def assign_trading_dates(n: int, end_date: date) -> list:
    """Compute N trading-day dates ending at the most recent trading day <= end_date."""
    last = end_date
    while not is_trading_day(last):
        last -= timedelta(days=1)
    dates = [last]
    for _ in range(n - 1):
        d = dates[-1] - timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        dates.append(d)
    return list(reversed(dates))


def parse_chart_data(script_text: str) -> dict:
    """
    Extract labels + per-ETF cumulative AUM data from Farside chart config.
    Handles two formats:
    1. `const seriesData = { TICKER: [vals...], ... }`  (BTC, ETH)
    2. `new Chart(ctx, { data: { labels: [...], datasets: [{label, data}, ...] } })`  (HYP)
    Returns {'labels': [date_strs], 'series': {ticker: [vals]}} or {}.
    """
    if not script_text or 'seriesData' not in script_text and 'new Chart(' not in script_text:
        return {}

    # Format 1: seriesData object
    m = re.search(r'(?:const|let|var)\s+seriesData\s*=\s*(\{)', script_text)
    if m:
        start = m.end() - 1
        depth = 0
        end = None
        for i in range(start, len(script_text)):
            if script_text[i] == '{': depth += 1
            elif script_text[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                series = json.loads(script_text[start:end])
                # BTC/ETH labels are "MMM YYYY" repeated, not useful as dates — return None
                return {'labels': None, 'series': series}
            except json.JSONDecodeError:
                pass

    # Format 2: inline datasets in new Chart() config
    m = re.search(r'new Chart\(', script_text)
    if m:
        # Find the data: { ... } block
        dm = re.search(r'data\s*:\s*\{', script_text[m.end():])
        if dm:
            # Walk to extract labels and datasets
            block_start = m.end() + dm.end() - 1  # at '{'
            depth = 0
            block_end = None
            for i in range(block_start, len(script_text)):
                if script_text[i] == '{': depth += 1
                elif script_text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        block_end = i + 1
                        break
            if block_end:
                block = script_text[block_start:block_end]
                # Extract labels array
                lm = re.search(r'labels\s*:\s*(\[[^\]]*\])', block)
                # Extract datasets array
                dsm = re.search(r'datasets\s*:\s*(\[)', block)
                labels = json.loads(lm.group(1)) if lm else []
                series = {}
                if dsm:
                    ds_start = dsm.end() - 1
                    depth = 0
                    ds_end = None
                    for i in range(ds_start, len(block)):
                        if block[i] == '[': depth += 1
                        elif block[i] == ']':
                            depth -= 1
                            if depth == 0:
                                ds_end = i + 1
                                break
                    if ds_end:
                        datasets_str = block[ds_start:ds_end]
                        # Each dataset is {label, data, ...}
                        for dsm in re.finditer(r'\{\s*"label"\s*:\s*"([^"]+)"[^}]*"data"\s*:\s*(\[[^\]]+\])', datasets_str):
                            ticker = dsm.group(1)
                            data = json.loads(dsm.group(2))
                            series[ticker] = data
                if series:
                    return {'labels': labels, 'series': series}
    return {}


async def fetch_chart_script(url: str) -> str:
    """Fetch a Farside page, bypass Cloudflare, return the chart script content."""
    js = """
        () => {
            const scripts = Array.from(document.querySelectorAll('script'));
            let s = scripts.find(s => s.textContent && s.textContent.includes('seriesData'));
            if (s) return s.textContent;
            s = scripts.find(s => s.textContent && s.textContent.includes('new Chart('));
            if (s) return s.textContent;
            return null;
        }
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            ctx = await browser.new_context(
                viewport={'width': 1440, 'height': 1200},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = await ctx.new_page()
            for attempt in range(3):
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_timeout(15000)
                    title = await page.title()
                    if 'Just a moment' not in title:
                        break
                except Exception as e:
                    print(f'  attempt {attempt+1} error: {e}', file=sys.stderr)
                    await page.wait_for_timeout(5000)
            script_text = await page.evaluate(js)
            if not script_text:
                # One more retry with longer wait
                await page.wait_for_timeout(10000)
                script_text = await page.evaluate(js)
            return script_text or ''
        finally:
            await browser.close()


def compute_rows(series_data: dict, dates: list, etfs: list) -> tuple:
    """
    Convert cumulative AUM data to:
    - daily net flows (diff of AUM)
    - cumulative AUM (as-is, with dates)
    Returns (flow_rows, aum_rows).
    """
    n = len(dates)
    flow_rows = []
    aum_rows = []
    has_total = 'Total' in series_data
    for i in range(n):
        date_str = dates[i].isoformat()
        flow_values = {}
        aum_values = {}
        total_flow = 0.0
        total_aum = 0.0
        any_aum = False
        for etf in etfs:
            aum_series = series_data[etf]
            cur = aum_series[i] if i < len(aum_series) else None
            prev = aum_series[i - 1] if i > 0 and i - 1 < len(aum_series) else None
            if cur is None:
                aum_values[etf] = None
                flow_values[etf] = None
            else:
                aum_values[etf] = round(cur, 1)
                total_aum += cur
                any_aum = True
                if prev is None:
                    flow_values[etf] = round(cur, 1)  # first day
                else:
                    flow_values[etf] = round(cur - prev, 1)
                if flow_values[etf] is not None:
                    total_flow += flow_values[etf]
        if has_total and i < len(series_data['Total']):
            aum_total = round(series_data['Total'][i], 1)
            flow_total = round(series_data['Total'][i] - (series_data['Total'][i-1] if i > 0 else 0), 1)
        else:
            aum_total = round(total_aum, 1) if any_aum else None
            flow_total = round(total_flow, 1)
        flow_rows.append({'date': date_str, 'values': flow_values, 'total': flow_total})
        aum_rows.append({'date': date_str, 'values': aum_values, 'total': aum_total})
    return flow_rows, aum_rows


def parse_label_date(label: str) -> date:
    """Parse Farside chart label like '12 May 2026' or 'Jun 2026' to date object.
    Returns first-of-month for partial labels."""
    MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
    parts = label.strip().split()
    if len(parts) == 3:
        try:
            d = int(parts[0])
            m = MONTHS.get(parts[1])
            y = int(parts[2])
            if m: return date(y, m, d)
        except (ValueError, KeyError): pass
    if len(parts) == 2:
        m = MONTHS.get(parts[0])
        y = int(parts[1])
        if m: return date(y, m, 1)
    return None


async def scrape_asset(asset: str, cfg: dict) -> dict:
    print(f'\n=== {asset.upper()} ===', flush=True)
    print(f'  URL: {cfg["url"]}', flush=True)
    script_text = await fetch_chart_script(cfg['url'])
    if not script_text:
        print(f'  ERROR: empty script', file=sys.stderr)
        return None
    chart = parse_chart_data(script_text)
    if not chart or not chart.get('series'):
        print(f'  ERROR: no chart data found', file=sys.stderr)
        return None
    series_data = chart['series']
    chart_labels = chart.get('labels')

    # Normalize ticker names: HYP uses "Bitwise (BHYP)" etc. → keep "BHYP" only
    if asset == 'hyp':
        norm = {}
        for k, v in series_data.items():
            m = re.search(r'\(([A-Z]+)\)', k)
            ticker = m.group(1) if m else k
            norm[ticker] = v
        series_data = norm

    etfs = sorted([k for k in series_data.keys() if k != 'Total'])
    print(f'  ETFs ({len(etfs)}): {etfs}', flush=True)
    n = max(len(v) for v in series_data.values())
    print(f'  data points: {n}', flush=True)

    # Try to use chart labels for dates (HYP case); else compute from trading days
    if chart_labels and len(chart_labels) >= n and any('20' in str(l) and len(str(l).split()) == 3 for l in chart_labels[:5]):
        # Labels have full dates like "12 May 2026"
        dates = [parse_label_date(l) for l in chart_labels[:n]]
        if all(d is not None for d in dates):
            print(f'  date range: {dates[0].isoformat()} → {dates[-1].isoformat()} (from chart labels)', flush=True)
        else:
            dates = assign_trading_dates(n, date.today())
    else:
        dates = assign_trading_dates(n, date.today())
        print(f'  date range: {dates[0].isoformat()} → {dates[-1].isoformat()} (computed)', flush=True)

    # Trim trailing duplicates (Farside pads data with current-day placeholder AUM)
    # If the last data point has the same AUM as the previous (and same Total), it's padding
    if n > 1:
        # Use the first etf as proxy
        proxy_etf = etfs[0]
        aum_proxy = series_data[proxy_etf]
        while n > 1 and aum_proxy[n-1] == aum_proxy[n-2]:
            n -= 1
        print(f'  trimmed trailing duplicates → {n} real data points', flush=True)
        dates = dates[:n]
        # Truncate series_data to n
        for k in series_data:
            series_data[k] = series_data[k][:n]

    flow_rows, aum_rows = compute_rows(series_data, dates, etfs)
    return {
        'source': cfg['url'],
        'scrapedAt': datetime.utcnow().isoformat() + 'Z',
        'inception': cfg['inception'].isoformat(),
        'lastUpdated': dates[-1].isoformat(),
        'etfs': etfs,
        'rows': flow_rows,
        'aum': aum_rows,
    }


async def main():
    for asset, cfg in SOURCES.items():
        result = await scrape_asset(asset, cfg)
        if not result:
            continue
        out_path = OUT_DIR / f'{asset}.json'
        with open(out_path, 'w') as f:
            json.dump(result, f, indent=2)
        size_kb = out_path.stat().st_size / 1024
        print(f'  -> {out_path} ({size_kb:.1f} KB, {len(result["rows"])} rows)', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
