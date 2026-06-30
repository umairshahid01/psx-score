"""
scraper.py
==========
Pulls the raw material the scorer needs for one company, live from public PSX
pages — no API key, no login.

scrape_company("OGDC") -> {
    "symbol", "profile": {...},
    "financials": [ {year, revenue, net_profit, eps, ...}, ... ],  # ascending
    "price_history": [ {date, close}, ... ],
    "reports": [ {title, url, year}, ... ],
    "warnings": [...], "data_quality": 0..1, "scraped_at": "...Z"
}
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

import config
import utils

try:
    import pdfplumber
except Exception:  # noqa: BLE001
    pdfplumber = None

# ---------------------------------------------------------------------------
# Line-item dictionaries
# ---------------------------------------------------------------------------

LINE_ITEMS = {
    "revenue": [
        r"net sales", r"net revenue", r"\bturnover\b", r"\brevenue\b",
        r"\bsales\b", r"markup.*interest earned", r"interest earned",
        r"total income", r"total revenue", r"gross revenue",
    ],
    "gross_profit": [r"gross profit"],
    "operating_profit": [r"operating profit", r"profit from operations", r"operating income"],
    "net_profit": [
        r"profit after tax", r"profit for the year", r"profit attributable",
        r"net profit", r"profit/\(loss\) after tax", r"profit / \(loss\) for the year",
        r"net income", r"profit after taxation",
    ],
    "eps": [
        r"earnings per share", r"\beps\b", r"basic.*per share",
        r"basic earnings per share", r"diluted earnings per share",
    ],
    "total_assets": [r"total assets"],
    "total_equity": [
        r"total equity", r"shareholders.{0,3} equity", r"share holders.{0,3} equity",
        r"equity attributable", r"total shareholders", r"net assets",
    ],
    "total_liabilities": [r"total liabilities", r"total liabilities and equity"],
    "current_assets": [r"total current assets", r"current assets"],
    "current_liabilities": [r"total current liabilities", r"current liabilities"],
    "total_debt": [
        r"long.?term financing", r"long.?term debt", r"\bborrowings\b",
        r"total debt", r"lease liabilities", r"long term borrowings",
        r"short term borrowings", r"short.?term financing",
    ],
    "operating_cashflow": [
        r"cash generated from operations",
        r"net cash (generated )?from operating activities",
        r"cash flows? from operating activities",
        r"operating activities",
    ],
    "dividend_per_share": [
        r"dividend per share", r"cash dividend", r"interim dividend",
        r"final dividend", r"\bdps\b", r"total dividend",
        r"dividend declared", r"proposed dividend", r"dividend.*\bper\b",
        r"dividend.*\bshare\b", r"payout per share",
    ],
    "shares_outstanding": [
        r"number of shares", r"shares outstanding", r"paid.?up.*shares",
        r"ordinary shares.*issued", r"issued.*shares",
    ],
    # banking-specific
    "net_interest_income": [r"net (markup|interest) income", r"net markup"],
    "capital_adequacy": [r"capital adequacy ratio", r"\bcar\b"],
}

_YEAR_RE = re.compile(r"(?:FY)?\s*'?(\d{2}|\d{4})\b")

NO_SCALE_FIELDS = {"eps", "dividend_per_share", "capital_adequacy", "shares_outstanding"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _match_line_item(label: str) -> Optional[str]:
    low = label.lower()
    for field, patterns in LINE_ITEMS.items():
        for pat in patterns:
            if re.search(pat, low):
                return field
    return None


def _harvest_label_value_pairs(soup) -> List[tuple]:
    pairs: List[tuple] = []
    for stat in soup.find_all(class_=re.compile("stats|quote|summary|data", re.I)):
        labels = stat.find_all(class_=re.compile("name|label|title|key", re.I))
        values = stat.find_all(class_=re.compile("value|val|amount|number|data", re.I))
        for lab, val in zip(labels, values):
            pairs.append((_text(lab), _text(val)))
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            pairs.append((_text(cells[0]), _text(cells[1])))
    return pairs

# ---------------------------------------------------------------------------
# Live price scraping — multiple strategies
# ---------------------------------------------------------------------------

def _scrape_live_price(symbol: str, session) -> Optional[float]:
    """Try several PSX endpoints to get a real-time price."""
    candidate_urls = [
        f"https://dps.psx.com.pk/quotes/{symbol}",
        f"https://dps.psx.com.pk/api/companies/{symbol}/quote",
        f"https://dps.psx.com.pk/symbol/{symbol}",
        config.PSX_COMPANY_URL.format(symbol=symbol),
    ]

    for url in candidate_urls:
        try:
            raw = utils.fetch(url, session=session)
            if not raw:
                continue

            import json as _json
            try:
                data = _json.loads(raw)
                for key in ("currentPrice", "current", "ldcp", "last", "close",
                             "lastTradePrice", "price", "ltp"):
                    if isinstance(data, dict) and key in data:
                        val = utils.to_number(data[key])
                        if val and val > 0:
                            return val
                if isinstance(data, dict) and "data" in data:
                    inner = data["data"]
                    if isinstance(inner, dict):
                        for key in ("currentPrice", "current", "ldcp", "last", "close", "price"):
                            if key in inner:
                                val = utils.to_number(inner[key])
                                if val and val > 0:
                                    return val
            except Exception:
                pass

            soup = BeautifulSoup(raw, "lxml")
            price_patterns = [
                r"current.*price", r"last.*price", r"^price$",
                r"ldcp", r"last.*trade", r"ltp", r"^last$",
            ]
            pairs = _harvest_label_value_pairs(soup)
            for label, value in pairs:
                if any(re.search(p, label, re.I) for p in price_patterns):
                    num = utils.to_number(value)
                    if num and num > 0:
                        return num

            for el in soup.find_all(class_=re.compile(r"price|ltp|ldcp|last", re.I)):
                txt = _text(el)
                num = utils.to_number(txt)
                if num and num > 0:
                    return num

        except Exception:
            continue

    return None


# ---------------------------------------------------------------------------
# Company profile
# ---------------------------------------------------------------------------

def scrape_profile(symbol: str, session) -> Dict:
    url = config.PSX_COMPANY_URL.format(symbol=symbol)
    html = utils.fetch(url, session=session)
    profile: Dict = {"symbol": symbol, "source_url": url}

    if not html:
        profile["_unavailable"] = True
        return profile

    soup = BeautifulSoup(html, "lxml")

    name = _text(soup.find(class_=re.compile("company.*name", re.I))) or _text(soup.find("h1"))
    if name:
        profile["name"] = name

    sector = _text(soup.find(class_=re.compile("sector", re.I)))
    if sector:
        profile["sector"] = sector.upper()

    wanted = {
        "price": [r"^last$", r"current", r"^price$", r"ldcp", r"ltp", r"last.*trade"],
        "change_pct": [r"change.*%", r"%.*change", r"chg"],
        "market_cap": [r"market cap"],
        "pe": [r"p/e", r"price.?to.?earnings"],
        "pb": [r"p/b", r"price.?to.?book"],
        "dividend_yield": [r"dividend yield", r"div yield"],
        "eps_ttm": [r"\beps\b"],
        "shares": [r"shares", r"free float", r"outstanding"],
        "week52_high": [r"52.*high", r"high.*52"],
        "week52_low": [r"52.*low", r"low.*52"],
        "volume": [r"^volume$", r"\bvol\b"],
        # v2: extract all available financial ratios from the profile section
        "book_value": [r"book\s*value"],
        "current_ratio_profile": [r"current\s*ratio"],
        "debt_to_equity_profile": [r"debt.?to.?equity", r"long\s*term\s*debt\s*to\s*equity",
                                   r"d/?e\s*ratio"],
        "roe_profile": [r"return\s*on\s*equity", r"\broe\b"],
        "net_margin_profile": [r"net\s*profit\s*margin", r"net\s*margin"],
        "gross_margin_profile": [r"gross\s*profit\s*margin", r"gross\s*margin"],
        "cash_payout_profile": [r"cash\s*payout", r"payout\s*ratio"],
        "interest_cover_profile": [r"interest\s*cover"],
        "equity_to_assets_profile": [r"equity\s*to\s*assets"],
    }

    pairs = _harvest_label_value_pairs(soup)
    for field, patterns in wanted.items():
        for label, value in pairs:
            if any(re.search(p, label, re.I) for p in patterns):
                num = utils.to_number(value)
                if num is not None:
                    profile[field] = num
                    break

    # --- market_cap sanity check ---
    # PSX often shows market cap in millions without a suffix. If we have
    # both price and shares, derive the correct value. Otherwise, try common
    # scale factors (×1e6 for millions).
    px = profile.get("price")
    sh = profile.get("shares")
    if px and sh and px > 0 and sh > 0:
        profile["market_cap"] = px * sh                 # authoritative
    elif profile.get("market_cap"):
        mc = profile["market_cap"]
        # if the raw value looks oddly small, PSX likely shows it "in millions"
        if mc < 1e9 and px and px > 0:
            for scale in [1e9, 1e6, 1e3]:
                candidate = mc * scale
                if candidate > 1e10:
                    profile["market_cap"] = candidate
                    break

    return profile


# ---------------------------------------------------------------------------
# Financial tables
# ---------------------------------------------------------------------------

def scrape_financial_tables(symbol: str, session) -> List[Dict]:
    url = config.PSX_COMPANY_URL.format(symbol=symbol)
    html = utils.fetch(url, session=session)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    by_year: Dict[int, Dict] = {}

    for table in soup.find_all("table"):
        years = _detect_year_columns(table)
        if not years:
            continue
        scale_hint = _detect_scale_hint(table)

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = _text(cells[0])
            field = _match_line_item(label)
            if not field:
                continue
            values = [_text(c) for c in cells[1:]]
            hint = "" if field in NO_SCALE_FIELDS else scale_hint

            for col_idx, year in years.items():
                if col_idx - 1 < len(values):
                    num = utils.to_number(values[col_idx - 1], hint)
                    if num is not None:
                        rec = by_year.setdefault(year, {"year": year, "_sources": {}})
                        if field not in rec:
                            rec[field] = num
                            rec["_sources"][field] = f"PSX table FY{year}"

    records = [by_year[y] for y in sorted(by_year)]

    for rec in records:
        _gap_fill_record(rec)

    return records


def _gap_fill_record(rec: Dict, profile: Dict = None) -> None:
    """
    Fill every missing field using accounting identities and — when available —
    real financial ratios scraped from the PSX profile section. Falls back to
    conservative industry proxies only as a last resort.
    """
    yr = rec.get("year", "?")
    src = rec.setdefault("_sources", {})
    p = profile or {}

    def _set(field, value, reason):
        if rec.get(field) is None and value is not None:
            rec[field] = value
            src[field] = reason

    # ---- balance sheet identities ----
    _set("total_liabilities",
         _sub(rec.get("total_assets"), rec.get("total_equity")),
         f"derived: assets − equity (FY{yr})")
    _set("total_equity",
         _sub(rec.get("total_assets"), rec.get("total_liabilities")),
         f"derived: assets − liabilities (FY{yr})")
    _set("total_assets",
         _add(rec.get("total_equity"), rec.get("total_liabilities")),
         f"derived: equity + liabilities (FY{yr})")

    # ---- total_equity from book_value × shares ----
    if rec.get("total_equity") is None:
        bv = p.get("book_value")
        sh = p.get("shares")
        if bv and sh and bv > 0 and sh > 0:
            _set("total_equity", bv * sh,
                 f"derived: book value Rs{bv:.1f} × {sh:.0f} shares")

    # ---- total_debt: from profile D/E ratio × equity, else proxy ----
    if rec.get("total_debt") is None:
        de = p.get("debt_to_equity_profile")
        eq = rec.get("total_equity")
        if de is not None and eq:
            _set("total_debt", de * eq,
                 f"derived: D/E {de:.2f} × equity (profile ratio, FY{yr})")
        elif rec.get("total_liabilities") is not None:
            _set("total_debt", rec["total_liabilities"] * 0.7,
                 f"estimated ~70% of total liabilities as debt (FY{yr})")

    # ---- current assets / liabilities: from profile current ratio, else proxy ----
    ta = rec.get("total_assets")
    tl = rec.get("total_liabilities")
    cr_profile = p.get("current_ratio_profile")
    if rec.get("current_assets") is None and ta is not None:
        pct_ca = 0.40  # default
        if cr_profile and cr_profile > 0 and tl:
            # estimate current_liabilities first, then current_assets
            est_cl = tl * 0.55
            est_ca = cr_profile * est_cl
            if est_ca <= ta:
                _set("current_assets", est_ca,
                     f"derived: CR {cr_profile:.2f} × est. CL (profile ratio, FY{yr})")
                _set("current_liabilities", est_cl,
                     f"estimated ~55% of total liabilities (FY{yr})")
            else:
                _set("current_assets", ta * pct_ca,
                     f"estimated ~40% of total assets (FY{yr})")
        else:
            _set("current_assets", ta * pct_ca,
                 f"estimated ~40% of total assets (FY{yr})")
    if rec.get("current_liabilities") is None and tl is not None:
        _set("current_liabilities", tl * 0.55,
             f"estimated ~55% of total liabilities (FY{yr})")

    # ---- operating cashflow: estimate from net_profit when missing ----
    if rec.get("operating_cashflow") is None and rec.get("net_profit") is not None:
        _set("operating_cashflow", rec["net_profit"] * 0.90,
             f"estimated ~90% of net profit (FY{yr}, no CF data)")

    # ---- EPS: derive from net_profit and shares_outstanding if available ----
    if rec.get("eps") is None:
        np_ = rec.get("net_profit")
        sh  = rec.get("shares_outstanding") or p.get("shares")
        if np_ is not None and sh is not None and sh > 0:
            _set("eps", np_ / sh,
                 f"derived: net profit / shares (FY{yr})")

    # ---- dividend: do NOT default to 0 here ----
    # The dedicated scrape_dividends() pass fills this in.
    # Setting 0.0 blindly would incorrectly mark dividend-paying
    # companies as non-payers when the table parser just missed the row.


def _sub(a, b):
    if a is not None and b is not None:
        return a - b
    return None


def _add(a, b):
    if a is not None and b is not None:
        return a + b
    return None


def _detect_year_columns(table) -> Dict[int, int]:
    head = table.find("tr")
    if not head:
        return {}
    cells = head.find_all(["th", "td"])
    out: Dict[int, int] = {}
    for idx, cell in enumerate(cells):
        if idx == 0:
            continue
        m = _YEAR_RE.search(_text(cell))
        if m:
            yr = int(m.group(1))
            if yr < 100:
                yr += 2000
            if 1990 <= yr <= datetime.now().year + 1:
                out[idx] = yr
    return out


def _detect_scale_hint(table) -> str:
    blob = _text(table.find("caption") or "") + " " + _text(table.find("thead") or "")
    cap = table.find_previous(string=re.compile(r"rupees in|amounts in|rs\.? in", re.I))
    if cap:
        blob += " " + str(cap)
    return blob


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

def scrape_price_history(symbol: str, session) -> List[Dict]:
    """EOD close history, always sorted ascending (oldest → newest)."""
    url = config.PSX_TIMESERIES.format(symbol=symbol)
    data = utils.fetch(url, session=session, as_json=True)
    out: List[Dict] = []

    if isinstance(data, dict) and "data" in data:
        rows = data["data"]
    elif isinstance(data, list):
        rows = data
    else:
        return out

    for row in rows:
        try:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                ts, close = row[0], row[1]
                date = datetime.utcfromtimestamp(int(ts)).date().isoformat() \
                    if str(ts).isdigit() else str(ts)
                out.append({"date": date, "close": float(close)})
            elif isinstance(row, dict):
                out.append({
                    "date": str(row.get("date") or row.get("time")),
                    "close": float(row.get("close") or row.get("price")),
                })
        except Exception:
            continue

    seen = set()
    cleaned: List[Dict] = []
    for pt in out:
        d = pt.get("date")
        c = pt.get("close")
        if not d or c is None or c <= 0 or d in seen:
            continue
        seen.add(d)
        cleaned.append(pt)

    cleaned.sort(key=lambda r: r["date"])
    return cleaned


# ---------------------------------------------------------------------------
# Annual-report PDFs (gap-fill)
# ---------------------------------------------------------------------------

def find_report_links(symbol: str, session) -> List[Dict]:
    url = config.PSX_COMPANY_URL.format(symbol=symbol)
    html = utils.fetch(url, session=session)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    reports: List[Dict] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = _text(a)
        if not href.lower().endswith(".pdf"):
            continue
        if not re.search(r"annual|financial|account|report|quarter", (href + " " + title), re.I):
            continue
        full = href if href.startswith("http") else f"{config.PSX_BASE}{href}"
        if full in seen:
            continue
        seen.add(full)
        ym = _YEAR_RE.search(title) or _YEAR_RE.search(href)
        year = None
        if ym:
            year = int(ym.group(1))
            year = year + 2000 if year < 100 else year
        reports.append({"title": title or "Report", "url": full, "year": year})

    reports.sort(key=lambda r: (r["year"] or 0), reverse=True)
    return reports


def parse_report_pdf(pdf_url: str, session) -> Dict:
    if pdfplumber is None:
        return {}
    raw = utils.fetch(pdf_url, session=session, expect_binary=True)
    if not raw:
        return {}
    found: Dict[str, float] = {}
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if not text:
                    continue
                for line in text.splitlines():
                    field = _match_line_item(line)
                    if not field or field in found:
                        continue
                    nums = re.findall(r"-?\(?\d[\d,]*\.?\d*\)?", line)
                    for token in nums:
                        val = utils.to_number(token)
                        if val is not None and abs(val) > 0:
                            found[field] = val
                            break
                if len(found) >= len(LINE_ITEMS):
                    break
    except Exception as exc:
        print(f" [pdf] could not parse {pdf_url}: {exc}")
    return found


# ---------------------------------------------------------------------------
# Secondary data source: StockAnalysis.com (S&P Global data)
# ---------------------------------------------------------------------------
# PSX's own page only has income-statement items and 3 ratios.
# Balance sheet, cash flow, dividends, ROE, D/E — all missing.
# StockAnalysis.com/quote/psx/{symbol}/statistics/ has everything.

_SA_URL = "https://stockanalysis.com/quote/psx/{symbol}/statistics/"
_SA_DIV_URL = "https://stockanalysis.com/quote/psx/{symbol}/dividend/"

# Map StockAnalysis labels → our internal field names
_SA_MAP = {
    # Balance sheet
    "equity (book value)":  "total_equity",
    "total debt":           "total_debt",
    "book value per share": "book_value",
    "cash & cash equivalents": "cash",
    "working capital":      "working_capital",
    # Cash flow
    "operating cash flow":  "operating_cashflow",
    "free cash flow":       "free_cashflow",
    "capital expenditures": "capex",
    # Income
    "revenue":              "revenue",
    "net income":           "net_profit",
    "earnings per share (eps)": "eps",
    # Ratios
    "return on equity (roe)": "roe_pct",
    "return on assets (roa)": "roa_pct",
    "profit margin":        "profit_margin_pct",
    "operating margin":     "operating_margin_pct",
    "pe ratio":             "pe",
    "pb ratio":             "pb",
    "current ratio":        "current_ratio",
    "debt / equity":        "debt_to_equity",
    # Dividend
    "dividend per share":   "dividend_per_share",
    "dividend yield":       "dividend_yield_pct",
    "payout ratio":         "payout_ratio_pct",
    "years of dividend growth": "dividend_growth_years",
    # Price
    "52-week price change": "price_change_52w",
    "beta (5y)":            "beta",
    # Shares
    "shares outstanding":   "shares",
}

_PCT_FIELDS = {"roe_pct","roa_pct","profit_margin_pct","operating_margin_pct",
               "dividend_yield_pct","payout_ratio_pct","price_change_52w"}


def scrape_stockanalysis(symbol: str, session) -> Dict:
    """Fetch comprehensive financial data from StockAnalysis.com.

    This is the authoritative fallback when PSX is unreachable. It logs
    loudly so failures are visible in the console, and it never raises —
    any error returns {} so the caller degrades gracefully.
    """
    url = _SA_URL.format(symbol=symbol)
    print(f"  [SA] fetching {url}")
    try:
        html = utils.fetch(url, session=session)
    except Exception as exc:  # noqa: BLE001
        print(f"  [SA] fetch raised for {symbol}: {exc}")
        return {}

    if not html:
        print(f"  [SA] no HTML returned for {symbol} (blocked / 404 / timeout)")
        return {}

    data: Dict = {}
    try:
        soup = BeautifulSoup(html, "lxml")

        # Parse all label-value table rows
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                label = _text(cells[0]).lower().strip()
                raw_val = _text(cells[-1])

                field = _SA_MAP.get(label)
                if not field:
                    continue

                # Handle percentage values
                if field in _PCT_FIELDS:
                    cleaned = raw_val.replace("%", "").replace("+", "").strip()
                    num = utils.to_number(cleaned)
                    if num is not None:
                        data[field] = num
                else:
                    num = utils.to_number(raw_val)
                    if num is not None:
                        data[field] = num

        # Also try harvesting from key-value pair divs (SA uses both)
        pairs = _harvest_label_value_pairs(soup)
        for label, raw_val in pairs:
            field = _SA_MAP.get(label.lower().strip())
            if not field or field in data:
                continue
            if field in _PCT_FIELDS:
                cleaned = raw_val.replace("%", "").replace("+", "").strip()
                num = utils.to_number(cleaned)
            else:
                num = utils.to_number(raw_val)
            if num is not None:
                data[field] = num
    except Exception as exc:  # noqa: BLE001
        print(f"  [SA] parse error for {symbol}: {exc}")
        return {}

    # --- Also fetch dividend history page for per-year totals ---
    div_url = _SA_DIV_URL.format(symbol=symbol)
    try:
        div_html = utils.fetch(div_url, session=session)
        if div_html:
            div_soup = BeautifulSoup(div_html, "lxml")
            yearly_divs: Dict[int, float] = {}
            for table in div_soup.find_all("table"):
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue
                    date_str = _text(cells[0])    # e.g. "Apr 28, 2026"
                    amount_str = _text(cells[1])  # e.g. "6.000 PKR"
                    yr_match = re.search(r"20\d{2}", date_str)
                    if not yr_match:
                        continue
                    year = int(yr_match.group())
                    amt_match = re.search(r"[\d,.]+", amount_str)
                    if not amt_match:
                        continue
                    try:
                        amt = float(amt_match.group().replace(",", ""))
                    except ValueError:
                        continue
                    if amt > 0:
                        yearly_divs[year] = yearly_divs.get(year, 0.0) + amt
            if yearly_divs:
                data["dividend_by_year"] = yearly_divs
    except Exception as exc:  # noqa: BLE001
        print(f"  [SA] dividend parse error for {symbol}: {exc}")

    if data:
        data["_source"] = "stockanalysis.com"
        n = len([k for k in data if not k.startswith("_")])
        print(f"  [SA] OK {symbol}: parsed {n} fields "
              f"(CR={data.get('current_ratio')}, ROE={data.get('roe_pct')}, "
              f"D/E={data.get('debt_to_equity')})")
    else:
        print(f"  [SA] {symbol}: page fetched but 0 fields parsed "
              f"(structure may have changed)")
    return data


def _merge_secondary_data(financials: List[Dict], profile: Dict,
                          sa_data: Dict, warnings: List[str]) -> None:
    """Merge StockAnalysis.com data into the financial records and profile."""
    if not sa_data:
        return

    # --- Update profile with better values ---
    for sa_key, profile_key in [
        ("roe_pct", "roe_profile"),
        ("current_ratio", "current_ratio_profile"),
        ("debt_to_equity", "debt_to_equity_profile"),
        ("book_value", "book_value"),
        ("dividend_yield_pct", "dividend_yield"),
        ("shares", "shares"),
        ("pe", "pe"),
        ("pb", "pb"),
        ("profit_margin_pct", "net_margin_profile"),
    ]:
        if sa_key in sa_data and not profile.get(profile_key):
            profile[profile_key] = sa_data[sa_key]

    # --- Market cap fallback: shares × price (when PSX didn't give cap) ---
    if not profile.get("market_cap"):
        shares = sa_data.get("shares")
        price = profile.get("price")
        if shares and price and shares > 0 and price > 0:
            profile["market_cap"] = shares * price
            profile["_market_cap_source"] = "shares × price (StockAnalysis)"

    # --- When PSX gave us NO financial records (e.g. 502 outage), build one
    #     from StockAnalysis so its real numbers aren't discarded. ---
    if not financials:
        sa_year = None
        # Try to infer the fiscal year from any SA field; else use current year
        from datetime import datetime as _dt
        sa_year = _dt.now().year
        synth = {"year": sa_year, "_sources": {}, "_synthesized": True}
        for sa_key, fin_key in [
            ("revenue", "revenue"),
            ("net_profit", "net_profit"),
            ("eps", "eps"),
            ("total_equity", "total_equity"),
            ("total_debt", "total_debt"),
            ("operating_cashflow", "operating_cashflow"),
        ]:
            if sa_data.get(sa_key) is not None:
                synth[fin_key] = sa_data[sa_key]
                synth["_sources"][fin_key] = "StockAnalysis (S&P Global)"
        # Only add the record if SA actually gave us something substantive
        if any(k in synth for k in
               ("revenue", "net_profit", "total_equity", "operating_cashflow")):
            financials.append(synth)
            warnings.append(
                "PSX financial tables unavailable — figures sourced from "
                "StockAnalysis.com (S&P Global).")

    if not financials:
        return

    latest = financials[-1]

    # --- Fill balance-sheet gaps in the latest financial record ---
    for sa_key, fin_key, source_label in [
        ("total_equity",     "total_equity",     "StockAnalysis (S&P Global)"),
        ("total_debt",       "total_debt",       "StockAnalysis (S&P Global)"),
        ("operating_cashflow","operating_cashflow","StockAnalysis (S&P Global)"),
        ("revenue",          "revenue",          "StockAnalysis (S&P Global)"),
        ("net_profit",       "net_profit",       "StockAnalysis (S&P Global)"),
        ("eps",              "eps",              "StockAnalysis (S&P Global)"),
    ]:
        if latest.get(fin_key) is None and sa_data.get(sa_key) is not None:
            latest[fin_key] = sa_data[sa_key]
            latest.setdefault("_sources", {})[fin_key] = source_label

    # --- Derive current_assets / current_liabilities from SA current_ratio ---
    cr = sa_data.get("current_ratio")
    if cr and cr > 0:
        tl = latest.get("total_liabilities")
        if tl and latest.get("current_liabilities") is None:
            est_cl = tl * 0.55
            latest["current_liabilities"] = est_cl
            latest["current_assets"] = cr * est_cl
            latest.setdefault("_sources", {})["current_assets"] = \
                f"derived from CR {cr:.2f} (StockAnalysis)"
            latest.setdefault("_sources", {})["current_liabilities"] = \
                f"estimated ~55% of liabilities"

    # --- Dividend: use SA per-year dividend history (the definitive fix) ---
    yearly_divs = sa_data.get("dividend_by_year", {})
    dps = sa_data.get("dividend_per_share")

    if yearly_divs:
        # We have actual per-year totals from SA's dividend history table
        for rec in financials:
            yr = rec.get("year")
            if not yr:
                continue
            if yr in yearly_divs:
                rec["dividend_per_share"] = yearly_divs[yr]
                rec.setdefault("_sources", {})["dividend_per_share"] = \
                    f"StockAnalysis dividend history Rs{yearly_divs[yr]:.2f} (FY{yr})"
            # Also check fiscal year offset: PSX companies with Jun FY end
            # may have dividends attributed to the calendar year of the ex-date
            elif yr - 1 in yearly_divs and rec.get("dividend_per_share") is None:
                rec["dividend_per_share"] = yearly_divs[yr - 1]
                rec.setdefault("_sources", {})["dividend_per_share"] = \
                    f"StockAnalysis dividend history Rs{yearly_divs[yr-1]:.2f} (CY{yr-1})"
    elif dps and dps > 0:
        # Fallback: use the latest annual DPS from statistics page
        yr = latest.get("year")
        if latest.get("dividend_per_share") is None or latest["dividend_per_share"] == 0.0:
            latest["dividend_per_share"] = dps
            latest.setdefault("_sources", {})["dividend_per_share"] = \
                f"StockAnalysis annual DPS Rs{dps:.2f}"
        # Back-fill older years if SA says dividends have been growing
        growth_yrs = sa_data.get("dividend_growth_years")
        if growth_yrs and growth_yrs > 0:
            for rec in financials:
                if rec.get("dividend_per_share") is None:
                    rec_yr = rec.get("year", 0)
                    if yr and rec_yr >= yr - growth_yrs:
                        rec["dividend_per_share"] = dps * 0.8
                        rec.setdefault("_sources", {})["dividend_per_share"] = \
                            f"est. from {growth_yrs}yr growth streak (StockAnalysis)"

    if sa_data:
        warnings.append("Balance-sheet and ratio data supplemented from StockAnalysis.com.")


# ---------------------------------------------------------------------------
# Dividend scraping — dedicated multi-strategy pass
# ---------------------------------------------------------------------------

_DIV_LABEL_RE = re.compile(
    r"cash\s*divid|interim\s*divid|final\s*divid|total\s*divid|divid.*per\s*share"
    r"|\bdps\b|payout\s*per\s*share|proposed\s*divid|dividend\s*declar",
    re.I,
)

_ANNOUNCEMENT_DIV_RE = re.compile(
    r"(?:cash\s+)?dividend.*?(?:rs\.?|pkr)\s*([\d,.]+)"
    r"|(?:rs\.?|pkr)\s*([\d,.]+)\s*(?:per\s*share\s*)?(?:cash\s+)?dividend"
    r"|(\d+)\s*%\s*(?:cash\s+)?dividend"          # "150% dividend" → 15 per share (face 10)
    r"|(?:cash\s+)?dividend.*?(\d+(?:\.\d+)?)\s*%",  # "dividend of 200%" → 20 per share
    re.I,
)


def scrape_dividends(symbol: str, session, profile: dict = None) -> Dict[int, float]:
    """
    Dedicated dividend scraper.  Tries multiple strategies:
      1. Scan announcement / corporate-action tables on the company page
      2. Scan financial statement tables (already done by the main parser, but
         do a focused pass looking only for dividend rows with relaxed matching)
      3. Derive from dividend yield + share price if both are in the profile
    Returns {fiscal_year: dividend_per_share_amount}.
    """
    url = config.PSX_COMPANY_URL.format(symbol=symbol)
    html = utils.fetch(url, session=session)
    divs: Dict[int, float] = {}

    if not html:
        return divs

    soup = BeautifulSoup(html, "lxml")
    current_year = datetime.now().year

    # --- Strategy 1: scan ALL tables for dividend-related rows ---------------
    for table in soup.find_all("table"):
        years = _detect_year_columns(table)
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = _text(cells[0]).lower()
            if not _DIV_LABEL_RE.search(label):
                continue
            values = [_text(c) for c in cells[1:]]
            for col_idx, year in years.items():
                if col_idx - 1 < len(values):
                    num = utils.to_number(values[col_idx - 1])
                    if num is not None and num >= 0:
                        # PSX sometimes shows dividends as % of face value (Rs 10)
                        # A DPS > 500 almost certainly is a percentage
                        if num > 500:
                            num = num / 100 * 10  # convert % to Rs per share (face=10)
                        divs.setdefault(year, 0.0)
                        divs[year] = max(divs[year], num)  # keep largest (total annual)

    # --- Strategy 2: scan announcement / payout blocks -----------------------
    # PSX pages sometimes have an announcements section with plain-text lines
    # like "Interim Cash Dividend Rs.5/- per share" or "200% Cash Dividend"
    for block in soup.find_all(
        True, string=re.compile(r"divid", re.I)
    ):
        text = _text(block) + " " + _text(block.parent)
        m = _ANNOUNCEMENT_DIV_RE.search(text)
        if not m:
            continue

        # Determine the amount
        amount = None
        if m.group(1):
            amount = utils.to_number(m.group(1))
        elif m.group(2):
            amount = utils.to_number(m.group(2))
        elif m.group(3):
            # Percentage of face value (face = Rs 10 for most PSX stocks)
            pct = utils.to_number(m.group(3))
            if pct is not None:
                amount = pct / 100 * 10
        elif m.group(4):
            pct = utils.to_number(m.group(4))
            if pct is not None:
                amount = pct / 100 * 10

        if amount is None or amount <= 0:
            continue

        # Try to associate with a year
        ym = _YEAR_RE.search(text)
        year = None
        if ym:
            year = int(ym.group(1))
            if year < 100:
                year += 2000
        if year is None or year < 1990 or year > current_year + 1:
            year = current_year  # best guess: current year

        divs.setdefault(year, 0.0)
        divs[year] += amount  # accumulate interim + final

    # --- Strategy 3: derive from dividend yield in the profile ---------------
    if profile and not divs:
        dy = profile.get("dividend_yield")
        px = profile.get("price")
        if dy and px and dy > 0 and px > 0:
            estimated_dps = round(px * dy / 100, 2)
            divs[current_year] = estimated_dps

    return divs


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def scrape_company(symbol: str, deep_pdf: bool = True) -> Dict:
    """Full scrape for one company. Always returns a dict; never raises."""
    symbol = symbol.strip().upper()
    session = utils.make_session()
    warnings: List[str] = []
    scraped_at = utils.now_iso()

    profile = scrape_profile(symbol, session)
    if profile.get("_unavailable"):
        warnings.append("Company page could not be reached on PSX.")

    financials = scrape_financial_tables(symbol, session)
    if not financials:
        warnings.append("Structured financial tables not found on the page.")

    price_history = scrape_price_history(symbol, session)

    # Live price: dedicated endpoint → company page → last close
    live_price = _scrape_live_price(symbol, session)
    if live_price and live_price > 0:
        profile["price"] = live_price
        profile["_price_source"] = "live scrape"
        profile["_price_as_of"] = scraped_at
    elif profile.get("price") and profile["price"] > 0:
        profile["_price_source"] = "company page"
        profile["_price_as_of"] = scraped_at
    elif price_history:
        last_pt = price_history[-1]
        profile["price"] = last_pt["close"]
        profile["_price_source"] = f"last close ({last_pt['date']})"
        profile["_price_as_of"] = last_pt["date"]
        warnings.append(f"Share price sourced from last available close ({last_pt['date']}).")
    else:
        profile["_price_source"] = "unavailable"
        warnings.append("Share price could not be retrieved.")

    if profile.get("change_pct") is None and len(price_history) >= 2:
        prev = price_history[-2]["close"]
        last = price_history[-1]["close"]
        if prev:
            profile["change_pct"] = round((last - prev) / prev * 100, 2)

    # PDF gap-fill when financials are missing or sparse
    reports = find_report_links(symbol, session)
    if deep_pdf and reports and (not financials or _too_sparse(financials[-1])):
        pdf_fields = parse_report_pdf(reports[0]["url"], session)
        if pdf_fields:
            year = reports[0].get("year") or datetime.now().year
            target = None
            for rec in financials:
                if rec.get("year") == year:
                    target = rec
                    break
            if target is None:
                target = {"year": year, "_sources": {}}
                financials.append(target)
                financials.sort(key=lambda r: r["year"])
            for k, v in pdf_fields.items():
                if target.get(k) is None:
                    target[k] = v
                    target.setdefault("_sources", {})[k] = \
                        f"annual report PDF ({reports[0].get('title', '')})"
            warnings.append("Some figures were filled from the annual-report PDF.")

    # --- Secondary source: StockAnalysis.com ---
    # PSX's page lacks balance sheet, cash flow, ROE, D/E, current ratio,
    # and dividend data. StockAnalysis.com (powered by S&P Global) has all of it.
    sa_data = scrape_stockanalysis(symbol, session)
    _merge_secondary_data(financials, profile, sa_data, warnings)

    # Final gap-fill pass on every record (with profile ratios)
    for rec in financials:
        _gap_fill_record(rec, profile=profile)

    # --- Dedicated dividend scraping pass ---
    # The main table parser often misses dividends because PSX shows them
    # in announcement sections, not inside financial-statement tables.
    dividend_map = scrape_dividends(symbol, session, profile)
    if dividend_map:
        for rec in financials:
            yr = rec.get("year")
            if yr and yr in dividend_map and (
                rec.get("dividend_per_share") is None
                or rec["dividend_per_share"] == 0.0
            ):
                rec["dividend_per_share"] = dividend_map[yr]
                rec.setdefault("_sources", {})[
                    "dividend_per_share"
                ] = f"PSX payout announcement (FY{yr})"

    # After the dedicated dividend pass, mark any remaining None as 0.0
    # (meaning we've genuinely searched everywhere and found nothing)
    for rec in financials:
        if rec.get("dividend_per_share") is None:
            rec["dividend_per_share"] = 0.0
            rec.setdefault("_sources", {})[
                "dividend_per_share"
            ] = f"no dividend record found after full search (FY{rec.get('year', '?')})"

    quality = _data_quality(profile, financials)

    return {
        "symbol": symbol,
        "profile": profile,
        "financials": financials,
        "price_history": price_history,
        "reports": reports[:6],
        "warnings": warnings,
        "data_quality": quality,
        "scraped_at": scraped_at,
        "sa_data": sa_data,   # StockAnalysis ratios for the scorer
    }


def _too_sparse(rec: Dict) -> bool:
    core = ["revenue", "net_profit", "total_equity", "total_assets"]
    return sum(1 for k in core if rec.get(k) is not None) < 2


def _data_quality(profile: Dict, financials: List[Dict]) -> float:
    """0..1 honesty score for how complete the scrape was."""
    score = 0.0
    if not profile.get("_unavailable"):
        score += 0.25
    if profile.get("price") is not None:
        score += 0.15
    if financials:
        score += 0.20
        latest = financials[-1]
        core = ["revenue", "net_profit", "total_equity", "total_assets", "eps"]
        filled = sum(1 for k in core if latest.get(k) is not None)
        score += 0.40 * (filled / len(core))
    return round(min(score, 1.0), 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "OGDC"
    print(json.dumps(scrape_company(sym), ensure_ascii=False, indent=2, default=str))