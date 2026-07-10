"""
predictor.py
============
The "Prediction" engine — a rules-based PSX stock outlook builder whose logic
is modeled on how experienced Pakistani market analysts actually read a chart
on-air (The Bulls & Bears style of analysis):

  1. TREND STRUCTURE first  — higher-highs / higher-lows vs lower-lows.
     "Jab tak higher-low break nahi hota, trend intact hai."
  2. DYNAMIC SUPPORT        — the daily EMA-21 as the trailing support, the
     EMA-89 (and 200-SMA when enough history) as the trend arbiter:
     as long as price sustains above them the bullish trend is alive.
  3. SUPPORT / RESISTANCE ZONES — clusters of recent swing lows / highs.
  4. FIBONACCI RETRACEMENT  — 38.2 / 50 / 61.8 % of the last dominant rally;
     a 50 % retracement that holds is a classic buy-on-dips zone.
  5. RSI(14) + DIVERGENCE   — bearish divergence at highs => expect sideways /
     correction (don't chase); bullish divergence at lows => accumulation.
  6. VOLUME                 — expansion confirms a move, dry volume warns.
  7. TRADE PLAN             — staggered Buy-1 / Buy-2 near supports, a defined
     stop-loss *below* the support cluster, and targets at the recent high /
     measured move.  Risk is always defined ("apna risk define karke chalein"):
     the note reminds the user to size the position so total portfolio risk
     stays around 2–3 %.
  8. FUNDAMENTAL OVERLAY    — the existing 0-100 fundamental score decides how
     much conviction the technical setup deserves (strong company on a dip is
     a very different animal from a weak company on a dip).

Everything here is pure computation on the scraped payload — no network.
The same algorithm is mirrored in JavaScript inside dashboard.html so DEMO
mode behaves identically; keep the two in sync if you tune the rules.

THIS IS EDUCATIONAL GUIDANCE ONLY — NOT A BUY OR SELL CALL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config

# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------

def ema(values: List[float], period: int) -> List[Optional[float]]:
    """Classic exponential moving average; None until enough data."""
    if not values:
        return []
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, n):
        d = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def swing_points(values: List[float], lookback: int = 3) -> Tuple[List[int], List[int]]:
    """Indices of local swing highs and lows (fractal style)."""
    highs, lows = [], []
    n = len(values)
    for i in range(lookback, n - lookback):
        win = values[i - lookback:i + lookback + 1]
        v = values[i]
        if v == max(win) and win.count(v) == 1:
            highs.append(i)
        if v == min(win) and win.count(v) == 1:
            lows.append(i)
    return highs, lows


# ---------------------------------------------------------------------------
# candle synthesis (weekly candles built from close series)
# ---------------------------------------------------------------------------

def infer_interval_days(dates: List[str]) -> float:
    """Median gap between points, in days."""
    if len(dates) < 3:
        return 1.0
    gaps = []
    prev = None
    for d in dates:
        try:
            t = datetime.fromisoformat(d[:10])
        except Exception:
            continue
        if prev is not None:
            gaps.append((t - prev).days)
        prev = t
    gaps = sorted(g for g in gaps if g > 0)
    return float(gaps[len(gaps) // 2]) if gaps else 1.0


def build_candles(history: List[Dict]) -> Tuple[List[Dict], str]:
    """
    Build OHLC candles from the close series.

    * daily data  -> weekly candles (open = first close of the ISO week,
                     high/low = max/min of the week, close = last close,
                     volume = sum if available)
    * weekly data -> monthly candles (same aggregation over calendar months)

    The candles are honestly labelled as being *synthesised from closing
    prices* because the public PSX EOD feed does not expose intraday OHLC.
    """
    if not history:
        return [], "weekly"
    interval = infer_interval_days([p["date"] for p in history])
    if interval <= 2.5:
        keyfn = lambda t: f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}"
        label = "weekly"
    else:
        keyfn = lambda t: f"{t.year}-{t.month:02d}"
        label = "monthly"

    buckets: Dict[str, Dict] = {}
    order: List[str] = []
    for p in history:
        try:
            t = datetime.fromisoformat(p["date"][:10])
        except Exception:
            continue
        c = p.get("close")
        if c is None:
            continue
        k = keyfn(t)
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"date": p["date"], "open": c, "high": c, "low": c,
                          "close": c, "volume": p.get("volume") or 0}
            order.append(k)
        else:
            b["high"] = max(b["high"], c)
            b["low"] = min(b["low"], c)
            b["close"] = c
            b["date"] = p["date"]
            b["volume"] += p.get("volume") or 0
    return [buckets[k] for k in order], label


# ---------------------------------------------------------------------------
# level detection
# ---------------------------------------------------------------------------

def cluster_levels(prices: List[float], tol: float = 0.015) -> List[Dict]:
    """Group nearby swing prices into support/resistance zones with strength."""
    levels: List[Dict] = []
    for p in sorted(prices):
        placed = False
        for lv in levels:
            if abs(p - lv["price"]) / lv["price"] <= tol:
                lv["touches"] += 1
                lv["price"] = (lv["price"] * (lv["touches"] - 1) + p) / lv["touches"]
                placed = True
                break
        if not placed:
            levels.append({"price": p, "touches": 1})
    levels.sort(key=lambda l: (-l["touches"], l["price"]))
    return levels


def fib_levels(closes: List[float]) -> Optional[Dict]:
    """
    Retracement levels of the dominant recent rally: from the lowest low of
    the recent window to the highest high after it.
    """
    n = len(closes)
    if n < 20:
        return None
    window = closes[-min(n, 120):]
    lo_i = min(range(len(window)), key=lambda i: window[i])
    after = window[lo_i:]
    if len(after) < 5:
        return None
    hi_rel = max(range(len(after)), key=lambda i: after[i])
    lo, hi = window[lo_i], after[hi_rel]
    if hi <= lo * 1.05:               # no meaningful rally to retrace
        return None
    rng = hi - lo
    return {
        "rally_low": lo, "rally_high": hi,
        "levels": {
            "23.6%": hi - rng * 0.236,
            "38.2%": hi - rng * 0.382,
            "50.0%": hi - rng * 0.500,
            "61.8%": hi - rng * 0.618,
        },
    }


# ---------------------------------------------------------------------------
# v3.6 — Wyckoff market-cycle engine
# ---------------------------------------------------------------------------
# Accumulation -> Advancing (markup) -> Distribution -> Decline (markdown).
# The detector looks for the most recent CONSOLIDATION BOX (a stretch where
# price stayed inside a tight band), reads the trend that led into it, where
# price sits in the 52-week range, and whether volumes inside the box dried
# up (classic accumulation) — then names the phase in plain words.
# The identical algorithm is mirrored in JavaScript inside dashboard.html so
# DEMO mode behaves the same; keep the two in sync if you tune the numbers.

WYCKOFF_TOL = 0.18            # box height: total range <= 18% of the low
WYCKOFF_MAX_OFFSET = 8        # bars allowed OUTSIDE the box at the right edge


def wyckoff(closes: List[float], vols: List, per_year: int,
            price: float, structure: str) -> Dict:
    n = len(closes)
    out: Dict = {"phase": "undefined", "phase_label": "Not enough history",
                 "box": None, "breakout": None, "prior_trend": None,
                 "vol_state": None, "base_months": None, "points": 0.0,
                 "cycle_seg": None, "notes": []}
    if n < 40 or not price:
        return out

    lookback = min(n, per_year * 2)
    min_bars = max(8, round(per_year * 0.12))     # ~8 weekly / ~30 daily bars
    lim = n - lookback

    # ---- find the longest recent consolidation box ------------------------
    best = None
    for off in range(0, min(WYCKOFF_MAX_OFFSET + 1, n - 5)):
        end = n - 1 - off
        hi = lo = closes[end]
        start = end
        i = end - 1
        while i >= lim and i >= 0:
            h2, l2 = max(hi, closes[i]), min(lo, closes[i])
            if l2 > 0 and (h2 - l2) / l2 <= WYCKOFF_TOL:
                hi, lo = h2, l2
                start = i
                i -= 1
            else:
                break
        bars = end - start + 1
        if bars >= min_bars and (best is None or bars > best["bars"]):
            best = {"hi": hi, "lo": lo, "start": start, "end": end,
                    "bars": bars, "off": off}

    yr = closes[-per_year:] if n >= per_year else closes
    hi52, lo52 = max(yr), min(yr)
    posr = (price - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5

    phase = None
    if best:
        box_hi, box_lo = best["hi"], best["lo"]
        off = best["off"]
        breakout = off > 0 and price > box_hi * 1.01
        breakdown = off > 0 and price < box_lo * 0.99
        in_box = off == 0 and box_lo * 0.99 <= price <= box_hi * 1.01

        pstart = max(0, best["start"] - best["bars"])
        base = closes[pstart] if closes[pstart] else None
        prior_chg = ((closes[best["start"]] - base) / base) if base else 0.0
        prior = "down" if prior_chg < -0.10 else "up" if prior_chg > 0.10 else "flat"

        vol_state = None
        if vols and any(v for v in vols if v):
            inbox = [v for v in vols[best["start"]:best["end"] + 1] if v]
            before = [v for v in vols[pstart:best["start"]] if v]
            if inbox and before:
                ratio = (sum(inbox) / len(inbox)) / max(1e-9, sum(before) / len(before))
                vol_state = ("drying" if ratio < 0.8
                             else "expanding" if ratio > 1.25 else "steady")

        # a real base must be FLAT — if price is still clearly sliding inside
        # the detected band, the decline simply hasn't finished yet
        b0 = closes[best["start"]]
        box_slope = (closes[best["end"]] - b0) / b0 if b0 else 0.0

        if breakout and prior in ("down", "flat"):
            phase = "breakout"
        elif breakdown and prior == "up":
            phase = "markdown"
        elif in_box and box_slope < -0.08 and structure == "downtrend":
            phase = "markdown"
        elif in_box:
            if prior == "up" and posr >= 0.75:
                phase = "distribution"
            elif prior == "down" or posr <= 0.45:
                phase = "accumulation"
            else:
                phase = "accumulation" if vol_state == "drying" else "neutral_range"
        else:
            phase = ("markup" if structure == "uptrend"
                     else "markdown" if structure == "downtrend" else "neutral_range")

        out["box"] = {"low": box_lo, "high": box_hi,
                      "start_idx": best["start"], "end_idx": best["end"],
                      "bars": best["bars"]}
        out["breakout"] = "up" if breakout else "down" if breakdown else None
        out["prior_trend"] = prior
        out["vol_state"] = vol_state
        out["base_months"] = round(best["bars"] / (per_year / 12.0), 1)
    else:
        phase = ("markup" if structure == "uptrend"
                 else "markdown" if structure == "downtrend" else "neutral_range")

    LABELS = {
        "accumulation":  ("Accumulation — smart money is quietly collecting",
                          "accumulation", 1.0),
        "breakout":      ("Breaking OUT of the accumulation box — the classic entry",
                          "accumulation", 2.5),
        "markup":        ("Advancing phase — the trend is being ridden",
                          "advancing", 1.5),
        "neutral_range": ("Ranging — the cycle hasn't picked a direction yet",
                          None, 0.0),
        "distribution":  ("Distribution — smart money may be handing shares to the crowd",
                          "distribution", -2.0),
        "markdown":      ("Decline phase — supply is in control",
                          "decline", -2.5),
        "undefined":     ("Not enough history", None, 0.0),
    }
    label, seg, pts = LABELS[phase]
    out.update({"phase": phase, "phase_label": label,
                "cycle_seg": seg, "points": pts})

    notes = []
    if out["box"]:
        bm = out["base_months"]
        notes.append(f"A consolidation box of about {bm} months was found "
                     f"between ₨{out['box']['low']:.2f} and ₨{out['box']['high']:.2f}.")
        if bm and bm >= 8 and phase in ("accumulation", "breakout", "markup"):
            notes.append("The bigger the base, the higher in the space — a long "
                         "boring base often powers a long rally after the break.")
    if out["vol_state"] == "drying" and phase in ("accumulation", "breakout"):
        notes.append("Volumes dried up inside the box — the free-floating supply "
                     "is getting cornered, textbook accumulation behaviour.")
    if out["vol_state"] == "expanding" and phase == "distribution":
        notes.append("Volumes are expanding near the highs — someone big may be "
                     "using the excitement to sell.")
    out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# the main call
# ---------------------------------------------------------------------------

def predict(scraped: Dict, fundamental: Dict) -> Dict:
    """
    scraped     — output of scraper.scrape_company()
    fundamental — output of scorer.score_company()
    returns a JSON-serialisable prediction payload (see keys below).
    """
    history = scraped.get("price_history") or []
    closes = [p["close"] for p in history if p.get("close") is not None]
    dates = [p["date"] for p in history]
    profile = scraped.get("profile") or {}
    price = profile.get("price") or (closes[-1] if closes else None)

    out: Dict = {
        "symbol": scraped.get("symbol"),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "disclaimer": ("This prediction is educational guidance built from "
                       "public price history and fundamentals. It is NOT a "
                       "buy or sell recommendation. Markets carry risk — "
                       "always do your own research and define your risk."),
        "price": price,
    }
    if len(closes) < 30 or price is None:
        out["error"] = "Not enough price history to build a prediction."
        return out

    interval = infer_interval_days(dates)
    per_year = 252 if interval <= 2.5 else 52
    # ---- indicators (periods follow the analysts' toolkit: EMA21 / EMA89 /
    #      200-MA on daily charts; scaled down when the feed is weekly) ----
    scale = 1 if interval <= 2.5 else 5
    p21, p89, p200 = max(3, 21 // scale), max(8, 89 // scale), max(20, 200 // scale)
    ema21, ema89 = ema(closes, p21), ema(closes, p89)
    sma200 = sma(closes, p200)
    rsi14 = rsi(closes, 14)

    last = len(closes) - 1
    v_ema21, v_ema89, v_sma200 = ema21[last], ema89[last], sma200[last]
    v_rsi = rsi14[last]

    # ---- trend structure: compare recent swing highs & lows ----
    lb = 2 if per_year == 52 else 3
    hi_idx, lo_idx = swing_points(closes, lb)
    structure = "sideways"
    if len(hi_idx) >= 2 and len(lo_idx) >= 2:
        hh = closes[hi_idx[-1]] > closes[hi_idx[-2]]
        hl = closes[lo_idx[-1]] > closes[lo_idx[-2]]
        if hh and hl:
            structure = "uptrend"
        elif (not hh) and (not hl):
            structure = "downtrend"

    above21 = v_ema21 is not None and price > v_ema21
    above89 = v_ema89 is not None and price > v_ema89
    above200 = v_sma200 is not None and price > v_sma200

    # ---- RSI divergence on the last two swing highs / lows ----
    divergence = None
    if len(hi_idx) >= 2 and rsi14[hi_idx[-1]] is not None and rsi14[hi_idx[-2]] is not None:
        if closes[hi_idx[-1]] > closes[hi_idx[-2]] and rsi14[hi_idx[-1]] < rsi14[hi_idx[-2]] - 1:
            divergence = {"type": "bearish", "points": [hi_idx[-2], hi_idx[-1]]}
    if divergence is None and len(lo_idx) >= 2 \
            and rsi14[lo_idx[-1]] is not None and rsi14[lo_idx[-2]] is not None:
        if closes[lo_idx[-1]] < closes[lo_idx[-2]] and rsi14[lo_idx[-1]] > rsi14[lo_idx[-2]] + 1:
            divergence = {"type": "bullish", "points": [lo_idx[-2], lo_idx[-1]]}

    # ---- support / resistance clusters from recent swings ----
    recent_cut = max(0, last - per_year)          # ~1 year of swings
    sup_prices = [closes[i] for i in lo_idx if i >= recent_cut and closes[i] < price]
    res_prices = [closes[i] for i in hi_idx if i >= recent_cut and closes[i] > price]
    supports = cluster_levels(sup_prices)[:3]
    resistances = cluster_levels(res_prices)[:3]
    supports.sort(key=lambda l: -l["price"])       # nearest support first
    resistances.sort(key=lambda l: l["price"])     # nearest resistance first

    fib = fib_levels(closes)
    hi52 = max(closes[-per_year:]) if len(closes) >= 5 else max(closes)
    lo52 = min(closes[-per_year:]) if len(closes) >= 5 else min(closes)

    # ---- volume read (only when the feed provides it) ----
    vols = [p.get("volume") for p in history]
    volume_note = None
    if any(v for v in vols if v):
        v_recent = [v for v in vols[-10:] if v]
        v_base = [v for v in vols[-60:-10] if v]
        if v_recent and v_base:
            ratio = (sum(v_recent) / len(v_recent)) / max(1e-9, sum(v_base) / len(v_base))
            volume_note = ("expanding" if ratio > 1.25 else
                           "drying up" if ratio < 0.75 else "normal")

    # ---- trade plan (staggered buys, defined stop, targets) ----
    # A buy zone must sit AT OR BELOW the market — "buy on dips", never chase.
    buy_cands = [s["price"] for s in supports if s["price"] < price * 0.999]
    if v_ema21 and v_ema21 < price * 0.999:
        buy_cands.append(v_ema21)                          # EMA-21 as the dip-buy net
    buy1 = max(buy_cands) if buy_cands else price * 0.97   # nearest one below price
    buy2 = None
    if fib:
        for k in ("50.0%", "61.8%"):
            lv = fib["levels"][k]
            if lv < buy1 * 0.995:
                buy2 = lv
                break
    if buy2 is None and len(supports) > 1 and supports[1]["price"] < buy1 * 0.995:
        buy2 = supports[1]["price"]
    if buy2 is None or buy2 >= buy1:
        buy2 = buy1 * 0.93
    deepest = min(buy1, buy2)
    stop = deepest * 0.96                                  # just below the support cluster
    if fib and fib["levels"]["61.8%"] < stop:
        stop = fib["levels"]["61.8%"] * 0.985
    t1 = resistances[0]["price"] if resistances else hi52
    if t1 <= price:
        t1 = hi52 if hi52 > price else price * 1.10
    t2 = max(hi52, t1) * 1.08                              # break of the high, next leg
    avg_buy = (buy1 + buy2) / 2
    rr = (t1 - avg_buy) / max(1e-9, (avg_buy - stop))

    # ---- weight of evidence → technical stance ----
    bull = bear = 0.0
    why_bull, why_bear = [], []
    if structure == "uptrend":
        bull += 2; why_bull.append("price is making higher highs and higher lows")
    elif structure == "downtrend":
        bear += 2; why_bear.append("price is making lower highs and lower lows")
    if above21:
        bull += 1; why_bull.append("price is holding above its 21-EMA (short-term support)")
    else:
        bear += 1; why_bear.append("price has slipped below its 21-EMA")
    if above89:
        bull += 1.5; why_bull.append("the bigger trend line (89-EMA) is still under the price")
    else:
        bear += 1.5; why_bear.append("price is below the 89-EMA — the bigger trend is under pressure")
    if above200:
        bull += 1
    elif v_sma200 is not None:
        bear += 1
    if divergence:
        if divergence["type"] == "bearish":
            bear += 1.5; why_bear.append("a bearish RSI divergence formed at the recent highs")
        else:
            bull += 1.5; why_bull.append("a bullish RSI divergence formed at the recent lows")
    if v_rsi is not None:
        if v_rsi >= 70:
            bear += 0.5; why_bear.append(f"RSI is hot at {v_rsi:.0f} — the stock may need to rest")
        elif v_rsi <= 35:
            bull += 0.5; why_bull.append(f"RSI is low at {v_rsi:.0f} — sellers look tired")
    if volume_note == "expanding" and structure == "uptrend":
        bull += 0.5; why_bull.append("volumes are expanding with the move")
    if volume_note == "drying up" and structure == "uptrend":
        bear += 0.25; why_bear.append("volumes are drying up — the rally needs fuel")

    # ---- v3.6: Wyckoff market-cycle phase (accumulation/advancing/...) ----
    wy = wyckoff(closes, vols, per_year, price, structure)
    if wy["points"] > 0:
        bull += wy["points"]
        why_bull.append("Wyckoff read: " + wy["phase_label"].lower())
    elif wy["points"] < 0:
        bear += -wy["points"]
        why_bear.append("Wyckoff read: " + wy["phase_label"].lower())

    tech_score = 50 + (bull - bear) * 7.5
    tech_score = max(5, min(95, tech_score))
    fund_score = float(fundamental.get("score") or 50)

    # v3.7 — two-factor verdict: chart health 55% + business health 45%.
    # (Insider transactions moved to the Fundamentals view as their own slab.)
    combined = tech_score * 0.55 + fund_score * 0.45

    if combined >= 72 and structure != "downtrend":
        verdict_key = "strong_bullish"
    elif combined >= 58:
        verdict_key = "bullish_dips"
    elif combined >= 44:
        verdict_key = "sideways"
    elif combined >= 30:
        verdict_key = "cautious"
    else:
        verdict_key = "bearish"

    VERDICTS = {
        "strong_bullish": ("Sunny ☀️", "Looking Strong",
                           "Trend, momentum and the business itself all point up. "
                           "The plan: buy the dips near support, not the spikes."),
        "bullish_dips":   ("Mostly Sunny 🌤️", "Positive — Buy on Dips",
                           "The path of least resistance is up, but only chase value: "
                           "wait for the price to come to your buy zones."),
        "sideways":       ("Cloudy ⛅", "Sideways — Be Patient",
                           "Bulls and bears are balanced right now. Let the stock "
                           "consolidate and prove itself before committing."),
        "cautious":       ("Rainy 🌧️", "Weak — Extra Care Needed",
                           "More warning signs than green lights. If you must "
                           "participate, keep positions small and stops tight."),
        "bearish":        ("Stormy ⛈️", "Negative — Better to Wait",
                           "Both the chart and the numbers are struggling. "
                           "Standing aside is also a position."),
    }
    face, label, blurb = VERDICTS[verdict_key]

    candles, candle_tf = build_candles(history)
    out.update({
        "interval": "daily" if interval <= 2.5 else "weekly",
        "candles": candles,
        "candle_timeframe": candle_tf,
        "indicators": {
            "ema21": v_ema21, "ema89": v_ema89, "sma200": v_sma200,
            "ema21_series": ema21, "ema89_series": ema89,
            "rsi": v_rsi, "rsi_series": rsi14,
        },
        "structure": structure,
        "divergence": divergence,
        "supports": supports,
        "resistances": resistances,
        "fibonacci": fib,
        "week52": {"high": hi52, "low": lo52},
        "volume_note": volume_note,
        "trade_plan": {
            "buy1": buy1, "buy2": buy2, "stop_loss": stop,
            "target1": t1, "target2": t2, "risk_reward": rr,
            "risk_note": ("Size the position so that hitting the stop-loss "
                          "costs no more than 2–3% of your total portfolio."),
        },
        "wyckoff": wy,
        "scores": {"technical": round(tech_score, 1),
                   "fundamental": round(fund_score, 1),
                   "combined": round(combined, 1)},
        "reasons": {"bullish": why_bull, "bearish": why_bear},
        "verdict": {"key": verdict_key, "face": face,
                    "label": label, "blurb": blurb},
    })
    return out
