"""
about.py
========
v4.1 — the "About this company" engine.

Builds the About block shown in the Fundamentals tab's Company Profile area:

    * Company name IN FULL      (e.g. "Lucky Cement Limited", not "Lucky Cement")
    * The company's PSX category (the exchange's own sector name)
    * A short plain-language description of what the business actually does
    * 3–5 of its top products / lines of business, where a source names them

ABSOLUTE RULE — NOTHING IS INVENTED
-----------------------------------
Every field is lifted verbatim (or trimmed) from a real, citable page:

    1. dps.psx.com.pk company page      — the exchange's own profile
    2. stockanalysis.com company page   — S&P Global sourced description
    3. the issuer's OWN official website — discovered from its PSX page

If none of those publish a description, the tool says so plainly rather than
writing a plausible-sounding sentence. Products are only listed when a source
*enumerates* them; a sector name is never turned into a guessed product list.

Every returned field carries the URL it came from so the dashboard can render a
clickable "verify this" link next to it, exactly like every other metric.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

import config
import utils

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
CACHE_HOURS = 24 * 30          # company descriptions change very rarely
MIN_DESC_CHARS = 60            # shorter than this is a label, not a description
MAX_DESC_CHARS = 900           # keep the card readable
MAX_PRODUCTS = 5
MIN_PRODUCTS = 3               # target; fewer is fine if the source lists fewer

_SA_PROFILE_URL = "https://stockanalysis.com/quote/psx/{symbol}/company/"

_CACHE_SUB = "about"


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def _cache_dir() -> str:
    return os.path.join(config.CACHE_DIR, _CACHE_SUB)


def _cache_path(symbol: str) -> str:
    return os.path.join(_cache_dir(), f"{symbol.upper()}.json")


def _cache_load(symbol: str) -> Optional[Dict]:
    try:
        path = _cache_path(symbol)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        if time.time() - float(blob.get("saved_at", 0)) > CACHE_HOURS * 3600:
            return None
        payload = blob.get("payload")
        # Never serve a cached "nothing found" forever — retry those sooner.
        if payload and not payload.get("description"):
            if time.time() - float(blob.get("saved_at", 0)) > 24 * 3600:
                return None
        return payload
    except Exception:  # noqa: BLE001
        return None


def _cache_save(symbol: str, payload: Dict) -> None:
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(_cache_path(symbol), "w", encoding="utf-8") as fh:
            json.dump({"saved_at": time.time(), "payload": payload}, fh)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return _WS.sub(" ", text).strip()


def _trim_to_sentence(text: str, limit: int = MAX_DESC_CHARS) -> str:
    """Cut to `limit` chars without slicing a sentence in half."""
    text = _clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "; "):
        idx = cut.rfind(stop)
        if idx > limit * 0.55:
            return cut[: idx + 1].strip()
    idx = cut.rfind(" ")
    return (cut[:idx] if idx > 0 else cut).rstrip(" ,;:") + "…"


# Boilerplate that shows up on exchange/aggregator pages but says nothing about
# the business. If a candidate description is mostly one of these, reject it.
_JUNK_PATTERNS = (
    r"^\s*(home|profile|company|about|overview|financials?|announcements?)\s*$",
    r"cookie", r"javascript", r"enable\s+js", r"all\s+rights\s+reserved",
    r"terms\s+(of|and)\s+", r"privacy\s+policy", r"sign\s*(in|up)\b",
    r"^\s*loading", r"click\s+here", r"page\s+not\s+found",
    r"advertis", r"subscribe", r"newsletter",
)


def _looks_like_junk(text: str) -> bool:
    if not text or len(text) < MIN_DESC_CHARS:
        return True
    low = text.lower()
    for pat in _JUNK_PATTERNS:
        if re.search(pat, low):
            return True
    # A real description is prose: it should contain a verb-ish structure.
    if not re.search(r"\b(is|are|was|were|has|have|operates?|engaged|manufactur|"
                     r"produces?|provides?|offers?|sells?|markets?|distribut|"
                     r"supplies|deals?|involved|principally|primarily|through|"
                     r"together with|subsidiar)\b", low):
        return True
    return False


def _first_good(texts: List[str]) -> Optional[str]:
    for t in texts:
        t = _clean(t)
        if not _looks_like_junk(t):
            return t
    return None


# ---------------------------------------------------------------------------
# 1) Company name in full
# ---------------------------------------------------------------------------
_SUFFIX_RE = re.compile(
    r"\b(limited|ltd\.?|corporation|corp\.?|company|co\.?|incorporated|inc\.?|"
    r"\(pvt\)|private|plc)\b", re.I)


def _pick_fullest_name(candidates: List[str]) -> Optional[str]:
    """Prefer the most complete legal name (the one carrying a suffix)."""
    cleaned = []
    for c in candidates:
        c = _clean(c or "")
        # Page <title> tags append site furniture — cut everything from the
        # first of those markers onwards.
        c = re.split(r"\s*[|·]\s*|\s+[-–—]\s+(?=(?:PSX|Stock|Share|Quote|"
                     r"Financials|Company|Pakistan\s+Stock)\b)", c)[0]
        c = re.sub(r"\s*\b(stock|share)\s+(price|quote)\b.*$", "", c, flags=re.I)
        c = re.sub(r"\s*\b(quote|overview|profile|financials?|summary)\b\s*$",
                   "", c, flags=re.I)
        # strip a ticker echo anywhere: "LUCK - ", " (LUCK)", " [LUCK]"
        c = re.sub(r"^[A-Z0-9]{2,10}\s*[-–—:|]\s*", "", c)
        c = re.sub(r"\s*[\(\[][A-Z0-9]{2,10}[\)\]]", " ", c)
        c = _clean(c).strip(" -–—:|,")
        if 2 < len(c) <= 120:
            cleaned.append(c)
    if not cleaned:
        return None
    with_suffix = [c for c in cleaned if _SUFFIX_RE.search(c)]
    pool = with_suffix or cleaned
    # longest wins — "Lucky Cement Limited" beats "Lucky Cement"
    return max(pool, key=len)


def _names_from_psx(soup: BeautifulSoup) -> List[str]:
    out = []
    for finder in (
        lambda: soup.find(class_=re.compile(r"company.*name", re.I)),
        lambda: soup.find(class_=re.compile(r"quote.*name|name.*quote", re.I)),
        lambda: soup.find("h1"),
        lambda: soup.find("h2"),
    ):
        try:
            el = finder()
            if el:
                out.append(el.get_text(" ", strip=True))
        except Exception:  # noqa: BLE001
            continue
    try:
        if soup.title:
            out.append(soup.title.get_text(" ", strip=True))
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# 2) Description — PSX company page
# ---------------------------------------------------------------------------
def _desc_from_psx(soup: BeautifulSoup) -> Optional[str]:
    """The PSX company page carries a business-description block on its
    Profile tab. Layouts have changed over the years, so try several shapes."""
    cands: List[str] = []

    # (a) an element explicitly labelled as the business/company description
    for pat in (r"business.*(desc|profile|nature)", r"company.*(desc|profile)",
                r"about.*company", r"nature.*business", r"\bprofile\b"):
        for el in soup.find_all(class_=re.compile(pat, re.I)):
            cands.append(el.get_text(" ", strip=True))
        for el in soup.find_all(id=re.compile(pat, re.I)):
            cands.append(el.get_text(" ", strip=True))

    # (b) a heading whose sibling block holds the prose
    for head in soup.find_all(re.compile(r"^h[2-5]$")):
        label = _clean(head.get_text(" ", strip=True)).lower()
        if re.search(r"(business|company)\s+(description|profile)|about", label):
            sib = head.find_next_sibling()
            hops = 0
            while sib is not None and hops < 3:
                cands.append(sib.get_text(" ", strip=True))
                sib = sib.find_next_sibling()
                hops += 1

    # (c) a definition/table row whose label mentions the business
    for cell in soup.find_all(["th", "td", "dt"]):
        label = _clean(cell.get_text(" ", strip=True)).lower()
        if re.search(r"business|nature of|principal activit", label):
            nxt = cell.find_next_sibling(["td", "dd"])
            if nxt:
                cands.append(nxt.get_text(" ", strip=True))

    # (d) meta description as a last resort
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        cands.append(meta["content"])

    return _first_good(cands)


# ---------------------------------------------------------------------------
# 3) Description — StockAnalysis company profile (S&P Global)
# ---------------------------------------------------------------------------
def _desc_from_stockanalysis(symbol: str, session) -> Optional[str]:
    url = _SA_PROFILE_URL.format(symbol=symbol)
    html = utils.fetch(url, session=session)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    cands: List[str] = []

    # StockAnalysis renders the profile prose inside the main column; the
    # longest paragraph on the page is reliably the business description.
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) >= MIN_DESC_CHARS:
            cands.append(txt)
    cands.sort(key=len, reverse=True)

    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        cands.append(meta["content"])

    return _first_good(cands)


# ---------------------------------------------------------------------------
# 4) Description — the issuer's own website
# ---------------------------------------------------------------------------
def _desc_from_website(website: str, session) -> Optional[str]:
    if not website:
        return None
    html = utils.fetch(website, session=session)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    cands: List[str] = []

    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        cands.append(meta["content"])
    og = soup.find("meta", attrs={"property": re.compile("og:description", re.I)})
    if og and og.get("content"):
        cands.append(og["content"])

    for el in soup.find_all(class_=re.compile(r"about|intro|overview", re.I)):
        cands.append(el.get_text(" ", strip=True))
    for p in sorted(soup.find_all("p"),
                    key=lambda x: len(x.get_text(strip=True)), reverse=True)[:6]:
        cands.append(p.get_text(" ", strip=True))

    return _first_good(cands)


# ---------------------------------------------------------------------------
# 5) Products — extracted ONLY where a source enumerates them
# ---------------------------------------------------------------------------
# Phrases that introduce an explicit product/brand/segment list.
_LIST_LEADS = (
    r"products?\s+include[sd]?",
    r"product\s+(?:range|portfolio|lines?)\s+(?:includes?|comprises?|consists?\s+of)",
    r"brands?\s+(?:include[sd]?|such\s+as|namely)",
    r"under\s+the\s+brand\s+names?",
    r"markets?\s+(?:its\s+products?\s+)?under\s+the\s+brand[s]?",
    r"offers?",
    r"manufactur(?:es|ing)\s+(?:and\s+sell[s]?\s+)?",
    r"produces?",
    r"sells?",
    r"provides?",
    r"engaged\s+in\s+the\s+(?:manufactur\w*|production|sale)\s+of",
    r"principally\s+engaged\s+in\s+the\s+\w+\s+of",
    r"operates?\s+(?:through|in)\s+(?:the\s+following\s+)?segments?",
    r"segments?\s*(?:include[sd]?|:)",
    r"business\s+(?:lines?|divisions?)\s+(?:include[sd]?|:)",
    r"deals?\s+in",
    r"supplies",
)

_LEAD_RE = re.compile(r"(?:%s)" % "|".join(_LIST_LEADS), re.I)

# Words/phrases that are categories of thing, not products
_NOT_A_PRODUCT = re.compile(
    r"^(and|or|the|a|an|its|their|other|others|various|etc|more|company|"
    r"customers?|clients?|services?|solutions?|products?|markets?|pakistan|"
    r"world|group|business(?:es)?|operations?|subsidiar\w*|segment[s]?|"
    r"division[s]?|well as|addition|which|that|these|those|it|they|we|"
    r"(?:related|allied|associated|ancillary|similar|such)\s+\w+|"
    r"\w+\s+there(?:of|to)|names?)$", re.I)


def _split_list_phrase(phrase: str) -> List[str]:
    """Turn 'cement, clinker and ready-mix concrete' into three items."""
    phrase = _clean(phrase)
    # "…segments:" / "…include -" leave the separator at the front of the tail;
    # drop it so the list that follows is not thrown away.
    phrase = re.sub(r"^\s*[:\-–—]\s*", "", phrase)
    # Stop at the end of the clause. A sentence ends at ". " followed by a
    # capital (so decimals like "1.5 million" and "Pvt. Ltd." survive), or at
    # a semicolon / colon / dash.
    phrase = re.split(r"\.\s+(?=[A-Z0-9])|\.$|;|:|\s[—–]\s|\u2014|\u2013",
                      phrase)[0]
    # A leftover lead word the pattern could not swallow (e.g. "…brand names X")
    phrase = re.sub(r"^\s*names?\b\s*", "", phrase, flags=re.I)
    phrase = re.sub(r"\s+(?:in|to|for|across|throughout)\s+Pakistan\b.*$", "",
                    phrase, flags=re.I)
    parts = re.split(r",|\band\b|\bas well as\b|\bplus\b|/|\|", phrase, flags=re.I)
    out = []
    for p in parts:
        p = _clean(p).strip(" .;:-–—\u2022()[]\"'")
        # drop leading articles/possessives
        p = re.sub(r"^(?:the|a|an|its|their|our|various|other)\s+", "", p, flags=re.I)
        if not p or len(p) < 3 or len(p) > 60:
            continue
        if _NOT_A_PRODUCT.match(p):
            continue
        if p.count(" ") > 5:              # a whole clause, not a product
            continue
        # Title-case only ALL-CAPS shouty items; otherwise keep source casing
        if p.isupper() and len(p) > 4:
            p = p.title()
        out.append(p)
    return out


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen, out = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _products_from_text(text: str) -> List[str]:
    """Pull an explicit product list out of a description. Returns [] when the
    text does not actually enumerate products — we never guess."""
    if not text:
        return []
    found: List[str] = []
    for m in _LEAD_RE.finditer(text):
        tail = text[m.end():m.end() + 260]
        items = _split_list_phrase(tail)
        # A single generic word after "offers" is not a product list.
        if len(items) >= 2:
            found.extend(items)
        elif len(items) == 1 and len(items[0].split()) >= 2:
            found.extend(items)
        if len(found) >= MAX_PRODUCTS * 2:
            break
    return _dedupe_keep_order(found)[:MAX_PRODUCTS]


def _products_from_website(website: str, session) -> List[str]:
    """Read the issuer's own site navigation for a Products/Brands menu."""
    if not website:
        return []
    html = utils.fetch(website, session=session)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    items: List[str] = []

    for anchor in soup.find_all("a"):
        label = _clean(anchor.get_text(" ", strip=True))
        href = (anchor.get("href") or "").lower()
        if not re.search(r"product|brand|range|portfolio", href):
            continue
        if not label or len(label) < 3 or len(label) > 45:
            continue
        if _NOT_A_PRODUCT.match(label):
            continue
        if re.search(r"^(products?|our products?|brands?|all|view|more|home)$",
                     label, re.I):
            continue
        items.append(label)

    return _dedupe_keep_order(items)[:MAX_PRODUCTS]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def get_about(symbol: str, session=None, psx_html: str = None,
              allow_fetch: bool = True) -> Dict:
    """Build the About block for one company. Never raises."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {}

    cached = _cache_load(symbol)
    if cached:
        return cached

    psx_url = config.PSX_COMPANY_URL.format(symbol=symbol)
    out: Dict = {
        "symbol": symbol,
        "name": None,
        "category": None,
        "description": None,
        "products": [],
        "website": None,
        "sources": {},
        "notes": [],
        "generated_at": utils.now_iso(),
    }

    if session is None:
        try:
            session = utils.make_session()
        except Exception:  # noqa: BLE001
            session = None

    # ---- PSX company page -------------------------------------------------
    soup = None
    try:
        html = psx_html
        if html is None and allow_fetch:
            html = utils.fetch(psx_url, session=session)
        if html:
            soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = None

    name_candidates: List[str] = []
    if soup is not None:
        name_candidates.extend(_names_from_psx(soup))

    # ---- Authoritative name + category from the PSX symbols endpoint ------
    try:
        import psx_data
        uni = psx_data.get_universe()
        for row in (uni.get("symbols") or []):
            if (row.get("symbol") or "").upper() == symbol:
                if row.get("name"):
                    name_candidates.append(row["name"])
                if row.get("sector"):
                    out["category"] = row["sector"]
                    out["sources"]["category"] = {
                        "label": "PSX official symbol directory",
                        "url": config.PSX_SYMBOLS_URL,
                    }
                break
    except Exception:  # noqa: BLE001
        pass

    if not out["category"] and soup is not None:
        try:
            sector_el = soup.find(class_=re.compile("sector", re.I))
            if sector_el:
                sec = _clean(sector_el.get_text(" ", strip=True))
                if sec and len(sec) < 80:
                    out["category"] = sec.upper()
                    out["sources"]["category"] = {
                        "label": "PSX company page", "url": psx_url}
        except Exception:  # noqa: BLE001
            pass

    full_name = _pick_fullest_name(name_candidates)
    if full_name:
        out["name"] = full_name
        out["sources"]["name"] = {
            "label": "PSX official listing", "url": psx_url}

    # ---- Description: PSX → StockAnalysis → issuer website ---------------
    desc = None
    if soup is not None:
        desc = _desc_from_psx(soup)
        if desc:
            out["sources"]["description"] = {
                "label": "PSX company profile", "url": psx_url}

    if not desc and allow_fetch:
        try:
            desc = _desc_from_stockanalysis(symbol, session)
            if desc:
                out["sources"]["description"] = {
                    "label": "StockAnalysis company profile (S&P Global)",
                    "url": _SA_PROFILE_URL.format(symbol=symbol)}
        except Exception:  # noqa: BLE001
            pass

    # Issuer's own website — also the best source for a product list
    website = None
    if allow_fetch:
        try:
            import deepdata
            website = deepdata.discover_website(symbol, session, psx_html=psx_html)
        except Exception:  # noqa: BLE001
            website = None
    if website:
        out["website"] = website

    if not desc and website:
        try:
            desc = _desc_from_website(website, session)
            if desc:
                out["sources"]["description"] = {
                    "label": "Company's official website", "url": website}
        except Exception:  # noqa: BLE001
            pass

    if desc:
        out["description"] = _trim_to_sentence(desc)
    else:
        out["notes"].append(
            "No business description is published on this company's PSX page, "
            "its StockAnalysis profile, or its own website. Nothing has been "
            "written here in their place.")

    # ---- Products: from the sourced description, then the issuer's site ---
    products = _products_from_text(out["description"] or "")
    if products:
        out["sources"]["products"] = dict(
            out["sources"].get("description")
            or {"label": "PSX company profile", "url": psx_url})

    if len(products) < MIN_PRODUCTS and website and allow_fetch:
        try:
            extra = _products_from_website(website, session)
            if extra:
                merged = _dedupe_keep_order(products + extra)[:MAX_PRODUCTS]
                if len(merged) > len(products):
                    products = merged
                    out["sources"]["products"] = {
                        "label": "Company's official website", "url": website}
        except Exception:  # noqa: BLE001
            pass

    out["products"] = products
    if not products:
        out["notes"].append(
            "No source lists this company's products explicitly, so none are "
            "shown — the sector name alone is not evidence of a product line.")

    # Only persist a result that is actually complete. A light pass
    # (allow_fetch=False, used by the 100-company ranking scan) that found no
    # description must NOT be cached, or the individual click would keep
    # serving that empty answer for a month.
    if allow_fetch or out["description"]:
        _cache_save(symbol, out)
    return out


# ---------------------------------------------------------------------------
# CLI helper:  python about.py LUCK
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import sys
    sym = (sys.argv[1] if len(sys.argv) > 1 else "LUCK").upper()
    data = get_about(sym)
    print(json.dumps(data, indent=2, ensure_ascii=False))
