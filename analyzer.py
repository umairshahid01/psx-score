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
    return jsonify({"ok": True, "app": config.APP_NAME, "time": utils.now_iso()})


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
            return hit[1]

    scraped = scraper.scrape_company(symbol)
    result = scorer.score_company(scraped)

    with _cache_lock:
        _analysis_cache[symbol] = (time.time(), result)
    return result


@app.route("/api/analyze")
def analyze():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "Pass ?symbol=TICKER"}), 400
    if request.args.get("fresh") in ("1", "true"):
        with _cache_lock:
            _analysis_cache.pop(symbol.upper(), None)
    try:
        return jsonify(_analyse(symbol))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Analysis failed: {exc}", "symbol": symbol}), 500



# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def _warm_universe():
    """Refresh the stock list in the background so the first page load is quick."""
    try:
        psx_data.get_universe(force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] universe warm-up failed: {exc}")


def _open_browser():
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://{config.HOST}:{config.PORT}")
    except Exception:  # noqa: BLE001
        pass


def main():
    print("=" * 60)
    print(f"  {config.APP_NAME}")
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
