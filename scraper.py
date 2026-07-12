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
    # v3 — needed for ROIC (real NOPAT) and CCE. Order matters:
    # profit_before_tax must be tested before income_tax so that
    # "profit before taxation" never matches the bare-tax patterns.
    "profit_before_tax": [
        r"profit before tax", r"pre.?tax profit", r"profit / \(loss\) before tax",
    ],
    "income_tax": [
        r"^\s*taxation\s*$", r"income tax", r"provision for tax", r"tax expense",
        r"^\s*tax\s*$",
    ],
    "cash": [
        r"cash and cash equivalents", r"cash and bank balances",
        r"cash & cash equivalents", r"cash at bank",
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

def _price_from_html(symbol: str, html: str) -> Optional[float]:
    """
    v3.3: extract the live price from the ALREADY-DOWNLOADED company page,
    using the exact same parsing logic _scrape_live_price applies — this just
    skips the redundant second download of the same page.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
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
            num = utils.to_number(_text(el))
            if num and num > 0:
                return num
    except Exception:  # noqa: BLE001
        pass
    return None


def _scrape_live_price(symbol: str, session) -> Optional[float]:
    """Try several PSX endpoints to get a real-time price."""
    # v3.3: the /quotes, /api/companies and /symbol endpoints are dead (404)
    # and were burning retry time on every analysis. The company page is the
    # endpoint that actually carries the live price.
    candidate_urls = [
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

def scrape_profile(symbol: str, session, html: str = None) -> Dict:
    url = config.PSX_COMPANY_URL.format(symbol=symbol)
    if html is None:                       # v3.3: reuse the page when provided
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

def scrape_financial_tables(symbol: str, session, html: str = None) -> List[Dict]:
    if html is None:                       # v3.3: reuse the page when provided
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
    v3 — STRICT ORIGINAL-DATA POLICY.

    Only EXACT accounting identities are applied (pure arithmetic on figures
    that were actually scraped from the company's own statements):

        liabilities = assets − equity        equity = assets − liabilities
        assets      = equity + liabilities
        equity      = book value/share × shares (both scraped)
        eps         = net profit ÷ shares outstanding (both scraped)

    NO approximations, NO sector proxies, NO percentage-of-total guesses.
    Anything that cannot be computed exactly stays None and the scorer will
    show it as N/A and re-weight the remaining metrics.
    """
    yr = rec.get("year", "?")
    src = rec.setdefault("_sources", {})
    p = profile or {}

    def _set(field, value, reason):
        if rec.get(field) is None and value is not None:
            rec[field] = value
            src[field] = reason

    # ---- balance sheet identities (exact) ----
    _set("total_liabilities",
         _sub(rec.get("total_assets"), rec.get("total_equity")),
         f"identity: assets − equity (FY{yr})")
    _set("total_equity",
         _sub(rec.get("total_assets"), rec.get("total_liabilities")),
         f"identity: assets − liabilities (FY{yr})")
    _set("total_assets",
         _add(rec.get("total_equity"), rec.get("total_liabilities")),
         f"identity: equity + liabilities (FY{yr})")

    # ---- total_equity from book_value × shares (both real, exact) ----
    if rec.get("total_equity") is None:
        bv = p.get("book_value")
        sh = p.get("shares")
        if bv and sh and bv > 0 and sh > 0:
            _set("total_equity", bv * sh,
                 f"identity: book value Rs{bv:.1f} × {sh:.0f} shares")

    # ---- EPS: exact derivation from net_profit ÷ shares ----
    if rec.get("eps") is None:
        np_ = rec.get("net_profit")
        sh  = rec.get("shares_outstanding") or p.get("shares")
        if np_ is not None and sh is not None and sh > 0:
            _set("eps", np_ / sh,
                 f"identity: net profit ÷ shares (FY{yr})")

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
                pt = {"date": date, "close": float(close)}
                # PSX EOD rows are [timestamp, close, volume] — keep the
                # volume when present so the Prediction tab can read it.
                if len(row) >= 3:
                    try:
                        pt["volume"] = float(row[2])
                    except Exception:
                        pass
                out.append(pt)
            elif isinstance(row, dict):
                pt = {
                    "date": str(row.get("date") or row.get("time")),
                    "close": float(row.get("close") or row.get("price")),
                }
                if row.get("volume") is not None:
                    try:
                        pt["volume"] = float(row["volume"])
                    except Exception:
                        pass
                out.append(pt)
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

def find_report_links(symbol: str, session, html: str = None) -> List[Dict]:
    if html is None:                      # v3.3: avoid re-downloading the page
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
    # v3.6 — banks: S&P/StockAnalysis publish capital ratios for many banks.
    # These feed the Capital Adequacy ladder (never invented, always linked).
    "capital adequacy ratio": "capital_adequacy_pct",
    "total capital ratio":    "capital_adequacy_pct",
    "tier 1 capital ratio":   "tier1_ratio_pct",
    "tier 1 ratio":           "tier1_ratio_pct",
    "cet1 ratio":             "cet1_ratio_pct",
    "common equity tier 1 ratio": "cet1_ratio_pct",
    # v3.4 — S&P publishes ROIC itself, computed from the filed statements.
    # This is the strongest possible tier-0 for the ROIC fundamental.
    "return on capital (roic)": "roic_pct",
    "return on invested capital (roic)": "roic_pct",
    "return on capital employed (roce)": "roce_pct",
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

_PCT_FIELDS = {"roe_pct","roa_pct","roic_pct","roce_pct","profit_margin_pct",
               "operating_margin_pct","dividend_yield_pct","payout_ratio_pct",
               "price_change_52w",
               "capital_adequacy_pct","tier1_ratio_pct","cet1_ratio_pct"}


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


# ---------------------------------------------------------------------------
# StockAnalysis annual statements (v3)
# ---------------------------------------------------------------------------
# Multi-year, as-filed line items needed for a REAL (never estimated) ROIC:
#   NOPAT            = Operating Income × (1 − Income Tax ÷ Pretax Income)
#   Invested Capital = Total Debt + Shareholders' Equity  (averaged, 2 yrs)
# plus Cash & Equivalents / Total Assets for the CCE metric.
# All figures on these pages are the company's own reported statements as
# aggregated by S&P Global — original data, no derivation.

_SA_INCOME_URL  = "https://stockanalysis.com/quote/psx/{symbol}/financials/"
_SA_BALANCE_URL = "https://stockanalysis.com/quote/psx/{symbol}/financials/balance-sheet/"

# v3.3 — label matching is VARIANT-AWARE and prefix-based, because
# StockAnalysis prints e.g. "Income Tax Expense" (not "Income Tax") and
# "Total Current Liabilities". An exact-lookup map silently starved ROIC.
# Rows that are ratios/derived ("... Growth", "... Margin", "(YoY)",
# "Effective Tax Rate") are excluded before matching.
_SA_STMT_FIELDS = [
    # income statement (order matters: first match wins)
    ("operating_profit",    ("operating income", "operating profit", "ebit")),
    ("profit_before_tax",   ("pretax income", "profit before tax",
                             "income before taxes", "earnings before tax")),
    ("income_tax",          ("income tax expense", "income tax",
                             "provision for income tax", "taxation")),
    ("net_profit",          ("net income to common", "net income", "net profit")),
    ("interest_expense",    ("interest expense",)),
    ("interest_income",     ("interest & investment income", "interest and investment income",
                             "interest income")),
    ("eps",                 ("eps (basic)",)),
    ("eps_diluted",         ("eps (diluted)",)),
    ("revenue",             ("revenue", "total revenue", "net sales")),
    # balance sheet
    ("cash",                ("cash & equivalents", "cash and equivalents",
                             "cash & cash equivalents", "cash and cash equivalents")),
    ("current_assets",      ("total current assets",)),
    ("current_liabilities", ("total current liabilities",)),
    ("total_debt",          ("total debt",)),
    ("total_equity",        ("shareholders' equity", "shareholders equity",
                             "stockholders' equity", "total equity")),
    ("total_assets",        ("total assets",)),
    ("total_liabilities",   ("total liabilities",)),
    ("working_capital",     ("working capital",)),
    ("book_value",          ("book value per share",)),
]
_SA_ROW_EXCLUDE = re.compile(r"growth|margin|\(yoy\)|effective tax|per share growth|yield", re.I)


def _sa_stmt_field(label: str):
    lab = label.lower().strip()
    if not lab or _SA_ROW_EXCLUDE.search(lab):
        return None
    for field, variants in _SA_STMT_FIELDS:
        for v in variants:
            if lab == v or lab.startswith(v):
                return field
    return None

_FY_COL_RE = re.compile(r"(?:FY\s*)?((?:19|20)\d{2})")


def _parse_sa_statement_table(soup, out: Dict[int, Dict]) -> int:
    """Parse an SA financials table: header row = fiscal years, rows = items."""
    parsed = 0
    for table in soup.find_all("table"):
        header = table.find("tr")
        if not header:
            continue
        cols = header.find_all(["th", "td"])
        year_by_col: Dict[int, int] = {}
        for ci, cell in enumerate(cols):
            m = _FY_COL_RE.search(_text(cell))
            if m:
                year_by_col[ci] = int(m.group(1))
        if not year_by_col:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            field = _sa_stmt_field(_text(cells[0]))
            if not field:
                continue
            for ci, year in year_by_col.items():
                if ci >= len(cells):
                    continue
                raw = _text(cells[ci])
                if not raw or raw in ("-", "—", "n/a", "N/A", "Upgrade"):
                    continue
                num = utils.to_number(raw.replace("%", ""))
                if num is None:
                    continue
                rec = out.setdefault(year, {"year": year})
                if field not in rec:
                    rec[field] = num
                    parsed += 1
    return parsed


def scrape_sa_statements(symbol: str, session) -> Dict[int, Dict]:
    """
    Fetch multi-year annual statements from StockAnalysis (income statement +
    balance sheet). Returns {year: {field: value}} with a consistent scale
    within the source, so any ratio computed purely inside this dataset
    (ROIC, cash/assets, margins, growth) uses original figures only.
    Never raises — returns {} on any failure.
    """
    out: Dict[int, Dict] = {}
    for url in (_SA_INCOME_URL.format(symbol=symbol),
                _SA_BALANCE_URL.format(symbol=symbol)):
        try:
            html = utils.fetch(url, session=session)
            if not html:
                print(f"  [SA-STMT] no HTML from {url}")
                continue
            n = _parse_sa_statement_table(BeautifulSoup(html, "lxml"), out)
            print(f"  [SA-STMT] {url.rsplit('/quote/',1)[-1]}: {n} values")
        except Exception as exc:  # noqa: BLE001
            print(f"  [SA-STMT] failed {url}: {exc}")
    return out


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
# v3.6 — Capital Adequacy ladder (banks & financials)
# ---------------------------------------------------------------------------
# The bank scoring model needs the Capital Adequacy Ratio, but PSX statement
# tables only sometimes carry it. This dedicated ladder hunts the REAL
# reported number through four tiers, and every tier records BOTH the exact
# source label and a clickable URL on the record, so the metric explainer
# links straight to the page the figure came from. Nothing is ever estimated:
# if all tiers miss, the metric honestly stays out and the deep store (official
# filed annual reports, deepdata.py) keeps trying in the background.
#
#   Tier 1  PSX statement tables            (existing LINE_ITEMS pass)
#   Tier 2  PSX company page ratio cards    (label/value blocks + free text)
#   Tier 3  StockAnalysis statistics page   (already fetched in parallel)
#   Tier 4  StockAnalysis ratios page       (one extra fetch, banks only)
#   (then)  Official filed annual reports   (deepdata.py, per-document links)

_SA_RATIOS_URL = "https://stockanalysis.com/quote/psx/{symbol}/financials/ratios/"

_CAR_PHRASE_RE = re.compile(
    r"capital\s+adequacy(\s+ratio)?|total\s+capital\s+ratio", re.I)
_CAR_ACRONYM_RE = re.compile(r"\bCAR\b")            # case-SENSITIVE on purpose
_TIER1_RE = re.compile(r"tier\s*-?\s*1\s+(capital\s+)?ratio|cet\s*-?\s*1", re.I)
_CAR_TEXT_RE = re.compile(
    r"capital\s+adequacy\s+ratio[^0-9%]{0,60}?(\d{1,2}(?:\.\d{1,2})?)\s*%", re.I)
_CAR_YEAR_RE = re.compile(r"(?:FY\s*)?((?:19|20)\d{2})")


def _car_plausible(v) -> bool:
    """Sanity window for a reported CAR/Tier-1 percentage (SBP floor ~11.5%)."""
    return v is not None and 4.0 <= v <= 60.0


def _car_label_matches(label: str) -> bool:
    return bool(_CAR_PHRASE_RE.search(label) or _CAR_ACRONYM_RE.search(label))


def _car_from_html(html: str):
    """Reported CAR from a PSX company page's ratio cards / tables / text.
    Returns (value, context_year_or_None) or (None, None)."""
    if not html:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    # a) label/value pairs anywhere on the page (PSX ratio cards use these)
    for label, raw in _harvest_label_value_pairs(soup):
        if not _car_label_matches(label):
            continue
        v = utils.to_number(str(raw).replace("%", ""))
        if _car_plausible(v):
            ym = _CAR_YEAR_RE.search(label + " " + str(raw))
            return v, (int(ym.group(1)) if ym else None)
    # b) two-cell table rows
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = _text(cells[0])
            if not _car_label_matches(label):
                continue
            for cell in cells[1:]:
                v = utils.to_number(_text(cell).replace("%", ""))
                if _car_plausible(v):
                    ym = _CAR_YEAR_RE.search(label)
                    return v, (int(ym.group(1)) if ym else None)
    # c) plain text like "Capital Adequacy Ratio stood at 17.4%"
    m = _CAR_TEXT_RE.search(soup.get_text(" ", strip=True))
    if m:
        v = utils.to_number(m.group(1))
        if _car_plausible(v):
            return v, None
    return None, None


def _car_from_sa_ratios(symbol: str, session) -> Dict[int, float]:
    """Per-year capital ratios from StockAnalysis's ratios page. {} on miss."""
    out: Dict[int, float] = {}
    url = _SA_RATIOS_URL.format(symbol=symbol)
    try:
        html = utils.fetch(url, session=session)
    except Exception as exc:  # noqa: BLE001
        print(f"  [CAR] SA ratios fetch failed for {symbol}: {exc}")
        return out
    if not html:
        return out
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        header = table.find("tr")
        if not header:
            continue
        year_by_col: Dict[int, int] = {}
        for ci, cell in enumerate(header.find_all(["th", "td"])):
            m = _FY_COL_RE.search(_text(cell))
            if m:
                year_by_col[ci] = int(m.group(1))
        if not year_by_col:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2 or not _car_label_matches(_text(cells[0])):
                continue
            for ci, year in year_by_col.items():
                if ci >= len(cells):
                    continue
                v = utils.to_number(_text(cells[ci]).replace("%", ""))
                if _car_plausible(v) and year not in out:
                    out[year] = v
    if out:
        print(f"  [CAR] SA ratios page gave {len(out)} year(s) for {symbol}")
    return out


def fill_capital_adequacy(symbol: str, session, page_html: str,
                          financials: List[Dict], sa_data: Dict,
                          warnings: List[str]) -> None:
    """Run the ladder. Mutates `financials` in place; never raises."""
    try:
        if not financials:
            return
        if any(r.get("capital_adequacy") is not None for r in financials):
            return                                   # Tier 1 already delivered
        latest = financials[-1]
        psx_url = config.PSX_COMPANY_URL.format(symbol=symbol)

        def _set(rec, val, label, url):
            rec["capital_adequacy"] = val
            rec.setdefault("_sources", {})["capital_adequacy"] = label
            rec.setdefault("_source_urls", {})["capital_adequacy"] = url

        # ---- Tier 2: PSX company page ratio cards / free text -------------
        val, yr = _car_from_html(page_html)
        if val is not None:
            target = latest
            if yr:
                for r in financials:
                    if r.get("year") == yr:
                        target = r
                        break
            _set(target, val,
                 "PSX company page — reported Capital Adequacy Ratio"
                 + (f" (FY{yr})" if yr else ""), psx_url)
            print(f"  [CAR] {symbol}: {val}% from PSX company page")
            return

        # ---- Tier 3: StockAnalysis statistics page (already fetched) ------
        sa_url = _SA_URL.format(symbol=symbol)
        if sa_data:
            for key, label in (
                ("capital_adequacy_pct",
                 "StockAnalysis statistics — Total Capital / Capital Adequacy Ratio (S&P Global)"),
                ("tier1_ratio_pct",
                 "StockAnalysis statistics — Tier-1 capital ratio (total CAR not published; S&P Global)"),
                ("cet1_ratio_pct",
                 "StockAnalysis statistics — CET-1 ratio (total CAR not published; S&P Global)"),
            ):
                v = sa_data.get(key)
                if _car_plausible(v):
                    _set(latest, v, label, sa_url)
                    print(f"  [CAR] {symbol}: {v}% from SA statistics ({key})")
                    return

        # ---- Tier 4: StockAnalysis ratios page (one extra request) --------
        per_year = _car_from_sa_ratios(symbol, session)
        if per_year:
            ratios_url = _SA_RATIOS_URL.format(symbol=symbol)
            filled = 0
            for rec in financials:
                y = rec.get("year")
                if y in per_year and rec.get("capital_adequacy") is None:
                    _set(rec, per_year[y],
                         f"StockAnalysis ratios page — capital ratio FY{y} (S&P Global)",
                         ratios_url)
                    filled += 1
            if not any(r.get("capital_adequacy") is not None for r in financials):
                y, v = max(per_year.items())
                _set(latest, v,
                     f"StockAnalysis ratios page — capital ratio FY{y} (S&P Global)",
                     ratios_url)
                filled += 1
            if filled:
                warnings.append("Capital Adequacy sourced from StockAnalysis's "
                                "ratios page (S&P Global) — see the metric's "
                                "source link.")
                return

        # ---- All tiers missed — say so honestly; the deep store keeps trying
        warnings.append(
            "Capital Adequacy Ratio is not published on the PSX company page "
            "or StockAnalysis for this bank yet — it will fill automatically "
            "from the bank's official filed annual report once the background "
            "deep store collects it (re-run in a little while).")
        print(f"  [CAR] {symbol}: not found on PSX/StockAnalysis; deep store queued")
    except Exception as exc:  # noqa: BLE001
        print(f"  [CAR] ladder failed for {symbol}: {exc}")


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


def scrape_dividends(symbol: str, session, profile: dict = None, html: str = None) -> Dict[int, float]:
    """
    Dedicated dividend scraper.  Tries multiple strategies:
      1. Scan announcement / corporate-action tables on the company page
      2. Scan financial statement tables (already done by the main parser, but
         do a focused pass looking only for dividend rows with relaxed matching)
      3. Derive from dividend yield + share price if both are in the profile
    Returns {fiscal_year: dividend_per_share_amount}.
    """
    if html is None:                       # v3.3: reuse the page when provided
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
    """
    Full scrape for one company. Always returns a dict; never raises.

    v3.3 PERFORMANCE ARCHITECTURE — identical data, a fraction of the time:
      * the PSX company page is fetched ONCE and re-used by every parser
        (profile, live price, financial tables, report links, dividends);
      * all independent sources (price history, StockAnalysis statistics,
        StockAnalysis annual statements, dividends) are fetched IN PARALLEL,
        so wall time ≈ the slowest source instead of the sum of all of them;
      * annual-report PDFs are NEVER downloaded inside a user request —
        the persistent deep store is merged instantly, and any missing
        symbol is queued for the background deep worker.
    """
    symbol = symbol.strip().upper()
    session = utils.make_session()
    warnings: List[str] = []
    scraped_at = utils.now_iso()
    utils.progress_reset(symbol)                       # v3.5: real progress
    utils.progress_update(symbol, 4, "Contacting PSX…")

    # ---- Phase 0: ONE fetch of the company page --------------------------
    page_html = utils.fetch(config.PSX_COMPANY_URL.format(symbol=symbol),
                            session=session)
    utils.progress_update(symbol, 16, "Reading company profile…")

    profile = scrape_profile(symbol, session, html=page_html)
    if profile.get("_unavailable"):
        warnings.append("Company page could not be reached on PSX.")

    # ---- Phase 1: independent sources in parallel ------------------------
    utils.progress_update(symbol, 24,
                          "Collecting price history & filed statements…")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="scrape") as ex:
        f_hist = ex.submit(scrape_price_history, symbol, utils.make_session())
        f_sa   = ex.submit(scrape_stockanalysis, symbol, utils.make_session())
        f_stmt = ex.submit(scrape_sa_statements, symbol, utils.make_session())
        f_divs = ex.submit(scrape_dividends, symbol, utils.make_session(),
                           profile, page_html)
        # v3.6 — real announcements / catalysts (reuses the fetched page and
        # only touches the network for the portal-wide fallback listing).
        # deep_pdf=False (used by the recommendations scan) keeps the
        # title-level classification but skips every document download.
        import catalyst as _catalyst
        f_cat = ex.submit(_catalyst.fetch_catalysts, symbol,
                          utils.make_session(), page_html, deep_pdf)
        # v4.0 — Material Information engine (reads MI PDFs end-to-end, OCR
        # fallback for scanned letters; verdicts are EXTRACTED, never assumed).
        # Only run for individual X-rays — a 100-symbol ranking scan must not
        # download hundreds of PDFs.
        if deep_pdf:
            f_mat = ex.submit(_catalyst.fetch_material_info, symbol,
                              utils.make_session(), page_html)
        else:
            f_mat = None

        # main thread keeps working on the already-downloaded page
        financials = scrape_financial_tables(symbol, session, html=page_html)
        if not financials:
            warnings.append("Structured financial tables not found on the page.")
        reports = find_report_links(symbol, session, html=page_html)
        utils.progress_update(symbol, 34, "Parsing PSX financial tables…")

        def _safe(fut, fallback):
            try:
                return fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [scrape] parallel task failed for {symbol}: {exc}")
                return fallback

        price_history = _safe(f_hist, [])
        utils.progress_update(symbol, 46, "Price history received…")
        sa_data       = _safe(f_sa, {})
        utils.progress_update(symbol, 55, "Key statistics received (S&P Global)…")
        sa_statements = _safe(f_stmt, {})
        utils.progress_update(symbol, 64, "Annual statements received…")
        dividend_map  = _safe(f_divs, {})
        utils.progress_update(symbol, 68, "Dividend history received…")
        announcements = _safe(f_cat, {"checked": False, "items": [],
                                      "error": "catalyst scan failed"})
        if f_mat is not None:
            material_info = _safe(f_mat, {"checked": False, "items": [],
                                          "error": "material-info scan failed"})
        else:   # ranking mode — honestly report that MI was not scanned
            material_info = {"checked": False, "items": [], "found": 0,
                             "read": 0, "error": None,
                             "skipped": "not scanned in ranking mode"}
        utils.progress_update(symbol, 72,
                              "Material Information filings read…")

    # ---- Live price: company page → last close ---------------------------
    live_price = _scrape_live_price(symbol, session) if page_html is None \
        else _price_from_html(symbol, page_html)
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

    # ---- Merge secondary data --------------------------------------------
    utils.progress_update(symbol, 76, "Merging sources…")
    _merge_secondary_data(financials, profile, sa_data, warnings)
    # v3.7 — the Capital Adequacy metric was removed from the banking model
    # (7 fundamentals, equal weight), so the CAR ladder is no longer invoked.
    # fill_capital_adequacy() below is retained as a tested utility.

    # ---- v3.3: deep store merged INSTANTLY; fetching happens in background
    deep_info = {}
    utils.progress_update(symbol, 82, "Merging official filed reports…")
    try:
        import deepdata                       # lazy import (avoids cycles)
        deep_info = deepdata.fill_gaps(symbol, financials, profile,
                                       session=session, allow_fetch=False,
                                       psx_html=page_html)
        if deep_info.get("filled"):
            warnings.append(
                f"{deep_info['filled']} figure(s) sourced directly from the "
                f"company's official filed reports (see per-metric source links).")
        if deep_info.get("queued"):
            warnings.append(
                "The company's official filed reports are being collected in "
                "the background — re-run in a little while for even deeper data.")
    except Exception as exc:  # noqa: BLE001
        print(f"  [deep] skipped for {symbol}: {exc}")

    # Final gap-fill pass on every record (with profile ratios)
    for rec in financials:
        _gap_fill_record(rec, profile=profile)

    # --- Dedicated dividend pass (fetched in parallel above) ---
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
        "sa_statements": sa_statements,   # v3: {year: {field: value}} as-filed
        "deep_data": deep_info,           # v3.2: official-filings fill summary
        "announcements": announcements,   # v3.6: real PSX filings + catalysts
        "material_info": material_info,   # v4.0: MI letters read end-to-end
        "source_urls": {                  # v3: clickable references per source
            "psx":          config.PSX_COMPANY_URL.format(symbol=symbol),
            "psx_announcements": config.PSX_ANNOUNCEMENTS_URL,
            "sa_stats":     _SA_URL.format(symbol=symbol),
            "sa_income":    _SA_INCOME_URL.format(symbol=symbol),
            "sa_balance":   _SA_BALANCE_URL.format(symbol=symbol),
            "sa_dividend":  _SA_DIV_URL.format(symbol=symbol),
            "sa_ratios":    _SA_RATIOS_URL.format(symbol=symbol),
        },
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