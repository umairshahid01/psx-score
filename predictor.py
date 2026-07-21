"""
predictor.py
============
The "Prediction" engine — a rules-based PSX stock outlook builder whose logic
is modeled on how experienced Pakistani market analysts actually read a chart
on-air (The Bulls & Bears style of analysis):

  1. TREND STRUCTURE first  — higher-highs / higher-lows vs lower-lows.
     "Jab tak higher-low break nahi hota, trend intact hai."
  2. DYNAMIC SUPPORT        — the daily EMA-21 as the trailing support, the
     EMA-89 (and 200-SMA when enough history) as the trend arbiter.
  3. SUPPORT / RESISTANCE ZONES — clusters of recent swing lows / highs.
  4. FIBONACCI RETRACEMENT  — 38.2 / 50 / 61.8 % of the last dominant rally.
  5. RSI(14) + DIVERGENCE   — read IN CONTEXT of the trend, not mechanically.
  6. VOLUME                 — expansion confirms a move, dry volume warns.
  7. TRADE PLAN             — staggered Buy-1 / Buy-2 near supports, a defined
     stop-loss below the structural invalidation level, and measured-move
     targets.  Risk is always defined ("apna risk define karke chalein").
  8. FUNDAMENTAL OVERLAY    — the 0-100 fundamental score decides how much
     conviction the technical setup deserves.

--------------------------------------------------------------------------
v4.0 TECHNICAL ENGINE OVERHAUL — what was broken and why
--------------------------------------------------------------------------
The old engine could label a stock that had just printed a fresh 52-week high
as "Decline phase — supply is in control". That was not a one-off glitch on
one symbol; it was a chain of five compounding defects:

 (1) TREND STRUCTURE WAS DECIDED BY NOISE.
     Structure came from comparing the last TWO 3-bar fractals — a ~7-day
     window of micro-wiggles. Re-running the same stock shape with nothing but
     different day-to-day noise flipped the verdict between uptrend and
     downtrend. A verdict that changes when the noise changes is not a verdict.
     FIX: pivots now come from a ZIGZAG filter — a swing only counts once price
     has reversed by at least max(4%, 1.5 x ATR%) — so only structurally
     meaningful highs and lows are compared.

 (2) THE MOST RECENT PRICE ACTION WAS INVISIBLE.
     Fractal detection cannot confirm a pivot until `lookback` more bars have
     printed, so the final bars — the breakout itself — were excluded. A stock
     could be making a new high right now and the engine would not see it.
     FIX: the LIVE LEG is evaluated explicitly. If price is above the last
     confirmed pivot high, that is a higher-high in progress and it counts.

 (3) WYCKOFF BLINDLY INHERITED THE BROKEN STRUCTURE.
     When no consolidation box was found, phase was simply f(structure) — so
     defect (1) propagated straight into "Decline phase". And the box search
     only looked 8 bars back from the right edge, so a stock that had broken
     out 15 bars ago could never find the base it broke out of.
     FIX: the box search window now scales with the timeframe, and a genuine
     breakout is recognised regardless of what came before it (re-accumulation
     breakouts are the most powerful setup in the method, and the old code
     discarded them purely because the prior trend was "up").

 (4) NO SANITY GUARDS ANYWHERE.
     Nothing ever asked the obvious question: "can a stock sitting at its
     52-week high, above every moving average, possibly be in a decline?"
     FIX: position-in-range guards now veto impossible phases and impossible
     structures outright. This is the backstop that makes the reported failure
     unreachable no matter what the sub-detectors conclude.

 (5) RSI WAS READ MECHANICALLY.
     RSI 90 was scored as a NEGATIVE. In a confirmed uptrend a high RSI is a
     momentum thrust — historically a continuation signal, not a warning. And
     divergences were detected against pivots from months ago that price had
     long since invalidated, producing claims like "price made a new low"
     about a stock printing all-time highs. FIX: RSI is now regime-aware, and
     divergences expire — they must be recent, close together, and unbroken.

Also fixed: the trade plan could place "Buy zone 1" 35% below the market and
then advertise the resulting 8:1 reward-to-risk as if it were an opportunity.
Buy zones are now clamped to a reachable band, the stop sits below the real
structural invalidation level, targets use measured moves, and the plan
declares honestly whether it is actionable now or waiting for a pullback.

Every trade-plan number ships with its own formula and inputs in
`trade_plan["math"]`, which is exactly what the info popovers in the dashboard
render — so the explanation shown to the user is produced by the same
computation that produced the number and can never drift away from it.

DATA CONTRACT: everything is computed from closes up to and including the
as-of session (the previous trading day). Today's partial intraday session is
never mixed in. See utils.as_of_date() / utils.trim_history_to_as_of().

Everything here is pure computation on the scraped payload — no network.
The same algorithm is mirrored in JavaScript inside dashboard.html so DEMO
mode behaves identically; keep the two in sync if you tune the rules.

THIS IS EDUCATIONAL GUIDANCE ONLY — NOT A BUY OR SELL CALL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

import config

try:
    import utils
except Exception:                                    # pragma: no cover
    utils = None                                     # keeps the module importable


def round1(x: float) -> float:
    """
    Round to 1 decimal place the way JavaScript's toFixed(1) does.

    Python's built-in round() uses banker's rounding (round-half-to-even), so
    round(76.25, 1) is 76.2 while JS (+76.25).toFixed(1) is "76.3". That one
    tick of difference was enough to make the LIVE (Python) and DEMO
    (JavaScript) engines disagree on a displayed score even though every
    underlying level was identical. Matching JS explicitly keeps the two
    engines byte-identical, which is the whole point of mirroring them.
    """
    try:
        return float(Decimal(repr(float(x))).quantize(Decimal("0.1"),
                                                      rounding=ROUND_HALF_UP))
    except Exception:                                # pragma: no cover
        return round(float(x), 1)


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


def atr_pct(closes: List[float], period: int = 14) -> float:
    """
    Average true range as a PERCENT of price.

    The public PSX EOD feed carries no intraday high/low, so true range is
    approximated by the absolute close-to-close change — the honest option
    when only closes exist. This measures "how big is a normal move for THIS
    stock", which is what makes swing filtering and stop placement adaptive
    instead of one-size-fits-all.
    """
    if len(closes) < 3:
        return 2.0
    win = closes[-(period + 1):] if len(closes) > period else closes
    moves = []
    for i in range(1, len(win)):
        prev = win[i - 1]
        if prev:
            moves.append(abs(win[i] - prev) / prev * 100.0)
    if not moves:
        return 2.0
    return max(0.4, min(25.0, sum(moves) / len(moves)))


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
# v4.0 — ZIGZAG: only structurally significant swings
# ---------------------------------------------------------------------------
# A pivot is confirmed only after price has reversed by `threshold` percent
# from it. This is the single most important fix in the file: it is what stops
# day-to-day noise from deciding whether a stock is in an uptrend.

def zigzag(closes: List[float], threshold_pct: float) -> List[Dict]:
    """
    Return alternating significant pivots, oldest -> newest:
        [{"idx": int, "price": float, "kind": "H"|"L"}, ...]

    `threshold_pct` is the minimum reversal (in %) required to confirm a turn.
    The final, still-forming leg is NOT emitted here — callers handle it
    explicitly as the "live leg" so a breakout in progress stays visible.
    """
    n = len(closes)
    if n < 5 or threshold_pct <= 0:
        return []
    thr = threshold_pct / 100.0

    pivots: List[Dict] = []
    ext_i, ext_p = 0, closes[0]
    direction = 0                      # +1 = tracking a high, -1 = tracking a low

    for i in range(1, n):
        p = closes[i]
        if direction >= 0 and p > ext_p:
            ext_i, ext_p = i, p
        elif direction <= 0 and p < ext_p:
            ext_i, ext_p = i, p

        if direction >= 0 and ext_p > 0 and (ext_p - p) / ext_p >= thr:
            pivots.append({"idx": ext_i, "price": ext_p, "kind": "H"})
            direction = -1
            ext_i, ext_p = i, p
        elif direction <= 0 and ext_p > 0 and (p - ext_p) / ext_p >= thr:
            pivots.append({"idx": ext_i, "price": ext_p, "kind": "L"})
            direction = 1
            ext_i, ext_p = i, p

    cleaned: List[Dict] = []
    for pv in pivots:
        if cleaned and cleaned[-1]["kind"] == pv["kind"]:
            if (pv["kind"] == "H" and pv["price"] > cleaned[-1]["price"]) or \
               (pv["kind"] == "L" and pv["price"] < cleaned[-1]["price"]):
                cleaned[-1] = pv
            continue
        cleaned.append(pv)
    return cleaned


def read_structure(closes: List[float], price: float, pivots: List[Dict],
                   hi52: float, lo52: float,
                   v_ema21: Optional[float], v_ema89: Optional[float]) -> Dict:
    """
    Decide uptrend / downtrend / sideways from SIGNIFICANT swings plus the
    live (still-forming) leg, then apply non-negotiable sanity guards.
    """
    highs = [p for p in pivots if p["kind"] == "H"]
    lows = [p for p in pivots if p["kind"] == "L"]

    hh = hl = lh = ll = False
    basis: List[str] = []

    if len(highs) >= 2:
        hh = highs[-1]["price"] > highs[-2]["price"]
        lh = not hh
    if len(lows) >= 2:
        hl = lows[-1]["price"] > lows[-2]["price"]
        ll = not hl

    # ---- the LIVE LEG: price beyond the last confirmed pivot is a new
    #      high/low forming RIGHT NOW. The old engine was blind to this.
    if highs and price > highs[-1]["price"]:
        hh, lh = True, False
        basis.append("price is above its last significant swing high (new high forming)")
    if lows and price < lows[-1]["price"]:
        ll, hl = True, False
        basis.append("price is below its last significant swing low (new low forming)")

    structure = "sideways"
    confidence = "low"
    if hh and hl:
        structure = "uptrend"
        confidence = "high" if len(pivots) >= 4 else "medium"
        basis.append("higher highs and higher lows")
    elif lh and ll:
        structure = "downtrend"
        confidence = "high" if len(pivots) >= 4 else "medium"
        basis.append("lower highs and lower lows")
    elif hh or hl:
        structure, confidence = "uptrend", "low"
        basis.append("partial higher structure")
    elif lh or ll:
        structure, confidence = "downtrend", "low"
        basis.append("partial lower structure")

    # ---------------------------------------------------------------
    # SANITY GUARDS — the backstop. These override everything above.
    # A stock cannot be "in a downtrend" while it is printing 52-week
    # highs above every moving average, no matter what the pivots say.
    # ---------------------------------------------------------------
    rng = (hi52 - lo52) if hi52 > lo52 else 0.0
    posr = ((price - lo52) / rng) if rng > 0 else 0.5
    stacked_up = (v_ema21 is not None and v_ema89 is not None
                  and price > v_ema21 > v_ema89)
    stacked_dn = (v_ema21 is not None and v_ema89 is not None
                  and price < v_ema21 < v_ema89)

    if posr >= 0.90 and v_ema89 is not None and price > v_ema89 \
            and structure != "uptrend":
        structure, confidence = "uptrend", "high"
        basis = ["price is in the top 10% of its 52-week range and above the "
                 "89-EMA — that is an uptrend by definition"]
    elif posr <= 0.10 and v_ema89 is not None and price < v_ema89 \
            and structure != "downtrend":
        structure, confidence = "downtrend", "high"
        basis = ["price is in the bottom 10% of its 52-week range and below "
                 "the 89-EMA — that is a downtrend by definition"]
    elif stacked_up and structure == "downtrend":
        structure, confidence = "sideways", "low"
        basis = ["moving averages are stacked bullishly, so a downtrend call "
                 "cannot be justified"]
    elif stacked_dn and structure == "uptrend":
        structure, confidence = "sideways", "low"
        basis = ["moving averages are stacked bearishly, so an uptrend call "
                 "cannot be justified"]

    return {"structure": structure, "confidence": confidence,
            "basis": basis, "hh": hh, "hl": hl, "lh": lh, "ll": ll,
            "pos_in_range": round(posr, 3)}


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
            t = datetime.fromisoformat(str(d)[:10])
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

    * daily data  -> weekly candles
    * weekly data -> monthly candles

    v4.0: the candle OPEN is the PREVIOUS bucket's close, not the first close
    inside the bucket. With close-only data the old approach silently swallowed
    the gap between sessions, so a week that gapped up and then drifted showed
    a red (down) candle even though the stock finished the week far higher than
    it started. Chaining open to the prior close makes the candle body represent
    the actual period-over-period change — which is what the chart is read for.

    The candles remain honestly labelled as *synthesised from closing prices*
    because the public PSX EOD feed does not expose intraday OHLC.
    """
    if not history:
        return [], "weekly"
    interval = infer_interval_days([p.get("date") for p in history])
    if interval <= 2.5:
        keyfn = lambda t: f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}"
        label = "weekly"
    else:
        keyfn = lambda t: f"{t.year}-{t.month:02d}"
        label = "monthly"

    buckets: Dict[str, Dict] = {}
    order: List[str] = []
    prev_close: Optional[float] = None
    for p in history:
        try:
            t = datetime.fromisoformat(str(p["date"])[:10])
        except Exception:
            continue
        c = p.get("close")
        if c is None:
            continue
        k = keyfn(t)
        b = buckets.get(k)
        if b is None:
            op = prev_close if prev_close is not None else c
            buckets[k] = {"date": p["date"], "open": op,
                          "high": max(op, c), "low": min(op, c),
                          "close": c, "volume": p.get("volume") or 0}
            order.append(k)
        else:
            b["high"] = max(b["high"], c)
            b["low"] = min(b["low"], c)
            b["close"] = c
            b["date"] = p["date"]
            b["volume"] += p.get("volume") or 0
        prev_close = c
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
# v4.0 — Wyckoff market-cycle engine (rewritten)
# ---------------------------------------------------------------------------
# Accumulation -> Advancing (markup) -> Distribution -> Decline (markdown).
#
# What changed vs v3.6:
#   * the box search window scales with the timeframe instead of a fixed 8
#     bars, so a base broken weeks ago is still discoverable;
#   * a BREAKOUT counts as a breakout whatever the prior trend was;
#   * DISTRIBUTION now requires evidence of actual distribution (momentum
#     rolling over or a bearish divergence), not merely "price is high";
#   * position-in-range guards veto phases that are logically impossible.
#
# The identical algorithm is mirrored in JavaScript inside dashboard.html so
# DEMO mode behaves the same; keep the two in sync if you tune the numbers.

WYCKOFF_TOL = 0.18            # box height: total range <= 18% of the low


def wyckoff(closes: List[float], vols: List, per_year: int,
            price: float, structure: str,
            v_ema21: Optional[float] = None, v_ema89: Optional[float] = None,
            divergence: Optional[Dict] = None) -> Dict:
    n = len(closes)
    out: Dict = {"phase": "undefined", "phase_label": "Not enough history",
                 "box": None, "breakout": None, "prior_trend": None,
                 "vol_state": None, "base_months": None, "points": 0.0,
                 "cycle_seg": None, "notes": [], "guard": None}
    if n < 40 or not price:
        return out

    lookback = min(n, per_year * 2)
    min_bars = max(8, round(per_year * 0.12))     # ~8 weekly / ~30 daily bars
    lim = n - lookback

    # v4.0: how far back from the right edge a box may END. A stock that broke
    # out two months ago must still be able to find the base it broke out of.
    max_offset = max(8, round(per_year / 6.0))    # ~42 daily / ~9 weekly

    best = None
    for off in range(0, min(max_offset + 1, n - 5)):
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

    above21 = v_ema21 is not None and price > v_ema21
    above89 = v_ema89 is not None and price > v_ema89

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

        b0 = closes[best["start"]]
        box_slope = (closes[best["end"]] - b0) / b0 if b0 else 0.0

        if breakout:
            # v4.0: a breakout is a breakout. Coming out of a base that formed
            # AFTER a rally (re-accumulation) is if anything more bullish than
            # coming out of one that formed after a decline. The old rule
            # required prior in ("down","flat") and silently discarded these.
            phase = "breakout"
        elif breakdown:
            phase = "markdown"
        elif in_box and box_slope < -0.08 and structure == "downtrend":
            phase = "markdown"
        elif in_box:
            rolling_over = (not above21) or \
                (divergence or {}).get("type") == "bearish"
            if prior == "up" and posr >= 0.75 and rolling_over:
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

    # -----------------------------------------------------------------
    # SANITY GUARDS — impossible phases are vetoed here, unconditionally.
    # This is what makes "52-week high == Decline phase" unreachable.
    # -----------------------------------------------------------------
    guard = None
    if phase in ("markdown", "distribution") and posr >= 0.85 and above89:
        phase = "breakout" if out.get("breakout") == "up" else "markup"
        guard = ("A decline/distribution read was vetoed: price is in the top "
                 "15% of its 52-week range and holding above the 89-EMA.")
    elif phase in ("markup", "breakout", "accumulation") and posr <= 0.12 \
            and v_ema89 is not None and not above89:
        phase = "markdown"
        guard = ("An advance read was vetoed: price is in the bottom 12% of "
                 "its 52-week range and below the 89-EMA.")

    LABELS = {
        "accumulation":  ("Accumulation — smart money is quietly collecting",
                          "accumulation", 1.0),
        "breakout":      ("Breaking OUT of the consolidation box — the classic entry",
                          "advancing", 2.5),
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
                "cycle_seg": seg, "points": pts, "guard": guard})

    notes = []
    if out["box"]:
        bm = out["base_months"]
        notes.append(f"A consolidation box of about {bm} months was found "
                     f"between \u20a8{out['box']['low']:.2f} and "
                     f"\u20a8{out['box']['high']:.2f}.")
        if bm and bm >= 8 and phase in ("accumulation", "breakout", "markup"):
            notes.append("The bigger the base, the higher in the space — a long "
                         "boring base often powers a long rally after the break.")
    if out["vol_state"] == "drying" and phase in ("accumulation", "breakout"):
        notes.append("Volumes dried up inside the box — the free-floating supply "
                     "is getting cornered, textbook accumulation behaviour.")
    if out["vol_state"] == "expanding" and phase in ("breakout", "markup"):
        notes.append("Volumes expanded as price left the box — demand, not drift, "
                     "is doing the work.")
    if out["vol_state"] == "expanding" and phase == "distribution":
        notes.append("Volumes are expanding near the highs — someone big may be "
                     "using the excitement to sell.")
    if guard:
        notes.append(guard)
    out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# v4.0 — the trade plan, with its own arithmetic explained
# ---------------------------------------------------------------------------

def build_trade_plan(price: float, closes: List[float], supports: List[Dict],
                     resistances: List[Dict], fib: Optional[Dict],
                     v_ema21: Optional[float], hi52: float, lo52: float,
                     atrp: float, box: Optional[Dict],
                     structure: str) -> Dict:
    """
    Produce Buy-1 / Buy-2 / stop / targets / reward-risk AND a `math` block
    describing exactly how each figure was derived.

    Design rules (the old engine broke all four):
      * a buy zone must be REACHABLE — a level 35% below the market is not a
        buy zone, it is a different investment thesis;
      * the stop belongs below the structural invalidation level, sized by the
        stock's own volatility (ATR), not a flat percentage;
      * targets must have a derivation — nearest resistance, then a measured
        move — instead of an arbitrary +10%;
      * reward:risk must be quoted against the FIRST entry the user would
        actually take, so it cannot be inflated by an unreachable average.
    """
    math_: Dict[str, Dict] = {}
    unit = max(1.5, min(12.0, atrp))          # one "normal move" for this stock

    # ---------------- BUY 1 : nearest reachable structural support ----------
    band_lo, band_hi = price * (1 - 0.12), price * (1 - 0.005)
    cands: List[Tuple[float, str]] = []
    for s in supports:
        if band_lo <= s["price"] <= band_hi:
            cands.append((s["price"], f"support cluster ({s['touches']} touches)"))
    if v_ema21 is not None and band_lo <= v_ema21 <= band_hi:
        cands.append((v_ema21, "21-EMA (the dip-buy net)"))
    if fib:
        for k in ("23.6%", "38.2%"):
            lv = fib["levels"][k]
            if band_lo <= lv <= band_hi:
                cands.append((lv, f"Fibonacci {k} give-back"))
    if box and band_lo <= box["high"] <= band_hi:
        cands.append((box["high"], "top of the consolidation box (old ceiling, now floor)"))

    if cands:
        buy1, buy1_src = max(cands, key=lambda c: c[0])
    else:
        buy1 = price * (1 - unit / 100.0)
        buy1_src = (f"no structural level within reach \u2014 one ATR "
                    f"({unit:.1f}%) below the close")
    math_["buy1"] = {
        "title": "Buy zone 1 \u2014 the first nibble",
        "formula": "highest structural support inside [close \u2212 12%, close \u2212 0.5%]",
        "inputs": [f"Close (as-of session) = \u20a8{price:.2f}",
                   f"Search band = \u20a8{band_lo:.2f} \u2013 \u20a8{band_hi:.2f}",
                   f"Chosen level = {buy1_src}"],
        "result": buy1,
        "why": ("Never chase a spike. The first entry sits at the nearest level "
                "where buyers have already defended the stock, so the trade "
                "starts from strength rather than from hope. Capping the search "
                "at 12% is deliberate \u2014 a level further away than that is not "
                "a buy zone, it is a different trade."),
    }

    # ---------------- BUY 2 : the deeper dip -------------------------------
    # Depth is capped RELATIVE TO BUY 1, not to the raw price. Without this a
    # volatile stock could place Buy 2 26% under the market, which then drags
    # the stop-loss down with it and makes the whole plan unusable.
    max_depth = max(0.05, min(0.18, unit * 3 / 100.0))
    lo2, hi2 = buy1 * (1 - max_depth), buy1 * (1 - 0.03)
    c2: List[Tuple[float, str]] = []
    for s in supports:
        if lo2 <= s["price"] <= hi2:
            c2.append((s["price"], f"next support cluster ({s['touches']} touches)"))
    if fib:
        for k in ("38.2%", "50.0%", "61.8%"):
            lv = fib["levels"][k]
            if lo2 <= lv <= hi2:
                c2.append((lv, f"Fibonacci {k} give-back"))
    if box and lo2 <= box["low"] <= hi2:
        c2.append((box["low"], "floor of the consolidation box"))

    if c2:
        buy2, buy2_src = max(c2, key=lambda c: c[0])
    else:
        buy2 = buy1 * (1 - max(0.05, unit * 1.5 / 100.0))
        buy2_src = (f"no second structural level in range \u2014 1.5 ATR "
                    f"({unit * 1.5:.1f}%) under Buy 1")
    math_["buy2"] = {
        "title": "Buy zone 2 \u2014 the deeper dip",
        "formula": ("highest structural support inside "
                    "[Buy 1 \u2212 max-depth, Buy 1 \u2212 3%]"),
        "inputs": [f"Buy 1 = \u20a8{buy1:.2f}",
                   f"Max depth = min(18%, 3 \u00d7 ATR) = {max_depth * 100:.1f}%",
                   f"Search band = \u20a8{lo2:.2f} \u2013 \u20a8{hi2:.2f}",
                   f"Chosen level = {buy2_src}"],
        "result": buy2,
        "why": ("Staggering the entry means a deeper dip improves your average "
                "instead of ruining your week. The depth is capped relative to "
                "Buy 1 so the two zones stay part of the same trade \u2014 a second "
                "buy 30% lower is not an average-down, it is a different idea."),
    }

    # ---------------- STOP : below structural invalidation -----------------
    # Only a NEARBY support may anchor the stop. The old code took the highest
    # support below Buy 2 wherever it happened to be — on a stock that had
    # tripled, that was a level from last year's base, producing a "stop-loss"
    # more than half the share price away and a meaningless reward:risk.
    struct_low = None
    if supports:
        near = [s["price"] for s in supports
                if buy2 * 0.88 <= s["price"] < buy2 * 1.001]
        if near:
            struct_low = max(near)
    anchor = struct_low if struct_low else buy2
    buffer_pct = max(3.0, unit * 1.5)
    stop = anchor * (1 - buffer_pct / 100.0)
    stop = min(stop, buy2 * 0.99)             # always genuinely below Buy 2

    # hard ceiling on how much a single idea may risk from the first entry
    max_risk_pct = min(22.0, max(8.0, unit * 4.0))
    floor_stop = buy1 * (1 - max_risk_pct / 100.0)
    capped = stop < floor_stop
    if capped:
        stop = floor_stop
    math_["stop"] = {
        "title": "Stop-loss \u2014 the safety rope",
        "formula": "(nearest structural support below Buy 2) \u00d7 (1 \u2212 buffer)",
        "inputs": [f"Anchor level = \u20a8{anchor:.2f}"
                   + ("  (nearby structural support)" if struct_low else "  (Buy 2)"),
                   f"Stock's average daily move (ATR) = {atrp:.2f}%",
                   f"Buffer = max(3%, 1.5 \u00d7 ATR) = {buffer_pct:.2f}%",
                   f"Risk ceiling = min(22%, 4 \u00d7 ATR) = {max_risk_pct:.1f}% "
                   f"of Buy 1" + ("  \u2014 APPLIED" if capped else "")],
        "result": stop,
        "why": ("The stop sits below the level that would prove the idea wrong, "
                "with a cushion sized by how much THIS stock normally moves \u2014 "
                "so ordinary wiggle does not knock you out, but a real breakdown "
                "does. The risk ceiling is the backstop: if the nearest genuine "
                "support is unreasonably far below, the stop is pulled up rather "
                "than quietly asking you to risk half your capital."),
    }

    # ---------------- TARGET 1 : nearest ceiling or measured move ----------
    above = [r["price"] for r in resistances if r["price"] > price * 1.005]
    if above:
        t1 = min(above)
        t1_src = "nearest overhead resistance cluster"
    elif hi52 > price * 1.005:
        t1 = hi52
        t1_src = "the 52-week high"
    else:
        # Blue sky — nothing overhead at all. One projection is not enough:
        # a stock that broke out of a small late-stage box would otherwise get
        # a target barely above the close. Take the most credible of three
        # standard projections, then cap it so it stays an objective, not a
        # fantasy.
        projections: List[Tuple[float, str]] = []
        if box and box["high"] > box["low"]:
            mm = box["high"] - box["low"]
            projections.append((price + mm,
                                f"measured move: box height \u20a8{mm:.2f} "
                                f"projected from the close"))
        if fib and fib["rally_high"] > fib["rally_low"]:
            ext = fib["rally_low"] + (fib["rally_high"] - fib["rally_low"]) * 1.272
            if ext > price * 1.02:
                projections.append((ext, "Fibonacci 127.2% extension of the "
                                         "dominant rally"))
        projections.append((price * (1 + max(0.10, unit * 4 / 100.0)),
                            f"4 ATR ({max(10.0, unit * 4):.1f}%) projected "
                            f"from the close"))
        t1, t1_src = max(projections, key=lambda c: c[0])
        t1 = min(t1, price * 1.60)            # objective, not fantasy
        t1_src = "blue sky \u2014 " + t1_src
    t1 = max(t1, price * 1.02)
    math_["t1"] = {
        "title": "Target 1",
        "formula": "nearest resistance above the close, else a measured-move projection",
        "inputs": [f"Close (as-of session) = \u20a8{price:.2f}",
                   f"52-week high = \u20a8{hi52:.2f}",
                   f"Basis = {t1_src}"],
        "result": t1,
        "why": ("The first place sellers are likely to reappear. When there is "
                "nothing overhead at all (blue sky), the classic substitute is a "
                "measured move: project the height of the base the stock broke "
                "out of. Booking part of the position into strength here is what "
                "turns a good chart into a realised gain."),
    }

    # ---------------- TARGET 2 : extension beyond target 1 -----------------
    leg = max(t1 - buy1, price * 0.05)
    t2 = t1 + leg
    math_["t2"] = {
        "title": "Target 2 \u2014 if Target 1 breaks",
        "formula": "Target 1 + (Target 1 \u2212 Buy 1)",
        "inputs": [f"Target 1 = \u20a8{t1:.2f}", f"Buy 1 = \u20a8{buy1:.2f}",
                   f"Projected second leg = \u20a8{leg:.2f}"],
        "result": t2,
        "why": ("When a stock clears its first ceiling on strength, the classic "
                "projection is a second leg the same size as the first. This is "
                "a stretch objective, not an expectation \u2014 most trades should "
                "be substantially reduced before it."),
    }

    # ---------------- REWARD : RISK ----------------------------------------
    risk = max(1e-9, buy1 - stop)
    reward = t1 - buy1
    rr = reward / risk
    avg_buy = (buy1 + buy2) / 2.0
    rr_avg = (t1 - avg_buy) / max(1e-9, avg_buy - stop)
    math_["rr"] = {
        "title": "Reward vs risk",
        "formula": "(Target 1 \u2212 Buy 1) \u00f7 (Buy 1 \u2212 Stop-loss)",
        "inputs": [f"Reward = \u20a8{t1:.2f} \u2212 \u20a8{buy1:.2f} = \u20a8{reward:.2f}",
                   f"Risk = \u20a8{buy1:.2f} \u2212 \u20a8{stop:.2f} = \u20a8{risk:.2f}",
                   f"Ratio = {rr:.2f} : 1",
                   f"If both zones fill, average entry \u20a8{avg_buy:.2f} "
                   f"\u2192 {rr_avg:.2f} : 1"],
        "result": rr,
        "why": ("Measured from the FIRST entry you would actually take \u2014 not "
                "from a blended average you may never get \u2014 so the number "
                "cannot be flattered by an unreachable second buy zone. Anything "
                "under roughly 1.5:1 is a trade paying you too little for the "
                "risk it asks you to carry."),
    }

    # ---------------- is the plan actionable right now? --------------------
    dist = (price - buy1) / price * 100.0 if price else 0.0
    if price <= buy1 * 1.02:
        state = "actionable"
        state_note = "Price is at or inside Buy zone 1 right now."
    elif dist <= 8:
        state = "near"
        state_note = (f"Price is {dist:.1f}% above Buy zone 1 \u2014 close, but "
                      f"patience still pays.")
    else:
        state = "wait"
        state_note = (f"Price is {dist:.1f}% above Buy zone 1. This is a "
                      f"watch-list setup, not an entry \u2014 chasing it here puts "
                      f"the stop uncomfortably far away.")
    math_["state"] = {
        "title": "Is this plan live right now?",
        "formula": "(close \u2212 Buy 1) \u00f7 close",
        "inputs": [f"Close (as-of session) = \u20a8{price:.2f}",
                   f"Buy 1 = \u20a8{buy1:.2f}",
                   f"Distance = {dist:.1f}%"],
        "result": dist,
        "why": state_note,
    }

    return {"buy1": buy1, "buy2": buy2, "stop_loss": stop,
            "target1": t1, "target2": t2,
            "risk_reward": rr, "risk_reward_avg": rr_avg,
            "avg_buy": avg_buy, "atr_pct": atrp,
            "entry_state": state, "entry_note": state_note,
            "math": math_,
            "risk_note": ("Size the position so that hitting the stop-loss "
                          "costs no more than 2\u20133% of your total portfolio.")}


# ---------------------------------------------------------------------------
# the main call
# ---------------------------------------------------------------------------

def predict(scraped: Dict, fundamental: Dict) -> Dict:
    """
    scraped     — output of scraper.scrape_company()
    fundamental — output of scorer.score_company()
    returns a JSON-serialisable prediction payload.
    """
    raw_history = scraped.get("price_history") or []

    # ---- v4.0 AS-OF CONTRACT ---------------------------------------------
    # Everything below is computed from sessions up to and including the
    # previous trading day. Today's partial session is dropped outright.
    history = raw_history
    if utils is not None:
        history, as_of_meta = utils.trim_history_to_as_of(raw_history)
    else:                                    # pragma: no cover
        as_of_meta = {"as_of": None, "last_close_date": None, "stale": False,
                      "dropped": 0, "stale_days": 0}

    closes = [p["close"] for p in history if p.get("close") is not None]
    dates = [p.get("date") for p in history]
    profile = scraped.get("profile") or {}

    # The close of the as-of session is authoritative for EVERY calculation.
    # An intraday quote is never mixed into a close-based series.
    price = closes[-1] if closes else profile.get("price")

    out: Dict = {
        "symbol": scraped.get("symbol"),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "as_of": as_of_meta.get("as_of"),
        "as_of_close_date": as_of_meta.get("last_close_date"),
        "data_stale": bool(as_of_meta.get("stale")),
        "data_stale_days": as_of_meta.get("stale_days", 0),
        "bars_dropped_today": as_of_meta.get("dropped", 0),
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
    scale = 1 if interval <= 2.5 else 5
    p21, p89, p200 = max(3, 21 // scale), max(8, 89 // scale), max(20, 200 // scale)
    ema21, ema89 = ema(closes, p21), ema(closes, p89)
    sma200 = sma(closes, p200)
    rsi14 = rsi(closes, 14)

    last = len(closes) - 1
    v_ema21, v_ema89, v_sma200 = ema21[last], ema89[last], sma200[last]
    v_rsi = rsi14[last]

    hi52 = max(closes[-per_year:]) if len(closes) >= 5 else max(closes)
    lo52 = min(closes[-per_year:]) if len(closes) >= 5 else min(closes)

    # ---- v4.0 trend structure from SIGNIFICANT swings ---------------------
    atrp = atr_pct(closes)
    swing_thr = max(4.0, atrp * 1.5)          # noise filter, adaptive per stock
    pivots = zigzag(closes, swing_thr)
    st = read_structure(closes, price, pivots, hi52, lo52, v_ema21, v_ema89)
    structure = st["structure"]

    # legacy fractal indices are still used for level clustering & the chart
    lb = 2 if per_year == 52 else 3
    hi_idx, lo_idx = swing_points(closes, lb)

    above21 = v_ema21 is not None and price > v_ema21
    above89 = v_ema89 is not None and price > v_ema89
    above200 = v_sma200 is not None and price > v_sma200

    # ---- RSI divergence — recent, tight, and NOT already invalidated ------
    # The old version happily reported a divergence between two pivots months
    # apart that price had long since blown through, producing statements like
    # "price made a new low" about a stock printing all-time highs.
    divergence = None
    recent_cut = max(0, last - per_year // 2)
    max_gap = max(10, per_year // 4)

    hi_recent = [i for i in hi_idx if i >= recent_cut]
    lo_recent = [i for i in lo_idx if i >= recent_cut]

    if len(hi_recent) >= 2:
        a, b = hi_recent[-2], hi_recent[-1]
        if (b - a) <= max_gap and rsi14[a] is not None and rsi14[b] is not None \
                and closes[b] > closes[a] and rsi14[b] < rsi14[a] - 3 \
                and price < closes[b] * 1.01:      # not already broken out above
            divergence = {"type": "bearish", "points": [a, b]}
    if divergence is None and len(lo_recent) >= 2:
        a, b = lo_recent[-2], lo_recent[-1]
        if (b - a) <= max_gap and rsi14[a] is not None and rsi14[b] is not None \
                and closes[b] < closes[a] and rsi14[b] > rsi14[a] + 3 \
                and price > closes[b] * 0.99 and price < closes[a] * 1.15:
            divergence = {"type": "bullish", "points": [a, b]}

    # ---- support / resistance clusters from recent swings ----
    cut = max(0, last - per_year)                  # ~1 year of swings
    sup_prices = [closes[i] for i in lo_idx if i >= cut and closes[i] < price]
    res_prices = [closes[i] for i in hi_idx if i >= cut and closes[i] > price]
    supports = cluster_levels(sup_prices)[:3]
    resistances = cluster_levels(res_prices)[:3]
    supports.sort(key=lambda l: -l["price"])       # nearest support first
    resistances.sort(key=lambda l: l["price"])     # nearest resistance first

    fib = fib_levels(closes)

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

    # ---- Wyckoff market-cycle phase --------------------------------------
    wy = wyckoff(closes, vols, per_year, price, structure,
                 v_ema21, v_ema89, divergence)

    # ---- trade plan ------------------------------------------------------
    plan = build_trade_plan(price, closes, supports, resistances, fib,
                            v_ema21, hi52, lo52, atrp, wy.get("box"), structure)

    # ---- weight of evidence → technical stance ----
    bull = bear = 0.0
    why_bull, why_bear = [], []
    if structure == "uptrend":
        bull += 2
        why_bull.append("price is making higher highs and higher lows")
    elif structure == "downtrend":
        bear += 2
        why_bear.append("price is making lower highs and lower lows")
    if above21:
        bull += 1
        why_bull.append("price is holding above its 21-EMA (short-term support)")
    else:
        bear += 1
        why_bear.append("price has slipped below its 21-EMA")
    if above89:
        bull += 1.5
        why_bull.append("the bigger trend line (89-EMA) is still under the price")
    else:
        bear += 1.5
        why_bear.append("price is below the 89-EMA — the bigger trend is under pressure")
    if above200:
        bull += 1
    elif v_sma200 is not None:
        bear += 1
    if divergence:
        if divergence["type"] == "bearish":
            bear += 1.5
            why_bear.append("a bearish RSI divergence formed at the recent highs")
        else:
            bull += 1.5
            why_bull.append("a bullish RSI divergence formed at the recent lows")

    # ---- v4.0: RSI read IN CONTEXT of the trend --------------------------
    # A hot RSI inside a confirmed uptrend is a momentum THRUST, which is a
    # continuation signal. Treating it as a warning (the old behaviour) marks
    # down exactly the strongest stocks on the exchange.
    if v_rsi is not None:
        bearish_div = (divergence or {}).get("type") == "bearish"
        bullish_div = (divergence or {}).get("type") == "bullish"
        strong_up = structure == "uptrend" and above21 and above89
        if v_rsi >= 70:
            if strong_up and not bearish_div:
                bull += 0.75
                why_bull.append(f"RSI {v_rsi:.0f} — a momentum thrust; strong "
                                f"trends stay overbought far longer than most expect")
            else:
                bear += 0.5
                why_bear.append(f"RSI is hot at {v_rsi:.0f} without trend support "
                                f"— the move may need to rest")
        elif v_rsi <= 30:
            if structure == "downtrend" and not bullish_div:
                bear += 0.5
                why_bear.append(f"RSI {v_rsi:.0f} inside a downtrend — weak, not "
                                f"cheap; falling knives cut")
            else:
                bull += 0.5
                why_bull.append(f"RSI is washed out at {v_rsi:.0f} — sellers look "
                                f"exhausted")
        elif v_rsi <= 40 and structure != "downtrend":
            bull += 0.25
            why_bull.append(f"RSI {v_rsi:.0f} — the pullback has cooled the stock off")

    if volume_note == "expanding" and structure == "uptrend":
        bull += 0.5
        why_bull.append("volumes are expanding with the move")
    if volume_note == "drying up" and structure == "uptrend":
        bear += 0.25
        why_bear.append("volumes are drying up — the rally needs fuel")

    if wy["points"] > 0:
        bull += wy["points"]
        why_bull.append("Wyckoff read: " + wy["phase_label"].lower())
    elif wy["points"] < 0:
        bear += -wy["points"]
        why_bear.append("Wyckoff read: " + wy["phase_label"].lower())

    # honest note when the setup is sound but price has run away from it
    if plan["entry_state"] == "wait":
        why_bear.append("the price has run well above the nearest buy zone — "
                        "the setup is sound but the entry is not")

    tech_score = 50 + (bull - bear) * 7.5
    tech_score = max(5, min(95, tech_score))
    fund_score = float(fundamental.get("score") or 50)

    # two-factor verdict: chart health 55% + business health 45%.
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
            "atr_pct": atrp,
        },
        "structure": structure,
        "structure_detail": st,
        "swing_threshold_pct": round(swing_thr, 2),
        "pivots": pivots[-12:],
        "divergence": divergence,
        "supports": supports,
        "resistances": resistances,
        "fibonacci": fib,
        "week52": {"high": hi52, "low": lo52},
        "volume_note": volume_note,
        "trade_plan": plan,
        "wyckoff": wy,
        "scores": {"technical": round1(tech_score),
                   "fundamental": round1(fund_score),
                   "combined": round1(combined)},
        "reasons": {"bullish": why_bull, "bearish": why_bear},
        "verdict": {"key": verdict_key, "face": face,
                    "label": label, "blurb": blurb},
    })
    return out
