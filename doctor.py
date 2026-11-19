"""
doctor.py
=========
Run this FIRST whenever the tool misbehaves:

    python doctor.py                 # full check
    python doctor.py LUCK            # ...using LUCK as the test company
    python doctor.py --quick         # skip the slow full-scrape test

It answers one question in plain language: **which part is broken?**

It checks, in order, the things that actually fail in practice:

  1. Python version and the packages the app needs
  2. Plain internet reachability
  3. The PSX request token (the thing that broke the app on 2026-09-24)
  4. Every PSX endpoint the app uses, one by one, with its real HTTP status
  5. The StockAnalysis secondary source
  6. A complete scrape + score of one company, with a field-coverage report

Nothing here writes to the cache or changes any setting, so it is always safe
to run. Every line is either a fact it measured or a step you can take.
"""

from __future__ import annotations

import importlib
import platform
import sys
import time
from typing import List, Optional, Tuple

import config
import utils

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[1m", "\033[0m")
if sys.platform == "win32":
    try:                                   # enable ANSI colours on Windows 10+
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:  # noqa: BLE001
        GREEN = RED = YELLOW = DIM = BOLD = OFF = ""

RESULTS: List[Tuple[str, str, str]] = []      # (level, title, detail)


def ok(title: str, detail: str = "") -> None:
    RESULTS.append(("ok", title, detail))
    print(f"  {GREEN}[ OK ]{OFF} {title}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def warn(title: str, detail: str = "") -> None:
    RESULTS.append(("warn", title, detail))
    print(f"  {YELLOW}[WARN]{OFF} {title}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def bad(title: str, detail: str = "") -> None:
    RESULTS.append(("bad", title, detail))
    print(f"  {RED}[FAIL]{OFF} {title}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def section(name: str) -> None:
    print(f"\n{BOLD}{name}{OFF}")
    print(f"{DIM}{'-' * len(name)}{OFF}")


# ---------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------
def check_environment() -> None:
    section("1. Python and packages")
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}", platform.platform())
    else:
        bad(f"Python {v.major}.{v.minor} is too old",
            "the app needs 3.10 or newer — reinstall from python.org")

    required = {
        "flask": "the local web server", "requests": "all network calls",
        "bs4": "HTML parsing", "lxml": "the HTML parser backend",
    }
    optional = {
        "flask_cors": "only needed if you open dashboard.html directly",
        "pdfplumber": "reading annual-report and filing PDFs",
        "pandas": "table handling", "openpyxl": "Excel export",
        "reportlab": "PDF export", "pytesseract": "OCR of scanned filings",
        "pypdfium2": "rendering scanned filings for OCR",
    }
    for mod, why in required.items():
        try:
            importlib.import_module(mod)
            ok(f"{mod} installed", why)
        except Exception:  # noqa: BLE001
            bad(f"{mod} is MISSING — {why}",
                f"fix: pip install -r requirements.txt")
    for mod, why in optional.items():
        try:
            importlib.import_module(mod)
            ok(f"{mod} installed", why)
        except Exception:  # noqa: BLE001
            warn(f"{mod} not installed — {why}", "optional; the app still runs")

    section("2. App files and version")
    for mod in ("psx_data", "scraper", "scorer", "predictor", "catalyst",
                "deepdata", "about", "exporter", "recommend", "analyzer"):
        try:
            importlib.import_module(mod)
            ok(f"{mod}.py loads")
        except Exception as exc:  # noqa: BLE001
            bad(f"{mod}.py FAILED to load", f"{type(exc).__name__}: {exc}")
    ok(f"version {config.APP_VERSION}",
       "if this is not the version you expect, GitHub is serving stale files")
    ok(f"as-of session {utils.as_of_iso()}",
       f"PSX local time now {utils.psx_now():%Y-%m-%d %H:%M}")


# ---------------------------------------------------------------------------
# 3. The PSX request token
# ---------------------------------------------------------------------------
def check_token() -> Optional[str]:
    section("3. The PSX request token  (X-Req-Id)")
    print(f"  {DIM}PSX has required this on every data endpoint since "
          f"2026-09-24.{OFF}")
    t0 = time.time()
    token = utils.TOKENS.get(force=True)
    ms = int((time.time() - t0) * 1000)
    if token:
        ok("token obtained",
           f"{token[:6]}…{token[-4:]} from {utils.TOKENS.last_page} ({ms} ms)")
        return token
    bad("could NOT get a request token", utils.TOKENS.last_error or "unknown")
    print(f"\n  {YELLOW}What this means:{OFF} every PSX data route will return "
          f"403 and\n  the app will show blank prices and empty charts.")
    print(f"  {YELLOW}What to try:{OFF}")
    print("    • open https://dps.psx.com.pk/historical in a browser — does it load?")
    print("    • if it loads there but not here, something between you and PSX")
    print("      is blocking the app: a VPN, a corporate proxy, or antivirus")
    print("      HTTPS scanning. Try switching those off, or use a mobile hotspot.")
    print("    • if PSX has moved the token, edit config.PSX_TOKEN['pages'].")
    return None


# ---------------------------------------------------------------------------
# 4. Every endpoint, one by one
# ---------------------------------------------------------------------------
def check_endpoints(symbol: str) -> dict:
    section(f"4. PSX endpoints  (test company: {symbol})")
    found = {}

    tests = [
        ("company page", config.PSX_COMPANY_URL.format(symbol=symbol),
         False, "profile, sector, financial tables, filings"),
        ("symbol list", config.PSX_SYMBOLS_URL, True,
         "every listed company — powers search"),
        ("price history (EOD)", config.PSX_TIMESERIES.format(symbol=symbol),
         True, "the chart, EMAs, RSI, the whole Technical tab"),
        ("KSE-100 index page", config.PSX_INDEX_URL.format(index="KSE100"),
         False, "index membership, the ranked lists"),
        ("announcements", config.PSX_ANNOUNCEMENTS_URL, False,
         "Material Information and insider transactions"),
    ]

    for name, url, as_json, why in tests:
        res = utils.request(url, as_json=as_json)
        tag = f"HTTP {res.status}" if res.status else "no response"
        size = (len(res.body) if isinstance(res.body, (str, bytes, list))
                else "?") if res.ok else 0
        if res.ok:
            ok(f"{name}", f"{tag}, {size} "
                          f"{'rows' if as_json else 'bytes'}, {res.elapsed_ms} ms")
            found[name] = res.body
        elif res.status == 403:
            bad(f"{name} — 403 Forbidden", f"token rejected · needed for: {why}")
        elif res.status == 404:
            bad(f"{name} — 404 Not Found",
                f"PSX may have moved this route · needed for: {why}")
        else:
            bad(f"{name} — {tag}", f"{res.error} · needed for: {why}")

    hist = found.get("price history (EOD)")
    if hist:
        rows = hist.get("data") if isinstance(hist, dict) else hist
        if isinstance(rows, list) and rows:
            ok("price history has usable rows",
               f"{len(rows)} bars, newest row looks like {str(rows[-1])[:60]}")
        else:
            bad("price history returned 200 but no rows",
                "PSX changed the payload shape — scraper.scrape_price_history "
                "needs updating")
    return found


# ---------------------------------------------------------------------------
# 5. Secondary source
# ---------------------------------------------------------------------------
def check_stockanalysis(symbol: str) -> None:
    section("5. StockAnalysis  (balance sheet, cash flow, dividends, ROIC)")
    import scraper
    url = scraper._SA_URL.format(symbol=symbol)
    res = utils.request(url)
    if not res.ok:
        bad(f"statistics page unreachable", f"HTTP {res.status} · {res.error}")
        warn("effect", "ROE, D/E, current ratio, ROIC and dividends may show N/A")
        return
    ok("statistics page reachable", f"HTTP {res.status}, {res.elapsed_ms} ms")
    data = scraper.scrape_stockanalysis(symbol, utils.make_session())
    fields = [k for k in data if not k.startswith("_")]
    if len(fields) >= 8:
        ok(f"parsed {len(fields)} fields", ", ".join(sorted(fields)[:8]) + " …")
    elif fields:
        warn(f"parsed only {len(fields)} fields", ", ".join(sorted(fields)))
    else:
        bad("page loaded but 0 fields parsed",
            "StockAnalysis changed its markup — scraper._SA_MAP needs updating")


# ---------------------------------------------------------------------------
# 6. The real thing
# ---------------------------------------------------------------------------
def check_full_scrape(symbol: str) -> None:
    section(f"6. A real end-to-end analysis of {symbol}")
    import scraper
    import scorer
    import predictor

    t0 = time.time()
    try:
        scraped = scraper.scrape_company(symbol, deep_pdf=False)
    except Exception as exc:  # noqa: BLE001
        bad("the scrape raised an exception", f"{type(exc).__name__}: {exc}")
        return
    secs = time.time() - t0

    prof = scraped.get("profile") or {}
    fins = scraped.get("financials") or []
    hist = scraped.get("price_history") or []

    ok(f"scrape finished in {secs:.1f}s",
       f"data quality {int((scraped.get('data_quality') or 0) * 100)}%")

    if prof.get("price"):
        ok(f"share price {prof['price']}", prof.get("_price_source", ""))
    else:
        bad("NO share price", "the Technical tab cannot work without this")

    if len(hist) >= 200:
        ok(f"{len(hist)} price bars", f"{hist[0]['date']} → {hist[-1]['date']}")
    elif hist:
        warn(f"only {len(hist)} price bars",
             "200-day MA and Wyckoff need roughly a year of history")
    else:
        bad("NO price history", "chart, EMAs, RSI and the trade plan are all dead")

    if fins:
        latest = fins[-1]
        core = ["revenue", "net_profit", "total_equity", "total_assets", "eps",
                "total_debt", "operating_cashflow"]
        have = [k for k in core if latest.get(k) is not None]
        if len(have) >= 5:
            ok(f"financials for FY{latest.get('year')}",
               f"{len(have)}/{len(core)} core line items")
        else:
            warn(f"financials look thin for FY{latest.get('year')}",
                 f"only {len(have)}/{len(core)}: {', '.join(have)}")
    else:
        bad("NO financial statements", "the fundamental score cannot be computed")

    try:
        result = scorer.score_company(scraped)
        score = result.get("score")
        metrics = result.get("metrics") or []
        live = [m for m in metrics if m.get("value") is not None]
        label = (result.get("verdict") or {}).get("label", "?")
        cov = f"{len(live)}/{len(metrics)} metrics had real data"
        if score is not None:
            ok(f"fundamental score {score}/100 — {label}", cov)
            if len(live) < len(metrics) * 0.5:
                warn("over half the metrics are N/A",
                     "the score is honest but thin — see the FAIL lines above")
        else:
            # v5.0 behaviour: refusing to score is correct, not a crash.
            bad(f"NOT RATED — no score could be earned ({cov})",
                (result.get("unrated_reason") or "")[:120])
    except Exception as exc:  # noqa: BLE001
        bad("scoring raised an exception", f"{type(exc).__name__}: {exc}")
        return

    try:
        pred = predictor.predict(scraped, result)
        plan = pred.get("trade_plan") or {}
        vd = pred.get("verdict") or {}
        label = vd.get("label") if isinstance(vd, dict) else str(vd)
        scores = pred.get("scores") or {}
        if plan:
            ok("technical engine ran",
               f"verdict: {str(label)[:48]} · chart "
               f"{scores.get('technical')} / combined {scores.get('combined')}")
        elif label:
            warn("technical engine ran but produced no trade plan",
                 f"verdict: {str(label)[:48]} — usually too little price history")
        else:
            bad("technical engine produced nothing",
                "check the price-history lines above")
    except Exception as exc:  # noqa: BLE001
        bad("the technical engine raised an exception",
            f"{type(exc).__name__}: {exc}")

    for w in (scraped.get("warnings") or [])[:6]:
        warn("scraper note", w)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def verdict() -> int:
    bads = [r for r in RESULTS if r[0] == "bad"]
    warns = [r for r in RESULTS if r[0] == "warn"]
    print("\n" + "=" * 68)
    if not bads:
        print(f"  {GREEN}{BOLD}ALL CLEAR{OFF} — "
              f"{len([r for r in RESULTS if r[0] == 'ok'])} checks passed, "
              f"{len(warns)} warning(s).")
        print("  The engine is healthy. Start the app with:  python analyzer.py")
    else:
        print(f"  {RED}{BOLD}{len(bads)} PROBLEM(S) FOUND{OFF} "
              f"({len(warns)} warning(s)):")
        for _, title, detail in bads:
            print(f"    {RED}•{OFF} {title}")
            if detail:
                print(f"      {DIM}{detail}{OFF}")
        tokenless = any("request token" in t for _, t, _ in bads)
        if tokenless:
            print(f"\n  {BOLD}Most likely cause:{OFF} the app cannot reach PSX "
                  f"to read its request\n  token. Everything else fails as a "
                  f"consequence. Fix that first.")
    print("=" * 68)
    return 1 if bads else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    quick = "--quick" in sys.argv
    symbol = (args[0] if args else "LUCK").upper()

    print("=" * 68)
    print(f"  {BOLD}PSX·SCORE DOCTOR{OFF}  ·  v{config.APP_VERSION}")
    print(f"  Checking everything the app needs in order to work.")
    print("=" * 68)

    check_environment()
    check_token()
    check_endpoints(symbol)
    check_stockanalysis(symbol)
    if quick:
        section("6. End-to-end analysis")
        warn("skipped", "--quick was passed; drop it for the full test")
    else:
        check_full_scrape(symbol)
    return verdict()


if __name__ == "__main__":
    sys.exit(main())
