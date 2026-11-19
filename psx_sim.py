"""
psx_sim.py — a faithful simulator of dps.psx.com.pk's post-2026-09-24 behaviour.

Reproduces, exactly, what the real portal now does:

  GET /historical        HTML 200, carries  window.__ps = {... "_k":"<tok>" ...}
  GET /                  same
  GET /company/{SYM}     HTML 200 (never gated), carries the token too

  GET /symbols                   no X-Requested-With -> 404 HTML not-found page
                                 no/ bad X-Req-Id    -> 403, EMPTY body
                                 both correct        -> 200 JSON
  GET /timeseries/eod/{SYM}      same gating -> 200 JSON [[ts, close, vol, open]]
  GET /indices/{CODE}            HTML 200, gated on token only (it is a page)

The token ROTATES every ROTATE_SECONDS, and old tokens are rejected with 403 —
which is the case that proves refresh-and-retry actually works.

Run:  python psx_sim.py [port]
"""
import json
import random
import secrets
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROTATE_SECONDS = 6          # fast rotation, so a test can hit a stale token
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

_lock = threading.Lock()
_current = {"token": secrets.token_hex(8), "born": time.time()}
_retired: list = []

STATS = {"token_pages": 0, "403": 0, "404": 0, "200": 0, "stale_403": 0}

SYMBOLS = [
    ("OGDC", "Oil & Gas Development Company Limited", "OIL & GAS EXPLORATION COMPANIES"),
    ("LUCK", "Lucky Cement Limited", "CEMENT"),
    ("HBL", "Habib Bank Limited", "COMMERCIAL BANKS"),
    ("ENGRO", "Engro Corporation Limited", "FERTILIZER"),
    ("SYS", "Systems Limited", "TECHNOLOGY & COMMUNICATION"),
    ("MARI", "Mari Energies Limited", "OIL & GAS EXPLORATION COMPANIES"),
    ("FFC", "Fauji Fertilizer Company Limited", "FERTILIZER"),
    ("MEBL", "Meezan Bank Limited", "COMMERCIAL BANKS"),
]


def current_token() -> str:
    with _lock:
        if time.time() - _current["born"] > ROTATE_SECONDS:
            _retired.append(_current["token"])
            _current["token"] = secrets.token_hex(8)
            _current["born"] = time.time()
        return _current["token"]


def token_valid(tok: str) -> str:
    """-> 'ok' | 'stale' | 'bad'"""
    if not tok:
        return "bad"
    with _lock:
        if tok == _current["token"]:
            return "ok"
        if tok in _retired:
            return "stale"
    return "bad"


def page_html(title: str, body: str = "") -> bytes:
    tok = current_token()
    STATS["token_pages"] += 1
    return f"""<!doctype html>
<html><head><title>{title} | PSX Data Portal</title>
<script>window.__ps = {{"lc":"en","tz":"Asia/Karachi","_k":"{tok}","rv":"1"}};</script>
<script src="/static/script.js?v=1.76"></script>
</head><body>{body}</body></html>""".encode()


def eod_series(symbol: str, days: int = 420):
    """[[epoch_ms, close, volume, open], ...] oldest -> newest, like PSX."""
    rnd = random.Random(symbol)
    price = rnd.uniform(80, 900)
    out = []
    d = date.today() - timedelta(days=int(days * 1.45))
    while d <= date.today():
        if d.weekday() < 5:
            price = max(5.0, price * (1 + rnd.gauss(0.0006, 0.016)))
            ts = int(time.mktime(d.timetuple())) * 1000
            out.append([ts, round(price, 2),
                        int(rnd.uniform(2e5, 9e6)), round(price * 0.995, 2)])
        d += timedelta(days=1)
    return out[-days:]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):          # keep the test output clean
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-DIS-Request-ID", secrets.token_hex(6))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _gate(self, *, need_xrw: bool) -> bool:
        """True if the request may proceed. Mirrors PSX exactly."""
        if need_xrw and self.headers.get("X-Requested-With") != "XMLHttpRequest":
            STATS["404"] += 1
            self._send(404, page_html("Page not found"))
            return False
        state = token_valid(self.headers.get("X-Req-Id") or "")
        if state != "ok":
            STATS["403"] += 1
            if state == "stale":
                STATS["stale_403"] += 1
            self._send(403, b"")        # empty body, exactly like PSX
            return False
        return True

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]

        # --- HTML pages that CARRY the token (never gated) ---------------
        if path in ("/", "/historical"):
            return self._send(200, page_html("Historical"))
        if path.startswith("/company/") and path != "/company/payouts":
            sym = path.rsplit("/", 1)[-1].upper()
            body = (f"<h1>{sym}</h1>"
                    "<table><tr><th>Sector</th><td>CEMENT</td></tr>"
                    "<tr><th>Current</th><td>1,234.56</td></tr></table>")
            return self._send(200, page_html(sym, body))
        if path == "/static/script.js":
            js = (b'(function(){var ps=window.__ps;ps&&ps._k&&'
                  b'$.ajaxSetup({headers:{"X-Req-Id":ps._k}})})();')
            return self._send(200, js, "application/javascript")

        # --- index pages: HTML, token-gated, no X-Requested-With ---------
        if path.startswith("/indices/"):
            if not self._gate(need_xrw=False):
                return
            STATS["200"] += 1
            links = "".join(f'<tr><td><a href="/company/{s}">{s}</a></td></tr>'
                            for s, _, _ in SYMBOLS)
            return self._send(200, page_html("KSE100", f"<table>{links}</table>"))

        # --- JSON data routes: need BOTH headers -------------------------
        if path == "/symbols":
            if not self._gate(need_xrw=True):
                return
            STATS["200"] += 1
            payload = [{"symbol": s, "name": n, "sectorName": sec,
                        "isETF": False, "isDebt": False}
                       for s, n, sec in SYMBOLS]
            return self._send(200, json.dumps(payload).encode(),
                              "application/json")

        if path.startswith("/timeseries/eod/"):
            if not self._gate(need_xrw=True):
                return
            STATS["200"] += 1
            sym = path.rsplit("/", 1)[-1].upper()
            payload = {"status": 1, "message": "", "data": eod_series(sym)}
            return self._send(200, json.dumps(payload).encode(),
                              "application/json")

        if path == "/__stats":
            return self._send(200, json.dumps(STATS).encode(),
                              "application/json")

        STATS["404"] += 1
        return self._send(404, page_html("Page not found"))


def serve(port=PORT):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    s = serve()
    print(f"PSX simulator on http://127.0.0.1:{PORT} "
          f"(token rotates every {ROTATE_SECONDS}s)")
    s.serve_forever()
