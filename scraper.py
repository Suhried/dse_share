#!/usr/bin/env python3
"""
DSE Portfolio + Top Shares Tracker — JSON output for GitHub Actions
Outputs: docs/output.json
"""

import datetime as dt
import json
import ssl
import sys
import time
import warnings
from io import StringIO
from urllib.request import Request, urlopen

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

try:
    import requests
    from requests.adapters import HTTPAdapter
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pip install pandas lxml")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
PORTFOLIO = {
    "ACTIVEFINE": {"quantity": 14000, "total_cost": 98392.00},
    "ALLTEX":     {"quantity":  5000, "total_cost": 100436.14},
    "LEGACYFOOT": {"quantity":  2793, "total_cost": 214663.93},
}

# ---------------------------------------------------------------------------
# SSL / Session
# ---------------------------------------------------------------------------
SSL_CTX = ssl._create_unverified_context()

class _NoVerifyAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

SESSION = requests.Session()
SESSION.verify = False
SESSION.mount("https://", _NoVerifyAdapter())
SESSION.mount("http://",  HTTPAdapter())
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_float(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        return 0.0

def normalize_col(c):
    return str(c).replace("\n", " ").replace("\xa0", " ").strip().upper()

def parse_price(v):
    if v is None:
        return None
    t = str(v).replace(",", "").replace("\xa0", " ").strip()
    if t in ("", "-", "N/A", "NA", "NAN", "NONE", "NULL"):
        return None
    try:
        n = float(t)
        return n if n > 0 else None
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# DSE Market Table
# ---------------------------------------------------------------------------
def download_dse_prices():
    url = "https://dse.com.bd/latest_share_price_scroll_l.php"
    headers = {
        "User-Agent": SESSION.headers["User-Agent"],
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.dse.com.bd/",
    }
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20, context=SSL_CTX) as resp:
            return resp.read()
    except Exception as e:
        print(f"Could not connect to DSE: {e}")
        return None

def load_market_table():
    raw = download_dse_prices()
    if raw is None:
        return None
    html = raw.decode("utf-8", errors="ignore")
    try:
        tables = pd.read_html(StringIO(html))
    except Exception as e:
        print(f"Could not parse DSE HTML: {e}")
        return None
    for tbl in tables:
        if tbl.empty:
            continue
        tbl.columns = [normalize_col(c) for c in tbl.columns]
        if "TRADING CODE" in tbl.columns:
            return tbl
    return None

def get_stock_data(table, symbol):
    matches = table[
        table["TRADING CODE"].astype(str).str.strip().str.upper() == symbol.upper()
    ]
    if matches.empty:
        return {}
    row = matches.iloc[0]
    return {
        "ltp":   parse_price(row.get("LTP*")),
        "close": parse_price(row.get("CLOSEP*")),
    }

# ---------------------------------------------------------------------------
# Portfolio JSON
# ---------------------------------------------------------------------------
def build_portfolio_json():
    table = load_market_table()
    stocks = []
    total_cost = 0.0
    total_value = 0.0
    all_ok = True

    for sym, h in PORTFOLIO.items():
        qty  = h["quantity"]
        cost = h["total_cost"]
        avg  = cost / qty
        total_cost += cost

        data  = {}
        ltp   = None
        close = None
        price = None

        if table is not None:
            data  = get_stock_data(table, sym)
            ltp   = data.get("ltp")
            close = data.get("close")
            price = ltp if ltp is not None else close

        if price is None:
            all_ok = False
            stocks.append({
                "symbol": sym, "quantity": qty, "avg_cost": round(avg, 2),
                "ltp": None, "close": None, "current_value": None,
                "pl": None, "return_pct": None,
            })
        else:
            cur_val = qty * price
            pl      = cur_val - cost
            ret     = pl / cost * 100
            total_value += cur_val
            stocks.append({
                "symbol": sym, "quantity": qty, "avg_cost": round(avg, 2),
                "ltp": ltp, "close": close,
                "current_value": round(cur_val, 2),
                "pl": round(pl, 2),
                "return_pct": round(ret, 2),
            })

    summary = None
    if all_ok and total_value > 0:
        pl  = total_value - total_cost
        ret = pl / total_cost * 100
        summary = {
            "total_cost":  round(total_cost, 2),
            "total_value": round(total_value, 2),
            "pl":          round(pl, 2),
            "return_pct":  round(ret, 2),
        }

    return {"stocks": stocks, "summary": summary}

# ---------------------------------------------------------------------------
# BullBD Top Movers
# ---------------------------------------------------------------------------
BULLBD_BASE     = "https://m.bullbd.com"
REQUEST_TIMEOUT = 15
MIN_TRADING_DAYS = 3

def _get(url):
    for attempt in range(1, 4):
        try:
            r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            if attempt == 3:
                raise RuntimeError(f"Failed [{url}]: {exc}") from exc
            time.sleep(attempt * 0.5)

def _parse_table(html):
    soup = BeautifulSoup(html, "html.parser")
    tbl  = soup.find("table")
    if not tbl:
        return []
    headers, rows = [], []
    for tr in tbl.find_all("tr"):
        ths = tr.find_all("th")
        tds = tr.find_all("td")
        if ths and not tds:
            headers = [th.get_text(strip=True).lower() for th in ths]
            continue
        cells = [td.get_text(strip=True) for td in tds]
        if cells and headers and len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows

def fetch_top_movers(limit=20, losers=False):
    path = "/looser.php?tab=change_per" if losers else "/gainer.php?tab=change_per"
    html = _get(BULLBD_BASE + path)
    rows = _parse_table(html)
    if not rows:
        return []
    result = []
    for row in rows:
        symbol = (row.get("code") or "").strip().upper()
        ltp    = safe_float(row.get("ltp", 0))
        chg    = safe_float(row.get("chg%", 0) or row.get("chg %", 0))
        if symbol:
            result.append({"symbol": symbol, "ltp": ltp, "change_pct": chg})
    result.sort(key=lambda r: r["change_pct"], reverse=not losers)
    return result[:limit]

def fetch_7day_history(symbol):
    url = f"{BULLBD_BASE}/company_details.php?cmp={symbol}&tab=last"
    try:
        html = _get(url)
    except RuntimeError:
        return None
    rows = _parse_table(html)
    if not rows:
        return None
    rows = list(reversed(rows))
    dates, prices = [], []
    for row in rows:
        price = safe_float(row.get("close", "0"))
        if price <= 0:
            continue
        raw = row.get("date", "").strip()
        try:
            label = dt.datetime.strptime(raw, "%d %b %Y").strftime("%d-%b")
        except ValueError:
            label = raw[:6]
        dates.append(label)
        prices.append(price)
    if len(prices) < MIN_TRADING_DAYS:
        return None
    prices = prices[-7:]
    dates  = dates[-len(prices):]
    return dates, prices

def build_movers_json(limit=20, losers=False):
    try:
        movers = fetch_top_movers(limit=limit, losers=losers)
    except Exception as e:
        print(f"Failed to fetch movers: {e}")
        return []

    result = []
    for m in movers:
        sym = m["symbol"]
        print(f"  Fetching history: {sym}")
        history = fetch_7day_history(sym)
        time.sleep(0.4)
        if history is None:
            continue
        dates, prices = history
        seven_d_ret = (prices[-1] - prices[0]) / prices[0] * 100 if len(prices) >= 2 else 0.0
        result.append({
            "symbol":     sym,
            "price":      m["ltp"] if m["ltp"] > 0 else prices[-1],
            "today_pct":  round(m["change_pct"], 2),
            "seven_d_pct": round(seven_d_ret, 2),
            "dates":      dates,
            "prices":     prices,
        })
    result.sort(key=lambda r: r["seven_d_pct"], reverse=not losers)
    return result

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    now = dt.datetime.utcnow()
    # Bangladesh is UTC+6
    bd_now = now + dt.timedelta(hours=6)

    print("Building portfolio data...")
    portfolio = build_portfolio_json()

    print("Building top gainers...")
    gainers = build_movers_json(limit=20, losers=False)

    print("Building top losers...")
    losers = build_movers_json(limit=20, losers=True)

    output = {
        "generated_at":    now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_bd": bd_now.strftime("%d %b %Y, %I:%M %p"),
        "portfolio":       portfolio,
        "gainers":         gainers,
        "losers":          losers,
    }

    import os
    os.makedirs("docs", exist_ok=True)
    with open("docs/output.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"✓ docs/output.json written at {bd_now.strftime('%d %b %Y %I:%M %p')} BD time")

if __name__ == "__main__":
    main()
