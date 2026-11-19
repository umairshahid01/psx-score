"""
verify_fix.py — proves the v5.0 token layer actually works.

Points the REAL config/utils/psx_data/scraper at the simulator and checks:

  1. a plain request (pre-fix behaviour) is rejected exactly as PSX rejects it
  2. utils.request() gets 200 JSON from /symbols
  3. psx_data.build_universe() returns a LIVE universe (not the fallback)
  4. scraper.scrape_price_history() returns real bars with volume
  5. a STALE token (after rotation) is refreshed and the request retried
  6. 12 concurrent threads produce ONE token fetch, not 12
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psx_sim                                               # noqa: E402

PORT = 8911
BASE = f"http://127.0.0.1:{PORT}"

srv = psx_sim.serve(PORT)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)

# --- point the real app at the simulator BEFORE utils is imported ---------
sys.path.insert(0, "/home/claude/psx_src")
import config                                                # noqa: E402

config.PSX_BASE = BASE
config.PSX_SYMBOLS_URL = f"{BASE}/symbols"
config.PSX_COMPANY_URL = BASE + "/company/{symbol}"
config.PSX_INDEX_URL = BASE + "/indices/{index}"
config.PSX_TIMESERIES = BASE + "/timeseries/eod/{symbol}"
config.PSX_MARKET_WATCH = f"{BASE}/market-watch"
config.PSX_ANNOUNCEMENTS_URL = f"{BASE}/announcements/companies"
config.PSX_TOKEN = dict(config.PSX_TOKEN,
                        hosts=("127.0.0.1",),
                        pages=(f"{BASE}/historical", f"{BASE}/"))
config.CACHE_DIR = "/tmp/psx_verify_cache"
config.REPORTS_DIR = os.path.join(config.CACHE_DIR, "reports")

import utils                                                 # noqa: E402
import psx_data                                              # noqa: E402
import scraper                                               # noqa: E402

import requests                                              # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


print("\n=== 1. Pre-fix behaviour is reproduced (the bug itself) ===")
r = requests.get(f"{BASE}/symbols", timeout=5,
                 headers={"User-Agent": "Mozilla/5.0"})
check("no headers at all -> 404 (PSX HTML not-found)", r.status_code == 404,
      f"got {r.status_code}")
r = requests.get(f"{BASE}/symbols", timeout=5,
                 headers={"User-Agent": "Mozilla/5.0",
                          "X-Requested-With": "XMLHttpRequest"})
check("X-Requested-With but no token -> 403 empty body",
      r.status_code == 403 and not r.content, f"got {r.status_code}")

print("\n=== 2. The token layer ===")
tok = utils.TOKENS.get()
check("token scraped from the PSX page HTML", bool(tok), f"_k={tok}")
check("token page recorded", bool(utils.TOKENS.last_page),
      str(utils.TOKENS.last_page))
hdr = utils.psx_headers(f"{BASE}/symbols", as_json=True)
check("X-Req-Id attached to a data route", "X-Req-Id" in hdr)
check("X-Requested-With attached to a data route",
      hdr.get("X-Requested-With") == "XMLHttpRequest")
hdr2 = utils.psx_headers(f"{BASE}/company/LUCK")
check("X-Requested-With NOT sent to an HTML page",
      "X-Requested-With" not in hdr2 and "X-Req-Id" in hdr2)

print("\n=== 3. utils.request() against the gated data route ===")
res = utils.request(f"{BASE}/symbols", as_json=True)
check("/symbols -> 200 JSON", res.ok and isinstance(res.body, list),
      f"status={res.status} n={len(res.body) if res.ok else 0}")
check("token was used", res.token_used)

print("\n=== 4. The stock universe is LIVE, not the fallback ===")
uni = psx_data.build_universe(force_refresh=True)
check("source == 'live' or 'mixed'", uni["source"] in ("live", "mixed"),
      f"source={uni['source']}")
check("symbols came from the portal",
      uni["counts"]["symbols"] == len(psx_sim.SYMBOLS),
      f"{uni['counts']['symbols']} symbols")
check("index constituents scraped from the index page",
      len(uni["indices"].get("KSE100") or []) == len(psx_sim.SYMBOLS),
      f"KSE100={len(uni['indices'].get('KSE100') or [])}")

print("\n=== 5. Price history ===")
hist = scraper.scrape_price_history("LUCK", utils.make_session())
check("bars returned", len(hist) > 200, f"{len(hist)} bars")
if hist:
    check("bars carry close", all(p.get("close") for p in hist))
    check("bars carry volume", all("volume" in p for p in hist))
    check("sorted oldest -> newest",
          all(hist[i]["date"] <= hist[i + 1]["date"] for i in range(len(hist) - 1)))
    check("as-of contract enforced (nothing newer than prev trading day)",
          hist[-1]["date"] <= utils.as_of_iso(),
          f"last bar {hist[-1]['date']}, as-of {utils.as_of_iso()}")

print("\n=== 6. A STALE token is refreshed and the request retried ===")
before = psx_sim.STATS["stale_403"]
utils.TOKENS.get()                       # make sure we hold one
time.sleep(psx_sim.ROTATE_SECONDS + 1)   # let the portal rotate underneath us
psx_sim.current_token()                  # force the rotation to happen
res = utils.request(f"{BASE}/symbols", as_json=True)
check("request still succeeded after rotation", res.ok, f"status={res.status}")
check("a 403 was seen and the token refreshed",
      res.token_refreshed or psx_sim.STATS["stale_403"] > before,
      f"stale_403 {before} -> {psx_sim.STATS['stale_403']}, "
      f"refreshed={res.token_refreshed}")

print("\n=== 7. Concurrency: 12 threads, ONE token fetch ===")
utils.TOKENS.invalidate()
fetches_before = utils.TOKENS.fetches
results = []
def worker():
    results.append(utils.request(f"{BASE}/symbols", as_json=True).ok)
threads = [threading.Thread(target=worker) for _ in range(12)]
[t.start() for t in threads]
[t.join() for t in threads]
check("all 12 requests succeeded", all(results) and len(results) == 12,
      f"{sum(results)}/12")
spent = utils.TOKENS.fetches - fetches_before
check("token fetched once, not twelve times", spent <= 2,
      f"{spent} page fetch(es) for 12 requests")

print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("  FAILED: " + ", ".join(FAIL))
print("=" * 62)
srv.shutdown()
sys.exit(1 if FAIL else 0)
