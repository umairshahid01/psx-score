"""
utils.py
========
Shared helpers used across the scraper and the stock-list updater:

  * a resilient requests session (retries, rotating User-Agent, timeouts)
  * the PSX REQUEST-TOKEN layer (v5.0 — see below)
  * robust parsing of money figures the way PSX / annual reports print them
    e.g. "1,234,567", "(45,000)" -> negative, "Rs. 12.5 bn", "—" -> None
  * tiny JSON disk-cache helpers with a timestamp


v5.0 — WHY THE TOOL STOPPED WORKING, AND WHAT FIXES IT
------------------------------------------------------
On 2026-09-24 PSX put a token check in front of every DATA endpoint on
dps.psx.com.pk. The portal's own JavaScript (/static/script.js) does this:

    var ps = window.__ps;
    ps && ps._k && $.ajaxSetup({headers: {"X-Req-Id": ps._k}});

...where every HTML page carries an inline script of the form

    <script>window.__ps = {"lc":"en","tz":"Asia/Karachi","_k":"<token>","rv":"1"};</script>

So a data request now needs TWO headers that this app never sent:

    X-Requested-With: XMLHttpRequest   <- without it PSX serves its 404 page
    X-Req-Id:         <token>          <- without it PSX returns 403, empty body

The symptoms that follow are exactly what users reported:
  * GET /symbols                 -> 404/403, so the stock universe collapsed
                                    back to the small hard-coded fallback list
  * GET /timeseries/eod/{SYMBOL} -> 403, so there was no price history, hence
                                    no price, no chart, no EMA/RSI, no trade
                                    plan, and a fundamental score starved of
                                    its share-price input.

HTML pages were never gated, which is why the app still *looked* alive: the
company page loaded, so a profile appeared, and everything numeric was blank.

The layer below therefore:
  1. scrapes the token out of a PSX HTML page (TOKEN_PAGES, in order),
  2. caches it process-wide for TOKEN_TTL (the token rotates every few
     minutes, so this is deliberately shorter than the observed rotation),
  3. attaches it per-request — never on the session, because the session is
     shared across threads and the token changes under it,
  4. on a 403 from a PSX host, throws the token away, fetches a fresh one and
     retries ONCE, outside the normal retry budget,
  5. and if no token can be found at all, sends the request anyway, so the
     app keeps working the day PSX drops the check.

Nothing else here is PSX-specific, so it all stays easy to test.
"""

from __future__ import annotations
import json
import os
import random
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import config


# --- PSX token-layer settings (overridable in config.PSX_TOKEN) ------------
_TOK_CFG: Dict[str, Any] = getattr(config, "PSX_TOKEN", {}) or {}
_TOKEN_HOSTS: Tuple[str, ...] = tuple(
    _TOK_CFG.get("hosts") or ("dps.psx.com.pk",))
_TOKEN_PAGES: Tuple[str, ...] = tuple(
    _TOK_CFG.get("pages") or (f"{config.PSX_BASE}/historical",
                              f"{config.PSX_BASE}/",
                              f"{config.PSX_BASE}/company/OGDC"))
# PSX rotates the token every few minutes; refresh well inside that.
_TOKEN_TTL: float = float(_TOK_CFG.get("ttl_seconds", 150))
_TOKEN_FAIL_BACKOFF: float = float(_TOK_CFG.get("fail_backoff_seconds", 60))
# Paths that are JSON/AJAX data routes rather than HTML pages.
_PSX_DATA_PATHS: Tuple[str, ...] = tuple(
    _TOK_CFG.get("data_paths") or (
        "/symbols", "/timeseries/", "/market-watch",
        "/sector-summary/", "/company/payouts",
    ))
# NOTE: /indices/{CODE} and /historical are HTML pages, NOT data routes —
# they must not get X-Requested-With or PSX may serve a different shape.
# They still receive the X-Req-Id token, which HTML pages ignore harmlessly.


# ---------------------------------------------------------------------------
# HTTP — session
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    """A session with a browser-like header set."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(config.USER_AGENTS),
        "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s


# ---------------------------------------------------------------------------
# HTTP — the PSX request token (v5.0). See the module docstring.
# ---------------------------------------------------------------------------
# The inline script is   window.__ps = {... "_k":"<token>" ...}
# Two patterns: the precise one first, then a loose one so a cosmetic change to
# the surrounding object (key order, whitespace, single quotes) cannot blind us.
_TOKEN_PATTERNS = (
    re.compile(r"""window\.__ps\s*=\s*\{[^{}]*?['"]_k['"]\s*:\s*['"]([^'"]{4,})['"]"""),
    re.compile(r"""['"]_k['"]\s*:\s*['"]([A-Za-z0-9_\-=.]{6,})['"]"""),
)


def _psx_host(url: str) -> bool:
    """Is this URL on the token-gated PSX data portal?"""
    try:
        # .hostname (not .netloc) so a port never defeats the match
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(host == h or host.endswith("." + h) for h in _TOKEN_HOSTS)


def _is_psx_data_path(url: str) -> bool:
    """
    Does this PSX path need `X-Requested-With: XMLHttpRequest`?

    Only the JSON/AJAX data routes do. Sending it on an HTML page can change
    what PSX serves, so HTML page loads (/company/X, /historical, /) are left
    strictly alone — they are also the pages we read the token FROM.
    """
    try:
        path = urlparse(url).path.lower()
    except Exception:  # noqa: BLE001
        return False
    return any(path == p or path.startswith(p) for p in _PSX_DATA_PATHS)


class _TokenProvider:
    """
    Process-wide, thread-safe holder for the PSX `X-Req-Id` token.

    One lock guards everything, so twelve scraping threads starting at once
    produce ONE token fetch, not twelve. A failed fetch parks further attempts
    for TOKEN_FAIL_BACKOFF seconds instead of hammering the portal, and a
    token we already hold is kept and returned during that window — a stale
    token is strictly better than none.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token: Optional[str] = None
        self._fetched_at = 0.0
        self._failed_at = 0.0
        self.fetches = 0            # diagnostics (doctor.py reads these)
        self.failures = 0
        self.refreshes = 0
        self.last_error: Optional[str] = None
        self.last_page: Optional[str] = None

    # -- public ------------------------------------------------------------
    def get(self, *, force: bool = False) -> Optional[str]:
        with self._lock:
            now = time.time()
            if (not force and self._token
                    and (now - self._fetched_at) < _TOKEN_TTL):
                return self._token
            if (not force and self._failed_at
                    and (now - self._failed_at) < _TOKEN_FAIL_BACKOFF):
                return self._token          # may be None; caller copes
            token = self._scrape()
            if token:
                self._token = token
                self._fetched_at = time.time()
                self._failed_at = 0.0
                self.failures = 0
            else:
                self._failed_at = time.time()
                self.failures += 1
            return self._token

    def invalidate(self) -> None:
        """Called after a 403 — the token we hold is no longer accepted."""
        with self._lock:
            self._token = None
            self._fetched_at = 0.0
            self._failed_at = 0.0       # a 403 must not be stuck behind backoff
            self.refreshes += 1

    def peek(self) -> Optional[str]:
        with self._lock:
            return self._token

    def age(self) -> Optional[float]:
        with self._lock:
            return (time.time() - self._fetched_at) if self._fetched_at else None

    # -- internals ---------------------------------------------------------
    def _scrape(self) -> Optional[str]:
        """
        Load a PSX HTML page and pull `_k` out of it.

        Deliberately uses its OWN bare session and never calls fetch(), so
        there is no way for token acquisition to recurse into itself.
        """
        for page in _TOKEN_PAGES:
            self.fetches += 1
            try:
                sess = requests.Session()
                sess.headers.update({
                    "User-Agent": random.choice(config.USER_AGENTS),
                    "Accept": ("text/html,application/xhtml+xml,"
                               "application/xml;q=0.9,*/*;q=0.8"),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Connection": "keep-alive",
                })
                resp = sess.get(page, timeout=config.REQUEST_TIMEOUT)
                if resp.status_code != 200 or not resp.text:
                    self.last_error = f"{page} -> HTTP {resp.status_code}"
                    continue
                for pattern in _TOKEN_PATTERNS:
                    m = pattern.search(resp.text)
                    if m:
                        self.last_page = page
                        self.last_error = None
                        return m.group(1)
                self.last_error = f"{page} -> 200 but no _k in the page"
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{page} -> {type(exc).__name__}: {exc}"
            finally:
                try:
                    sess.close()
                except Exception:  # noqa: BLE001
                    pass
        return None


TOKENS = _TokenProvider()


def psx_headers(url: str, *, as_json: bool = False,
                force_token: bool = False) -> Dict[str, str]:
    """
    The extra headers a PSX request needs. Safe to call for any URL — returns
    {} for anything that is not on the PSX portal.

    Exposed publicly so code that streams a response itself (PDF downloads in
    catalyst.py / deepdata.py) can send the same headers without going through
    fetch().
    """
    if not _psx_host(url):
        return {}
    headers: Dict[str, str] = {
        "Referer": config.PSX_BASE + "/",
        "Origin": config.PSX_BASE,
    }
    if as_json or _is_psx_data_path(url):
        # Without this PSX answers a data route with its HTML 404 page.
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    token = TOKENS.get(force=force_token)
    if token:
        headers["X-Req-Id"] = token
    return headers


# ---------------------------------------------------------------------------
# HTTP — fetch
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# v5.0.1 — remembered-dead-URL cache (404 / 410 only). See request() for why.
# ---------------------------------------------------------------------------
_DEAD_TTL = 6 * 3600.0          # a page that 404s now is still gone in an hour
_DEAD_MAX = 4000                # hard bound; this must never grow unbounded
_dead_urls: Dict[str, Tuple[float, int]] = {}
_dead_lock = threading.Lock()


def _dead_get(url: str) -> Optional[int]:
    with _dead_lock:
        hit = _dead_urls.get(url)
        if not hit:
            return None
        when, status = hit
        if (time.time() - when) > _DEAD_TTL:
            _dead_urls.pop(url, None)
            return None
        return status


def _dead_put(url: str, status: int) -> None:
    with _dead_lock:
        if len(_dead_urls) >= _DEAD_MAX:
            # drop the oldest third rather than clearing everything
            for u, _ in sorted(_dead_urls.items(),
                               key=lambda kv: kv[1][0])[:_DEAD_MAX // 3]:
                _dead_urls.pop(u, None)
        _dead_urls[url] = (time.time(), status)


def dead_url_stats() -> Dict[str, int]:
    """Diagnostics for doctor.py — how many dead URLs are being skipped."""
    with _dead_lock:
        return {"remembered": len(_dead_urls)}


class Result:
    """What a request actually did — so failures can be reported, not guessed."""

    __slots__ = ("ok", "status", "body", "error", "url", "attempts",
                 "token_used", "token_refreshed", "elapsed_ms")

    def __init__(self, url: str) -> None:
        self.ok = False
        self.status: Optional[int] = None
        self.body: Any = None
        self.error: Optional[str] = None
        self.url = url
        self.attempts = 0
        self.token_used = False
        self.token_refreshed = False
        self.elapsed_ms = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Result {'ok' if self.ok else 'FAIL'} {self.status} "
                f"{self.url} ({self.elapsed_ms}ms)>")


def request(url: str, *, session: Optional[requests.Session] = None,
            as_json: bool = False, expect_binary: bool = False,
            method: str = "GET", data: Any = None,
            timeout: Optional[float] = None) -> Result:
    """
    The real worker: GET/POST a URL and report precisely what happened.

    Retries transient failures (5xx, network) with real exponential backoff.
    4xx is permanent and fails fast — EXCEPT a 403 from the PSX portal, which
    since 2026-09-24 means "your request token is missing or stale". That one
    case refreshes the token and retries once, outside the retry budget.
    """
    sess = session or make_session()
    res = Result(url)
    started = time.time()

    # v5.0.1 — DON'T RE-ASK A DEAD URL.
    # StockAnalysis has no /dividend/ page for a large slice of PSX names, and
    # some symbols have no financials page at all. Every scan was re-requesting
    # those same URLs and taking a fresh round-trip to be told 404 again — in a
    # 110-symbol scan with several such URLs per symbol that is a lot of wall
    # clock spent learning nothing. A 404/410 is remembered for _DEAD_TTL and
    # answered locally. Nothing else is cached: a 403, a 5xx or a timeout is
    # transient and must still be retried for real.
    if method.upper() == "GET":
        dead = _dead_get(url)
        if dead is not None:
            res.status = dead
            res.error = f"HTTPError: {dead} (remembered from an earlier request)"
            res.elapsed_ms = 0
            return res
    delay = max(0.2, float(config.REQUEST_BACKOFF))
    tries = max(1, int(config.REQUEST_RETRIES))
    token_retry_left = 1 if _psx_host(url) else 0
    force_token = False
    last_err: Optional[Exception] = None

    attempt = 0
    while attempt < tries:
        attempt += 1
        res.attempts = attempt
        try:
            headers = psx_headers(url, as_json=as_json,
                                  force_token=force_token)
            force_token = False
            if headers.get("X-Req-Id"):
                res.token_used = True
            # rotate UA per attempt to look less robotic
            headers.setdefault("User-Agent", random.choice(config.USER_AGENTS))
            resp = sess.request(method.upper(), url, headers=headers, data=data,
                                timeout=timeout or config.REQUEST_TIMEOUT)
            res.status = resp.status_code

            # --- the PSX token path -------------------------------------
            if resp.status_code == 403 and token_retry_left > 0:
                token_retry_left -= 1
                TOKENS.invalidate()
                force_token = True
                res.token_refreshed = True
                attempt -= 1          # a token refresh is not a real attempt
                print(f"  [psx] 403 on {url} — refreshing the request token "
                      f"and retrying once")
                time.sleep(0.4)
                continue

            resp.raise_for_status()

            if expect_binary:
                res.body = resp.content
            elif as_json:
                res.body = resp.json()
            else:
                res.body = resp.text
            res.ok = True
            res.elapsed_ms = int((time.time() - started) * 1000)
            return res

        except Exception as exc:  # noqa: BLE001
            last_err = exc
            status = getattr(getattr(exc, "response", None), "status_code",
                             None) or res.status
            # 4xx is permanent (dead endpoint / no such page) — do not burn
            # seconds retrying it.
            if status is not None and 400 <= status < 500:
                if status in (404, 410) and method.upper() == "GET":
                    _dead_put(url, status)
                break
            if attempt < tries:
                time.sleep(delay)
                delay *= 2.0            # real exponential backoff

    res.error = f"{type(last_err).__name__}: {last_err}" if last_err else "failed"
    res.elapsed_ms = int((time.time() - started) * 1000)
    hint = ""
    if res.status == 403 and _psx_host(url):
        hint = ("  <- PSX rejected the request token; see utils.TOKENS "
                "(run `python doctor.py`)")
    elif res.status == 404 and _psx_host(url) and _is_psx_data_path(url):
        hint = "  <- PSX 404 on a data route usually means a missing header"
    print(f"  [fetch] gave up on {url}: {res.error}{hint}")
    return res


def fetch(url: str, *, session: Optional[requests.Session] = None,
          as_json: bool = False, expect_binary: bool = False) -> Any:
    """
    GET a URL with retries and backoff.

    Returns parsed JSON (as_json=True), raw bytes (expect_binary=True) or text.
    Returns None on total failure rather than raising, so the app degrades
    gracefully instead of crashing on a single bad endpoint.

    Kept as the thin, long-standing interface every scraper already calls;
    request() above is the same thing with the failure detail attached.
    """
    res = request(url, session=session, as_json=as_json,
                  expect_binary=expect_binary)
    return res.body if res.ok else None


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"-?\(?\d[\d,]*\.?\d*\)?")

# A unit written *immediately after* the number (e.g. "12.5 bn", "5m").
# Bare single letters are allowed here because the \b guard stops them matching
# unrelated following words ("100 members" does not become 100 million).
_SUFFIX_SCALE = [
    (re.compile(r"^(b|bn|billion)\b"), 1e9),
    (re.compile(r"^(cr|crore)\b"), 1e7),
    (re.compile(r"^(mn|mln|million|m)\b"), 1e6),
    (re.compile(r"^(lac|lakh)\b"), 1e5),
    (re.compile(r"^(k|thousand|000)\b"), 1e3),
]

# A unit declared in a caption / column header (e.g. "Rupees in '000").
_CONTEXT_SCALE = [
    (re.compile(r"in\s*'?\s*billion|\bbillions?\b|\bbn\b"), 1e9),
    (re.compile(r"in\s*'?\s*crore|\bcrores?\b|\bcr\b"), 1e7),
    (re.compile(r"in\s*'?\s*million|\bmillions?\b|\bmn\b|\bmln\b"), 1e6),
    (re.compile(r"in\s*'?\s*lac|in\s*'?\s*lakh|\blakhs?\b"), 1e5),
    (re.compile(r"in\s*'?\s*000|in\s*'?\s*thousand|\bthousands?\b"), 1e3),
]

_BLANKS = {"-", "—", "–", "N/A", "n/a", "NA", "nil", "Nil", "", "*", "**"}


def _resolve_scale(suffix: str, context: str) -> float:
    suffix = suffix.strip().lower()
    for pat, factor in _SUFFIX_SCALE:
        if pat.match(suffix):
            return factor
    context = context.lower()
    for pat, factor in _CONTEXT_SCALE:
        if pat.search(context):
            return factor
    return 1.0


def to_number(raw: Any, scale_hint: str = "") -> Optional[float]:
    """
    Turn a messy financial string into a float.

      "1,234,567"                    -> 1234567.0
      "(45,000)"                     -> -45000.0       (accounting negatives)
      "Rs. 12.5 bn"                  -> 12500000000.0
      "1,500" with hint "in '000"    -> 1500000.0
      "—" / "" / "N/A"               -> None

    The unit is read from the text *after* the number or from `scale_hint`
    (a column header / caption). It is never read from inside the number, so a
    comma group like "45,000" is not mistaken for "thousands".
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if text in _BLANKS:
        return None

    negative = "(" in text and ")" in text
    m = _NUM_RE.search(text)
    if not m:
        return None

    token = m.group(0).replace("(", "").replace(")", "").replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    if negative:
        value = -abs(value)

    suffix = text[m.end():m.end() + 10]
    context = text[:m.start()] + " " + (scale_hint or "")
    return value * _resolve_scale(suffix, context)


def pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Safe percentage; returns None if it cannot be computed."""
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator) * 100.0


def cagr(first: Optional[float], last: Optional[float], years: int) -> Optional[float]:
    """Compound annual growth rate as a percentage; handles sign edge cases."""
    if first is None or last is None or years <= 0:
        return None
    if first <= 0 or last <= 0:
        # CAGR is undefined across a sign change; fall back to simple growth
        if first == 0:
            return None
        return ((last - first) / abs(first)) * 100.0
    return ((last / first) ** (1.0 / years) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# v4.0 — PSX trading calendar & the "as-of" contract
# ---------------------------------------------------------------------------
# NON-NEGOTIABLE RULE: every number the app computes, charts or displays comes
# from the LAST COMPLETED TRADING SESSION — i.e. the close of the previous
# trading day relative to the day the tool is being run. Never today's partial
# intraday session, never an assumed value.
#
#   run on Tue 21 Jul 2026  ->  as-of = Mon 20 Jul 2026 close
#   run on Mon 20 Jul 2026  ->  as-of = Fri 17 Jul 2026 close
#
# PSX trades Monday–Friday. Public holidays are NOT hard-coded (they change
# every year and a wrong hard-coded list is worse than none): instead the feed
# itself is the authority — we take the newest session in the feed that is
# on or before the computed as-of date. If that session is older than the
# as-of date, the data is flagged STALE and surfaced to the user rather than
# being silently passed off as current.

PKT = timezone(timedelta(hours=5))          # Pakistan Standard Time, UTC+5


def psx_now() -> datetime:
    """Wall-clock time in Pakistan, where the exchange actually lives."""
    return datetime.now(PKT)


def psx_today() -> date:
    return psx_now().date()


def is_trading_day(d: date) -> bool:
    """PSX runs Mon–Fri. (Holiday gaps are resolved from the feed itself.)"""
    return d.weekday() < 5


def previous_trading_day(ref: Optional[date] = None) -> date:
    """The most recent Mon–Fri strictly BEFORE `ref` (default: today in PKT)."""
    d = (ref or psx_today()) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def as_of_date() -> date:
    """
    The single source of truth for "what date is this analysis about".
    Always the previous trading day's close — never today's live session.
    """
    return previous_trading_day()


def as_of_iso() -> str:
    return as_of_date().isoformat()


def ts_to_psx_date(ts: Any) -> Optional[str]:
    """
    Convert a PSX epoch timestamp to the correct CALENDAR DATE in Pakistan.

    This matters: reading a PSX timestamp with utcfromtimestamp() can shift a
    session back by one day (a midnight-PKT stamp is 19:00 the previous day in
    UTC), which silently mislabels every bar in the series. Always PKT.
    """
    try:
        n = int(float(ts))
    except (TypeError, ValueError):
        return None
    if n > 1e11:                       # milliseconds
        n //= 1000
    try:
        return datetime.fromtimestamp(n, PKT).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def trim_history_to_as_of(history: List[Dict],
                          as_of: Optional[str] = None) -> Tuple[List[Dict], Dict]:
    """
    Enforce the as-of contract on a price series.

    Drops every bar dated AFTER the as-of date (i.e. today's partial session)
    and reports what the series actually ends on, so callers can warn instead
    of assuming.

    Returns (trimmed_history, meta) where meta is:
        as_of            target date (previous trading day)
        last_close_date  newest session actually present
        last_close       its closing price
        dropped          how many future/partial bars were removed
        stale            True if the feed has not caught up to as_of
        stale_days       calendar days between last_close_date and as_of
    """
    target = as_of or as_of_iso()
    meta = {"as_of": target, "last_close_date": None, "last_close": None,
            "dropped": 0, "stale": False, "stale_days": 0}
    if not history:
        meta["stale"] = True
        return [], meta

    kept, dropped = [], 0
    for pt in history:
        d = str(pt.get("date") or "")[:10]
        if not d:
            continue
        if d > target:
            dropped += 1
            continue
        kept.append(pt)

    kept.sort(key=lambda r: str(r.get("date"))[:10])
    meta["dropped"] = dropped
    if not kept:
        meta["stale"] = True
        return [], meta

    last = kept[-1]
    meta["last_close_date"] = str(last.get("date"))[:10]
    meta["last_close"] = last.get("close")
    if meta["last_close_date"] < target:
        try:
            gap = (date.fromisoformat(target)
                   - date.fromisoformat(meta["last_close_date"])).days
        except ValueError:
            gap = 0
        meta["stale_days"] = gap
        # 1–4 day gaps are normal (weekends + public holidays). Beyond that the
        # feed genuinely has not updated and the user deserves to know.
        meta["stale"] = gap > 4
    return kept, meta


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def _ensure_dirs() -> None:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    os.makedirs(config.REPORTS_DIR, exist_ok=True)


def cache_write(name: str, payload: Any) -> None:
    _ensure_dirs()
    path = os.path.join(config.CACHE_DIR, name)
    wrapper = {"_saved_at": datetime.now(timezone.utc).isoformat(),
               "_as_of": as_of_iso(),          # v4.0 — session this cache describes
               "data": payload}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(wrapper, fh, ensure_ascii=False, indent=2)


def cache_read(name: str, max_age_seconds: float,
               require_as_of: bool = True) -> Optional[Any]:
    """
    Return cached data if present and fresh enough, else None.

    v4.0 — a cache entry is also rejected when it was built for a DIFFERENT
    trading session. A TTL alone is not enough: a scan finished at 16:00 is
    still "4 hours old" the next morning, yet it describes yesterday's close,
    not the one the user is entitled to see. Stamping the session and matching
    on it is the only way to guarantee the as-of contract survives a cache hit.

    Pass require_as_of=False for date-independent payloads (e.g. the symbol
    universe), which are not tied to any one session.
    """
    path = os.path.join(config.CACHE_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            wrapper = json.load(fh)
        if require_as_of and wrapper.get("_as_of") != as_of_iso():
            return None                      # built for another session — refuse
        saved = datetime.fromisoformat(wrapper["_saved_at"])
        age = (datetime.now(timezone.utc) - saved).total_seconds()
        if age <= max_age_seconds:
            return wrapper["data"]
    except Exception:  # noqa: BLE001 - corrupt cache should just be ignored
        return None
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Analysis progress reporting (v3.5)
# ---------------------------------------------------------------------------
# The scraper and analyzer report REAL pipeline stages here; the dashboard
# polls GET /api/progress and renders a live percentage — genuine progress,
# not a fake animation.

_progress_lock = threading.Lock()
_progress: dict = {}


def progress_reset(symbol: str) -> None:
    with _progress_lock:
        _progress[symbol.upper()] = {"pct": 0, "stage": ""}


def progress_update(symbol: str, pct: float, stage: str = None) -> None:
    """Monotonic within a run — a late-arriving lower stage never rewinds."""
    with _progress_lock:
        cur = _progress.setdefault(symbol.upper(), {"pct": 0, "stage": ""})
        if pct >= cur["pct"]:
            cur["pct"] = min(100, round(pct, 1))
            if stage:
                cur["stage"] = stage


def progress_get(symbol: str) -> dict:
    """v5.0.1 — now carries an explicit `done` flag.

    The old payload was {pct, stage} only, so the dashboard had no way to know
    the pipeline had finished and kept polling at ~2.4 requests/second for as
    long as the panel was open. `done` lets the client stop the moment the work
    is complete instead of hammering the endpoint.
    """
    with _progress_lock:
        cur = dict(_progress.get(symbol.upper(), {"pct": 0, "stage": ""}))
    cur["done"] = bool(cur.get("pct", 0) >= 100)
    return cur
