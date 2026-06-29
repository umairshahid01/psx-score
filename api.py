"""
api.py
======
Cloud-hosted backend for the PSX·SCORE PWA.

Identical API surface to the desktop analyzer.py, but designed to run on
a free cloud platform (Render, Railway, Fly.io, or any PaaS that runs Python).

Endpoints:
    GET  /                           → serves index.html (the PWA dashboard)
    GET  /manifest.json              → PWA manifest
    GET  /sw.js                      → service worker
    GET  /api/health                 → heartbeat
    GET  /api/stocks?refresh=1       → current PSX stock universe
    GET  /api/analyze?symbol=OGDC    → scrape + score one company, live

Deploy:
    1. Push this file + the Python modules to a new GitHub repo (or a branch).
    2. Connect to Render → New Web Service → point at the repo.
    3. Build command:  pip install -r requirements.txt
    4. Start command:  gunicorn api:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2
    5. That's it — friends open the URL on any phone.
"""

from __future__ import annotations
import os
import threading
import time
from typing import Dict

from flask import Flask, jsonify, request, send_from_directory, Response

try:
    from flask_cors import CORS
except Exception:
    CORS = None

import config
import utils
import psx_data
import scraper
import scorer

app = Flask(__name__, static_folder="static")
if CORS:
    CORS(app)

HERE = os.path.dirname(os.path.abspath(__file__))

# In-memory analysis cache: symbol -> (timestamp, payload)
_analysis_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Static files (PWA)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(HERE, "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def sw():
    return send_from_directory(HERE, "sw.js", mimetype="application/javascript")


@app.route("/icon-<size>.png")
def icons(size):
    path = os.path.join(HERE, f"icon-{size}.png")
    if os.path.exists(path):
        return send_from_directory(HERE, f"icon-{size}.png")
    # If icon files don't exist yet, return a 1x1 transparent PNG placeholder
    import base64
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
        "nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
    )
    return Response(pixel, mimetype="image/png")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "app": config.APP_NAME, "time": utils.now_iso()})


@app.route("/api/stocks")
def stocks():
    force = request.args.get("refresh") in ("1", "true", "yes")
    uni = psx_data.get_universe(force_refresh=force)
    return jsonify(uni)


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
    except Exception as exc:
        return jsonify({"error": f"Analysis failed: {exc}", "symbol": symbol}), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def _warm_universe():
    try:
        psx_data.get_universe(force_refresh=True)
    except Exception as exc:
        print(f"[startup] universe warm-up failed: {exc}")


# Warm universe in background on first import
threading.Thread(target=_warm_universe, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"PSX·SCORE Cloud API running on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
