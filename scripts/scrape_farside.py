"""
Scrape Farside Investors ETF flow tables for BTC, ETH, HYPE.
Output: etf-data/{btc,eth,hyp}.json in repo root.

Uses Playwright (headless Chromium) to bypass Cloudflare JS challenge.
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

ASSETS = {
    "btc": "https://farside.co.uk/btc/",
    "eth": "https://farside.co.uk/eth/",
    "hyp": "https://farside.co.uk/hyp/",
}

OUT_DIR = Path(__file__).resolve().parent.parent / "etf-data"


# ---------- HTML parsing ----------

def clean_cell(html: str) -> str:
    """Strip HTML tags and whitespace; return plain text."""
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def row_cells(tr_html: str) -> list[str]:
    cs = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr_html, re.DOTALL)
    return [clean_cell(c) for c in cs]


def parse_value(s: str) -> float | None:
    s = (s or "").strip()
    if not s or s == "-":
        return None
    s = s.replace(",", "").replace(" ", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def parse_date(s: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), "%d %b %Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


DATE_RE = re.compile(r"^\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}$")


def extract_data_table(html: str) -> tuple[list[str], list[list[str]]]:
    """
    Find the Farside data table and return (etf_headers, data_rows).
    Table structure:
      row 0: [' '..., 'Total']           (often many NBSP cells + 'Total' at end)
      row 1: ['', 'IBIT', 'FBTC', ..., 'BTC', '']   (ETF tickers + leading '' + trailing '')
      row 2: ['Fee', '0.25%', ...]        (fee row, skipped)
      row 3+: data rows                  (Date, val1, val2, ..., valN, Total)
    """
    tables = re.findall(r"<table[^>]*>.*?</table>", html, re.DOTALL)
    chosen = None
    for t in tables:
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.DOTALL)
        if len(trs) < 5:
            continue
        first_cells = row_cells(trs[0])
        # Must have "Total" as last cell of first row
        if first_cells and first_cells[-1].lower() == "total" and len(first_cells) >= 3:
            chosen = t
            break
    if chosen is None:
        # Fallback: largest table
        chosen = max(tables, key=len) if tables else ""

    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", chosen, re.DOTALL)

    # ETF header row is the row right after the "Total" header row
    etf_headers: list[str] = []
    for r_i, tr in enumerate(trs):
        cs = row_cells(tr)
        if cs and cs[-1].lower() == "total":
            # Look at next non-empty row
            for nxt in trs[r_i + 1:]:
                nxt_cs = row_cells(nxt)
                if not nxt_cs:
                    continue
                # Skip if it looks like a fee row
                if nxt_cs[0].lower() in ("fee", "fund"):
                    continue
                # If middle cells have ticker-like text, use it
                if any(re.match(r"^[A-Z]{2,6}$", c) for c in nxt_cs[1:-1]):
                    etf_headers = nxt_cs
                    break
                # Otherwise, fall through to fee row check
                if nxt_cs[0].lower() in ("fee", "fund"):
                    continue
                # Generic fallback
                etf_headers = nxt_cs
                break
            break

    # Data rows: ones that start with a date
    data_rows: list[list[str]] = []
    for tr in trs:
        cs = row_cells(tr)
        if not cs:
            continue
        if cs[0] in ("Total", "Average", "Maximum", "Minimum", "Fee", "Fund"):
            continue
        if DATE_RE.match(cs[0]):
            data_rows.append(cs)
    return etf_headers, data_rows


def build_record(etf_headers: list[str], row: list[str]) -> dict | None:
    """Build a JSON record from one data row."""
    date_iso = parse_date(row[0])
    if not date_iso:
        return None
    # Layout: [Date, v1, v2, ..., vN, Total]
    # ETF columns: positions 1..-(1+1) i.e. 1..-2
    n_total_cells = len(row)
    if n_total_cells < 2:
        return None
    total = parse_value(row[-1])
    values = {}
    if etf_headers and len(etf_headers) >= 2:
        # etf_headers = ['', 'IBIT', 'FBTC', ..., 'BTC', '']
        # middle is positions 1..-2
        etf_names = [h for h in etf_headers[1:-1] if h]
        n_etfs = len(etf_names)
        if n_etfs > 0 and n_total_cells >= n_etfs + 2:
            for i, name in enumerate(etf_names):
                cell = row[1 + i] if (1 + i) < n_total_cells - 1 else None
                values[name] = parse_value(cell) if cell is not None else None
        else:
            # Fallback: assign by position
            n_vals = max(0, n_total_cells - 2)
            for i in range(n_vals):
                name = etf_names[i] if i < len(etf_names) else f"col_{i+1}"
                values[name] = parse_value(row[1 + i]) if (1 + i) < n_total_cells else None
    else:
        n_vals = max(0, n_total_cells - 2)
        for i in range(n_vals):
            values[f"col_{i+1}"] = parse_value(row[1 + i]) if (1 + i) < n_total_cells else None
    return {"date": date_iso, "values": values, "total": total}


# ---------- Playwright scraping ----------

async def bypass_cloudflare(page, url: str, max_attempts: int = 4) -> None:
    """Navigate to URL; if Cloudflare challenge appears, wait and retry."""
    for attempt in range(1, max_attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            print(f"  navigation timeout (attempt {attempt})", flush=True)
        # Check title for challenge
        title = await page.title()
        if "Just a moment" not in title and "Attention Required" not in title:
            # Maybe still loading — wait for table
            try:
                await page.wait_for_selector("table", timeout=20000)
                # Additional settle time for JS-rendered content
                await page.wait_for_timeout(2000)
                # Re-check title after settle
                title = await page.title()
                if "Just a moment" not in title:
                    return
            except PWTimeout:
                pass
        print(f"  Cloudflare challenge detected (attempt {attempt}/{max_attempts}); waiting 8s", flush=True)
        await page.wait_for_timeout(8000)
    raise RuntimeError(f"Could not bypass Cloudflare after {max_attempts} attempts")


async def scrape_one(browser, asset: str, url: str) -> dict:
    # Fresh context per asset to avoid challenge carryover
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1440, "height": 900},
    )
    page = await ctx.new_page()
    try:
        print(f"[{asset}] navigating to {url}", flush=True)
        await bypass_cloudflare(page, url)
        title = await page.title()
        print(f"[{asset}] title: {title}", flush=True)
        html = await page.content()

        etf_headers, raw_rows = extract_data_table(html)
        # Filter and rename empty header names
        etf_names = [h for h in etf_headers[1:-1] if h] if len(etf_headers) >= 2 else []
        print(f"[{asset}] etfs: {etf_names}", flush=True)
        records = []
        for row in raw_rows:
            rec = build_record(etf_headers, row)
            if rec:
                records.append(rec)
        print(f"[{asset}] {len(records)} data rows", flush=True)
        return {
            "asset": asset,
            "title": title,
            "source": url,
            "scrapedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "etfs": etf_names,
            "rows": records,
        }
    finally:
        await ctx.close()


async def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            for asset, url in ASSETS.items():
                try:
                    data = await scrape_one(browser, asset, url)
                    out_file = OUT_DIR / f"{asset}.json"
                    with open(out_file, "w") as f:
                        json.dump(data, f, indent=2)
                    print(f"[{asset}] wrote {out_file} ({len(data['rows'])} rows)\n", flush=True)
                except Exception as e:
                    print(f"[{asset}] ERROR: {e}", flush=True)
                    sys.exit(1)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
