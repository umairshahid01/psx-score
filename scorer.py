"""
scorer.py
=========
Turns a scrape (see scraper.py) into a 0-100 fundamental score with a
transparent, per-metric breakdown.

Design choices:
  * Each metric is scored 0-10 from real thresholds, then weighted to 0-100.
  * Banks / insurers use a different model — leverage and "current ratio" are
    meaningless for a balance sheet that is *made* of leverage, so we swap in
    ROE weighting and Capital Adequacy.
  * Missing data is handled honestly: metrics with no data are dropped and the
    remaining weights are renormalised, while a `coverage` figure tells the user
    how much of the model actually had numbers behind it.

Nothing here touches the network — it is pure functions over a dict, so it is
easy to reason about and test.
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


def higher_better(value: Optional[float], low: float, high: float) -> Optional[float]:
    """low -> 0, high -> 10, linear between (value above is better)."""
    if value is None:
        return None
    if high == low:
        return 5.0
    return _clamp10((value - low) / (high - low) * 10.0)


def lower_better(value: Optional[float], best: float, worst: float) -> Optional[float]:
    """best -> 10, worst -> 0 (smaller value is better)."""
    if value is None:
        return None
    if worst == best:
        return 5.0
    return _clamp10((worst - value) / (worst - best) * 10.0)


def band_better(value: Optional[float], lo_bad: float, lo_good: float,
                hi_good: float, hi_bad: float) -> Optional[float]:
    """Peak 10 inside [lo_good, hi_good], ramping to 0 at lo_bad / hi_bad."""
    if value is None:
        return None
    if value < lo_good:
        return higher_better(value, lo_bad, lo_good)
    if value > hi_good:
        return lower_better(value, hi_good, hi_bad)
    return 10.0


def _status(subscore: Optional[float]) -> Status:
    if subscore is None:
        return "na"
    if subscore >= 7:
        return "good"
    if subscore >= 4:
        return "warn"
    return "bad"


# ---------------------------------------------------------------------------
# Pull tidy series out of the financials list
# ---------------------------------------------------------------------------
def _series(financials: List[Dict], field: str) -> List[Tuple[int, float]]:
    out = [(r["year"], r[field]) for r in financials
           if r.get("year") is not None and r.get(field) is not None]
    out.sort(key=lambda t: t[0])
    return out


def _latest(financials: List[Dict], field: str) -> Optional[float]:
    s = _series(financials, field)
    return s[-1][1] if s else None


def _growth(financials: List[Dict], field: str, years: int) -> Optional[float]:
    """CAGR over up to `years`, using the longest window we actually have."""
    s = _series(financials, field)
    if len(s) < 2:
        return None
    last_year, last_val = s[-1]
    # find the record closest to `years` back
    target_year = last_year - years
    base = min(s, key=lambda t: abs(t[0] - target_year))
    span = last_year - base[0]
    if span <= 0:
        base = s[0]
        span = last_year - base[0]
    if span <= 0:
        return None
    return utils.cagr(base[1], last_val, span)


# ---------------------------------------------------------------------------
# Metric definitions
# Each returns: (subscore 0-10 | None, raw value | None, display string, note)
# ---------------------------------------------------------------------------
def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def _fmt_ratio(v: Optional[float]) -> str:
    return f"{v:.2f}x" if v is not None else "—"


def m_revenue_growth(fin, banking=False):
    field = "net_interest_income" if banking else "revenue"
    g = _growth(fin, field, 3) or _growth(fin, "revenue", 3)
    sub = higher_better(g, -10, 18)
    note = "3-yr revenue CAGR" if not banking else "3-yr core-income CAGR"
    return sub, g, _fmt_pct(g), note


def m_profit_margin(fin, banking=False):
    rev = _latest(fin, "revenue") or _latest(fin, "net_interest_income")
    np_ = _latest(fin, "net_profit")
    m = utils.pct(np_, rev)
    sub = higher_better(m, 2, 22 if not banking else 28)
    return sub, m, _fmt_pct(m), "Net profit margin"


def m_eps_growth(fin, banking=False):
    g = _growth(fin, "eps", 3)
    sub = higher_better(g, -10, 18)
    return sub, g, _fmt_pct(g), "3-yr EPS CAGR"


def m_debt_to_equity(fin, banking=False):
    debt = _latest(fin, "total_debt")
    eq = _latest(fin, "total_equity")
    de = (debt / eq) if (debt is not None and eq not in (None, 0)) else None
    sub = lower_better(de, 0.4, 2.0)
    return sub, de, _fmt_ratio(de), "Debt-to-equity (lower is better)"


def m_roe(fin, banking=False):
    np_ = _latest(fin, "net_profit")
    eq = _latest(fin, "total_equity")
    roe = utils.pct(np_, eq)
    sub = higher_better(roe, 5, 22)
    return sub, roe, _fmt_pct(roe), "Return on equity"


def m_current_ratio(fin, banking=False):
    ca = _latest(fin, "current_assets")
    cl = _latest(fin, "current_liabilities")
    cr = (ca / cl) if (ca is not None and cl not in (None, 0)) else None
    sub = band_better(cr, 0.8, 1.5, 3.0, 5.0)
    return sub, cr, _fmt_ratio(cr), "Current ratio (liquidity)"


def m_cashflow_quality(fin, banking=False):
    ocf = _latest(fin, "operating_cashflow")
    np_ = _latest(fin, "net_profit")
    ratio = (ocf / np_) if (ocf is not None and np_ not in (None, 0)) else None
    sub = higher_better(ratio, 0.4, 1.1)
    return sub, ratio, _fmt_ratio(ratio), "Operating cash flow vs net profit"


def m_dividend(fin, banking=False):
    s = _series(fin, "dividend_per_share")
    if not s:
        return None, None, "—", "Dividend consistency"
    paid = sum(1 for _, v in s if v and v > 0)
    consistency = paid / len(s)
    sub = higher_better(consistency, 0.3, 1.0)
    disp = f"{paid}/{len(s)} yrs"
    return sub, consistency * 100, disp, "Years a dividend was paid"


def m_capital_adequacy(fin, banking=False):
    car = _latest(fin, "capital_adequacy")
    sub = higher_better(car, 11.5, 18)  # SBP minimum is ~11.5%
    return sub, car, _fmt_pct(car), "Capital adequacy ratio"


METRIC_FUNCS = {
    "revenue_growth":   ("Revenue Growth",   m_revenue_growth),
    "profit_margin":    ("Profit Margin",    m_profit_margin),
    "eps_growth":       ("EPS Growth",       m_eps_growth),
    "debt_to_equity":   ("Debt / Equity",    m_debt_to_equity),
    "roe":              ("Return on Equity", m_roe),
    "current_ratio":    ("Current Ratio",    m_current_ratio),
    "cashflow_quality": ("Cash Flow Quality", m_cashflow_quality),
    "dividend":         ("Dividend",         m_dividend),
    "capital_adequacy": ("Capital Adequacy", m_capital_adequacy),
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
              or _guess_sector(scrape) or "")
    banking = is_financial_sector(sector)
    weights = config.WEIGHTS_BANKING if banking else config.WEIGHTS_GENERAL

    metrics: List[Dict] = []
    weighted_sum = 0.0
    weight_with_data = 0.0

    for key, weight in weights.items():
        label, func = METRIC_FUNCS[key]
        sub, value, display, note = func(fin, banking=banking)
        metrics.append({
            "key": key,
            "label": label,
            "weight": round(weight * 100),
            "subscore": None if sub is None else round(sub, 1),
            "value": value,
            "display": display,
            "status": _status(sub),
            "note": note,
        })
        if sub is not None:
            weighted_sum += (sub / 10.0) * weight
            weight_with_data += weight

    # Renormalise over the weight we actually had data for.
    if weight_with_data > 0:
        score = (weighted_sum / weight_with_data) * 100.0
    else:
        score = 0.0
    coverage = weight_with_data  # already sums to <=1.0

    highlights = [m["label"] for m in metrics if m["status"] == "good"]
    concerns = [m["label"] for m in metrics if m["status"] == "bad"]

    return {
        "symbol": scrape.get("symbol"),
        "model": "banking" if banking else "general",
        "sector": sector,
        "score": round(score, 1),
        "verdict": verdict_for(score),
        "coverage": round(coverage, 2),
        "data_quality": scrape.get("data_quality"),
        "metrics": metrics,
        "trends": _build_trends(fin),
        "highlights": highlights[:4],
        "concerns": concerns[:4],
        "warnings": scrape.get("warnings", []),
        "profile": scrape.get("profile", {}),
        "reports": scrape.get("reports", []),
        "price_history": scrape.get("price_history", []),
        "scraped_at": scrape.get("scraped_at"),
    }


def _guess_sector(scrape: Dict) -> str:
    return scrape.get("sector", "")


def _build_trends(fin: List[Dict]) -> Dict[str, List[Dict]]:
    """Per-year series the dashboard slices into 1/3/5/10-year windows."""
    fields = ["revenue", "net_profit", "eps", "total_equity",
              "operating_cashflow", "total_assets"]
    trends: Dict[str, List[Dict]] = {}
    for f in fields:
        trends[f] = [{"year": y, "value": v} for y, v in _series(fin, f)]
    return trends


# ---------------------------------------------------------------------------
# CLI / self-test with synthetic data (no network)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    # Synthetic 6-year run to prove the math end to end.
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
            {"year": 2021, "revenue": 99_000, "net_profit": 16_200, "eps": 12.0,
             "total_assets": 172_000, "total_equity": 108_000, "total_debt": 22_000,
             "current_assets": 49_000, "current_liabilities": 24_000,
             "operating_cashflow": 17_500, "dividend_per_share": 5},
            {"year": 2022, "revenue": 118_000, "net_profit": 19_500, "eps": 14.4,
             "total_assets": 188_000, "total_equity": 120_000, "total_debt": 20_000,
             "current_assets": 55_000, "current_liabilities": 26_000,
             "operating_cashflow": 21_000, "dividend_per_share": 6},
            {"year": 2023, "revenue": 140_000, "net_profit": 23_800, "eps": 17.6,
             "total_assets": 205_000, "total_equity": 134_000, "total_debt": 18_000,
             "current_assets": 61_000, "current_liabilities": 27_000,
             "operating_cashflow": 25_500, "dividend_per_share": 7},
            {"year": 2024, "revenue": 162_000, "net_profit": 28_900, "eps": 21.4,
             "total_assets": 224_000, "total_equity": 150_000, "total_debt": 16_000,
             "current_assets": 68_000, "current_liabilities": 28_000,
             "operating_cashflow": 30_000, "dividend_per_share": 8},
        ],
        "price_history": [], "reports": [],
        "scraped_at": utils.now_iso(),
    }
    result = score_company(demo)
    print(f"Score: {result['score']}  ({result['verdict']['label']})  "
          f"coverage={result['coverage']}")
    for m in result["metrics"]:
        bar = "█" * int((m["subscore"] or 0)) + "░" * (10 - int((m["subscore"] or 0)))
        print(f"  {m['label']:<18} {bar} {str(m['subscore']):>4}/10  "
              f"{m['display']:>10}  [{m['status']}]")
