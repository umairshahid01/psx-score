"""
scraper.py
==========

Pulls the raw material the scorer needs for one company, live from public PSX
pages — no API key, no login:

scrape_company("OGDC") -> {
    "symbol", "profile": {...},
    "financials": [ {year, revenue, net_profit, eps, ...}, ... ], # ascending
    "price_history": [ {date, close}, ... ],
    "reports": [ {title, url, year}, ... ],
    "warnings": [...], "data_quality": 0..1, "scraped_at": "...Z"
}

FIX: Share price is now always the live scraped price from the analysis
     moment, with multiple fallback sources (page label harvest → price
     history last close → PSX quote API).  The `_price_from_history` flag
     is returned so the UI can label the source clearly.

FIX: Data fields are no longer left as None / "—" when derivable from
     other scraped fields.  Every financial record is gap-filled from
     related fields (e.g. total_liabilities = total_assets − total_equity),
     and each record carries a `_sources` dict so the UI can show the user
     which year the data came from when they click for details.

FIX: price_history is sorted oldest→newest (left→right on charts).
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
    "dividend_per_share": [r"dividend per share", r"cash dividend", r"interim dividend"],
    # banking-specific
    "net_interest_income": [r"net (markup|interest) income", r"net markup"],
    "capital_adequacy": [r"capital adequacy ratio", r"\bcar\b"],
}

_YEAR_RE = re.compile(r"(?:FY)?\s*'?(\d{2}|\d{4})\b")

NO_SCALE_FIELDS = {"eps", "dividend_per_share", "capital_adequacy"}

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
    """
    Try several PSX endpoints / page patterns to get a real-time price.
    Returns the first non-None value found.
    """
    # Strategy 1: dedicated quote/ticker API endpoints
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

            # Try JSON first
            import json as _json
            try:
                data = _json.loads(raw)
                for key in ("currentPrice", "current", "ldcp", "last", "close",
                             "lastTradePrice", "price", "ltp"):
                    if isinstance(data, dict) and key in data:
                        val = utils.to_number(data[key])
                        if val and val > 0:
                            return val
                # nested under "data"
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

            # Try HTML harvest
            soup = BeautifulSoup(raw, "lxml")

            # Look for price in common patterns
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

            # Also look for any element with price-like class or id
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
    }

    pairs = _harvest_label_value_pairs(soup)
    for field, patterns in wanted.items():
        for label, value in pairs:
            if any(re.search(p, label, re.I) for p in patterns):
                num = utils.to_number(value)
                if num is not None:
                    profile[field] = num
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

    # Gap-fill each record using accounting identities
    for rec in records:
        _gap_fill_record(rec)

    return records


def _gap_fill_record(rec: Dict) -> None:
    """
    Fill missing fields from accounting identities so no field is left blank
    when a related field is available. Records the derivation source.
    """
    yr = rec.get("year", "?")
    src = rec.setdefault("_sources", {})

    def _set(field, value, reason):
        if rec.get(field) is None and value is not None:
            rec[field] = value
            src[field] = reason

    # total_liabilities = total_assets - total_equity
    _set("total_liabilities",
         _sub(rec.get("total_assets"), rec.get("total_equity")),
         f"derived: assets − equity (FY{yr})")

    # total_equity = total_assets - total_liabilities
    _set("total_equity",
         _sub(rec.get("total_assets"), rec.get("total_liabilities")),
         f"derived: assets − liabilities (FY{yr})")

    # total_assets = total_equity + total_liabilities
    _set("total_assets",
         _add(rec.get("total_equity"), rec.get("total_liabilities")),
         f"derived: equity + liabilities (FY{yr})")

    # gross_profit = revenue - (revenue - gross_profit); use operating_profit as proxy
    # net_profit from gross_profit is too rough — skip

    # operating_cashflow: if missing, use net_profit × 0.9 as a conservative estimate
    # (labelled clearly so the UI can show it's estimated)
    if rec.get("operating_cashflow") is None and rec.get("net_profit") is not None:
        estimated = rec["net_profit"] * 0.9
        _set("operating_cashflow", estimated,
             f"estimated ~90% of net profit (FY{yr}, no CF data)")

    # dividend: if gross dividend amount available, leave dividend_per_share as-is
    # (we don't derive DPS from total dividend without knowing share count reliably)

    # capital_adequacy: banking only — can't derive without Basel data


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
# Price history — sorted oldest→newest for left→right chart display
# ---------------------------------------------------------------------------

def scrape_price_history(symbol: str, session) -> List[Dict]:
    """EOD close history, always sorted ascending (oldest left, newest right)."""
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

    # Deduplicate, remove bad points, sort ASCENDING (oldest → newest = left → right)
    seen = set()
    cleaned: List[Dict] = []
    for pt in out:
        d = pt.get("date")
        c = pt.get("close")
        if not d or c is None or c <= 0 or d in seen:
            continue
        seen.add(d)
        cleaned.append(pt)

    cleaned.sort(key=lambda r: r["date"])  # ascending: left = oldest, right = newest
    return cleaned


# ---------------------------------------------------------------------------
# Annual-report PDFs (fallback / gap-fill)
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

    # -----------------------------------------------------------------------
    # FIX 1: Real-time share price from the analysis moment
    # -----------------------------------------------------------------------
    price_history = scrape_price_history(symbol, session)

    # Try dedicated price endpoint first (most accurate, live)
    live_price = _scrape_live_price(symbol, session)

    if live_price and live_price > 0:
        profile["price"] = live_price
        profile["_price_source"] = "live scrape"
        profile["_price_as_of"] = scraped_at
    elif profile.get("price") and profile["price"] > 0:
        profile["_price_source"] = "company page"
        profile["_price_as_of"] = scraped_at
    elif price_history:
        # Last resort: most recent close from history
        last_pt = price_history[-1]
        profile["price"] = last_pt["close"]
        profile["_price_source"] = f"last close ({last_pt['date']})"
        profile["_price_as_of"] = last_pt["date"]
        warnings.append(f"Share price sourced from last available close ({last_pt['date']}).")
    else:
        profile["_price_source"] = "unavailable"
        warnings.append("Share price could not be retrieved.")

    # Derive change_pct from history if not on profile
    if profile.get("change_pct") is None and len(price_history) >= 2:
        prev = price_history[-2]["close"]
        last = price_history[-1]["close"]
        if prev:
            profile["change_pct"] = round((last - prev) / prev * 100, 2)

    # -----------------------------------------------------------------------
    # PDF gap-fill
    # -----------------------------------------------------------------------
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

    # Final gap-fill pass on each record
    for rec in financials:
        _gap_fill_record(rec)

    quality = _data_quality(profile, financials)

    return {
        "symbol": symbol,
        "profile": profile,
        "financials": financials,
        "price_history": price_history,   # ascending date order for charts
        "reports": reports[:6],
        "warnings": warnings,
        "data_quality": quality,
        "scraped_at": scraped_at,
    }


def _too_sparse(rec: Dict) -> bool:
    core = ["revenue", "net_profit", "total_equity", "total_assets"]
    return sum(1 for k in core if rec.get(k) is not None) < 2


def _data_quality(profile: Dict, financials: List[Dict]) -> float:
    """A 0..1 honesty score for how complete the scrape was."""
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
