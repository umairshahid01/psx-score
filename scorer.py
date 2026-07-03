"""
scorer.py  (v3)
===============
Turns a scrape (see scraper.py) into a 0-100 fundamental score with a
transparent, per-metric breakdown.

STRICT ORIGINAL-DATA POLICY (v3)
--------------------------------
Every metric is computed ONLY from figures scraped from original sources:
the PSX company page (dps.psx.com.pk), the company's own filed statements
as aggregated by StockAnalysis/S&P Global, or the PSX end-of-day feed.

* NO sector proxies. NO "assume 70% of liabilities is debt". NO estimates.
* If the real inputs for a metric are missing, the metric shows N/A,
  contributes nothing, and the remaining metrics are RE-WEIGHTED so the
  score still spans 0-100.
* Exact arithmetic on original figures (e.g. NP ÷ revenue, CAGR of a real
  series, assets − equity) is calculation, not estimation, and is allowed.
* Every metric carries a clickable `source_url` pointing at the page the
  underlying numbers came from, so the user can verify it themselves.

THE FINANCIAL STORY (metric order, equal weights)
-------------------------------------------------
 1. Revenue Growth      — is the company selling more?
 2. Profit Margin       — how much of each rupee does it keep?
 3. EPS Growth          — is the profit per share growing?
 4. ROIC                — how well does it use ALL its capital?  (NOPAT ÷ avg invested capital)
 5. Return on Equity    — how well does it use the owners' capital?
 6. Debt / Equity       — how much of the business is borrowed?
 7. Current Ratio       — can it pay the bills due soon?
 8. Cash & Equivalents  — how much actual cash is on hand?
 9. Cash Flow Quality   — is the reported profit backed by real cash?
10. Dividend Yield      — how much cash does it hand back, vs the price?
11. P/E Ratio           — what does the market charge for those earnings?

Banks/financials use an adapted 8-metric model (ROIC, D/E, current ratio and
CCE are structurally meaningless for a bank's balance sheet) with capital
adequacy in their place — also equally weighted.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import config
import utils

Status = str  # "good" | "warn" | "bad" | "na"


# ---------------------------------------------------------------------------
# Generic 0-10 scorers  (None in → None out: no data is never scored)
# ---------------------------------------------------------------------------

def _clamp10(x: float) -> float:
    return max(0.0, min(10.0, x))


def higher_better(value: Optional[float], low: float, high: float) -> Optional[float]:
    if value is None or high == low:
        return None
    return _clamp10((value - low) / (high - low) * 10.0)


def lower_better(value: Optional[float], best: float, worst: float) -> Optional[float]:
    if value is None or worst == best:
        return None
    return _clamp10((worst - value) / (worst - best) * 10.0)


def band_better(value: Optional[float], lo_bad: float, lo_good: float,
                hi_good: float, hi_bad: float) -> Optional[float]:
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
# Tidy series helpers (PSX financial records)
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


def _growth(financials: List[Dict], field: str, years: int) -> Tuple[Optional[float], str]:
    """CAGR over ~`years` using ONLY real points; returns (growth, 'FYa–FYb')."""
    s = _series(financials, field)
    if len(s) < 2:
        return None, ""
    last_year, last_val = s[-1]
    target_year = last_year - years
    base = min(s, key=lambda t: abs(t[0] - target_year))
    span = last_year - base[0]
    if span <= 0:
        base = s[0]
        span = last_year - base[0]
    if span <= 0:
        return None, ""
    g = utils.cagr(base[1], last_val, span)
    return g, f"FY{base[0]}–FY{last_year}"


def _stmt_series(stmts: Dict[int, Dict], field: str) -> List[Tuple[int, float]]:
    out = [(y, r[field]) for y, r in (stmts or {}).items()
           if r.get(field) is not None]
    out.sort(key=lambda t: t[0])
    return out


def _rec_src(fin: List[Dict], field: str, ctx,
             default_label: str) -> Tuple[str, Optional[str]]:
    """
    v3.2 — source label/URL for a figure taken from the PSX financial records.
    If the figure was deep-fetched from an official filed report (deepdata.py),
    the record carries its exact provenance; link the user to that document.
    Otherwise fall back to the PSX company page.
    """
    s = _series(fin, field)
    if s:
        yr = s[-1][0]
        for r in fin:
            if r.get("year") == yr:
                url = (r.get("_source_urls") or {}).get(field)
                if url:
                    label = (r.get("_sources") or {}).get(field) or default_label
                    return label, url
                break
    return default_label, ctx["urls"].get("psx")


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def _fmt_ratio(v: float) -> str:
    return f"{v:.2f}x"


_NA = (None, None, "N/A", "Original data unavailable", "", None)


def _na(note: str, src_label: str = "", src_url: Optional[str] = None):
    return None, None, "N/A", note, src_label, src_url


# ---------------------------------------------------------------------------
# Metric functions
# Each returns: (subscore|None, value, display, note, source_label, source_url)
# subscore None ⇒ metric excluded from the score and re-weighted away.
# ---------------------------------------------------------------------------

def m_revenue_growth(fin, banking, sa, ctx):
    field = "net_interest_income" if banking else "revenue"
    g, rng = _growth(fin, field, 3)
    if g is None and banking:
        g, rng = _growth(fin, "revenue", 3)
    src_url = ctx["urls"].get("psx")
    if g is None:                          # try the as-filed SA statements
        stmts = ctx.get("stmts") or {}
        recs = [dict(year=y, revenue=v) for y, v in _stmt_series(stmts, "revenue")]
        g, rng = _growth(recs, "revenue", 3)
        src_url = ctx["urls"].get("sa_income")
    if g is None:
        return _na("Needs ≥2 years of reported revenue",
                   "No multi-year revenue found", ctx["urls"].get("psx"))
    sub = higher_better(g, -10, 18)
    note = "Core-income CAGR" if banking else "Revenue CAGR"
    return sub, g, _fmt_pct(g), f"{note} ({rng})", f"Reported statements, {rng}", src_url


def m_profit_margin(fin, banking, sa, ctx):
    rev = _latest(fin, "revenue") or _latest(fin, "net_interest_income")
    np_ = _latest(fin, "net_profit")
    yr = _latest_year(fin, "net_profit")
    src_url = ctx["urls"].get("psx")
    if rev is None or np_ is None:
        stmts = ctx.get("stmts") or {}
        rs, ns = _stmt_series(stmts, "revenue"), _stmt_series(stmts, "net_profit")
        if rs and ns and rs[-1][0] == ns[-1][0]:      # same fiscal year only
            rev, np_, yr = rs[-1][1], ns[-1][1], ns[-1][0]
            src_url = ctx["urls"].get("sa_income")
    m = utils.pct(np_, rev)
    if m is None:
        return _na("Needs reported revenue and net profit for the same year",
                   "Income statement not found", ctx["urls"].get("psx"))
    sub = higher_better(m, 2, 28 if banking else 22)
    if src_url == ctx["urls"].get("psx"):
        lbl, src_url = _rec_src(fin, "net_profit", ctx, f"FY{yr} income statement")
    else:
        lbl = f"FY{yr} income statement"
    return sub, m, _fmt_pct(m), f"Net profit ÷ revenue (FY{yr})", lbl, src_url


def m_eps_growth(fin, banking, sa, ctx):
    g, rng = _growth(fin, "eps", 3)
    src_url = ctx["urls"].get("psx")
    if g is None:
        stmts = ctx.get("stmts") or {}
        recs = [dict(year=y, eps=v) for y, v in _stmt_series(stmts, "eps")]
        g, rng = _growth(recs, "eps", 3)
        src_url = ctx["urls"].get("sa_income")
    if g is None:
        return _na("Needs ≥2 years of reported EPS",
                   "No multi-year EPS found", ctx["urls"].get("psx"))
    sub = higher_better(g, -10, 18)
    return sub, g, _fmt_pct(g), f"EPS CAGR ({rng})", f"Reported statements, {rng}", src_url


def _roic_from(recs_by_year: Dict[int, Dict]) -> Optional[Tuple[float, int, float]]:
    """
    ROIC from one consistent dataset (never mixes sources/scales):
      NOPAT_t = operating_profit_t × (1 − income_tax_t ÷ profit_before_tax_t)
      IC_t    = total_debt_t + total_equity_t
      ROIC    = NOPAT_t ÷ average(IC_t, IC_{t-1})
    Requires every input to be a real reported figure. Returns
    (roic_pct, year, tax_rate) or None.
    """
    years = sorted(y for y, r in recs_by_year.items() if r)
    for t in reversed(years):                      # newest year with full data
        r = recs_by_year.get(t) or {}
        op, pbt, tax = r.get("operating_profit"), r.get("profit_before_tax"), r.get("income_tax")
        d1, e1 = r.get("total_debt"), r.get("total_equity")
        prev = recs_by_year.get(t - 1) or {}
        d0, e0 = prev.get("total_debt"), prev.get("total_equity")
        if None in (op, pbt, tax, d1, e1, d0, e0):
            continue
        if pbt <= 0 or op <= 0:
            continue
        tax_rate = max(0.0, min(0.60, abs(tax) / pbt))
        nopat = op * (1 - tax_rate)
        ic_avg = ((d1 + e1) + (d0 + e0)) / 2.0
        if ic_avg <= 0:
            continue
        return nopat / ic_avg * 100.0, t, tax_rate * 100.0
    return None


def m_roic(fin, banking, sa, ctx):
    # dataset 1: PSX financial records (as parsed from the company's tables)
    psx = {r["year"]: r for r in fin if r.get("year") is not None}
    got = _roic_from(psx)
    src_label, src_url = "PSX company financials", ctx["urls"].get("psx")
    if got is None:                                # dataset 2: SA statements
        got = _roic_from(ctx.get("stmts") or {})
        src_label, src_url = ("StockAnalysis annual statements (S&P Global)",
                              ctx["urls"].get("sa_balance"))
    if got is None:
        return _na("Needs reported operating profit, tax, debt & equity for "
                   "2 consecutive years — no estimation is ever used",
                   "Complete statement set not found", ctx["urls"].get("sa_income"))
    roic, yr, trate = got
    sub = higher_better(roic, 4, 20)
    note = f"NOPAT ÷ avg invested capital (FY{yr}, tax {trate:.0f}%)"
    if src_url == ctx["urls"].get("psx"):
        src_label2, u = _rec_src(fin, "operating_profit", ctx, src_label)
        if u != ctx["urls"].get("psx"):
            src_label, src_url = src_label2, u
    return sub, roic, _fmt_pct(roic), note, f"FY{yr - 1}–FY{yr} {src_label}", src_url


def m_roe(fin, banking, sa, ctx):
    if sa.get("roe_pct") is not None:
        roe = sa["roe_pct"]
        return (higher_better(roe, 5, 22), roe, _fmt_pct(roe),
                "Net profit ÷ shareholders' equity",
                "StockAnalysis statistics (S&P Global)", ctx["urls"].get("sa_stats"))
    np_, eq = _latest(fin, "net_profit"), _latest(fin, "total_equity")
    yr = _latest_year(fin, "net_profit")
    roe = utils.pct(np_, eq)
    if roe is None:
        stmts = ctx.get("stmts") or {}
        ns, es = _stmt_series(stmts, "net_profit"), _stmt_series(stmts, "total_equity")
        if ns and es and ns[-1][0] == es[-1][0]:
            roe, yr = utils.pct(ns[-1][1], es[-1][1]), ns[-1][0]
            if roe is not None:
                return (higher_better(roe, 5, 22), roe, _fmt_pct(roe),
                        f"Net profit ÷ equity (FY{yr})",
                        f"FY{yr} statements", ctx["urls"].get("sa_balance"))
        return _na("Needs reported net profit and equity",
                   "Not found in any source", ctx["urls"].get("psx"))
    lbl, u = _rec_src(fin, "total_equity", ctx, f"FY{yr} PSX financials")
    return (higher_better(roe, 5, 22), roe, _fmt_pct(roe),
            f"Net profit ÷ equity (FY{yr})", lbl, u)


def m_debt_to_equity(fin, banking, sa, ctx):
    if sa.get("debt_to_equity") is not None:
        de = sa["debt_to_equity"]
        return (lower_better(de, 0.4, 2.0), de, _fmt_ratio(de),
                "Total debt ÷ shareholders' equity",
                "StockAnalysis statistics (S&P Global)", ctx["urls"].get("sa_stats"))
    debt, eq = _latest(fin, "total_debt"), _latest(fin, "total_equity")
    yr = _latest_year(fin, "total_debt")
    if debt is None or eq in (None, 0):
        stmts = ctx.get("stmts") or {}
        ds, es = _stmt_series(stmts, "total_debt"), _stmt_series(stmts, "total_equity")
        if ds and es and ds[-1][0] == es[-1][0] and es[-1][1]:
            de, yr = ds[-1][1] / es[-1][1], ds[-1][0]
            return (lower_better(de, 0.4, 2.0), de, _fmt_ratio(de),
                    f"Total debt ÷ equity (FY{yr})",
                    f"FY{yr} balance sheet", ctx["urls"].get("sa_balance"))
        return _na("Needs reported total debt and equity",
                   "Debt figure not found", ctx["urls"].get("psx"))
    de = debt / eq
    lbl, u = _rec_src(fin, "total_debt", ctx, f"FY{yr} PSX financials")
    return (lower_better(de, 0.4, 2.0), de, _fmt_ratio(de),
            f"Total debt ÷ equity (FY{yr})", lbl, u)


def m_current_ratio(fin, banking, sa, ctx):
    if sa.get("current_ratio") is not None:
        cr = sa["current_ratio"]
        return (band_better(cr, 0.8, 1.5, 3.0, 5.0), cr, _fmt_ratio(cr),
                "Current assets ÷ current liabilities",
                "StockAnalysis statistics (S&P Global)", ctx["urls"].get("sa_stats"))
    ca, cl = _latest(fin, "current_assets"), _latest(fin, "current_liabilities")
    yr = _latest_year(fin, "current_assets")
    if ca is None or cl in (None, 0):
        return _na("Needs reported current assets and current liabilities",
                   "Not found in any source", ctx["urls"].get("psx"))
    cr = ca / cl
    lbl, u = _rec_src(fin, "current_assets", ctx, f"FY{yr} PSX financials")
    return (band_better(cr, 0.8, 1.5, 3.0, 5.0), cr, _fmt_ratio(cr),
            f"Current assets ÷ current liabilities (FY{yr})", lbl, u)


def m_cce(fin, banking, sa, ctx):
    """Cash & Cash Equivalents, expressed as % of total assets (same-year,
    same-source figures only, so scale always cancels)."""
    cash, ta = _latest(fin, "cash"), _latest(fin, "total_assets")
    yr = _latest_year(fin, "cash")
    src_label, src_url = _rec_src(fin, "cash", ctx, f"FY{yr} PSX financials")
    if cash is None or ta in (None, 0) or _latest_year(fin, "total_assets") != yr:
        stmts = ctx.get("stmts") or {}
        cs, as_ = _stmt_series(stmts, "cash"), _stmt_series(stmts, "total_assets")
        if cs and as_ and cs[-1][0] == as_[-1][0] and as_[-1][1]:
            cash, ta, yr = cs[-1][1], as_[-1][1], cs[-1][0]
            src_label, src_url = (f"FY{yr} balance sheet (S&P Global)",
                                  ctx["urls"].get("sa_balance"))
        elif sa.get("cash") is not None:
            # single reported figure but no matching total assets → show, don't score
            return _na("Cash reported but total assets missing for the same year",
                       "StockAnalysis statistics", ctx["urls"].get("sa_stats"))
        else:
            return _na("Needs reported cash & equivalents and total assets",
                       "Not found in any source", ctx["urls"].get("psx"))
    pct = cash / ta * 100.0
    sub = higher_better(pct, 2, 18)
    return sub, pct, f"{pct:.1f}% of assets", \
        f"Cash & equivalents ÷ total assets (FY{yr})", src_label, src_url


def m_cashflow_quality(fin, banking, sa, ctx):
    sa_ocf, sa_ni = sa.get("operating_cashflow"), sa.get("net_profit")
    if sa_ocf is not None and sa_ni not in (None, 0):
        ratio = sa_ocf / sa_ni
        return (higher_better(ratio, 0.4, 1.1), ratio, _fmt_ratio(ratio),
                "Operating cash flow ÷ net profit",
                "StockAnalysis statistics (S&P Global)", ctx["urls"].get("sa_stats"))
    ocf, np_ = _latest(fin, "operating_cashflow"), _latest(fin, "net_profit")
    yr = _latest_year(fin, "operating_cashflow")
    if ocf is None or np_ in (None, 0):
        return _na("Needs reported operating cash flow and net profit",
                   "Cash-flow statement not found", ctx["urls"].get("psx"))
    ratio = ocf / np_
    lbl, u = _rec_src(fin, "operating_cashflow", ctx, f"FY{yr} PSX financials")
    return (higher_better(ratio, 0.4, 1.1), ratio, _fmt_ratio(ratio),
            f"Operating cash flow ÷ net profit (FY{yr})", lbl, u)


def m_dividend_yield(fin, banking, sa, ctx):
    if sa.get("dividend_yield_pct") is not None:
        y = sa["dividend_yield_pct"]
        return (band_better(y, 0.0, 4.0, 12.0, 28.0), y, _fmt_pct(y),
                "Annual dividend ÷ share price",
                "StockAnalysis dividend data (S&P Global)",
                ctx["urls"].get("sa_dividend"))
    price = ctx.get("price")
    dby = (sa.get("dividend_by_year") or {})
    dps, yr = None, None
    if dby:
        yr = max(dby)
        dps = dby[yr]
    if dps is None:
        dps, yr = _latest(fin, "dividend_per_share"), _latest_year(fin, "dividend_per_share")
    if dps is None or not price:
        return _na("Needs the reported dividend per share and a live PSX price",
                   "Dividend record not found", ctx["urls"].get("psx"))
    y = dps / price * 100.0
    lbl, u = _rec_src(fin, "dividend_per_share", ctx,
                      f"FY{yr} payouts ÷ PSX live price")
    return (band_better(y, 0.0, 4.0, 12.0, 28.0), y, _fmt_pct(y),
            f"FY{yr} DPS ₨{dps:g} ÷ price ₨{price:g}", lbl, u)


def m_pe_ratio(fin, banking, sa, ctx):
    pe, src_label, src_url = None, "", None
    if sa.get("pe") is not None:
        pe = sa["pe"]
        src_label, src_url = ("StockAnalysis statistics (S&P Global)",
                              ctx["urls"].get("sa_stats"))
        note = "Share price ÷ earnings per share"
    else:
        price, eps = ctx.get("price"), _latest(fin, "eps")
        yr = _latest_year(fin, "eps")
        if not price or eps is None:
            return _na("Needs a live PSX price and reported EPS",
                       "EPS not found", ctx["urls"].get("psx"))
        if eps <= 0:
            return (0.0, 0.0, "Loss-making",
                    "Negative EPS — P/E undefined, scored 0",
                    f"FY{yr} PSX financials", ctx["urls"].get("psx"))
        pe = price / eps
        src_label, src_url = f"PSX price ÷ FY{yr} EPS", ctx["urls"].get("psx")
        note = f"Price ₨{price:g} ÷ EPS ₨{eps:g} (FY{yr})"
    if pe <= 0:
        return (0.0, pe, "Loss-making", "Negative earnings — scored 0",
                src_label, src_url)
    sub = lower_better(pe, 4.0, 30.0)
    return sub, pe, f"{pe:.1f}x", note, src_label, src_url


def m_capital_adequacy(fin, banking, sa, ctx):
    car = _latest(fin, "capital_adequacy")
    yr = _latest_year(fin, "capital_adequacy")
    if car is None:
        return _na("Needs the bank's reported Capital Adequacy Ratio",
                   "CAR not found in filings", ctx["urls"].get("psx"))
    sub = higher_better(car, 11.5, 18)
    lbl, u = _rec_src(fin, "capital_adequacy", ctx, f"FY{yr} PSX financials")
    return (sub, car, _fmt_pct(car), f"Reported CAR (FY{yr}); SBP floor 11.5%",
            lbl, u)


METRIC_FUNCS = {
    "revenue_growth":   ("Revenue Growth",        m_revenue_growth),
    "profit_margin":    ("Profit Margin",          m_profit_margin),
    "eps_growth":       ("EPS Growth",             m_eps_growth),
    "roic":             ("Return on Invested Capital", m_roic),
    "roe":              ("Return on Equity",       m_roe),
    "debt_to_equity":   ("Debt / Equity",          m_debt_to_equity),
    "current_ratio":    ("Current Ratio",          m_current_ratio),
    "cce":              ("Cash & Equivalents",     m_cce),
    "cashflow_quality": ("Cash Flow Quality",      m_cashflow_quality),
    "dividend_yield":   ("Dividend Yield",         m_dividend_yield),
    "pe_ratio":         ("P/E Ratio",              m_pe_ratio),
    "capital_adequacy": ("Capital Adequacy",       m_capital_adequacy),
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
    sa = scrape.get("sa_data", {}) or {}
    symbol = scrape.get("symbol", "")

    urls = scrape.get("source_urls") or {}
    if not urls and symbol:                       # older payloads / demo
        urls = {
            "psx":         f"https://dps.psx.com.pk/company/{symbol}",
            "sa_stats":    f"https://stockanalysis.com/quote/psx/{symbol}/statistics/",
            "sa_income":   f"https://stockanalysis.com/quote/psx/{symbol}/financials/",
            "sa_balance":  f"https://stockanalysis.com/quote/psx/{symbol}/financials/balance-sheet/",
            "sa_dividend": f"https://stockanalysis.com/quote/psx/{symbol}/dividend/",
        }
    ctx = {
        "symbol": symbol,
        "urls":   urls,
        "stmts":  {int(y): r for y, r in (scrape.get("sa_statements") or {}).items()},
        "price":  (scrape.get("profile") or {}).get("price"),
    }

    metrics: List[Dict] = []
    weighted_sum     = 0.0
    available_weight = 0.0
    total_weight     = sum(weights.values())

    for key, weight in weights.items():
        label, func = METRIC_FUNCS[key]
        sub, value, display, note, src_label, src_url = func(fin, banking, sa, ctx)
        st = _status(sub)

        metrics.append({
            "key":          key,
            "label":        label,
            "weight":       round(weight * 100, 1),
            "subscore":     round(sub, 1) if sub is not None else None,
            "value":        value,
            "display":      display,
            "status":       st,
            "note":         note,
            "source_note":  src_label,
            "source_doc":   src_label,
            "source_date":  "",
            "source_url":   src_url,
            "estimated":    False,          # v3: estimation is never used
        })

        if sub is not None:
            weighted_sum     += (sub / 10.0) * weight
            available_weight += weight

    # RE-WEIGHT over the metrics that had real data — score always spans 0-100
    score = (weighted_sum / available_weight) * 100.0 if available_weight > 0 else 0.0
    coverage = available_weight / total_weight if total_weight > 0 else 0.0

    highlights = [m["label"] for m in metrics if m["status"] == "good"]
    concerns   = [m["label"] for m in metrics if m["status"] == "bad"]

    return {
        "symbol":       symbol,
        "model":        "banking" if banking else "general",
        "sector":       sector,
        "score":        round(score, 1),
        "verdict":      verdict_for(score),
        "coverage":     round(coverage, 2),
        "confidence":   round(coverage * 100),   # v3: all scored data is real
        "data_quality": scrape.get("data_quality"),
        "metrics":      metrics,
        "trends":       _build_trends(fin),
        "highlights":   highlights[:4],
        "concerns":     concerns[:4],
        "warnings":     scrape.get("warnings", []),
        "profile":      scrape.get("profile", {}),
        "reports":      scrape.get("reports", []),
        "price_history": scrape.get("price_history", []),
        "source_urls":  urls,
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
    demo = {
        "symbol": "DEMO",
        "profile": {"sector": "CEMENT", "price": 145.3},
        "data_quality": 0.9,
        "warnings": [],
        "financials": [
            {"year": 2023, "revenue": 140_000, "net_profit": 24_000, "eps": 17.8,
             "total_assets": 200_000, "total_equity": 135_000, "total_debt": 18_000,
             "current_assets": 60_000, "current_liabilities": 26_000, "cash": 22_000,
             "operating_profit": 38_000, "profit_before_tax": 34_000, "income_tax": 10_000,
             "operating_cashflow": 26_000, "dividend_per_share": 7},
            {"year": 2024, "revenue": 162_000, "net_profit": 28_900, "eps": 21.4,
             "total_assets": 224_000, "total_equity": 150_000, "total_debt": 16_000,
             "current_assets": 68_000, "current_liabilities": 28_000, "cash": 30_000,
             "operating_profit": 45_000, "profit_before_tax": 41_000, "income_tax": 12_100,
             "operating_cashflow": 30_000, "dividend_per_share": 8},
        ],
        "sa_data": {}, "sa_statements": {},
        "price_history": [], "reports": [],
        "scraped_at": utils.now_iso(),
    }
    result = score_company(demo)
    print(f"Score: {result['score']} ({result['verdict']['label']}) "
          f"confidence={result['confidence']}%")
    for m in result["metrics"]:
        ss = m["subscore"]
        bar = ("█" * int(ss) + "░" * (10 - int(ss))) if ss is not None else "·" * 10
        print(f"  {m['label']:<28} {bar} {str(ss):>4}/10 "
              f"{m['display']:>16} [{m['status']}]")
        print(f"    ↳ {m['source_note']}  {m['source_url'] or ''}")
