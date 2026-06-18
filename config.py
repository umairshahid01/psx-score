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

# Indices we surface in the UI (label -> portal code)
INDICES = {
    "KSE100": "KSE100",
    "KSE50":  "KSE50",
    "KSE30":  "KSE30",
    "KMI30":  "KMI30",
    "ALLSHR": "ALLSHR",   # All Shares
}

# Polite scraping
REQUEST_TIMEOUT = 20          # seconds
REQUEST_RETRIES = 3
REQUEST_BACKOFF = 1.5         # seconds, multiplied each retry
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
WEIGHTS_GENERAL = {
    "revenue_growth":   0.15,
    "profit_margin":    0.15,
    "eps_growth":       0.15,
    "debt_to_equity":   0.15,
    "roe":              0.15,
    "current_ratio":    0.10,
    "cashflow_quality": 0.10,
    "dividend":         0.05,
}

WEIGHTS_BANKING = {
    "revenue_growth":   0.15,   # net interest / markup income growth
    "profit_margin":    0.15,
    "eps_growth":       0.15,
    "roe":              0.20,    # ROE matters more for banks
    "capital_adequacy": 0.15,    # replaces debt_to_equity
    "cashflow_quality": 0.10,
    "dividend":         0.10,
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
