"""
analyzer.py
===========
The local engine. Run this (run.bat does it for you) and it:

  * serves the dashboard at  http://127.0.0.1:5000
  * refreshes the PSX stock universe on startup (so dropdowns are current)
  * answers the dashboard's API calls:
        GET /api/health
        GET /api/stocks?refresh=1          -> current KSE-100/50/30/KMI-30 + all
        GET /api/analyze?symbol=OGDC       -> scrape + score one company, live

Every analyze call scrapes PSX *fresh* (subject to a short session cache so a
double-click doesn't hammer the site). Nothing is precomputed and shipped — the
numbers are pulled at the moment you click Analyze.
"""

from __future__ import annotations
import os
import threading
import time
import webbrowser
from typing import Dict

from flask import Flask, jsonify, request, send_file, Response

try:
    from flask_cors import CORS
except Exception:  # noqa: BLE001 - optional; only needed if opened from file://
    CORS = None

import config
import utils
import psx_data
import scraper
import scorer
import predictor

app = Flask(__name__)
if CORS:
    CORS(app)

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(HERE, "dashboard.html")

# Tiny in-memory analysis cache: symbol -> (timestamp, payload)
_analysis_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------
@app.route("/")
def index() -> Response:
    if os.path.exists(DASHBOARD):
        return send_file(DASHBOARD)
    return Response("dashboard.html not found next to analyzer.py", status=500)


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "app": config.APP_NAME,
                    "version": config.APP_VERSION, "time": utils.now_iso()})


@app.route("/api/progress")
def progress():
    """v3.5 — real pipeline progress for the dashboard's percentage bar."""
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"pct": 0, "stage": ""})
    return jsonify(utils.progress_get(symbol))


# ---------------------------------------------------------------------------
# Stock universe
# ---------------------------------------------------------------------------
@app.route("/api/stocks")
def stocks():
    force = request.args.get("refresh") in ("1", "true", "yes")
    uni = psx_data.get_universe(force_refresh=force)
    return jsonify(uni)


# ---------------------------------------------------------------------------
# Analyse one company
# ---------------------------------------------------------------------------
def _analyse(symbol: str) -> Dict:
    symbol = symbol.strip().upper()
    ttl = config.ANALYSIS_TTL_MINUTES * 60
    with _cache_lock:
        hit = _analysis_cache.get(symbol)
        if hit and (time.time() - hit[0]) < ttl:
            utils.progress_update(symbol, 100, "Ready")   # instant cache hit
            return hit[1]

    scraped = scraper.scrape_company(symbol)
    utils.progress_update(symbol, 90, "Scoring the 11 fundamentals…")
    result = scorer.score_company(scraped)
    utils.progress_update(symbol, 100, "Ready")

    with _cache_lock:
        _analysis_cache[symbol] = (time.time(), result)
    return result


@app.route("/api/analyze")
def analyze():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "Pass ?symbol=TICKER"}), 400
    try:                                   # v3.3: background work yields
        import deepdata; deepdata.note_user_activity()
    except Exception:  # noqa: BLE001
        pass
    if request.args.get("fresh") in ("1", "true"):
        with _cache_lock:
            _analysis_cache.pop(symbol.upper(), None)
    try:
        return jsonify(_analyse(symbol))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Analysis failed: {exc}", "symbol": symbol}), 500


# ---------------------------------------------------------------------------
# Prediction (technical + fundamental outlook — guidance only, never advice)
# ---------------------------------------------------------------------------
_prediction_cache: Dict[str, tuple] = {}


@app.route("/api/predict")
def predict_route():
    """
    Scrape (or reuse the cached scrape), score fundamentals, then run the
    Bulls-&-Bears-style prediction engine in predictor.py.  The dashboard
    computes the identical analysis client-side from the /api/analyze payload,
    so this endpoint mainly exists for API users and scripting.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Pass ?symbol=TICKER"}), 400
    try:                                   # v3.3: background work yields
        import deepdata; deepdata.note_user_activity()
    except Exception:  # noqa: BLE001
        pass
    ttl = config.ANALYSIS_TTL_MINUTES * 60
    with _cache_lock:
        hit = _prediction_cache.get(symbol)
        if hit and (time.time() - hit[0]) < ttl:
            return jsonify(hit[1])
    try:
        scraped = scraper.scrape_company(symbol)
        utils.progress_update(symbol, 90, "Running the technical engine…")
        fundamental = scorer.score_company(scraped)
        result = predictor.predict(scraped, fundamental)
        utils.progress_update(symbol, 100, "Ready")
        result["fundamental"] = {
            "score": fundamental.get("score"),
            "verdict": fundamental.get("verdict"),
            "highlights": fundamental.get("highlights"),
            "concerns": fundamental.get("concerns"),
        }
        with _cache_lock:
            _prediction_cache[symbol] = (time.time(), result)
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Prediction failed: {exc}", "symbol": symbol}), 500



# ---------------------------------------------------------------------------
# v4.0 — Recommended stocks of the month
# ---------------------------------------------------------------------------
@app.route("/api/recommendations")
def recommendations():
    """Top monthly picks ranked by the blended fundamental + technical score.
    While the background scan is running the payload reports live progress."""
    try:
        import recommend
        force = request.args.get("refresh") in ("1", "true", "yes")
        return jsonify(recommend.get_recommendations(force=force))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "error": str(exc), "picks": []}), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def _warm_universe():
    """Refresh the stock list in the background so the first page load is quick."""
    try:
        psx_data.get_universe(force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] universe warm-up failed: {exc}")
    # v4.0 speed rule — the landing page's ranked lists come FIRST. Kick the
    # (parallel) recommendations build off before anything else so the main
    # page fills within seconds; a cached list is served instantly meanwhile.
    try:
        import recommend
        recommend.start_build(False)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] recommendations build not started: {exc}")
    # v3.2 — then the polite background pass that collects official filed
    # reports for every listed COMPANY (ETFs and debt instruments skipped).
    # v4.0.2 speed rule — the prewarm waits until USABLE ranked lists exist
    # (lists_ready), not merely until is_building() flips: checking
    # is_building() had a race — the gate could look BEFORE the build thread
    # started, see False, and release the prewarm to fight the scan for
    # bandwidth. lists_ready() has no such race, and recommend additionally
    # parks the prewarm (note_user_activity) while any scan is running.
    def _prewarm_after_recommend():
        try:
            import recommend
            waited = 0
            while waited < 1800 and not recommend.lists_ready():  # ≤30 min guard
                time.sleep(10)
                waited += 10
        except Exception:  # noqa: BLE001
            pass
        try:
            import deepdata
            uni = psx_data.get_universe()
            syms = [s["symbol"] for s in uni.get("symbols", [])
                    if not s.get("isETF") and not s.get("isDebt")]
            deepdata.start_prewarm(syms)
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] deep-data prewarm not started: {exc}")

    threading.Thread(target=_prewarm_after_recommend, daemon=True,
                     name="psx-prewarm-gate").start()


@app.route("/api/deep-status")
def deep_status():
    """Progress of the official-filings store (deepdata.py)."""
    try:
        import deepdata
        return jsonify(deepdata.status())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


def _open_browser():
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://{config.HOST}:{config.PORT}")
    except Exception:  # noqa: BLE001
        pass


def main():
    print("=" * 60)
    print(f"  {config.APP_NAME}  ·  v{config.APP_VERSION}")
    print(f"  Dashboard:  http://{config.HOST}:{config.PORT}")
    print("  Refreshing PSX stock list in the background ...")
    print("=" * 60)

    threading.Thread(target=_warm_universe, daemon=True).start()
    if config.OPEN_BROWSER:
        threading.Thread(target=_open_browser, daemon=True).start()

    # threaded so background scrapes don't block the UI
    app.run(host=config.HOST, port=config.PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
