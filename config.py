"""
config.py
=========
Central configuration for the PSX Fundamental Scorer.

Everything you might reasonably want to tweak lives here, so you can edit it once
on GitHub and every friend running run.bat picks the change up on their next
launch.
"""

from __future__ import annotations
import os

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
APP_NAME = "PSX Fundamental Scorer"
HOST = "127.0.0.1"
PORT = 5000
OPEN_BROWSER = True            # auto-open the dashboard on launch

# -----------------------------------------------------------------------------
# Where the live code lives (used by run.bat; kept here for reference).
# run.bat re-downloads every .py + the dashboard from GitHub on each launch,
# so updating the repo updates every user automatically.
# -----------------------------------------------------------------------------
GITHUB_USER = os.environ.get("PSX_GH_USER", "YOURNAME")
GITHUB_REPO = os.environ.get("PSX_GH_REPO", "psx-score")
GITHUB_BRANCH = os.environ.get("PSX_GH_BRANCH", "main")
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}"

# -----------------------------------------------------------------------------
# PSX data sources (public, no API key, no login).
# dps.psx.com.pk is the PSX Data Portal. These endpoints are scraped live.
# -----------------------------------------------------------------------------
PSX_BASE = "https://dps.psx.com.pk"
PSX_SYMBOLS_URL = f"{PSX_BASE}/symbols"               # JSON: every listed symbol + sector
PSX_MARKET_WATCH = f"{PSX_BASE}/market-watch"         # live board (price, change, volume)
PSX_COMPANY_URL = f"{PSX_BASE}/company/{{symbol}}"    # HTML: profile, price, ratios, financials
PSX_INDEX_URL = f"{PSX_BASE}/indices/{{index}}"       # HTML: index constituents
PSX_TIMESERIES = f"{PSX_BASE}/timeseries/eod/{{symbol}}"  # JSON: end-of-day price history
PSX_MAIN = "https://www.psx.com.pk"
PSX_ANNOUNCEMENTS_URL = f"{PSX_BASE}/announcements/companies"  # HTML: all company filings

# v3.6 — catalyst engine (real PSX announcements only; nothing is invented)
CATALYST_MONTHS = 12          # how far back announcements count as "fresh"
CATALYST_MAX_ITEMS = 30       # cap on classified items sent to the dashboard

# -----------------------------------------------------------------------------
# v4.0 — ANNOUNCEMENTS verdict engine.
# Every recent PSX filing (PDF or plain notice) is classified; for the most
# recent non-routine filings the tool also *opens the actual PDF* and reads the
# first pages so the verdict reflects the document's real content, not just its
# title. Strict caps keep an Analyze click fast and polite.
# -----------------------------------------------------------------------------
ANNOUNCEMENTS = {
    "pdf_peek": True,          # open PDFs of recent filings to read context
    "peek_max_pdfs": 14,       # effectively ALL recent non-routine PDFs get read
    "peek_max_pages": 3,       # pages read per PDF (verdict-relevant text is up front)
    "peek_max_mb": 8,          # skip PDFs bigger than this
    "peek_budget_s": 22,       # hard wall-clock cap for ALL pdf reads combined
    "peek_workers": 5,         # PDFs are fetched in parallel, not one-by-one
}

# -----------------------------------------------------------------------------
# v4.0 — RECOMMENDED STOCKS OF THE MONTH.
# A background pass runs the SAME fundamental scorer and the SAME technical
# prediction engine over a liquid pool of PSX names, blends both scores with
# the PREDICTOR weights below, and keeps the top picks for the month.
# Results are cached per calendar month so the dashboard loads instantly.
# -----------------------------------------------------------------------------
RECOMMEND = {
    "pool_index": "KSE100",    # candidate pool — the KSE-100 (both lists come from here)
    "extra_index": "KMI30",    # union'd in for wider coverage of the TOP list
    "max_symbols": 110,        # hard cap on the scan
    "top_n": 10,               # best-of-best surfaced on the landing page
    "avoid_n": 10,             # worst-of-worst (KSE-100 only) — the stay-away list
    "workers": 8,              # parallel evaluation threads (was serial + 2s sleeps)
    "delay_s": 0.0,            # no artificial pauses — the thread pool self-limits
    "cache_prefix": "recommendations",   # psx_cache/recommendations_YYYY-MM.json
    "rebuild_if_older_days": 7,          # refresh mid-month if stale
    "serve_stale": True,       # instantly serve last month's list while rebuilding
    "min_price_history": 60,   # skip names without enough chart history
}

# Secondary data source — PSX pages lack balance sheet, cash flow, dividends.
# StockAnalysis.com (powered by S&P Global) has comprehensive financial data.
SA_STATS_URL = "https://stockanalysis.com/quote/psx/{symbol}/statistics/"
SA_DIVIDEND_URL = "https://stockanalysis.com/quote/psx/{symbol}/dividend/"

# Indices we surface in the UI (label -> portal code)
INDICES = {
    "KSE100": "KSE100",
    "KSE50":  None,       # v3.4: PSX publishes no KSE-50 page (was a 404 on
                          # every refresh) — derived as the top 50 of KSE-100
    "KSE30":  "KSE30",
    "KMI30":  "KMI30",
    "ALLSHR": "ALLSHR",   # All Shares
}

# Polite scraping
# Version stamp — printed at startup, returned by /api/health, shown in the
# dashboard footer. If the console banner does not show this version, the
# files on GitHub (which PSX.bat re-downloads at every launch) are stale.
APP_VERSION = "4.0.0"

REQUEST_TIMEOUT = 15          # seconds
REQUEST_RETRIES = 2           # v3.3: retries only help transient errors
REQUEST_BACKOFF = 1.0         # seconds, multiplied each retry
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]

# -----------------------------------------------------------------------------
# Caching (so one session does not hammer PSX, but stays fresh).
# The universe is refreshed on every app open; analyses cached for the session.
# -----------------------------------------------------------------------------
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "psx_cache")
REPORTS_DIR = os.path.join(CACHE_DIR, "reports")
UNIVERSE_TTL_HOURS = 12       # re-scrape the stock list if cache older than this
ANALYSIS_TTL_MINUTES = 30     # re-scrape a company if its analysis is older than this

# -----------------------------------------------------------------------------
# Scoring model.
# Weights must sum to 1.0. Each metric is scored 0-10, then weighted to 0-100.
# Banking / insurance use a different model (see scorer.py) because leverage is
# structural for them.
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Metric weights (v3) — THE FINANCIAL STORY, top to bottom, ALL EQUAL.
# 1-3  Growth & profitability: sells more → keeps profit → per-share growth
# 4-5  Capital efficiency: all capital (ROIC) → owners' capital (ROE)
# 6-8  Balance-sheet safety: leverage → liquidity → cash on hand
# 9    Earnings quality: is the profit real cash?
# 10   Shareholder reward: dividend yield
# 11   Price tag: what the market charges for it all (P/E)
# STRICT v3 POLICY: a metric with no original data shows N/A and the rest
# are re-weighted — nothing is ever estimated.
# -----------------------------------------------------------------------------
WEIGHTS_GENERAL = {
    "revenue_growth":   1/11,
    "profit_margin":    1/11,
    "eps_growth":       1/11,
    "roic":             1/11,
    "roe":              1/11,
    "debt_to_equity":   1/11,
    "current_ratio":    1/11,
    "cce":              1/11,
    "cashflow_quality": 1/11,
    "dividend_yield":   1/11,
    "pe_ratio":         1/11,
}

# Banks / insurers / leasing: ROIC, D/E, current ratio and CCE are
# structurally meaningless for a financial balance sheet (deposits ARE the
# business), so the regulator's capital adequacy ratio stands in for the
# safety block. Same story, 8 equal weights.
WEIGHTS_BANKING = {
    # v3.7 — banking model uses 7 fundamentals, equal weight.
    # Capital Adequacy was removed: PSX/StockAnalysis do not reliably publish
    # it, and the tool never shows estimated values.
    "revenue_growth":   1/7,
    "profit_margin":    1/7,
    "eps_growth":       1/7,
    "roe":              1/7,
    "cashflow_quality": 1/7,
    "dividend_yield":   1/7,
    "pe_ratio":         1/7,
}

# Sectors that should use the banking / financial model.
FINANCIAL_SECTOR_KEYWORDS = [
    "BANK", "COMMERCIAL BANKS", "INVESTMENT BANKS",
    "INSURANCE", "MODARABA", "LEASING", "FINANCIAL SERVICES",
]

# Verdict bands (lower bound inclusive).
VERDICTS = [
    (85, "Rock Solid", "Exceptional fundamentals across the board."),
    (70, "Strong",     "Healthy, well-run business with minor blemishes."),
    (55, "Decent",     "Reasonable fundamentals; some areas need watching."),
    (40, "Mixed",      "Notable weaknesses balance the strengths."),
    (25, "Fragile",    "Several red flags in the financials."),
    (0,  "Weak",       "Fundamentals look poor on current data."),
]

# Trend windows offered in the UI (years).
TREND_WINDOWS = [1, 3, 5, 10]

# -----------------------------------------------------------------------------
# Prediction engine (see predictor.py — methodology modelled on how seasoned
# PSX analysts read charts: trend structure, EMA 21/89 + 200-MA, RSI(14)
# divergence, support/resistance clusters, Fibonacci retracement of the last
# rally, volumes, and a defined Buy-1/Buy-2/Stop/Target trade plan).
# Guidance only — never a buy/sell call.
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Deep official-filings fetcher (deepdata.py, v3.2)
# When PSX tables + StockAnalysis leave gaps, the tool downloads the company's
# OWN filed annual reports (exchange-hosted PDFs + the official website's
# investor-relations section), parses the multi-year financial tables, and
# stores every figure with document/URL/page provenance. Original data only —
# never an estimate. A polite background pre-warm walks the whole universe
# across runs so coverage keeps growing.
# -----------------------------------------------------------------------------
DEEPDATA = {
    "enabled": True,
    "dir": os.path.join(CACHE_DIR, "deepdata"),
    "max_pdfs_per_symbol": 4,     # download budget per symbol per pass
    "max_pdf_mb": 30,
    "crawl_pages": 12,            # website pages visited while hunting PDFs
    "crawl_depth": 2,
    "request_delay_s": 1.5,       # politeness between requests
    "fetch_budget_s": 150,        # hard time cap for one on-demand deep fetch
    "freshness_days": 45,         # re-check incomplete stores after this
    "freshness_complete_days": 120,
    "prewarm": True,              # background pass over the whole universe
    "prewarm_delay_s": 25,        # pause between symbols (be a good citizen)
    # ---- v3.3 performance & robustness ----
    "background": True,           # deep fetches NEVER run inside a user request
    "retry_incomplete_hours": 24, # cooldown before re-trying an incomplete symbol
    "max_pdf_pages": 250,         # parse cap per document (early-exits when done)
    "image_probe_pages": 6,       # pages sampled to detect image-only (scanned) PDFs
    "user_idle_grace_s": 90,      # prewarm pauses while a user is actively analyzing
    "ocr": False,                 # optional: pip install rapidocr-onnxruntime to
                                  # read image-only reports (off by default)
}

PREDICTOR = {
    "ema_fast": 21,          # daily EMA used as trailing/dynamic support
    "ema_slow": 89,          # trend arbiter — trend alive while price sustains it
    "sma_long": 200,         # long-term moving average
    "rsi_period": 14,
    "fib_ratios": [0.236, 0.382, 0.5, 0.618],
    "tech_weight": 0.55,     # blend of technical vs fundamental score
    "fund_weight": 0.45,
    "portfolio_risk_pct": "2–3%",   # position-sizing reminder from the show
}
