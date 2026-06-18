"""
scraper.py
==========
Pulls the raw material the scorer needs for one company, live from public PSX
pages — no API key, no login:

    scrape_company("OGDC") -> {
        "symbol", "profile": {...},
        "financials": [ {year, revenue, net_profit, eps, ...}, ... ],  # ascending
        "price_history": [ {date, close}, ... ],
        "reports": [ {title, url, year}, ... ],
        "warnings": [...], "data_quality": 0..1, "scraped_at": "...Z"
    }

Strategy, most-reliable first:
  1. Company page on the PSX Data Portal  -> price, sector, ratios, and the
     multi-year financial tables it renders.
  2. EOD timeseries endpoint              -> price history for trend charts.
  3. Annual-report PDFs                    -> parsed with pdfplumber to fill any
     gaps the structured tables left.

Real-world PSX pages and company PDFs vary a lot, so every extractor is
defensive: it never assumes a fixed column or row position, maps line items by
keyword, and records what it could not find in `warnings` (which feeds a
`data_quality` score the UI shows honestly).
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
    import pdfplumber  # heavy; imported lazily-friendly but fine at top
except Exception:  # noqa: BLE001
    pdfplumber = None


# ---------------------------------------------------------------------------
# Line-item dictionaries — how a label in a statement maps to a canonical field.
# Order matters: earlier patterns win. Patterns are matched case-insensitively
# against the row label.
# ---------------------------------------------------------------------------
LINE_ITEMS = {
    "revenue": [
        r"net sales", r"net revenue", r"\bturnover\b", r"\brevenue\b",
        r"\bsales\b", r"markup.*interest earned", r"interest earned",
        r"total income",
    ],
    "gross_profit": [r"gross profit"],
    "operating_profit": [r"operating profit", r"profit from operations", r"operating income"],
    "net_profit": [
        r"profit after tax", r"profit for the year", r"profit attributable",
        r"net profit", r"profit/\(loss\) after tax", r"profit / \(loss\) for the year",
    ],
    "eps": [r"earnings per share", r"\beps\b", r"basic.*per share"],
    "total_assets": [r"total assets"],
    "total_equity": [
        r"total equity", r"shareholders.{0,3} equity", r"share holders.{0,3} equity",
        r"equity attributable",
    ],
    "total_liabilities": [r"total liabilities"],
    "current_assets": [r"total current assets", r"current assets"],
    "current_liabilities": [r"total current liabilities", r"current liabilities"],
    "total_debt": [
        r"long.?term financing", r"long.?term debt", r"\bborrowings\b",
        r"total debt", r"lease liabilities",
    ],
    "operating_cashflow": [
        r"cash generated from operations",
        r"net cash (generated )?from operating activities",
        r"cash flows? from operating activities",
    ],
    "dividend_per_share": [r"dividend per share", r"cash dividend"],
    # banking-specific
    "net_interest_income": [r"net (markup|interest) income", r"net markup"],
    "capital_adequacy": [r"capital adequacy ratio", r"\bcar\b"],
}

_YEAR_RE = re.compile(r"(?:FY)?\s*'?(\d{2}|\d{4})\b")

# Per-share figures and ratios are printed in absolute terms even when the
# statement caption says "Rupees in '000" — that caption only scales the
# monetary totals, so these fields must ignore it.
NO_SCALE_FIELDS = {"eps", "dividend_per_share", "capital_adequacy"}


# ---------------------------------------------------------------------------
# Company profile (price, sector, ratios)
# ---------------------------------------------------------------------------
def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def scrape_profile(symbol: str, session) -> Dict:
    """
    Read the company landing page on the data portal. We pull labelled values
    (Price, Change, Market Cap, P/E, etc.) by scanning for label/value pairs
    rather than relying on a brittle layout.
    """
    url = config.PSX_COMPANY_URL.format(symbol=symbol)
    html = utils.fetch(url, session=session)
    profile: Dict = {"symbol": symbol, "source_url": url}
    if not html:
        profile["_unavailable"] = True
        return profile

    soup = BeautifulSoup(html, "lxml")

    # Company name + sector usually sit in the page header.
    name = _text(soup.find(class_=re.compile("company.*name", re.I))) or _text(soup.find("h1"))
    if name:
        profile["name"] = name
    sector = _text(soup.find(class_=re.compile("sector", re.I)))
    if sector:
        profile["sector"] = sector.upper()

    # Generic label -> value harvesting.
    wanted = {
        "price": [r"^last$", r"current", r"^price$"],
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


def _harvest_label_value_pairs(soup) -> List[tuple]:
    """
    Collect (label, value) pairs from several common DOM shapes:
      <div class=label>..</div><div class=value>..</div>
      <th>label</th><td>value</td>
      <span class=name>label</span><span class=val>value</span>
    """
    pairs: List[tuple] = []

    # definition-list / stat blocks
    for stat in soup.find_all(class_=re.compile("stats|quote|summary|data", re.I)):
        labels = stat.find_all(class_=re.compile("name|label|title|key", re.I))
        values = stat.find_all(class_=re.compile("value|val|amount|number|data", re.I))
        for lab, val in zip(labels, values):
            pairs.append((_text(lab), _text(val)))

    # table rows
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            pairs.append((_text(cells[0]), _text(cells[1])))

    return pairs


# ---------------------------------------------------------------------------
# Multi-year financial statements from the page tables
# ---------------------------------------------------------------------------
def scrape_financial_tables(symbol: str, session) -> List[Dict]:
    """
    Find statement-like tables on the company page (income statement, balance
    sheet, key ratios) and assemble per-year records. Tables are recognised by
    a header row that contains year tokens; row labels are mapped to canonical
    fields via LINE_ITEMS.
    """
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
                        rec = by_year.setdefault(year, {"year": year})
                        # don't overwrite a value we already found with a worse one
                        rec.setdefault(field, num)

    return [by_year[y] for y in sorted(by_year)]


def _detect_year_columns(table) -> Dict[int, int]:
    """Return {column_index: year} for header cells that look like years."""
    head = table.find("tr")
    if not head:
        return {}
    cells = head.find_all(["th", "td"])
    out: Dict[int, int] = {}
    for idx, cell in enumerate(cells):
        if idx == 0:
            continue  # first column is the label column
        m = _YEAR_RE.search(_text(cell))
        if m:
            yr = int(m.group(1))
            if yr < 100:  # two-digit -> 20xx
                yr += 2000
            if 1990 <= yr <= datetime.now().year + 1:
                out[idx] = yr
    return out


def _detect_scale_hint(table) -> str:
    """Look for 'Rupees in 000 / million' captions near the table."""
    blob = _text(table.find("caption")) + " " + _text(table.find("thead"))
    cap = table.find_previous(string=re.compile(r"rupees in|amounts in|rs\.? in", re.I))
    if cap:
        blob += " " + str(cap)
    return blob


def _match_line_item(label: str) -> Optional[str]:
    low = label.lower()
    for field, patterns in LINE_ITEMS.items():
        for pat in patterns:
            if re.search(pat, low):
                return field
    return None


# ---------------------------------------------------------------------------
# Price history (for the trend charts)
# ---------------------------------------------------------------------------
def scrape_price_history(symbol: str, session) -> List[Dict]:
    """EOD close history from the timeseries endpoint, if available."""
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
            # rows are typically [timestamp, close, volume] or {date, close}
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
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------------------
# Annual-report PDFs (fallback / gap-fill)
# ---------------------------------------------------------------------------
def find_report_links(symbol: str, session) -> List[Dict]:
    """Collect links that look like annual/financial report PDFs."""
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
    # newest first
    reports.sort(key=lambda r: (r["year"] or 0), reverse=True)
    return reports


def parse_report_pdf(pdf_url: str, session) -> Dict:
    """
    Best-effort extraction of latest-year figures from an annual-report PDF.
    Returns a flat {field: value} dict for whatever it can find.
    """
    if pdfplumber is None:
        return {}
    raw = utils.fetch(pdf_url, session=session, expect_binary=True)
    if not raw:
        return {}

    found: Dict[str, float] = {}
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            # Statements are usually in the back half; scan a bounded window.
            pages = pdf.pages
            for page in pages:
                text = page.extract_text() or ""
                if not text:
                    continue
                for line in text.splitlines():
                    field = _match_line_item(line)
                    if not field or field in found:
                        continue
                    # take the first number that appears after the label text
                    nums = re.findall(r"-?\(?\d[\d,]*\.?\d*\)?", line)
                    for token in nums:
                        val = utils.to_number(token)
                        if val is not None and abs(val) > 0:
                            found[field] = val
                            break
                if len(found) >= len(LINE_ITEMS):
                    break
    except Exception as exc:  # noqa: BLE001
        print(f"  [pdf] could not parse {pdf_url}: {exc}")
    return found


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def scrape_company(symbol: str, deep_pdf: bool = True) -> Dict:
    """Full scrape for one company. Always returns a dict; never raises."""
    symbol = symbol.strip().upper()
    session = utils.make_session()
    warnings: List[str] = []

    profile = scrape_profile(symbol, session)
    if profile.get("_unavailable"):
        warnings.append("Company page could not be reached on PSX.")

    financials = scrape_financial_tables(symbol, session)
    if not financials:
        warnings.append("Structured financial tables not found on the page.")

    price_history = scrape_price_history(symbol, session)
    if not price_history:
        warnings.append("Price history endpoint returned nothing.")

    reports = find_report_links(symbol, session)

    # Gap-fill the latest year from the newest report PDF if needed.
    if deep_pdf and reports and (not financials or _too_sparse(financials[-1])):
        pdf_fields = parse_report_pdf(reports[0]["url"], session)
        if pdf_fields:
            year = reports[0].get("year") or (datetime.now().year)
            target = None
            for rec in financials:
                if rec.get("year") == year:
                    target = rec
                    break
            if target is None:
                target = {"year": year}
                financials.append(target)
                financials.sort(key=lambda r: r["year"])
            for k, v in pdf_fields.items():
                target.setdefault(k, v)
            warnings.append("Some figures were filled from the annual-report PDF.")

    quality = _data_quality(profile, financials)

    return {
        "symbol": symbol,
        "profile": profile,
        "financials": financials,
        "price_history": price_history,
        "reports": reports[:6],
        "warnings": warnings,
        "data_quality": quality,
        "scraped_at": utils.now_iso(),
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
