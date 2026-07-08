"""
catalyst.py
===========
v3.6 — the CATALYST engine.

"Technical tells you WHEN. The catalyst tells you WHY."  Modeled on the
Bulls-&-Bears stock-selection method: after the chart shows an accumulation
phase, the second question is always *what news is going to move this thing?*
— an expansion, a new plant becoming operational, a joint venture, a big
contract, a demerger, insiders buying...

This module pulls the company's REAL material information / announcements
from the PSX Data Portal (dps.psx.com.pk), classifies each one with simple
keyword rules, and returns a JSON payload the dashboard can render — with a
CLICKABLE LINK for every single item so the user can open the actual filing
and read it themselves.

STRICT HONESTY RULES (do not soften these when editing):
  * Only announcements actually found on PSX are ever returned. Nothing is
    invented, estimated or "filled in".
  * If PSX cannot be reached, the payload says so ({"checked": false}) and
    the dashboard shows an honest message + a link the user can click.
  * If the company simply has no growth announcements, "has_catalyst" is
    false and the dashboard shows a single friendly line instead of noise.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config
import utils

# v3.6.1 — read settings defensively: even if an out-of-date config.py is on
# disk (partial GitHub update), the catalyst scan must still run, never crash.
_PSX_BASE = getattr(config, "PSX_BASE", "https://dps.psx.com.pk")
_ANN_URL = getattr(config, "PSX_ANNOUNCEMENTS_URL",
                   f"{_PSX_BASE}/announcements/companies")
_COMPANY_URL = getattr(config, "PSX_COMPANY_URL",
                       f"{_PSX_BASE}/company/{{symbol}}")
_MONTHS = int(getattr(config, "CATALYST_MONTHS", 12))
_MAX_ITEMS = int(getattr(config, "CATALYST_MAX_ITEMS", 30))

# ---------------------------------------------------------------------------
# Classification rules — (category key, human label, impact, weight, patterns)
# Order matters: the FIRST matching rule wins.
# ---------------------------------------------------------------------------
_RULES = [
    # -------- strong positive growth catalysts (the video's checklist) -----
    ("operational", "Plant / project now OPERATIONAL", "positive", 14, [
        r"commercial operation", r"\bcommission", r"commenced (commercial )?operations?",
        r"\bCOD\b", r"successfully (started|commenced)", r"(plant|project|facility).{0,40}operational",
        r"start of (commercial )?production", r"achiev\w+ (COD|commercial operations?)",
    ]),
    ("expansion", "Expansion / new plant / new capacity", "positive", 13, [
        r"\bexpansion\b", r"new (plant|line|unit|facility|factory|mill)",
        r"capacity (enhancement|expansion|addition|increase)", r"\bBMR\b",
        r"setting up .{0,50}(plant|project|facility|unit)",
        r"solar (power|energy|plant)", r"captive power", r"debottleneck",
        r"(installation|establishment) of .{0,40}(plant|unit|project)",
        r"storage (facility|terminal|plant)",
    ]),
    ("venture", "Joint venture / partnership / acquisition", "positive", 13, [
        r"joint venture", r"\bJV\b", r"memorandum of understanding", r"\bMOU\b",
        r"(agreement|partnership|collaboration|alliance) with",
        r"incorporation of (a )?(new )?(subsidiary|company)",
        r"\bacquisition\b", r"acquir\w+ ", r"equity (investment|stake|participation) in",
        r"investment in .{0,50}(company|project|subsidiary|venture)",
        r"shareholders? agreement", r"strategic (partner|investment)",
    ]),
    ("contract", "New contract / order won", "positive", 12, [
        r"contract (award|awarded|signed|secured|win|won)",
        r"award of (a )?contract", r"letter of (intent|award)", r"\bLOI\b",
        r"(supply|purchase|work|export) order", r"secures? .{0,40}(contract|order|project)",
        r"wins? .{0,40}(contract|order|tender|bid)", r"successful bidder",
    ]),
    ("restructuring", "Demerger / restructuring / scheme", "positive", 12, [
        r"de-?merger", r"scheme of (arrangement|amalgamation|compromise)",
        r"restructur", r"spin-?off", r"\bamalgamation\b",
        r"reorgani[sz]ation", r"conversion of .{0,40}(company|status)",
    ]),
    ("insider", "Insiders / sponsors buying, or buy-back", "positive", 11, [
        r"buy.?back", r"purchase of (its own )?shares",
        r"(purchase|buying|acquisition) of shares by (the )?(director|sponsor|CEO|chief|executive)",
        r"trading in (the )?shares? .{0,40}by (director|sponsor|executive|employee)",
    ]),
    ("diversification", "New venture / product / business line", "positive", 10, [
        r"diversification", r"new (business|venture|segment|product|brand)",
        r"launch of ", r"entering into .{0,40}(business|market|sector)",
        r"commencement of .{0,40}(business|project)",
    ]),
    ("rights", "Right shares (raising money to grow)", "positive", 8, [
        r"right (shares?|issue)", r"issuance of right", r"rights? offering",
    ]),
    ("payout", "Dividend / bonus announced", "positive", 4, [
        r"\bdividend\b", r"bonus (shares?|issue)", r"\bpayout\b",
    ]),
    # -------- clear negatives ---------------------------------------------
    ("shutdown", "Plant shutdown / operations suspended", "negative", -15, [
        r"shut ?down", r"closure of ", r"suspension of (operations|production|plant)",
        r"(temporarily|partially) (closed|suspended|halted)",
        r"discontinu\w+ (of )?(operations|production|business)",
        r"winding.?up", r"\bdelist", r"\bdefault(ed)?\b",
    ]),
    # -------- routine filings (shown, but not treated as catalysts) --------
    ("routine", "Routine filing", "routine", 0, [
        r"board meeting", r"financial results?", r"transmission of",
        r"annual (general meeting|report)", r"\bAGM\b", r"\bEOGM\b",
        r"extraordinary general meeting", r"analyst briefing", r"corporate briefing",
        r"credit rating", r"book closure", r"election of directors",
        r"(change|appointment|resignation|casual vacancy) of (director|chief|CFO|CEO|auditor|company secretary)",
        r"code of conduct", r"free float", r"pattern of shareholding",
        r"quarterly (accounts|report)", r"half year", r"gate ?pass",
    ]),
]

_POSITIVE_TONE = re.compile(r"pleased to (announce|inform|share)|alhamdulillah", re.I)

# ---------------------------------------------------------------------------
# v3.7 — INSIDER TRANSACTIONS
# ---------------------------------------------------------------------------
# PSX-listed companies must file notices when directors, sponsors, CXOs,
# executives or substantial shareholders trade the company's shares (e.g.
# "Transaction of 4 or more shares by Directors, CEO, ...", buy-backs, etc).
# We detect those filings, read the direction where the title states it, and
# form an honest verdict. Every item keeps its clickable PSX filing link.

_INSIDER_HIT_RE = re.compile(
    r"director|sponsor|chief executive|\bCEO\b|\bCFO\b|\bCOO\b|\bCIO\b|"
    r"executive(?!\s+summary)|company secretary|substantial shareholder|"
    r"major shareholder|head of|key management|insider|buy.?back|treasury shares|"
    r"transactions? (of|in) .{0,20}shares?|trading (in|of) shares?|"
    r"(purchase|sale|disposal|acquisition) of shares by", re.I)

_INSIDER_BUY_RE = re.compile(
    r"\b(purchase[ds]?|purchasing|bought|buy(?!.?back)(ing)?|acquisition|"
    r"acquir\w+|subscri\w+|buy.?back)\b", re.I)
_INSIDER_SELL_RE = re.compile(
    r"\b(sale|sold|sell(ing)?|dispos\w+|divest\w+|offload\w+)\b", re.I)


def _insider_direction(title: str) -> str:
    """'buy' | 'sell' | 'mixed' | 'undisclosed' from a filing title.
    Most standard PSD notices don't state the direction in the title —
    we NEVER guess; those are honestly labelled 'undisclosed'."""
    b = bool(_INSIDER_BUY_RE.search(title))
    s = bool(_INSIDER_SELL_RE.search(title))
    if b and s:
        return "mixed"
    if b:
        return "buy"
    if s:
        return "sell"
    return "undisclosed"


def build_insider_block(items: List[Dict]) -> Dict:
    """Filter announcement items down to insider trades + form a verdict."""
    ins = []
    for it in items:
        title = it.get("title", "")
        if not _INSIDER_HIT_RE.search(title):
            continue
        ins.append({
            "date": it.get("date"),
            "title": title,
            "direction": _insider_direction(title),
            "urls": it.get("urls", []),
        })
    buys = sum(1 for i in ins if i["direction"] == "buy")
    sells = sum(1 for i in ins if i["direction"] == "sell")
    mixed = sum(1 for i in ins if i["direction"] == "mixed")
    undis = sum(1 for i in ins if i["direction"] == "undisclosed")

    if not ins:
        signal, note = "none", (
            f"PSX was scanned live — no insider share transactions were filed "
            f"by directors, sponsors or executives in the last {_MONTHS} "
            "months. Use the link to see for yourself.")
    elif buys and not sells:
        signal, note = "good", (
            f"{buys} filing{'s' if buys > 1 else ''} show insiders BUYING their "
            "own company's shares. People with the best information putting "
            "personal money in is historically one of the strongest quiet "
            "bullish signals — open each filing to see who and how much.")
    elif sells and not buys:
        signal, note = "bad", (
            f"{sells} filing{'s' if sells > 1 else ''} show insiders SELLING "
            "shares. Not always sinister (tax, personal needs), but repeated "
            "insider selling — especially near price highs — deserves real "
            "caution. Open each filing before trusting the rally.")
    elif buys and sells:
        signal, note = "mixed", (
            f"Insiders both bought ({buys}) and sold ({sells}) in the window — "
            "no clear signal either way. Open the filings and check who did "
            "what, and at what size.")
    else:
        signal, note = "activity", (
            f"{len(ins)} insider share-transaction filing"
            f"{'s' if len(ins) > 1 else ''} found, but the notice titles do "
            "not state the direction. Nothing is assumed — open the filings "
            "(one click) to see whether they bought or sold.")

    return {"items": ins, "buys": buys, "sells": sells, "mixed": mixed,
            "undisclosed": undis, "signal": signal, "note": note}


_RULES_COMPILED = [
    (key, label, impact, weight, [re.compile(p, re.I) for p in pats])
    for key, label, impact, weight, pats in _RULES
]

# tolerant date parsing — PSX shows dates in several formats
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d-%m-%Y", "%d/%m/%Y",
                 "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d %Y")
_DATE_HINT = re.compile(
    r"(\d{1,2}[-/ ](?:\d{1,2}|[A-Za-z]{3,9})[-/ ]\d{2,4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})")


def _parse_date(text: str) -> Optional[str]:
    """Return ISO date (YYYY-MM-DD) from a messy cell, or None."""
    if not text:
        return None
    m = _DATE_HINT.search(text.strip())
    if not m:
        return None
    raw = m.group(1).replace(",", ", ").replace("  ", " ").strip()
    raw = re.sub(r"\s+", " ", raw)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # last resort: dd-Mon-yy style
    try:
        return datetime.strptime(raw, "%d-%b-%y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def classify(title: str) -> Dict:
    """Classify one announcement title. Pure function — also mirrored in JS."""
    t = (title or "").strip()
    for key, label, impact, weight, pats in _RULES_COMPILED:
        for p in pats:
            if p.search(t):
                out = {"category": key, "label": label,
                       "impact": impact, "weight": weight}
                if impact == "routine" and _POSITIVE_TONE.search(t):
                    # a "pleased to announce" inside a routine bucket usually
                    # hides good news — bump it to a mild positive
                    out = {"category": "goodnews", "impact": "positive",
                           "weight": 6, "label": "Company sounds pleased — read it"}
                return out
    if _POSITIVE_TONE.search(t):
        return {"category": "goodnews", "label": "Company sounds pleased — read it",
                "impact": "positive", "weight": 6}
    return {"category": "other", "label": "Material information",
            "impact": "routine", "weight": 0}


# ---------------------------------------------------------------------------
# scraping
# ---------------------------------------------------------------------------

def _rows_from_company_page(html: str, base_url: str) -> List[Dict]:
    """Announcement rows embedded in the dps company page."""
    items: List[Dict] = []
    if not html:
        return items
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            date_iso = _parse_date(cells[0].get_text(" ", strip=True))
            if not date_iso:
                continue
            # title = the longest text cell after the date
            texts = [c.get_text(" ", strip=True) for c in cells[1:]]
            texts = [t for t in texts if t and not _parse_date(t)]
            if not texts:
                continue
            title = max(texts, key=len)
            if len(title) < 6:
                continue
            urls = []
            for a in row.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "javascript")):
                    continue
                full = urljoin(base_url, href)
                lab = ("Open filing (PDF)" if ".pdf" in full.lower()
                       or "/download/" in full else "Open on PSX")
                if full not in [u["url"] for u in urls]:
                    urls.append({"label": lab, "url": full})
            items.append({"date": date_iso, "title": title, "urls": urls})
    return items


def _rows_from_announcements_page(symbol: str, session) -> List[Dict]:
    """Fallback: the portal-wide company announcements page, filtered."""
    items: List[Dict] = []
    try:
        html = utils.fetch(_ANN_URL, session=session)
    except Exception:  # noqa: BLE001
        return items
    if not html:
        return items
    soup = BeautifulSoup(html, "lxml")
    sym = symbol.upper()
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        cell_texts = [c.get_text(" ", strip=True) for c in cells]
        if not any(t.upper() == sym for t in cell_texts):
            continue
        date_iso = None
        for t in cell_texts:
            date_iso = _parse_date(t)
            if date_iso:
                break
        if not date_iso:
            continue
        cand = [t for t in cell_texts
                if t.upper() != sym and not _parse_date(t) and len(t) > 8]
        if not cand:
            continue
        title = max(cand, key=len)
        urls = []
        for a in row.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript")):
                continue
            full = urljoin(_PSX_BASE, href)
            lab = ("Open filing (PDF)" if ".pdf" in full.lower()
                   or "/download/" in full else "Open on PSX")
            if full not in [u["url"] for u in urls]:
                urls.append({"label": lab, "url": full})
        items.append({"date": date_iso, "title": title, "urls": urls})
    return items


def fetch_catalysts(symbol: str, session=None, page_html: str = None) -> Dict:
    """
    Main entry. Returns:
    {
      "checked": bool,          # False => PSX could not be scanned (be honest)
      "error": str|None,
      "as_of": iso,
      "window_months": 12,
      "source_url": company announcements URL (always clickable),
      "items":     [ every classified announcement in the window ],
      "catalysts": [ the positive growth ones ],
      "negatives": [ the clearly bad ones ],
      "routine_count": int,
      "has_catalyst": bool,
      "score": 0-100,           # 50 = neutral fuel gauge
      "summary": one plain-English line for the dashboard
    }
    """
    symbol = symbol.strip().upper()
    company_url = _COMPANY_URL.format(symbol=symbol)
    out: Dict = {
        "checked": False, "error": None,
        "as_of": utils.now_iso(),
        "window_months": _MONTHS,
        "source_url": company_url,
        "announcements_url": _ANN_URL,
        "items": [], "catalysts": [], "negatives": [],
        "insiders": {"items": [], "buys": 0, "sells": 0, "mixed": 0,
                     "undisclosed": 0, "signal": "unknown", "note": ""},
        "routine_count": 0, "has_catalyst": False,
        "score": 50, "summary": "",
    }
    try:
        session = session or utils.make_session()
        if page_html is None:
            page_html = utils.fetch(company_url, session=session)
        raw = _rows_from_company_page(page_html, company_url)
        if not raw:
            raw = _rows_from_announcements_page(symbol, session)
        out["checked"] = True

        # de-duplicate + window filter + classify
        cutoff = (datetime.utcnow()
                  - timedelta(days=30.5 * _MONTHS)).strftime("%Y-%m-%d")
        seen = set()
        items: List[Dict] = []
        for it in raw:
            key = (it["date"], it["title"][:80].lower())
            if key in seen:
                continue
            seen.add(key)
            if it["date"] < cutoff:
                continue
            cls = classify(it["title"])
            item = {**it, **cls}
            if not item["urls"]:
                # every item must stay verifiable → link to the source page
                item["urls"] = [{"label": "View on PSX", "url": company_url}]
            items.append(item)

        items.sort(key=lambda x: x["date"], reverse=True)
        items = items[:_MAX_ITEMS]
        out["items"] = items
        out["insiders"] = build_insider_block(items)   # v3.7 insider verdict
        out["catalysts"] = [i for i in items if i["impact"] == "positive"
                            and i["category"] != "payout"]
        out["negatives"] = [i for i in items if i["impact"] == "negative"]
        out["routine_count"] = sum(1 for i in items if i["impact"] == "routine")
        payouts = [i for i in items if i["category"] == "payout"]
        out["has_catalyst"] = bool(out["catalysts"])

        # ---- fuel-gauge score (0-100, 50 = nothing either way) ------------
        score = 50.0
        if not items:
            score = 45.0
        for i in out["catalysts"]:
            score += i["weight"]
        for i in payouts:
            score += i["weight"]
        for i in out["negatives"]:
            score += i["weight"]          # weights are negative already
        out["score"] = round(max(10.0, min(95.0, score)), 1)

        # ---- the one-liner -------------------------------------------------
        n = len(out["catalysts"])
        if out["negatives"]:
            out["summary"] = ("PSX filings show a red flag — open the linked "
                              "announcement before doing anything else.")
        elif n >= 2:
            out["summary"] = (f"{n} real growth announcements on PSX in the last "
                              f"{_MONTHS} months — the rally has "
                              "actual fuel behind it, not just hope.")
        elif n == 1:
            out["summary"] = ("One real growth announcement on PSX — a spark "
                              "exists; watch whether the company keeps delivering on it.")
        elif items:
            out["summary"] = (f"PSX was scanned live — {len(items)} filing"
                              f"{'s' if len(items) > 1 else ''} found in the last "
                              f"{_MONTHS} months, but all routine (board meetings, "
                              "results, reports). No catalyst to jump-start a new "
                              "rally or growth spurt — the chart is running on "
                              "momentum and mood alone.")
        else:
            out["summary"] = (f"PSX was scanned live — no announcements were found "
                              f"filed by this company in the last {_MONTHS} months. "
                              "No catalyst to jump-start a new rally or growth "
                              "spurt; use the link to see for yourself.")
        return out
    except Exception as exc:  # noqa: BLE001
        out["checked"] = False
        out["error"] = str(exc)
        out["summary"] = ("PSX announcements could not be scanned this time — "
                          "use the link to check them yourself.")
        return out


if __name__ == "__main__":
    import json, sys  # noqa: E401
    sym = sys.argv[1] if len(sys.argv) > 1 else "OGDC"
    print(json.dumps(fetch_catalysts(sym), indent=2)[:4000])
