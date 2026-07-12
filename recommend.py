"""
recommend.py
============
v4.0 — RECOMMENDED STOCKS OF THE MONTH.

Runs the tool's OWN two engines — the fundamental scorer (scorer.py) and the
technical prediction engine (predictor.py) — over a liquid pool of PSX names,
blends both scores exactly the way the Prediction tab does
(PREDICTOR tech_weight / fund_weight), and keeps the top picks for the month.

STRICT HONESTY RULES:
  * Every number shown comes from the same live-scraped data the Analyze and
    Prediction buttons use. Nothing is estimated or hand-picked.
  * A pick's bullets are generated from its ACTUAL metric values and its
    ACTUAL chart reads — the templates only translate them into plain words.
  * The whole payload carries a disclaimer: educational guidance only,
    never investment advice.

The scan is expensive (it scrapes each candidate), so it runs in a background
thread and the result is cached per calendar month in psx_cache/. The API
serves progress while the scan is running so the dashboard can show a live
"building" state instead of a spinner black-hole.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import config
import utils

_CFG = dict(getattr(config, "RECOMMEND", {}) or {})
_POOL_INDEX = _CFG.get("pool_index", "KSE100")
_EXTRA_INDEX = None  # v4.0: ALL lists come strictly from the KSE-100
_MAX_SYMBOLS = int(_CFG.get("max_symbols", 110))
_TOP_N = int(_CFG.get("top_n", 5))
_BLUE_N = int(_CFG.get("blue_n", 5))
_TOP_MIN = float(_CFG.get("top_min_score", 60))
_AVOID_MAX = float(_CFG.get("avoid_max_score", 48))
_BLUE_MIN = float(_CFG.get("blue_min_score", 60))
_AVOID_N = int(_CFG.get("avoid_n", 5))
_WORKERS = max(1, int(_CFG.get("workers", 12)))
_DELAY_S = float(_CFG.get("delay_s", 0.0))
_CACHE_PREFIX = _CFG.get("cache_prefix", "recommendations")
_REBUILD_DAYS = float(_CFG.get("rebuild_if_older_days", 7))
_SERVE_STALE = bool(_CFG.get("serve_stale", True))
_MIN_HISTORY = int(_CFG.get("min_price_history", 60))

_TECH_W = float(getattr(config, "PREDICTOR", {}).get("tech_weight", 0.55))
_FUND_W = float(getattr(config, "PREDICTOR", {}).get("fund_weight", 0.45))

_DISCLAIMER = ("These picks are generated automatically from public PSX data by "
               "the same fundamental + technical engines used by the Analyze and "
               "Prediction buttons. They are educational guidance only — NOT a "
               "recommendation to buy or sell. Markets carry risk; always do "
               "your own research and never risk money you cannot afford to lose.")

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_building = False
_progress = {"done": 0, "total": 0, "current": ""}


def _month_key(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def _cache_path(month: str) -> str:
    return os.path.join(config.CACHE_DIR, f"{_CACHE_PREFIX}_{month}.json")


def _load_cached(month: str) -> Optional[Dict]:
    path = _cache_path(month)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        gen = data.get("generated_at", "")
        try:
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(gen.replace("Z", "+00:00"))
                        ).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001
            age_days = 0
        data["_age_days"] = round(age_days, 2)
        return data
    except Exception:  # noqa: BLE001
        return None


def _save_cached(month: str, payload: Dict) -> None:
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with open(_cache_path(month), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# plain-English bullet builders (translate REAL values into simple words)
# ---------------------------------------------------------------------------
def _fundamental_points(fund: Dict) -> List[str]:
    """Short, kid-simple bullets from the ACTUAL scored metrics."""
    pts: List[str] = []
    metrics = {m.get("key"): m for m in (fund.get("metrics") or [])}

    def val(key):
        m = metrics.get(key) or {}
        return m.get("display"), m.get("status")

    d, s = val("revenue_growth")
    if s == "good" and d:
        pts.append(f"📈 Sales are growing about {d} a year — it keeps selling more.")
    d, s = val("eps_growth")
    if s == "good" and d:
        pts.append(f"💵 Profit per share is rising ~{d} a year — each share earns more.")
    d, s = val("profit_margin")
    if s == "good" and d:
        pts.append(f"🧮 It keeps {d} of every sale as profit — a healthy margin.")
    d, s = val("roe")
    if s == "good" and d:
        pts.append(f"🏆 Owners' money earns {d} a year (ROE) — capital is working hard.")
    d, s = val("debt_to_equity")
    if s == "good" and d:
        pts.append(f"🛡️ Very little borrowed money ({d} debt vs owners' money) — a safe balance sheet.")
    d, s = val("dividend_yield")
    if s == "good" and d:
        pts.append(f"💰 It pays real cash to shareholders — about {d} dividend yield.")
    d, s = val("pe_ratio")
    if s == "good" and d:
        pts.append(f"🏷️ The price tag is cheap for what you get (P/E {d}).")
    d, s = val("cashflow_quality")
    if s == "good" and d:
        pts.append(f"💧 Its profit is real cash, not paper ({d} cash-flow cover).")

    concerns = fund.get("concerns") or []
    if concerns:
        pts.append(f"👀 Worth watching: {concerns[0]} is its weakest spot.")
    if not pts:
        pts.append(f"🏫 Overall report card: {round(fund.get('score') or 0)}/100 on fundamentals.")
    return pts[:5]


def _technical_points(pred: Dict) -> List[str]:
    """Short, kid-simple bullets from the ACTUAL chart reads."""
    pts: List[str] = []
    st = pred.get("structure")
    if st == "uptrend":
        pts.append("🚶 The price is climbing stairs — every high AND every rest is higher (uptrend).")
    elif st == "downtrend":
        pts.append("⚠️ The price is walking DOWN stairs — that's why buy zones matter here.")
    else:
        pts.append("😴 The price is pacing in a range — waiting for it to pick a door.")

    ind = pred.get("indicators") or {}
    price = pred.get("price")
    if price and ind.get("ema89") and price > ind["ema89"]:
        pts.append("🛟 It's standing above its big trend line (89-EMA) — the long walk is upward.")
    elif ind.get("ema89"):
        pts.append("🛟 It's below the big trend line (89-EMA) — the long trend needs repair.")
    if price and ind.get("ema21") and price > ind["ema21"]:
        pts.append("🦘 Dips keep getting bought above the short-term trampoline (21-EMA).")

    wy = (pred.get("wyckoff") or {})
    ph = wy.get("phase")
    if ph in ("accumulation", "breakout", "markup"):
        nice = {"accumulation": "quiet-collecting season (patient buyers at work)",
                "breakout": "just broke out of its long boring base",
                "markup": "climbing season of its market cycle"}[ph]
        pts.append(f"📦 Wyckoff read: it's in the {nice}.")
    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi >= 70:
            pts.append(f"🔋 Energy meter is hot (RSI {rsi:.0f}) — buy the dips, don't chase spikes.")
        elif rsi <= 40:
            pts.append(f"🔋 Sellers look tired (RSI {rsi:.0f}) — the springs are compressed.")
    vn = pred.get("volume_note")
    if vn == "expanding":
        pts.append("📢 Volumes are expanding with the move — the rally has fuel.")
    rr = (pred.get("trade_plan") or {}).get("risk_reward")
    if rr:
        pts.append(f"⚖️ The plan's reward-vs-risk is about {rr:.1f}:1.")
    return pts[:5]


def _fundamental_flaws(fund: Dict) -> List[str]:
    """Weakness-only bullets — used on the stay-away card so it never lists
    strengths under a 'where the business is weak' heading."""
    pts: List[str] = []
    label = {"revenue_growth": "Sales growth", "eps_growth": "Per-share profit",
             "profit_margin": "Profit margin", "roe": "Return on owners' money",
             "roic": "Return on invested capital",
             "debt_to_equity": "Debt load", "current_ratio": "Short-term liquidity",
             "cce": "Cash reserves", "cashflow_quality": "Cash-flow quality",
             "dividend_yield": "Dividend", "pe_ratio": "Valuation",
             "capital_adequacy": "Capital strength"}
    story = {"revenue_growth": "sales are shrinking or stalling — less money coming in the door",
             "eps_growth": "each share is earning less than before",
             "profit_margin": "very little of each sale survives as profit",
             "roe": "owners' money is earning a poor return",
             "roic": "the capital ploughed into the business earns too little",
             "debt_to_equity": "it leans heavily on borrowed money — a fragile balance sheet",
             "current_ratio": "short-term bills are big versus short-term cash",
             "cce": "the cash cushion is thin",
             "cashflow_quality": "reported profit is not turning into real cash",
             "dividend_yield": "shareholders see little or no cash payout",
             "pe_ratio": "the price tag is expensive for what you actually get",
             "capital_adequacy": "its capital buffer is weaker than it should be"}
    for m in (fund.get("metrics") or []):
        if m.get("status") == "bad":
            k = m.get("key")
            d = f" ({m.get('display')})" if m.get("display") else ""
            pts.append(f"🔻 {label.get(k, m.get('label', k))}{d}: {story.get(k, 'a genuinely weak reading in the filings')}.")
    if not pts:
        pts.append(f"🏫 No single metric is disastrous — the WHOLE report card is mediocre "
                   f"({round(float(fund.get('score') or 0))}/100), which is its own warning.")
    return pts[:5]


def _technical_warnings(pred: Dict) -> List[str]:
    """Warning-only chart bullets for the stay-away card."""
    pts: List[str] = []
    ind = pred.get("indicators") or {}
    if pred.get("structure") == "downtrend":
        pts.append("⚠️ The price is walking DOWN stairs — every high and every rest is lower.")
    elif pred.get("structure") == "sideways":
        pts.append("😴 The price is going nowhere — months of drift with no buyers stepping up.")
    price = pred.get("price")
    if ind.get("ema89") and price and price < ind["ema89"]:
        pts.append("🩹 It trades BELOW its big trend line (89-EMA) — the long trend is broken.")
    if ind.get("ema21") and price and price < ind["ema21"]:
        pts.append("📉 Even the short-term trampoline (21-EMA) is above the price — rallies keep getting sold.")
    ph = (pred.get("wyckoff") or {}).get("phase")
    if ph in ("distribution", "markdown"):
        pts.append("📦 Wyckoff read: big holders look like they're QUIETLY SELLING, not collecting.")
    rsi = ind.get("rsi")
    if rsi is not None and rsi < 40:
        pts.append(f"🔋 Momentum is drained (RSI {rsi:.0f}) — sellers have had the upper hand for a while.")
    rr = (pred.get("trade_plan") or {}).get("risk_reward")
    if rr is not None and rr < 1.2:
        pts.append(f"⚖️ Even the best-case plan pays only {rr:.1f}:1 versus its risk — poor odds.")
    tech = float((pred.get("scores") or {}).get("technical") or 0)
    if not pts:
        pts.append(f"🌡️ The chart's overall health is just {tech:.0f}/100 — no warning screams, but nothing invites either.")
    return pts[:5]


# ---------------------------------------------------------------------------
# v4.0 — BLUE-CHIP model. Grounded in how the term is used on the PSX
# (financial press + broker commentary): large, well-established KSE-100
# heavyweights with strong balance sheets, consistent earnings and a real
# dividend track record, high liquidity / index weight (KSE-30 membership is
# the practical marker), and lower volatility. Classic examples cited across
# Pakistani financial media: OGDC, HBL, MCB, UBL, ENGRO, FFC, LUCK, HUBC,
# PPL, PSO. The score below rewards exactly those observable traits — it is
# computed from the SAME scraped analysis, never from a hard-coded list.
# ---------------------------------------------------------------------------
def _blue_chip_score(r: Dict, kse30: set) -> float:
    fund = float(r["scores"]["fundamental"] or 0)          # financial strength
    metrics = r.get("_metric_status") or {}
    div_ok = metrics.get("dividend_yield") == "good"       # real cash payouts
    debt_ok = metrics.get("debt_to_equity") == "good"
    cash_ok = metrics.get("cashflow_quality") == "good"
    heavyweight = r["symbol"] in kse30                     # index-grade size/liquidity
    seasoned = (r.get("history_len") or 0) >= 250          # ≳5y of listed price data
    not_broken = r.get("structure") != "downtrend"
    score = (0.50 * fund
             + 14.0 * (1 if div_ok else 0)
             + 8.0 * (1 if heavyweight else 0)
             + 6.0 * (1 if debt_ok or cash_ok else 0)
             + 6.0 * (1 if seasoned else 0)
             + 6.0 * (1 if not_broken else 0))
    return round(min(100.0, score), 1)


def _blue_line(sym: str, r: Dict, kse30: set) -> str:
    traits = []
    if r["symbol"] in kse30:
        traits.append("an index heavyweight (KSE-30 grade liquidity)")
    if (r.get("_metric_status") or {}).get("dividend_yield") == "good":
        traits.append("a real cash-dividend payer")
    if (r.get("_metric_status") or {}).get("debt_to_equity") == "good":
        traits.append("a conservative balance sheet")
    if (r.get("history_len") or 0) >= 250:
        traits.append("a long, seasoned trading history")
    t = ", ".join(traits[:3]) if traits else "broad financial strength across its filings"
    return (f"{sym} fits the classic PSX blue-chip mould — {t} — on top of a "
            f"{r['scores']['fundamental']:.0f}/100 business health score. "
            f"Blue-chip grade: {r.get('blue_score', 0):.0f}/100.")


def _growth_aspects(scraped: Dict, fund: Dict) -> List[Dict]:
    """Concrete growth story: REAL PSX catalysts first, then fundamentals."""
    out: List[Dict] = []
    ann = scraped.get("announcements") or {}
    for c in (ann.get("catalysts") or [])[:3]:
        url = (c.get("urls") or [{}])[0].get("url")
        out.append({"text": f"🚀 {c.get('label')} — filed on PSX {c.get('date')}: "
                            f"“{(c.get('title') or '')[:110]}”",
                    "url": url})
    metrics = {m.get("key"): m for m in (fund.get("metrics") or [])}
    rg = metrics.get("revenue_growth") or {}
    if rg.get("status") == "good":
        out.append({"text": f"📊 Its own reported statements show sales compounding at "
                            f"{rg.get('display')} a year — growth that already happened, "
                            "not a promise.", "url": rg.get("source_url")})
    eg = metrics.get("eps_growth") or {}
    if eg.get("status") == "good" and len(out) < 4:
        out.append({"text": f"🌱 Per-share profit compounding at {eg.get('display')} a year "
                            "means the business is growing faster than its share count.",
                    "url": eg.get("source_url")})
    if not out:
        out.append({"text": "🔎 No filed growth catalyst right now — this pick stands on "
                            "the strength of its numbers and its chart alone.",
                    "url": (ann.get("source_url"))})
    return out[:4]


def _why_line(sym: str, fund: Dict, pred: Dict, combined: float) -> str:
    v = (pred.get("verdict") or {}).get("label", "")
    return (f"{sym} scores {combined:.0f}/100 when its business health "
            f"({(fund.get('score') or 0):.0f}) and chart health "
            f"({(pred.get('scores') or {}).get('technical', 0):.0f}) are blended the "
            f"same way the Prediction engine does — outlook: {v}.")


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------
def _candidate_pool() -> List[str]:
    """STRICTLY the KSE-100 — every landing-page list draws only from here.
    Also stashes the KSE-30 set (blue-chip heavyweight marker)."""
    import psx_data
    uni = psx_data.get_universe()
    idx = uni.get("indices") or {}
    kse100 = list(idx.get(_POOL_INDEX) or [])
    flags = {s["symbol"]: s for s in uni.get("symbols") or []}
    keep = lambda s: not (flags.get(s, {}).get("isETF") or flags.get(s, {}).get("isDebt"))  # noqa: E731
    pool = [s for s in kse100 if keep(s)][:_MAX_SYMBOLS]
    _candidate_pool.kse100 = set(pool)
    _candidate_pool.kse30 = {s for s in (idx.get("KSE30") or []) if keep(s)}
    return pool


def _evaluate(symbol: str) -> Optional[Dict]:
    """Scrape + score + predict one candidate. None on any failure."""
    import scraper
    import scorer
    import predictor
    try:
        # v4.0 speed rule — ranking does NOT open filing PDFs. The Material
        # Information engine and announcement PDF deep-reads (up to ~45s of
        # wall-clock budget PER company) belong to the individual X-ray, not
        # to a 100-symbol scan. Titles are still classified, so the growth
        # bullets keep working — the scan just stops downloading documents.
        scraped = scraper.scrape_company(symbol, deep_pdf=False)
        hist = [p for p in (scraped.get("price_history") or [])
                if p.get("close") is not None]
        if len(hist) < _MIN_HISTORY:
            return None
        fund = scorer.score_company(scraped)
        pred = predictor.predict(scraped, fund)
        if pred.get("error"):
            return None
        combined = float((pred.get("scores") or {}).get("combined") or 0)
        plan = pred.get("trade_plan") or {}
        profile = scraped.get("profile") or {}
        return {
            "symbol": symbol,
            "name": profile.get("name") or symbol,
            "sector": profile.get("sector") or scraped.get("sector") or "",
            "price": profile.get("price"),
            "scores": {
                "fundamental": round(float(fund.get("score") or 0), 1),
                "technical": round(float((pred.get("scores") or {}).get("technical") or 0), 1),
                "combined": round(combined, 1),
            },
            "verdict": pred.get("verdict"),
            "why": _why_line(symbol, fund, pred, combined),
            "fundamental_points": _fundamental_points(fund),
            "fundamental_flaws": _fundamental_flaws(fund),
            "technical_points": _technical_points(pred),
            "technical_warnings": _technical_warnings(pred),
            "growth_aspects": _growth_aspects(scraped, fund),
            "plan": {
                "buy1": plan.get("buy1"), "buy2": plan.get("buy2"),
                "stop_loss": plan.get("stop_loss"),
                "target1": plan.get("target1"), "target2": plan.get("target2"),
                "risk_reward": plan.get("risk_reward"),
            },
            "structure": pred.get("structure"),
            "history_len": len(hist),
            "_metric_status": {m.get("key"): m.get("status")
                               for m in (fund.get("metrics") or [])},
        }
    except Exception:  # noqa: BLE001
        return None


def _avoid_line(sym: str, r: Dict) -> str:
    s = r["scores"]
    bits = []
    if s["fundamental"] < 45:
        bits.append(f"weak business health ({s['fundamental']:.0f}/100)")
    if s["technical"] < 45:
        bits.append(f"a sick-looking chart ({s['technical']:.0f}/100)")
    if r.get("structure") == "downtrend":
        bits.append("a price that keeps making lower lows")
    why = " and ".join(bits) if bits else "the weakest blended score in the whole KSE-100 scan"
    return (f"{sym} sits at the BOTTOM of the KSE-100 with an overall {s['combined']:.0f}/100 — "
            f"held back by {why}. The same engines that pick the top list say: look elsewhere.")


def _rank_lists(results: List[Dict], kse30: set) -> Dict:
    """Build the three landing lists from evaluated KSE-100 candidates.
    Every list is THRESHOLDED — a stock must genuinely qualify, so lists may
    hold fewer than their max (or none). No filler, ever.
    v4.0.1 EXCLUSIVITY RULE — the Blue-Chip list is picked FIRST and its
    members are removed from the Top-5 and Stay-Away pools, so no stock
    ever appears in two lists at once."""
    for r in results:
        r["blue_score"] = _blue_chip_score(r, kse30)

    # ---- 1) Blue chips first (they claim their symbols exclusively) -------
    by_blue = sorted(results, key=lambda r: r.get("blue_score", 0), reverse=True)
    blue = []
    for r in by_blue:
        if r.get("blue_score", 0) < _BLUE_MIN or r["scores"]["fundamental"] < 50:
            continue
        b = dict(r)
        b["rank"] = len(blue) + 1
        b["why"] = _blue_line(b["symbol"], b, kse30)
        blue.append(b)
        if len(blue) >= _BLUE_N:
            break
    blue_syms = {b["symbol"] for b in blue}

    # ---- 2) Top picks — blue chips excluded --------------------------------
    by_best = sorted(results, key=lambda r: (r["scores"]["combined"],
                                             r.get("structure") != "downtrend"),
                     reverse=True)
    top = [r for r in by_best
           if r["symbol"] not in blue_syms
           and r["scores"]["combined"] >= _TOP_MIN][:_TOP_N]
    for i, p in enumerate(top, 1):
        p["rank"] = i

    # ---- 3) Stay-away — blue chips AND top picks excluded ------------------
    taken = blue_syms | {p["symbol"] for p in top}
    by_worst = sorted(results, key=lambda r: (r["scores"]["combined"],
                                              r.get("structure") != "downtrend"))
    avoid = []
    for r in by_worst:
        if r["symbol"] in taken or r["scores"]["combined"] > _AVOID_MAX:
            continue
        a = dict(r)
        a["rank"] = len(avoid) + 1
        a["why"] = _avoid_line(a["symbol"], a)
        avoid.append(a)
        if len(avoid) >= _AVOID_N:
            break
    return {"top": top, "avoid": avoid, "blue": blue}


def _public(r: Dict) -> Dict:
    """Strip private fields before the payload goes to the browser."""
    r = dict(r)
    r.pop("_metric_status", None)
    return r


def _build(month: str) -> None:
    """Parallel scan of the KSE-100 → candidates + three thresholded lists."""
    global _building
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: List[Dict] = []
    try:
        pool = _candidate_pool()
        kse30 = getattr(_candidate_pool, "kse30", set())
        with _lock:
            _progress.update({"done": 0, "total": len(pool), "current": ""})
        done = 0
        with ThreadPoolExecutor(max_workers=_WORKERS,
                                thread_name_prefix="psx-reco") as ex:
            futs = {ex.submit(_evaluate, sym): sym for sym in pool}
            for fut in as_completed(futs):
                sym = futs[fut]
                done += 1
                with _lock:
                    _progress.update({"done": done, "current": sym})
                try:
                    r = fut.result()
                except Exception:  # noqa: BLE001
                    r = None
                if r:
                    results.append(r)
        with _lock:
            _progress.update({"done": len(pool), "current": ""})

        lists = _rank_lists(results, kse30)
        prices = [r["price"] for r in results if r.get("price")]
        payload = {
            "status": "ready",
            "month": month,
            "month_label": datetime.strptime(month, "%Y-%m").strftime("%B %Y"),
            "generated_at": utils.now_iso(),
            "pool": _POOL_INDEX,
            "scanned": len(pool),
            "usable": len(results),
            "blend": {"technical": _TECH_W, "fundamental": _FUND_W},
            "price_bounds": {"min": round(min(prices), 2) if prices else 0,
                             "max": round(max(prices), 2) if prices else 0},
            "thresholds": {"top_min": _TOP_MIN, "avoid_max": _AVOID_MAX,
                           "blue_min": _BLUE_MIN,
                           "top_n": _TOP_N, "avoid_n": _AVOID_N, "blue_n": _BLUE_N},
            "kse30": sorted(kse30),
            # full evaluated field — the browser filters this by price range
            "candidates": [_public(r) for r in
                           sorted(results, key=lambda r: r["scores"]["combined"],
                                  reverse=True)],
            "picks": [_public(r) for r in lists["top"]],
            "avoid": [_public(r) for r in lists["avoid"]],
            "blue": [_public(r) for r in lists["blue"]],
            "disclaimer": _DISCLAIMER,
        }
        _save_cached(month, payload)
    finally:
        with _lock:
            _building = False


def _latest_any_cache() -> Optional[Dict]:
    """Newest recommendations_*.json on disk, regardless of month."""
    try:
        pat = os.path.join(config.CACHE_DIR, f"{_CACHE_PREFIX}_*.json")
        files = sorted(glob.glob(pat), reverse=True)
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def start_build(force: bool = False) -> bool:
    """Kick off a background build if needed. Returns True if started."""
    global _building
    month = _month_key()
    cached = _load_cached(month)
    if cached and not force and cached.get("_age_days", 0) <= _REBUILD_DAYS:
        return False
    with _lock:
        if _building:
            return False
        _building = True
    threading.Thread(target=_build, args=(month,), daemon=True,
                     name="psx-recommend").start()
    return True


def is_building() -> bool:
    """True while the background KSE-100 scan is running."""
    with _lock:
        return _building


def get_recommendations(force: bool = False) -> Dict:
    """API entry. NEVER blocks: current cache > stale cache (marked
    refreshing) > building payload with live progress."""
    month = _month_key()
    cached = _load_cached(month)
    if cached and not force:
        cached.pop("_age_days", None)
        start_build(False)          # quiet refresh if it's getting old
        return cached
    start_build(force)
    # v4.0 speed rule: an old month's list beats a spinner every time.
    if _SERVE_STALE and not force:
        stale = _latest_any_cache()
        if stale and stale.get("picks"):
            stale.pop("_age_days", None)
            stale["status"] = "ready"
            stale["stale"] = True
            stale["refreshing"] = True
            return stale
    with _lock:
        prog = dict(_progress)
        building = _building
    return {"status": "building" if building else "empty",
            "month": month,
            "month_label": datetime.now(timezone.utc).strftime("%B %Y"),
            "progress": prog,
            "pool": _POOL_INDEX,
            "picks": [], "avoid": [], "blue": [], "candidates": [],
            "disclaimer": _DISCLAIMER}


if __name__ == "__main__":
    import sys
    print(json.dumps(get_recommendations("--force" in sys.argv), indent=2)[:3000])
