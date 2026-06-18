"""
scorer.py
=========
Turns a scrape (see scraper.py) into a 0-100 fundamental score with a
transparent, per-metric breakdown.

Every metric always produces a display value — never N/A or blank.
When real data is unavailable, a clearly-labelled estimate is used and
the metric is flagged estimated=True so the UI can mark it with *.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import config
import utils

Status = str  # "good" | "warn" | "bad" | "na"


# ---------------------------------------------------------------------------
# Generic 0-10 scorers
# ---------------------------------------------------------------------------

def _clamp10(x: float) -> float:
    return max(0.0, min(10.0, x))


def higher_better(value: Optional[float], low: float, high: float) -> float:
    if value is None:
        return 5.0  # neutral mid-point when data absent
    if high == low:
        return 5.0
    return _clamp10((value - low) / (high - low) * 10.0)


def lower_better(value: Optional[float], best: float, worst: float) -> float:
    if value is None:
        return 5.0
    if worst == best:
        return 5.0
    return _clamp10((worst - value) / (worst - best) * 10.0)


def band_better(value: Optional[float], lo_bad: float, lo_good: float,
                hi_good: float, hi_bad: float) -> float:
    if value is None:
        return 5.0
    if value < lo_good:
        return higher_better(value, lo_bad, lo_good)
    if value > hi_good:
        return lower_better(value, hi_good, hi_bad)
    return 10.0


def _status(subscore: float, estimated: bool = False) -> Status:
    if estimated and subscore == 5.0:
        return "warn"  # neutral estimate → amber
    if subscore >= 7:
        return "good"
    if subscore >= 4:
        return "warn"
    return "bad"


# ---------------------------------------------------------------------------
# Pull tidy series from financials
# ---------------------------------------------------------------------------

def _series(financials: List[Dict], field: str) -> List[Tuple[int, float]]:
    out = [(r["year"], r[field]) for r in financials
           if r.get("year") is not None and r.get(field) is not None]
    out.sort(key=lambda t: t[0])
    return out


def _latest(financials: List[Dict], field: str) -> Optional[float]:
    s = _series(financials, field)
    return s[-1][1] if s else None


def _latest_year(financials: List[Dict], field: str) -> Optional[int]:
    s = _series(financials, field)
    return s[-1][0] if s else None


def _growth(financials: List[Dict], field: str, years: int) -> Optional[float]:
    s = _series(financials, field)
    if len(s) < 2:
        return None
    last_year, last_val = s[-1]
    target_year = last_year - years
    base = min(s, key=lambda t: abs(t[0] - target_year))
    span = last_year - base[0]
    if span <= 0:
        base = s[0]
        span = last_year - base[0]
    if span <= 0:
        return None
    return utils.cagr(base[1], last_val, span)


def _growth_years_used(financials: List[Dict], field: str, years: int) -> str:
    s = _series(financials, field)
    if len(s) < 2:
        return ""
    last_year = s[-1][0]
    target_year = last_year - years
    base = min(s, key=lambda t: abs(t[0] - target_year))
    return f"FY{base[0]}–FY{last_year}"


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _fmt_pct(v: float, estimated: bool = False) -> str:
    suffix = " *" if estimated else ""
    return f"{v:.1f}%{suffix}"


def _fmt_ratio(v: float, estimated: bool = False) -> str:
    suffix = " *" if estimated else ""
    return f"{v:.2f}x{suffix}"


# ---------------------------------------------------------------------------
# Metric functions
# Each returns: (subscore 0-10, raw value, display str, note, source_note, estimated)
# subscore and raw value are ALWAYS a number — never None.
# ---------------------------------------------------------------------------

# Sector-level typical growth used when company-specific data is absent
_SECTOR_GROWTH_PROXY = 8.0     # % — conservative PSX mid-market
_SECTOR_MARGIN_PROXY = 10.0    # %
_SECTOR_ROE_PROXY    = 12.0    # %
_SECTOR_DE_PROXY     = 1.0     # ratio
_SECTOR_CR_PROXY     = 1.3     # ratio
_SECTOR_CF_PROXY     = 0.85    # ratio


def m_revenue_growth(fin, banking=False):
    field = "net_interest_income" if banking else "revenue"
    g = (_growth(fin, field, 3) or _growth(fin, "revenue", 3))
    yr_range = (_growth_years_used(fin, field, 3) or _growth_years_used(fin, "revenue", 3))
    estimated = False

    if g is None:
        g = (_growth(fin, field, 2) or _growth(fin, "revenue", 2) or
             _growth(fin, field, 1) or _growth(fin, "revenue", 1))
        yr_range = (_growth_years_used(fin, field, 2) or _growth_years_used(fin, "revenue", 2))
        estimated = g is not None

    if g is None:
        g = _SECTOR_GROWTH_PROXY
        estimated = True
        yr_range = ""

    sub = higher_better(g, -10, 18)
    note = "3-yr revenue CAGR" if not banking else "3-yr core-income CAGR"
    if estimated:
        note = "Revenue CAGR (estimated — limited history) *"
    src = f"Source: {yr_range}" if yr_range else "Sector proxy (no history)"
    return sub, g, _fmt_pct(g, estimated), note, src, estimated


def m_profit_margin(fin, banking=False):
    rev = _latest(fin, "revenue") or _latest(fin, "net_interest_income")
    np_ = _latest(fin, "net_profit")
    m = utils.pct(np_, rev)
    estimated = False

    if m is None and np_ is not None:
        ta = _latest(fin, "total_assets")
        if ta:
            m = utils.pct(np_, ta)
            estimated = True

    if m is None:
        m = _SECTOR_MARGIN_PROXY
        estimated = True

    yr = _latest_year(fin, "net_profit")
    sub = higher_better(m, 2, 22 if not banking else 28)
    note = "Net profit margin"
    if estimated:
        note = "Return on assets proxy *" if np_ is not None else "Sector proxy *"
    src = f"FY{yr}" if yr else "Sector estimate"
    return sub, m, _fmt_pct(m, estimated), note, src, estimated


def m_eps_growth(fin, banking=False):
    g = _growth(fin, "eps", 3)
    yr_range = _growth_years_used(fin, "eps", 3)
    estimated = False

    if g is None:
        g = _growth(fin, "eps", 2) or _growth(fin, "eps", 1)
        yr_range = _growth_years_used(fin, "eps", 2)
        estimated = g is not None

    if g is None:
        # Derive from net_profit growth as proxy
        g = _growth(fin, "net_profit", 3) or _growth(fin, "net_profit", 2)
        yr_range = _growth_years_used(fin, "net_profit", 3) or \
                   _growth_years_used(fin, "net_profit", 2)
        estimated = g is not None
        if g is not None:
            yr_range = yr_range  # keep range

    if g is None:
        g = _SECTOR_GROWTH_PROXY
        estimated = True
        yr_range = ""

    sub = higher_better(g, -10, 18)
    note = "3-yr EPS CAGR"
    if estimated:
        note = "EPS CAGR (proxy from net profit) *" if yr_range else "Sector proxy *"
    src = f"Source: {yr_range}" if yr_range else "Sector estimate"
    return sub, g, _fmt_pct(g, estimated), note, src, estimated


def m_debt_to_equity(fin, banking=False):
    debt = _latest(fin, "total_debt")
    eq   = _latest(fin, "total_equity")
    estimated = False

    if debt is None:
        tl = _latest(fin, "total_liabilities")
        if tl is not None:
            debt = tl * 0.70   # conservative: 70 % of liabilities as debt
            estimated = True

    if eq is None or debt is None:
        # Both sides missing — use sector proxy
        de = _SECTOR_DE_PROXY
        estimated = True
        sub = lower_better(de, 0.4, 2.0)
        yr_d = yr_e = None
        note = "Liabilities-to-equity (sector proxy) *"
        src = "Sector estimate"
        return sub, de, _fmt_ratio(de, True), note, src, True

    de = debt / eq if eq != 0 else _SECTOR_DE_PROXY
    yr_d = _latest_year(fin, "total_debt") or _latest_year(fin, "total_liabilities")
    yr_e = _latest_year(fin, "total_equity")

    sub = lower_better(de, 0.4, 2.0)
    note = "Debt-to-equity (lower is better)"
    if estimated:
        note = "Liabilities-to-equity (debt detail unavailable) *"
    src = f"FY{yr_d}/{yr_e}" if yr_d and yr_e else "Latest available"
    return sub, de, _fmt_ratio(de, estimated), note, src, estimated


def m_roe(fin, banking=False):
    np_ = _latest(fin, "net_profit")
    eq  = _latest(fin, "total_equity")
    roe = utils.pct(np_, eq)
    estimated = False

    if roe is None:
        roe = _SECTOR_ROE_PROXY
        estimated = True

    yr = _latest_year(fin, "net_profit")
    sub = higher_better(roe, 5, 22)
    note = "Return on equity"
    if estimated:
        note = "Return on equity (sector proxy) *"
    src = f"FY{yr}" if yr else "Sector estimate"
    return sub, roe, _fmt_pct(roe, estimated), note, src, estimated


def m_current_ratio(fin, banking=False):
    ca = _latest(fin, "current_assets")
    cl = _latest(fin, "current_liabilities")
    estimated = False

    if ca is None:
        ta = _latest(fin, "total_assets")
        if ta is not None:
            ca = ta * 0.40
            estimated = True

    if cl is None:
        tl = _latest(fin, "total_liabilities")
        if tl is not None:
            cl = tl * 0.55
            estimated = True

    if ca is None or cl is None or cl == 0:
        cr = _SECTOR_CR_PROXY
        estimated = True
    else:
        cr = ca / cl

    yr = (_latest_year(fin, "current_assets") or
          _latest_year(fin, "total_assets"))
    sub = band_better(cr, 0.8, 1.5, 3.0, 5.0)
    note = "Current ratio (liquidity)"
    if estimated:
        note = "Estimated current ratio *"
    src = f"FY{yr}" if yr else "Sector estimate"
    return sub, cr, _fmt_ratio(cr, estimated), note, src, estimated


def m_cashflow_quality(fin, banking=False):
    ocf = _latest(fin, "operating_cashflow")
    np_ = _latest(fin, "net_profit")
    estimated = False

    if ocf is None and np_ is not None:
        ocf = np_ * 0.90
        estimated = True

    if ocf is None or np_ is None or np_ == 0:
        ratio = _SECTOR_CF_PROXY
        estimated = True
    else:
        ratio = ocf / np_

    yr = (_latest_year(fin, "operating_cashflow") or
          _latest_year(fin, "net_profit"))
    sub = higher_better(ratio, 0.4, 1.1)
    note = "Operating cash flow vs net profit"
    if estimated:
        note = "Cash flow quality (estimated) *"
    src = f"FY{yr}" if yr else "Sector estimate"
    return sub, ratio, _fmt_ratio(ratio, estimated), note, src, estimated


def m_dividend(fin, banking=False):
    s = _series(fin, "dividend_per_share")
    estimated = False

    if not s:
        # No dividend records at all → score as 0 paid / 1 year
        sub = 0.0
        disp = "0/1 yrs *"
        src = "No dividend record found"
        return sub, 0.0, disp, "Dividend consistency (no data) *", src, True

    paid = sum(1 for _, v in s if v and v > 0)
    consistency = paid / len(s)
    sub = higher_better(consistency, 0.3, 1.0)
    yr_range = f"FY{s[0][0]}–FY{s[-1][0]}"
    disp = f"{paid}/{len(s)} yrs"
    return sub, consistency * 100, disp, "Years a dividend was paid", yr_range, estimated


def m_capital_adequacy(fin, banking=False):
    car = _latest(fin, "capital_adequacy")
    estimated = False

    if car is None:
        car = 11.5   # SBP minimum floor
        estimated = True

    yr = _latest_year(fin, "capital_adequacy")
    sub = higher_better(car, 11.5, 18)
    note = "Capital adequacy ratio"
    if estimated:
        note = "Capital adequacy (SBP minimum floor) *"
    src = f"FY{yr}" if yr else "Estimated at SBP minimum"
    return sub, car, _fmt_pct(car, estimated), note, src, estimated


METRIC_FUNCS = {
    "revenue_growth":   ("Revenue Growth",   m_revenue_growth),
    "profit_margin":    ("Profit Margin",     m_profit_margin),
    "eps_growth":       ("EPS Growth",        m_eps_growth),
    "debt_to_equity":   ("Debt / Equity",     m_debt_to_equity),
    "roe":              ("Return on Equity",  m_roe),
    "current_ratio":    ("Current Ratio",     m_current_ratio),
    "cashflow_quality": ("Cash Flow Quality", m_cashflow_quality),
    "dividend":         ("Dividend",          m_dividend),
    "capital_adequacy": ("Capital Adequacy",  m_capital_adequacy),
}


# ---------------------------------------------------------------------------
# Model selection + verdict
# ---------------------------------------------------------------------------

def is_financial_sector(sector: str) -> bool:
    s = (sector or "").upper()
    return any(k in s for k in config.FINANCIAL_SECTOR_KEYWORDS)


def verdict_for(score: float) -> Dict[str, str]:
    for lo, label, blurb in config.VERDICTS:
        if score >= lo:
            return {"label": label, "blurb": blurb}
    return {"label": "Unrated", "blurb": ""}


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def score_company(scrape: Dict) -> Dict:
    fin = scrape.get("financials", []) or []
    sector = (scrape.get("profile", {}).get("sector")
              or scrape.get("sector", "") or "")
    banking = is_financial_sector(sector)
    weights = config.WEIGHTS_BANKING if banking else config.WEIGHTS_GENERAL

    metrics: List[Dict] = []
    weighted_sum   = 0.0
    total_weight   = sum(weights.values())
    estimated_weight = 0.0

    for key, weight in weights.items():
        label, func = METRIC_FUNCS[key]
        result = func(fin, banking=banking)

        if len(result) == 6:
            sub, value, display, note, src_note, estimated = result
        else:
            sub, value, display, note = result
            src_note, estimated = "", False

        # sub is always a number now; status derives from it
        st = _status(sub, estimated)

        metrics.append({
            "key":         key,
            "label":       label,
            "weight":      round(weight * 100),
            "subscore":    round(sub, 1),
            "value":       value,
            "display":     display,
            "status":      st,
            "note":        note,
            "source_note": src_note,
            "estimated":   estimated,
        })

        weighted_sum += (sub / 10.0) * weight
        if estimated:
            estimated_weight += weight

    score = (weighted_sum / total_weight) * 100.0 if total_weight > 0 else 0.0

    # confidence = share of weight backed by real (non-estimated) data
    real_weight    = total_weight - estimated_weight
    confidence_pct = round((real_weight / total_weight) * 100) if total_weight > 0 else 0

    highlights = [m["label"] for m in metrics if m["status"] == "good"]
    concerns   = [m["label"] for m in metrics if m["status"] == "bad"]

    return {
        "symbol":       scrape.get("symbol"),
        "model":        "banking" if banking else "general",
        "sector":       sector,
        "score":        round(score, 1),
        "verdict":      verdict_for(score),
        "coverage":     round(total_weight, 2),
        "confidence":   confidence_pct,
        "data_quality": scrape.get("data_quality"),
        "metrics":      metrics,
        "trends":       _build_trends(fin),
        "highlights":   highlights[:4],
        "concerns":     concerns[:4],
        "warnings":     scrape.get("warnings", []),
        "profile":      scrape.get("profile", {}),
        "reports":      scrape.get("reports", []),
        "price_history": scrape.get("price_history", []),
        "scraped_at":   scrape.get("scraped_at"),
    }


def _build_trends(fin: List[Dict]) -> Dict[str, List[Dict]]:
    fields = ["revenue", "net_profit", "eps", "total_equity",
              "operating_cashflow", "total_assets"]
    trends: Dict[str, List[Dict]] = {}
    for f in fields:
        trends[f] = [{"year": y, "value": v} for y, v in _series(fin, f)]
    return trends


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    demo = {
        "symbol": "DEMO",
        "profile": {"sector": "CEMENT", "price": 145.3},
        "data_quality": 0.9,
        "warnings": [],
        "financials": [
            {"year": 2019, "revenue": 80_000, "net_profit": 12_000, "eps": 9.1,
             "total_assets": 150_000, "total_equity": 90_000, "total_debt": 25_000,
             "current_assets": 40_000, "current_liabilities": 22_000,
             "operating_cashflow": 13_500, "dividend_per_share": 4},
            {"year": 2020, "revenue": 88_000, "net_profit": 13_800, "eps": 10.3,
             "total_assets": 160_000, "total_equity": 98_000, "total_debt": 24_000,
             "current_assets": 44_000, "current_liabilities": 23_000,
             "operating_cashflow": 15_000, "dividend_per_share": 4.5},
            {"year": 2024, "revenue": 162_000, "net_profit": 28_900, "eps": 21.4,
             "total_assets": 224_000, "total_equity": 150_000, "total_debt": 16_000,
             "current_assets": 68_000, "current_liabilities": 28_000,
             "operating_cashflow": 30_000, "dividend_per_share": 8},
        ],
        "price_history": [], "reports": [],
        "scraped_at": utils.now_iso(),
    }

    result = score_company(demo)
    print(f"Score: {result['score']} ({result['verdict']['label']}) "
          f"confidence={result['confidence']}%")
    for m in result["metrics"]:
        bar = "█" * int(m["subscore"]) + "░" * (10 - int(m["subscore"]))
        est = " [est]" if m["estimated"] else ""
        print(f"  {m['label']:<22} {bar} {str(m['subscore']):>4}/10 "
              f"{m['display']:>14} [{m['status']}]{est}")
        if m["source_note"]:
            print(f"    ↳ {m['source_note']}")
PYEOF
echo "scorer done"