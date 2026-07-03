"""
deepdata.py  (v3.2)
===================
Deep, ORIGINAL-DATA-ONLY fetcher for PSX-listed companies.

When PSX's own tables and StockAnalysis still leave gaps, this module goes to
the primary sources themselves:

  1. The company's OFFICIAL FILINGS hosted on the exchange
     (PDF annual / quarterly reports linked from dps.psx.com.pk), and
  2. The company's OWN OFFICIAL WEBSITE — discovered from its PSX page —
     whose investor-relations section is crawled (politely, same-domain,
     depth- and page-capped) for annual-report PDFs.

Downloaded PDFs are parsed for MULTI-YEAR financial tables — Pakistani annual
reports almost always contain a "Six Years at a Glance" / "Key Operating &
Financial Data" page, which yields six years of as-filed figures in one shot.

Everything parsed is written to a PERSISTENT per-symbol store
(psx_cache/deepdata/SYMBOL.json) with per-field provenance — the exact
document title, URL and page number every figure came from — so:

  * the next run answers instantly from disk,
  * the KPI info modal can link the user straight to the source PDF, and
  * the strict no-estimation policy holds: only numbers physically present
    in official documents are ever stored or merged.

A polite background PRE-WARM worker walks the whole PSX universe a symbol at
a time whenever the tool runs, persisting progress, so coverage of all listed
companies grows across runs without ever blocking the user. The stock the
user actually searches always gets an immediate on-demand deep fetch.

Nothing in this module estimates, derives or extrapolates a value.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import config
import utils

try:
    import pdfplumber
except Exception:  # noqa: BLE001
    pdfplumber = None

try:
    from bs4 import BeautifulSoup
except Exception:  # noqa: BLE001
    BeautifulSoup = None


# ---------------------------------------------------------------------------
# Settings / store
# ---------------------------------------------------------------------------

CFG = getattr(config, "DEEPDATA", {})
STORE_DIR = CFG.get("dir") or os.path.join(config.CACHE_DIR, "deepdata")

# Fields the scoring model needs; the fetcher works until the latest years
# have them (or the budget runs out). CAR only matters for banks.
REQUIRED_FIELDS = [
    "revenue", "net_profit", "eps",
    "operating_profit", "profit_before_tax", "income_tax",
    "total_assets", "total_equity", "total_debt",
    "current_assets", "current_liabilities", "cash",
    "operating_cashflow", "dividend_per_share",
]
BANKING_EXTRA = ["capital_adequacy", "net_interest_income"]

_YEAR_TOKEN = re.compile(r"\b(?:FY\s*)?((?:19|20)\d{2})\b")
_NUM_TOKEN = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_SCALE_LINE = re.compile(
    r"(?:rupees|rs\.?|pkr|amounts?)[^.\n]{0,40}?"
    r"(thousand|'?000|million|mn\b|billion|bn\b)", re.I)
_NO_SCALE = {"eps", "dividend_per_share", "capital_adequacy"}
_SKIP_DOMAINS = ("facebook.", "twitter.", "x.com", "linkedin.", "youtube.",
                 "instagram.", "psx.com.pk", "sbp.org", "secp.gov",
                 "google.", "whatsapp.", "mailto:", "javascript:")
_IR_HINT = re.compile(r"invest|financ|report|annual|account|sharehold|agm|result", re.I)
_PDF_HINT = re.compile(r"annual|financ|account|report|statement|result", re.I)

_lock = threading.Lock()
_status = {"prewarm_running": False, "prewarm_done": 0, "prewarm_total": 0,
           "last_symbol": None, "stored": 0}


def _store_path(symbol: str) -> str:
    return os.path.join(STORE_DIR, f"{symbol.upper()}.json")


def load_store(symbol: str) -> Optional[Dict]:
    try:
        with open(_store_path(symbol), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def save_store(symbol: str, data: Dict) -> None:
    os.makedirs(STORE_DIR, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = _store_path(symbol) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _store_path(symbol))


def _is_fresh(store: Optional[Dict]) -> bool:
    if not store:
        return False
    try:
        age_days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(store["updated_at"])).days
    except Exception:  # noqa: BLE001
        return False
    limit = CFG.get("freshness_complete_days", 120) if store.get("complete") \
        else CFG.get("freshness_days", 45)
    return age_days < limit


# ---------------------------------------------------------------------------
# 1. Discover the company's official website from its PSX page
# ---------------------------------------------------------------------------

def discover_website(symbol: str, session, psx_html: Optional[str] = None) -> Optional[str]:
    """The PSX company page lists the issuer's own website — find it."""
    if BeautifulSoup is None:
        return None
    html = psx_html or utils.fetch(
        config.PSX_COMPANY_URL.format(symbol=symbol), session=session)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # Preferred: an anchor labelled / positioned as the website field
    candidates: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith("http"):
            continue
        low = href.lower()
        if any(d in low for d in _SKIP_DOMAINS):
            continue
        ctx = " ".join([
            a.get_text(" ", strip=True),
            (a.parent.get_text(" ", strip=True)[:120] if a.parent else ""),
        ]).lower()
        if "website" in ctx or "web:" in ctx:
            candidates.insert(0, href)          # explicit "Website:" label wins
        else:
            candidates.append(href)
    for url in candidates:
        host = urlparse(url).netloc.lower()
        if host and "." in host:
            return f"{urlparse(url).scheme}://{host}"
    return None


# ---------------------------------------------------------------------------
# 2. Crawl the official website for financial-report PDFs
# ---------------------------------------------------------------------------

def crawl_for_pdfs(base_url: str, session,
                   max_pages: int = None, max_depth: int = None) -> List[Dict]:
    """
    Polite same-domain crawl: homepage → investor/financial pages → PDFs.
    Returns [{title, url, year}] newest-first. Never raises.
    """
    if BeautifulSoup is None or not base_url:
        return []
    max_pages = max_pages or CFG.get("crawl_pages", 12)
    max_depth = max_depth or CFG.get("crawl_depth", 2)
    delay = CFG.get("request_delay_s", 1.5)
    host = urlparse(base_url).netloc.lower()

    seen_pages: Set[str] = set()
    pdfs: Dict[str, Dict] = {}
    queue: List[Tuple[str, int]] = [(base_url, 0)]

    while queue and len(seen_pages) < max_pages:
        url, depth = queue.pop(0)
        norm = url.split("#")[0].rstrip("/")
        if norm in seen_pages:
            continue
        seen_pages.add(norm)
        try:
            html = utils.fetch(url, session=session)
        except Exception:  # noqa: BLE001
            html = None
        time.sleep(delay)
        if not html:
            continue
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            continue

        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"].strip())
            if urlparse(href).netloc.lower() != host:
                continue
            text = a.get_text(" ", strip=True)
            blob = f"{href} {text}"
            if href.lower().split("?")[0].endswith(".pdf"):
                if not _PDF_HINT.search(blob):
                    continue
                ym = _YEAR_TOKEN.search(text) or _YEAR_TOKEN.search(href)
                year = int(ym.group(1)) if ym else None
                key = href.split("#")[0]
                if key not in pdfs:
                    pdfs[key] = {"title": text or "Financial report",
                                 "url": key, "year": year,
                                 "annual": bool(re.search(r"annual", blob, re.I))}
            elif depth < max_depth and _IR_HINT.search(blob):
                queue.append((href, depth + 1))

    out = list(pdfs.values())
    # newest annual reports first — they contain multi-year "at a glance" tables
    out.sort(key=lambda r: (r.get("annual", False), r.get("year") or 0), reverse=True)
    return out


# ---------------------------------------------------------------------------
# 3. Multi-year PDF financial-table parser
# ---------------------------------------------------------------------------

def _page_scale_hint(text: str) -> str:
    m = _SCALE_LINE.search(text or "")
    return m.group(0) if m else ""


def _match_field(label: str) -> Optional[str]:
    from scraper import _match_line_item                     # lazy: no cycle
    return _match_line_item(label)


def _year_header(line: str, now_year: int) -> List[int]:
    """A header line with ≥2 plausible fiscal years, in column order."""
    years = [int(y) for y in _YEAR_TOKEN.findall(line)]
    years = [y for y in years if 1995 <= y <= now_year + 1]
    uniq = list(dict.fromkeys(years))
    if len(uniq) < 2:
        return []
    # must be monotone (ascending or descending) to look like table columns
    asc = all(b > a for a, b in zip(uniq, uniq[1:]))
    desc = all(b < a for a, b in zip(uniq, uniq[1:]))
    return uniq if (asc or desc) else []


def _sane(field: str, v: float) -> bool:
    if field == "eps":
        return abs(v) < 5_000
    if field == "dividend_per_share":
        return 0 <= v < 5_000
    if field == "capital_adequacy":
        return 0 < v < 60
    return abs(v) < 1e15


def _parse_text_page(text: str, years_ctx: List[int], scale: str,
                     now_year: int) -> Tuple[Dict[int, Dict], List[int]]:
    """Parse one page of text. Returns ({year:{field:val}}, active year header)."""
    out: Dict[int, Dict] = {}
    years = years_ctx
    for line in (text or "").splitlines():
        hdr = _year_header(line, now_year)
        if hdr:
            years = hdr
            continue
        if not years:
            continue
        m = _NUM_TOKEN.search(line)
        if not m:
            continue
        label = line[:m.start()].strip(" .:‥…\t-")
        if len(label) < 3 or not re.search(r"[a-zA-Z]", label):
            continue
        field = _match_field(label)
        if not field:
            continue
        tokens = _NUM_TOKEN.findall(line[m.start():])
        # drop tokens that are just the years repeated inside the row
        tokens = [t for t in tokens
                  if not (t.replace(",", "").isdigit()
                          and int(t.replace(",", "")) in years)]
        if len(tokens) < 2:                     # need multi-year alignment
            continue
        n = min(len(tokens), len(years))
        for i in range(n):
            val = utils.to_number(tokens[i], "" if field in _NO_SCALE else scale)
            if val is None or not _sane(field, val):
                continue
            rec = out.setdefault(years[i], {})
            rec.setdefault(field, val)
    return out, years


def _parse_table_rows(table: List[List], scale: str, now_year: int) -> Dict[int, Dict]:
    out: Dict[int, Dict] = {}
    if not table or len(table) < 2:
        return out
    header = " ".join(str(c or "") for c in table[0])
    years = _year_header(header, now_year)
    if not years:
        return out
    # map each header cell with a year to its column index
    col_year: Dict[int, int] = {}
    for ci, cell in enumerate(table[0]):
        m = _YEAR_TOKEN.search(str(cell or ""))
        if m:
            y = int(m.group(1))
            if 1995 <= y <= now_year + 1:
                col_year[ci] = y
    for row in table[1:]:
        if not row:
            continue
        label = str(row[0] or "").strip()
        field = _match_field(label)
        if not field:
            continue
        for ci, y in col_year.items():
            if ci >= len(row):
                continue
            val = utils.to_number(str(row[ci] or ""),
                                  "" if field in _NO_SCALE else scale)
            if val is None or not _sane(field, val):
                continue
            out.setdefault(y, {}).setdefault(field, val)
    return out


def parse_pdf_multi_year(raw: bytes, doc_title: str, doc_url: str,
                         max_pages: int = 80) -> Tuple[Dict[int, Dict], Dict]:
    """
    Extract {year: {field: value}} from a financial-report PDF, with
    provenance {year: {field: {"label","url","page"}}}. Only values printed
    in the document are returned — nothing is derived.
    """
    by_year: Dict[int, Dict] = {}
    prov: Dict[int, Dict] = {}
    if pdfplumber is None or not raw:
        return by_year, prov
    now_year = datetime.now().year
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for pno, page in enumerate(pdf.pages[:max_pages], start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001
                    text = ""
                scale = _page_scale_hint(text)
                got, _hdr = _parse_text_page(text, [], scale, now_year)
                if not got:
                    try:
                        for tb in (page.extract_tables() or []):
                            t_got = _parse_table_rows(tb, scale, now_year)
                            for y, rec in t_got.items():
                                got.setdefault(y, {}).update(
                                    {k: v for k, v in rec.items()
                                     if k not in got.get(y, {})})
                    except Exception:  # noqa: BLE001
                        pass
                for y, rec in got.items():
                    tgt = by_year.setdefault(y, {})
                    ptg = prov.setdefault(y, {})
                    for f, v in rec.items():
                        if f not in tgt:
                            tgt[f] = v
                            ptg[f] = {"label": f"{doc_title} — p.{pno}",
                                      "url": doc_url, "page": pno}
    except Exception as exc:  # noqa: BLE001
        print(f"  [deep] PDF parse failed for {doc_url}: {exc}")
    return by_year, prov


# ---------------------------------------------------------------------------
# 4. Orchestrated deep fetch for one symbol
# ---------------------------------------------------------------------------

def _coverage_ok(by_year: Dict[int, Dict], banking: bool) -> bool:
    """Latest 2 filed years each have the full required field set."""
    req = REQUIRED_FIELDS + (BANKING_EXTRA if banking else [])
    years = sorted(int(y) for y in by_year)
    if len(years) < 2:
        return False
    for y in years[-2:]:
        rec = by_year.get(y) or by_year.get(str(y)) or {}
        if any(rec.get(f) is None for f in req):
            return False
    return True


def deep_fetch(symbol: str, session=None, banking: bool = False,
               psx_html: Optional[str] = None) -> Dict:
    """
    Fetch → parse → persist official-filing data for one symbol.
    Respects a hard time/download budget. Returns the (possibly partial)
    store; never raises.
    """
    symbol = symbol.upper()
    session = session or utils.make_session()
    budget_s = CFG.get("fetch_budget_s", 150)
    max_pdfs = CFG.get("max_pdfs_per_symbol", 4)
    max_bytes = CFG.get("max_pdf_mb", 30) * 1024 * 1024
    started = time.time()

    store = load_store(symbol) or {
        "symbol": symbol, "by_year": {}, "provenance": {},
        "website": None, "documents": [], "complete": False,
    }

    # candidate documents: exchange-hosted filings first, then the website
    from scraper import find_report_links                    # lazy: no cycle
    docs: List[Dict] = []
    try:
        docs.extend(find_report_links(symbol, session) or [])
    except Exception as exc:  # noqa: BLE001
        print(f"  [deep] PSX report links failed for {symbol}: {exc}")

    website = store.get("website")
    if not website:
        website = discover_website(symbol, session, psx_html)
        store["website"] = website
    if website:
        try:
            docs.extend(crawl_for_pdfs(website, session))
        except Exception as exc:  # noqa: BLE001
            print(f"  [deep] website crawl failed for {symbol}: {exc}")

    seen_urls = {d.get("url") for d in store.get("documents", [])}
    docs = [d for d in docs if d.get("url") and d["url"] not in seen_urls]
    docs.sort(key=lambda r: (bool(re.search(r"annual", (r.get('title') or '') + r['url'], re.I)),
                             r.get("year") or 0), reverse=True)

    fetched = 0
    for doc in docs:
        if fetched >= max_pdfs or (time.time() - started) > budget_s:
            break
        if _coverage_ok(store["by_year"], banking):
            break
        print(f"  [deep] {symbol}: downloading {doc['url']}")
        try:
            raw = utils.fetch(doc["url"], session=session, expect_binary=True)
        except Exception:  # noqa: BLE001
            raw = None
        if not raw or len(raw) > max_bytes:
            continue
        fetched += 1
        by_year, prov = parse_pdf_multi_year(raw, doc.get("title") or "Annual report",
                                             doc["url"])
        n_new = 0
        for y, rec in by_year.items():
            ys = str(y)
            tgt = store["by_year"].setdefault(ys, {})
            ptg = store["provenance"].setdefault(ys, {})
            for f, v in rec.items():
                if tgt.get(f) is None:
                    tgt[f] = v
                    ptg[f] = prov.get(y, {}).get(f)
                    n_new += 1
        store["documents"].append({"title": doc.get("title"), "url": doc["url"],
                                   "year": doc.get("year"), "fields_added": n_new})
        print(f"  [deep] {symbol}: +{n_new} figures from {doc.get('title')!r}")
        save_store(symbol, store)
        time.sleep(CFG.get("request_delay_s", 1.5))

    store["complete"] = _coverage_ok(store["by_year"], banking)
    save_store(symbol, store)
    return store


# ---------------------------------------------------------------------------
# 5. Merge into a scrape — MISSING FIELDS ONLY, with provenance
# ---------------------------------------------------------------------------

def _banking(profile: Dict) -> bool:
    s = (profile or {}).get("sector", "").upper()
    return any(k in s for k in config.FINANCIAL_SECTOR_KEYWORDS)


def _missing_fields(financials: List[Dict], banking: bool) -> int:
    req = REQUIRED_FIELDS + (BANKING_EXTRA if banking else [])
    recent = sorted(financials, key=lambda r: r.get("year") or 0)[-2:]
    if len(recent) < 2:
        return len(req) * 2
    return sum(1 for r in recent for f in req if r.get(f) is None)


def fill_gaps(symbol: str, financials: List[Dict], profile: Dict,
              session=None, allow_fetch: bool = True,
              psx_html: Optional[str] = None) -> Dict:
    """
    Called from scraper.scrape_company(). Uses the persistent store (running
    a live deep fetch if enabled and needed) to fill ONLY missing fields in
    the financial records, each stamped with its document + URL + page.
    """
    info = {"filled": 0, "documents": [], "website": None}
    if not CFG.get("enabled", True):
        return info
    banking = _banking(profile)
    gaps = _missing_fields(financials, banking)
    store = load_store(symbol)

    if gaps > 0 and allow_fetch and (not _is_fresh(store) or
                                     (store and not store.get("complete"))):
        try:
            store = deep_fetch(symbol, session=session, banking=banking,
                               psx_html=psx_html)
        except Exception as exc:  # noqa: BLE001
            print(f"  [deep] fetch failed for {symbol}: {exc}")
    if not store:
        return info

    info["website"] = store.get("website")
    by_year = store.get("by_year") or {}
    prov = store.get("provenance") or {}
    recs = {r.get("year"): r for r in financials if r.get("year") is not None}

    for ys, fields in by_year.items():
        try:
            y = int(ys)
        except Exception:  # noqa: BLE001
            continue
        rec = recs.get(y)
        if rec is None:
            rec = {"year": y, "_sources": {}}
            financials.append(rec)
            recs[y] = rec
        for f, v in fields.items():
            if rec.get(f) is not None or v is None:
                continue
            p = (prov.get(ys) or {}).get(f) or {}
            rec[f] = v
            rec.setdefault("_sources", {})[f] = \
                p.get("label") or "official annual report"
            rec.setdefault("_source_urls", {})[f] = p.get("url")
            info["filled"] += 1
    financials.sort(key=lambda r: r.get("year") or 0)
    info["documents"] = store.get("documents", [])[-5:]
    with _lock:
        _status["stored"] = _count_stored()
    return info


def _count_stored() -> int:
    try:
        return len([f for f in os.listdir(STORE_DIR) if f.endswith(".json")])
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# 6. Background pre-warm across the whole PSX universe
# ---------------------------------------------------------------------------

def _prewarm_state_path() -> str:
    return os.path.join(STORE_DIR, "_prewarm_state.json")


def prewarm_worker(symbols: List[str]) -> None:
    delay = CFG.get("prewarm_delay_s", 25)
    os.makedirs(STORE_DIR, exist_ok=True)
    try:
        with open(_prewarm_state_path(), encoding="utf-8") as fh:
            done = set(json.load(fh).get("done", []))
    except Exception:  # noqa: BLE001
        done = set()

    with _lock:
        _status.update(prewarm_running=True, prewarm_total=len(symbols),
                       prewarm_done=len(done), stored=_count_stored())
    session = utils.make_session()
    for sym in symbols:
        if sym in done:
            continue
        store = load_store(sym)
        if _is_fresh(store) and store.get("complete"):
            done.add(sym)
        else:
            try:
                print(f"[deep-prewarm] {sym} …")
                deep_fetch(sym, session=session)
            except Exception as exc:  # noqa: BLE001
                print(f"[deep-prewarm] {sym} failed: {exc}")
            done.add(sym)
            time.sleep(delay)
        with _lock:
            _status.update(prewarm_done=len(done), last_symbol=sym,
                           stored=_count_stored())
        try:
            with open(_prewarm_state_path(), "w", encoding="utf-8") as fh:
                json.dump({"done": sorted(done),
                           "updated_at": datetime.now(timezone.utc).isoformat()}, fh)
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        _status["prewarm_running"] = False
    print(f"[deep-prewarm] pass complete: {len(done)}/{len(symbols)} symbols")


def start_prewarm(symbols: List[str]) -> None:
    if not CFG.get("enabled", True) or not CFG.get("prewarm", True):
        return
    if not symbols:
        return
    t = threading.Thread(target=prewarm_worker, args=(symbols,), daemon=True)
    t.start()


def status() -> Dict:
    with _lock:
        s = dict(_status)
    s["stored"] = _count_stored()
    s["store_dir"] = STORE_DIR
    return s


# ---------------------------------------------------------------------------
# CLI: python deepdata.py SYMBOL  → deep fetch one symbol and print the store
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sym = (sys.argv[1] if len(sys.argv) > 1 else "OGDC").upper()
    s = deep_fetch(sym)
    print(json.dumps({k: v for k, v in s.items() if k != "provenance"},
                     indent=2)[:4000])
