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

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import config
import utils

_CFG = dict(getattr(config, "RECOMMEND", {}) or {})
_POOL_INDEX = _CFG.get("pool_index", "KSE30")
_EXTRA_INDEX = _CFG.get("extra_index", "KMI30")
_MAX_SYMBOLS = int(_CFG.get("max_symbols", 40))
_TOP_N = int(_CFG.get("top_n", 5))
_DELAY_S = float(_CFG.get("delay_s", 2.0))
_CACHE_PREFIX = _CFG.get("cache_prefix", "recommendations")
_REBUILD_DAYS = float(_CFG.get("rebuild_if_older_days", 7))
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
    import psx_data
    uni = psx_data.get_universe()
    idx = uni.get("indices") or {}
    pool: List[str] = []
    for name in (_POOL_INDEX, _EXTRA_INDEX):
        for s in (idx.get(name) or []):
            if s not in pool:
                pool.append(s)
    # drop ETFs / debt if flagged in the symbols table
    flags = {s["symbol"]: s for s in uni.get("symbols") or []}
    pool = [s for s in pool
            if not (flags.get(s, {}).get("isETF") or flags.get(s, {}).get("isDebt"))]
    return pool[:_MAX_SYMBOLS]


def _evaluate(symbol: str) -> Optional[Dict]:
    """Scrape + score + predict one candidate. None on any failure."""
    import scraper
    import scorer
    import predictor
    try:
        scraped = scraper.scrape_company(symbol)
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
            "technical_points": _technical_points(pred),
            "growth_aspects": _growth_aspects(scraped, fund),
            "plan": {
                "buy1": plan.get("buy1"), "buy2": plan.get("buy2"),
                "stop_loss": plan.get("stop_loss"),
                "target1": plan.get("target1"), "target2": plan.get("target2"),
                "risk_reward": plan.get("risk_reward"),
            },
            "structure": pred.get("structure"),
        }
    except Exception:  # noqa: BLE001
        return None


def _build(month: str) -> None:
    global _building
    results: List[Dict] = []
    try:
        pool = _candidate_pool()
        with _lock:
            _progress.update({"done": 0, "total": len(pool), "current": ""})
        for i, sym in enumerate(pool):
            with _lock:
                _progress.update({"done": i, "current": sym})
            r = _evaluate(sym)
            if r:
                results.append(r)
            time.sleep(_DELAY_S)
        with _lock:
            _progress.update({"done": len(pool), "current": ""})

        # rank purely by the blended score; prefer non-downtrends on ties
        results.sort(key=lambda r: (r["scores"]["combined"],
                                    r.get("structure") != "downtrend"),
                     reverse=True)
        picks = results[:_TOP_N]
        for rank, p in enumerate(picks, start=1):
            p["rank"] = rank
        payload = {
            "status": "ready",
            "month": month,
            "month_label": datetime.strptime(month, "%Y-%m").strftime("%B %Y"),
            "generated_at": utils.now_iso(),
            "pool": f"{_POOL_INDEX} ∪ {_EXTRA_INDEX}",
            "scanned": len(pool),
            "usable": len(results),
            "blend": {"technical": _TECH_W, "fundamental": _FUND_W},
            "picks": picks,
            "disclaimer": _DISCLAIMER,
        }
        _save_cached(month, payload)
    finally:
        with _lock:
            _building = False


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


def get_recommendations(force: bool = False) -> Dict:
    """API entry: cached picks, or a 'building' payload with live progress."""
    month = _month_key()
    cached = _load_cached(month)
    if cached and not force:
        cached.pop("_age_days", None)
        # a stale-but-usable cache still gets refreshed quietly in background
        start_build(False)
        return cached
    start_build(force)
    with _lock:
        prog = dict(_progress)
        building = _building
    return {"status": "building" if building else "empty",
            "month": month,
            "month_label": datetime.now(timezone.utc).strftime("%B %Y"),
            "progress": prog,
            "pool": f"{_POOL_INDEX} ∪ {_EXTRA_INDEX}",
            "picks": [],
            "disclaimer": _DISCLAIMER}


if __name__ == "__main__":
    import sys
    print(json.dumps(get_recommendations("--force" in sys.argv), indent=2)[:3000])
