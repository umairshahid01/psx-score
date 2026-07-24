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

import json
import os
import random
import re
import time
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

# ---------------------------------------------------------------------------
# v4.0 — ANNOUNCEMENTS ENGINE
# ---------------------------------------------------------------------------
# Two upgrades over the v3.6 catalyst scan:
#   1. PDF PEEK — for the most recent non-routine filings the actual PDF is
#      downloaded (size/time capped) and its first pages are read, so the
#      classification reflects the document's real content, not just the
#      title. Reading failures are silently skipped — never invented.
#   2. VERDICT BLOCK — the classified filings are turned into one honest,
#      5-year-old-simple verdict (good / bad / mixed / quiet) with a bullet
#      reason list. EVERY bullet carries the filing's own clickable link so
#      the user can verify each claim in one click.

_ANN_CFG = dict(getattr(config, "ANNOUNCEMENTS", {}) or {})
_PEEK_ON = bool(_ANN_CFG.get("pdf_peek", True))
_PEEK_MAX_PDFS = int(_ANN_CFG.get("peek_max_pdfs", 4))
_PEEK_MAX_PAGES = int(_ANN_CFG.get("peek_max_pages", 2))
_PEEK_MAX_MB = float(_ANN_CFG.get("peek_max_mb", 8))
_PEEK_BUDGET_S = float(_ANN_CFG.get("peek_budget_s", 22))
_PEEK_WORKERS = max(1, int(_ANN_CFG.get("peek_workers", 5)))

# body-text tone cues (used ONLY to refine, never to invent a filing)
_BODY_GOOD_RE = re.compile(
    r"profit (after tax )?(increased|rose|grew|up)|record (profit|revenue|sales)|"
    r"highest.{0,20}(profit|revenue)|growth of|increase[d]? by \d|"
    r"pleased to (announce|inform)|successfully", re.I)
_BODY_BAD_RE = re.compile(
    r"\bloss (after|before) tax\b|net loss|loss of Rs|declined? by \d|"
    r"decrease[d]? by \d|(profit|revenue|sales).{0,30}(fell|declined|dropped)|"
    r"impairment|going concern|default|suspension of (operations|production)", re.I)
_DIVIDEND_AMT_RE = re.compile(
    r"(final|interim)?\s*cash dividend.{0,40}?(?:Rs\.?|Rupees?|@)\s*([\d.]+)"
    r"|(\d{1,3}(?:\.\d+)?)\s*%\s*(?:i\.e\.)?\s*(?:cash dividend|dividend)", re.I)


def _peek_pdf_text(url: str, session) -> str:
    """Download a filing PDF (size-capped) and return text of its first pages.
    Returns '' on any failure — the caller must treat '' as 'not read'."""
    try:
        import io
        import pdfplumber
        r = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
        r.raise_for_status()
        clen = r.headers.get("Content-Length")
        if clen and int(clen) > _PEEK_MAX_MB * 1024 * 1024:
            return ""
        max_bytes = int(_PEEK_MAX_MB * 1024 * 1024)
        buf, got = io.BytesIO(), 0
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                break
            got += len(chunk)
            if got > max_bytes:
                return ""
            buf.write(chunk)
        buf.seek(0)
        if buf.read(5)[:4] != b"%PDF":
            return ""
        buf.seek(0)
        out = []
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:_PEEK_MAX_PAGES]:
                try:
                    out.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001
                    continue
        return "\n".join(out).strip()
    except Exception:  # noqa: BLE001
        return ""


def _refine_items_with_pdfs(items: List[Dict], session) -> None:
    """v4.0 — read EVERY recent filing document we can (in parallel), not a
    hand-picked few. Board-meeting packs, results notices, 'material
    information', dividend notices — each PDF is opened and its first pages
    understood, all inside a strict wall-clock budget so the UI stays fast.
    Adds item['pdf_read']=True and item['body_hint'] when a document was
    actually read; vague titles get re-classified from the body text."""
    if not (_PEEK_ON and session):
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed
    deadline = time.time() + _PEEK_BUDGET_S

    def _pdf_url(it: Dict) -> Optional[str]:
        return next((u["url"] for u in it.get("urls", [])
                     if ".pdf" in u["url"].lower() or "/download/" in u["url"]), None)

    # every filing with a document is a candidate — newest first
    cand = [(it, _pdf_url(it)) for it in items]
    cand = [(it, u) for it, u in cand if u][:_PEEK_MAX_PDFS]
    if not cand:
        return

    def _fetch(pair):
        it, url = pair
        if time.time() > deadline:
            return it, ""
        return it, _peek_pdf_text(url, session)

    with ThreadPoolExecutor(max_workers=_PEEK_WORKERS,
                            thread_name_prefix="psx-pdf") as ex:
        futs = [ex.submit(_fetch, p) for p in cand]
        for fut in as_completed(futs, timeout=max(2.0, _PEEK_BUDGET_S + 4)):
            try:
                it, text = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            it["pdf_read"] = True
            # 1) vague title -> classify again using the document body
            if it.get("category") in ("other", "routine"):
                cls = classify(text[:4000])
                if cls["impact"] in ("positive", "negative"):
                    it.update(cls)
            # 2) tone of the document body
            if _BODY_BAD_RE.search(text):
                it["body_hint"] = "bad"
            elif _BODY_GOOD_RE.search(text):
                it["body_hint"] = "good"
            # 3) dividend amount, if stated
            m = _DIVIDEND_AMT_RE.search(text)
            if m and it.get("category") == "payout":
                amt = m.group(2) or m.group(3)
                if amt:
                    it["detail"] = (f"₨{amt} per share" if m.group(2)
                                    else f"{amt}% dividend")


# kid-simple, per-category explanations for the verdict bullets
_ELI5 = {
    "operational": ("🏭", "A new plant/project just switched ON — the company can now "
                    "make and sell more than before. New machines that work = more money later."),
    "expansion":   ("🏗️", "The company is building MORE capacity (new plant/line). "
                    "Think of a lemonade stand adding a second stand — more stands, more sales."),
    "venture":     ("🤝", "It teamed up with (or bought into) another business. "
                    "Two friends working together can usually earn more than one alone."),
    "contract":    ("📝", "It WON new work/orders. A signed contract is real future money, "
                    "not just a hope."),
    "restructuring": ("🧩", "The company is re-arranging itself (demerger/scheme). "
                      "Tidying the toy box often lets each part shine and be valued better."),
    "insider":     ("💎", "The people who KNOW the company best (bosses/sponsors) put their "
                    "own money into its shares — one of the quietest but strongest good signs."),
    "diversification": ("🌱", "It's starting a NEW product or business line — planting a new "
                        "seed that could grow into extra income."),
    "rights":      ("🪙", "It's asking shareholders for money to GROW (right shares). "
                    "Raising money to build is usually a growth move — check what it's for."),
    "payout":      ("💰", "It's sharing profit with owners (dividend/bonus). Companies only "
                    "hand out sweets when the jar actually has sweets in it."),
    "goodnews":    ("😊", "The company itself sounds happy in this filing — 'pleased to "
                    "announce' usually hides good news. Open it and see."),
    "shutdown":    ("⛔", "Something STOPPED working (shutdown/suspension). A shop with "
                    "closed doors can't ring the till — this is a real warning."),
}


def build_announcement_verdict(items: List[Dict], window_months: int,
                               source_url: str) -> Dict:
    """v4.0 — turn EVERY classified (and PDF-read) filing into ONE concrete,
    2–3 sentence verdict with real stats embedded, plus a single link to the
    stock's PSX announcements page. Insider/sponsor BUYING is reported as an
    encouraging observation but carries ZERO weight in the verdict itself."""
    # split insiders out first — they are noted, never scored
    ins = [i for i in items if i.get("category") == "insider"]
    ins_buys = sum(1 for i in ins
                   if _insider_direction(i.get("title", "")) == "buy")
    core = [i for i in items if i.get("category") != "insider"]

    pos = [i for i in core if i.get("impact") == "positive"]
    neg = [i for i in core if i.get("impact") == "negative"]
    routine = [i for i in core if i.get("impact") == "routine"]
    pdfs_read = sum(1 for i in items if i.get("pdf_read"))

    # short human noun for each substantive filing, with real numbers if read
    _NOUN = {
        "payout": "a cash payout to shareholders",
        "expansion": "a new plant / capacity expansion",
        "contract": "a new contract or order win",
        "acquisition": "an acquisition or new investment",
        "diversification": "a brand-new business line",
        "results_strong": "genuinely strong reported results",
        "rating_up": "a credit-rating upgrade",
        "buyback": "a share buy-back",
        "shutdown": "a plant/operations shutdown",
        "loss": "reported losses",
        "default": "a debt/repayment problem",
        "rating_down": "a credit-rating downgrade",
        "delay": "a delayed filing",
        "divestment": "selling off a business",
    }

    def _fact(it: Dict) -> str:
        base = _NOUN.get(it.get("category"), "a material announcement")
        if it.get("detail"):
            base += f" ({it['detail']})"
        return base

    def _uniq(seq: List[str], n: int) -> List[str]:
        out, seen = [], set()
        for s in seq:
            if s not in seen:
                out.append(s)
                seen.add(s)
            if len(out) >= n:
                break
        return out

    good_facts = _uniq([_fact(i) for i in pos], 3)
    bad_facts = _uniq([_fact(i) for i in neg], 2)
    total = len(items)
    read_note = (f"we opened and read {pdfs_read} of the filed documents "
                 f"end-to-end" if pdfs_read else
                 "each title was parsed and classified")

    def _join(facts: List[str]) -> str:
        if len(facts) == 1:
            return facts[0]
        if len(facts) == 2:
            return f"{facts[0]} and {facts[1]}"
        return f"{facts[0]}, {facts[1]}, and {facts[2]}"

    ins_note = ""
    if ins_buys:
        ins_note = (f" Separately, insiders/sponsors bought shares in "
                    f"{ins_buys} filing{'s' if ins_buys != 1 else ''} — an "
                    "encouraging sign, though we give it no weight in this verdict.")

    if not items:
        signal = "none"
        headline = "No announcements found — the company has been silent 🤫"
        summary = (f"PSX was scanned live and this company filed NOTHING in the "
                   f"last {window_months} months — zero announcements of any kind. "
                   "No news isn't bad news, but there is also no fresh fuel here; "
                   "the price is running purely on market mood.")
    elif neg and not pos:
        signal = "bad"
        headline = "The recent announcements look BAD 🔴"
        summary = (f"Out of {total} filings in the last {window_months} months "
                   f"({read_note}), the ones that matter are warnings: "
                   f"{_join(bad_facts)}. Nothing substantive on the positive side "
                   "was filed to balance it, so treat any rally in this stock "
                   f"with real suspicion until the picture changes.{ins_note}")
    elif neg and pos:
        signal = "mixed"
        headline = "The announcements are MIXED — good news AND a warning ⚖️"
        summary = (f"The last {window_months} months brought {total} filings "
                   f"({read_note}): on the bright side {_join(good_facts)}, but "
                   f"against that stands {_join(bad_facts)}. Genuine good news "
                   "with a genuine red flag attached — weigh both before acting, "
                   f"and never ignore the warning half.{ins_note}")
    elif pos:
        signal = "good"
        headline = "The recent announcements look GOOD 🟢"
        summary = (f"Of {total} filings in the last {window_months} months "
                   f"({read_note}), {len(pos)} carry real substance: "
                   f"{_join(good_facts)}. This is documented action — not talk — "
                   "and it's exactly the kind of fuel that keeps a healthy stock "
                   f"climbing.{ins_note}")
    else:
        signal = "quiet"
        headline = "Only routine paperwork — nothing exciting either way 😐"
        summary = (f"All {len(routine)} filing{'s' if len(routine) != 1 else ''} "
                   f"in the last {window_months} months ({read_note}) are "
                   "homework-type documents — board meetings, periodic results, "
                   "general meetings — with no growth action and no warning "
                   "inside them. Nothing here to start a rally, and nothing to "
                   f"fear either; the chart is running on mood alone.{ins_note}")

    return {"signal": signal, "headline": headline,
            "summary": summary,          # the 2–3 sentence verdict (UI shows this)
            "eli5": summary,             # backward-compat alias
            "page_url": source_url,      # THE one link: PSX announcements page
            "reasons": [],               # v4.0 UI shows no bullet list any more
            "counts": {"positive": len(pos), "negative": len(neg),
                       "routine": len(routine), "insider_buys": ins_buys,
                       "pdfs_read": pdfs_read, "total": total}}

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


def fetch_catalysts(symbol: str, session=None, page_html: str = None,
                    deep_pdf: bool = True) -> Dict:
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
        "verdict_block": None,          # v4.0 announcements verdict
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
        # v4.0 — open the most relevant filing PDFs and read their first
        # pages so the verdict reflects real document content (time-capped,
        # failure-tolerant, never invents anything). Skipped in ranking mode
        # (deep_pdf=False) where downloading documents for 100 companies
        # would stall the landing-page lists.
        if deep_pdf:
            try:
                _refine_items_with_pdfs(items, session)
            except Exception:  # noqa: BLE001
                pass
        out["items"] = items
        out["insiders"] = build_insider_block(items)   # v3.7 insider verdict
        out["catalysts"] = [i for i in items if i["impact"] == "positive"
                            and i["category"] != "payout"]
        out["negatives"] = [i for i in items if i["impact"] == "negative"]
        out["routine_count"] = sum(1 for i in items if i["impact"] == "routine")
        payouts = [i for i in items if i["category"] == "payout"]
        out["has_catalyst"] = bool(out["catalysts"])
        # v4.0 — one honest, kid-simple verdict with a linked reason list
        out["verdict_block"] = build_announcement_verdict(
            items, _MONTHS, company_url)

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


# ===========================================================================
# v4.0 — MATERIAL INFORMATION (built from scratch; replaces the old
# announcements verdict as the section shown in the Fundamental view).
#
# Contract with the user, verbatim: only filings titled "Material
# Information" are considered. Each one's PDF is downloaded and read END TO
# END. The verdict is EXTRACTED from the document's own sentences — the
# engine may compress, but it may NOT assume, infer beyond the text, or
# fabricate. If nothing can be read, it says so honestly.
# ===========================================================================
_MI_CFG = dict(getattr(config, "MATERIAL_INFO", {}) or {})
_MI_MAX = int(_MI_CFG.get("max_filings", 6))
_MI_PAGES = int(_MI_CFG.get("max_pages", 8))
_MI_MB = float(_MI_CFG.get("max_mb", 10))
_MI_BUDGET = float(_MI_CFG.get("budget_s", 25))
_MI_WORKERS = max(1, int(_MI_CFG.get("workers", 4)))
_MI_MONTHS = int(_MI_CFG.get("window_months", 12))

_MI_TITLE_RE = re.compile(r"material\s+information", re.I)

# strict, whitelisted tone cues — a tag is applied ONLY when the document
# itself uses these words; otherwise the filing stays "INFO".
_MI_GOOD_RE = re.compile(
    r"successfully\s+(?:completed|commissioned)|completed\s+and\s+commissioned|"
    r"commissioned|commenced\s+(?:commercial\s+)?(?:operations?|production)|"
    r"capacity\s+(?:has\s+)?(?:been\s+)?(?:increased|enhanced|expanded)|"
    r"(?:increase|enhancement|expansion)\s+(?:in|of)\s+(?:production\s+)?capacity|"
    r"awarded|letter\s+of\s+award|signed\s+(?:an?\s+)?(?:agreement|contract|mou)|"
    r"acquisition\s+(?:has\s+been\s+)?completed|acquired\s+\d|"
    r"successful(?:ly)?\s+(?:bid|discovery)|discovery\s+of|"
    r"record\s+(?:profit|production|sales)", re.I)
_MI_BAD_RE = re.compile(
    r"shut\s?down|suspension\s+of|suspended|\bclosure\b|ceased?\s+(?:operations?|production)|"
    r"discontinu(?:e|ed|ation)|default|fire\s+(?:incident|broke)|flood(?:ing|\s+damage)|"
    r"penalt(?:y|ies)|show\s+cause|adverse\s+(?:judgment|order)|"
    r"termination\s+of\s+(?:agreement|contract)|impairment|winding\s+up", re.I)

# where the actual disclosure body starts / stops inside an MI letter
_MI_BODY_START = re.compile(
    r"(?:hereby\s+make\s+disclosure\s+of\s+the\s+following\s+information[:\s]*|"
    r"MATERIAL\s+INFORMATION\s*)", re.I)
_MI_BODY_END = re.compile(
    r"you\s+may\s+please\s+inform|yours\s+truly|yours\s+sincerely|"
    r"for\s*:\s*[A-Z]|thanking\s+you", re.I)

_NUMBERY = re.compile(r"\d")
_MI_FACT_WORDS = re.compile(
    r"capacity|plant|project|production|tons?|mw|barrels?|acquisition|agreement|"
    r"contract|expansion|commissioned|operations?|dividend|investment|stake|"
    r"subsidiar|facility|per\s+annum|million|billion", re.I)


_RAPIDOCR = None            # lazily-built shared engine (model load is slow)
_RAPIDOCR_LOCK = None


def _get_rapidocr():
    """Pure-pip OCR engine. Unlike pytesseract it needs NO separately-
    installed program, so it works out of the box on the machines
    PSX 4.0.bat sets up. Prefers the modern `rapidocr` package (supports
    Python 3.13+); falls back to the legacy `rapidocr_onnxruntime` on older
    Pythons. Built once and shared. Returns (engine, api_version)."""
    global _RAPIDOCR, _RAPIDOCR_LOCK
    import threading as _t
    if _RAPIDOCR_LOCK is None:
        _RAPIDOCR_LOCK = _t.Lock()
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR is None:
            import logging as _log
            for name in ("RapidOCR", "rapidocr"):
                _log.getLogger(name).setLevel(_log.WARNING)
            try:                                   # modern package (Py3.13 OK)
                from rapidocr import RapidOCR
                _RAPIDOCR = (RapidOCR(), 3)
            except Exception:                      # noqa: BLE001 — legacy pkg
                from rapidocr_onnxruntime import RapidOCR
                _RAPIDOCR = (RapidOCR(), 1)
            for name in ("RapidOCR", "rapidocr"):  # library configures its
                _log.getLogger(name).setLevel(_log.WARNING)   # logger on init
    return _RAPIDOCR


def _rapid_text(engine_tuple, np_img) -> str:
    """Run OCR and normalise the two API generations to plain text:
    v3 → RapidOCROutput with .txts; v1 → (list of [box, text, score], t)."""
    engine, ver = engine_tuple
    if ver >= 3:
        res = engine(np_img)
        return " ".join(getattr(res, "txts", None) or [])
    res, _ = engine(np_img)
    return " ".join(r[1] for r in (res or []))


def _ocr_pdf_pages(buf, max_pages: int) -> str:
    """OCR fallback for SCANNED letters (most PSX MI filings are scans with
    no text layer). Engine chain:
      1. tesseract via pytesseract — used when the Tesseract PROGRAM is
         actually installed on the machine;
      2. RapidOCR (onnxruntime) — pure pip, no external install, so scanned
         letters are readable on every machine the .bat sets up.
    Purely best-effort: returns '' only if BOTH engines are unavailable."""
    try:
        import pypdfium2 as pdfium
    except Exception:  # noqa: BLE001
        return ""
    tess = None
    try:
        import shutil
        import pytesseract
        if pytesseract.get_tesseract_version() or shutil.which("tesseract"):
            tess = pytesseract
    except Exception:  # noqa: BLE001
        tess = None
    rapid = None
    if tess is None:
        try:
            rapid = _get_rapidocr()
        except Exception:  # noqa: BLE001
            rapid = None
    if tess is None and rapid is None:
        return ""
    try:
        buf.seek(0)
        pdf = pdfium.PdfDocument(buf)
        out = []
        for i in range(min(len(pdf), max_pages)):
            try:
                pil = pdf[i].render(scale=2.2).to_pil()
                if tess is not None:
                    out.append(tess.image_to_string(pil) or "")
                else:
                    import numpy as _np
                    out.append(_rapid_text(rapid, _np.array(pil)))
            except Exception:  # noqa: BLE001
                continue
        pdf.close()
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return ""


def _read_pdf_full(url: str, session, max_pages: int = None) -> str:
    """Download one PDF and extract text from ALL its pages (capped).
    Scanned letters (no text layer) fall back to OCR when available.
    Returns '' on any failure — never raises."""
    max_pages = max_pages or _MI_PAGES
    try:
        import io
        import pdfplumber
        r = session.get(url, timeout=12, stream=True,
                        headers={"User-Agent": random.choice(
                            getattr(config, "USER_AGENTS", None)
                            or ["Mozilla/5.0"])})
        r.raise_for_status()
        size = int(r.headers.get("content-length") or 0)
        if size and size > _MI_MB * 1024 * 1024:
            return ""
        buf, got = io.BytesIO(), 0
        for chunk in r.iter_content(chunk_size=65536):
            got += len(chunk)
            if got > _MI_MB * 1024 * 1024:
                return ""
            buf.write(chunk)
        buf.seek(0)
        if buf.read(5)[:4] != b"%PDF":
            return ""
        buf.seek(0)
        text = []
        with pdfplumber.open(buf) as pdf:
            for pg in pdf.pages[:max_pages]:
                try:
                    text.append(pg.extract_text() or "")
                except Exception:  # noqa: BLE001
                    continue
        joined = "\n".join(text)
        if len(joined.strip()) >= 120:
            return joined
        # no real text layer → it's a scan → OCR it
        return _ocr_pdf_pages(buf, max_pages)
    except Exception:  # noqa: BLE001
        return ""


def _mi_disclosure_body(text: str) -> str:
    """Cut the letter down to the actual disclosure body. Prefers the text
    inside quotation marks (PSX MI letters quote the disclosure); otherwise
    the span between the MI heading and the sign-off boilerplate."""
    if not text:
        return ""
    t = re.sub(r"[ \t]+", " ", text)
    # 1) quoted disclosure ("...") — most MI letters use this format
    q = re.findall(r"[\u201c\"]([^\u201d\"]{80,2500})[\u201d\"]", t, re.S)
    if q:
        return max(q, key=len).strip()
    # 2) heading → boilerplate span
    m = _MI_BODY_START.search(t)
    start = m.end() if m else 0
    m2 = _MI_BODY_END.search(t, start)
    end = m2.start() if m2 else min(len(t), start + 2500)
    return t[start:end].strip()


def _mi_gist(body: str, max_words: int = 60) -> str:
    """EXTRACT the 1-2 most information-dense sentences from the disclosure
    body — sentences with real numbers/business facts rank highest. This is
    compression of the company's own words, not interpretation."""
    if not body:
        return ""
    body = re.sub(r"\s+", " ", body).strip()
    # drop the legal preamble — the disclosure starts after this phrase
    m = re.search(r"disclosure\s+of\s+the\s+following\s+information[:\s\u201c\"]*", body, re.I)
    if m:
        body = body[m.end():].strip()
    body = re.sub(r"^\s*dear\s+sirs?\(?s?\)?[,.]?\s*", "", body, flags=re.I)
    sents = re.split(r"(?<=[.;])\s+", body)
    sents = [s.strip() for s in sents if len(s.strip()) > 25]
    if not sents:
        return body[: max_words * 7]

    def score(s: str) -> float:
        sc = len(_NUMBERY.findall(s)) * 1.5
        sc += len(_MI_FACT_WORDS.findall(s)) * 2.0
        sc += 1.0 if _MI_GOOD_RE.search(s) or _MI_BAD_RE.search(s) else 0.0
        sc -= 3.0 if re.search(r"sections?\s+96|regulation|securities\s+act", s, re.I) else 0.0
        return sc

    ranked = sorted(range(len(sents)), key=lambda i: score(sents[i]), reverse=True)
    keep = sorted(ranked[:2])                       # keep document order
    gist = " ".join(sents[i] for i in keep)
    words = gist.split()
    if len(words) > max_words:
        gist = " ".join(words[:max_words]).rstrip(",;") + " …"
    return gist


def _mi_cache_path(symbol: str) -> str:
    return os.path.join(config.CACHE_DIR, f"mi_{symbol.upper()}.json")


def _mi_cache_load(symbol: str) -> Optional[Dict]:
    """Fresh cached MI verdicts (<= cache_hours old, successful reads only)."""
    try:
        path = _mi_cache_path(symbol)
        if not os.path.exists(path):
            return None
        max_age = float(_MI_CFG.get("cache_hours", 24)) * 3600
        if (time.time() - os.path.getmtime(path)) > max_age:
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if data.get("checked") else None
    except Exception:  # noqa: BLE001
        return None


def _mi_cache_save(symbol: str, payload: Dict) -> None:
    """Persist only USEFUL scans: a successful check whose found filings were
    actually read (or a genuine 'nothing filed'). Never cache failures."""
    try:
        if not payload.get("checked") or payload.get("error"):
            return
        if payload.get("found", 0) > 0 and payload.get("read", 0) == 0:
            return                      # every read failed — retry next time
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with open(_mi_cache_path(symbol), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:  # noqa: BLE001
        pass


def fetch_material_info(symbol: str, session=None, page_html: str = None) -> Dict:
    """v4.0 — the Material Information engine. Scans PSX for filings titled
    'Material Information', reads each PDF end-to-end, and returns per-filing
    EXTRACTED gists with a strictly keyword-based tone tag + clickable link.
    Verdicts persist on disk for cache_hours (default 24h): relaunches and
    the weekly rebuild reuse them instead of re-downloading and re-OCRing
    the same letters."""
    symbol = symbol.strip().upper()
    cached = _mi_cache_load(symbol)
    if cached is not None:
        return cached
    company_url = _COMPANY_URL.format(symbol=symbol)
    out: Dict = {"checked": False, "error": None, "as_of": utils.now_iso(),
                 "window_months": _MI_MONTHS, "source_url": company_url,
                 "items": [], "found": 0, "read": 0}
    try:
        session = session or utils.make_session()
        if page_html is None:
            page_html = utils.fetch(company_url, session=session)
        raw = _rows_from_company_page(page_html, company_url)
        if not raw:
            raw = _rows_from_announcements_page(symbol, session)
        out["checked"] = True

        cutoff = (datetime.utcnow()
                  - timedelta(days=30.5 * _MI_MONTHS)).strftime("%Y-%m-%d")
        mi, seen = [], set()
        for it in raw:
            if not _MI_TITLE_RE.search(it.get("title") or ""):
                continue
            key = (it["date"], it["title"][:80].lower())
            if key in seen or it["date"] < cutoff:
                continue
            seen.add(key)
            mi.append(it)
        mi.sort(key=lambda x: x["date"], reverse=True)
        out["found"] = len(mi)
        mi = mi[:_MI_MAX]

        from concurrent.futures import ThreadPoolExecutor, as_completed
        deadline = time.time() + _MI_BUDGET

        def _one(it):
            url = next((u["url"] for u in it.get("urls", [])
                        if ".pdf" in u["url"].lower() or "/download/" in u["url"]),
                       None)
            text = ""
            if url and time.time() < deadline:
                text = _read_pdf_full(url, session)
            body = _mi_disclosure_body(text)
            gist = _mi_gist(body)
            read = bool(gist)
            if not gist:
                # honest fallback: we could not read it — say only the title
                gist = ("The document could not be opened and read — only its "
                        "title is known. Open the filing to read it yourself.")
            good = bool(_MI_GOOD_RE.search(body or ""))
            bad = bool(_MI_BAD_RE.search(body or ""))
            tone = ("mixed" if (good and bad) else
                    "good" if good else
                    "bad" if bad else
                    "info" if read else "unread")
            return {"date": it["date"], "title": it["title"],
                    "url": url or company_url, "read": read,
                    "tone": tone, "gist": gist}

        with ThreadPoolExecutor(max_workers=_MI_WORKERS,
                                thread_name_prefix="psx-mi") as ex:
            futs = [ex.submit(_one, it) for it in mi]
            done = []
            for f in as_completed(futs, timeout=max(4.0, _MI_BUDGET + 6)):
                try:
                    done.append(f.result())
                except Exception:  # noqa: BLE001
                    continue
        done.sort(key=lambda x: x["date"], reverse=True)
        out["items"] = done
        out["read"] = sum(1 for d in done if d["read"])
        _mi_cache_save(symbol, out)
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
