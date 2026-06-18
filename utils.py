"""
utils.py
========
Shared helpers used across the scraper and the stock-list updater:

  * a resilient requests session (retries, rotating User-Agent, timeouts)
  * robust parsing of money figures the way PSX / annual reports print them
    e.g. "1,234,567", "(45,000)" -> negative, "Rs. 12.5 bn", "—" -> None
  * tiny JSON disk-cache helpers with a timestamp

Nothing here is PSX-specific so it stays easy to test.
"""

from __future__ import annotations
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

import config


# ---------------------------------------------------------------------------
# HTTP
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


def fetch(url: str, *, session: Optional[requests.Session] = None,
          as_json: bool = False, expect_binary: bool = False) -> Any:
    """
    GET a URL with retries and exponential backoff.

    Returns parsed JSON (as_json=True), raw bytes (expect_binary=True) or text.
    Returns None on total failure rather than raising, so the app degrades
    gracefully instead of crashing on a single bad endpoint.
    """
    sess = session or make_session()
    delay = config.REQUEST_BACKOFF
    last_err: Optional[Exception] = None

    for attempt in range(1, config.REQUEST_RETRIES + 1):
        try:
            # rotate UA per attempt to look less robotic
            sess.headers["User-Agent"] = random.choice(config.USER_AGENTS)
            resp = sess.get(url, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            if expect_binary:
                return resp.content
            if as_json:
                return resp.json()
            return resp.text
        except Exception as exc:  # noqa: BLE001 - we want any failure to retry
            last_err = exc
            if attempt < config.REQUEST_RETRIES:
                time.sleep(delay)
                delay *= config.REQUEST_BACKOFF

    print(f"  [fetch] gave up on {url}: {last_err}")
    return None


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"-?\(?\d[\d,]*\.?\d*\)?")

# A unit written *immediately after* the number (e.g. "12.5 bn", "5m").
# Bare single letters are allowed here because the \b guard stops them matching
# unrelated following words ("100 members" does not become 100 million).
_SUFFIX_SCALE = [
    (re.compile(r"^(bn|billion)\b"), 1e9),
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
# Disk cache
# ---------------------------------------------------------------------------
def _ensure_dirs() -> None:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    os.makedirs(config.REPORTS_DIR, exist_ok=True)


def cache_write(name: str, payload: Any) -> None:
    _ensure_dirs()
    path = os.path.join(config.CACHE_DIR, name)
    wrapper = {"_saved_at": datetime.now(timezone.utc).isoformat(), "data": payload}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(wrapper, fh, ensure_ascii=False, indent=2)


def cache_read(name: str, max_age_seconds: float) -> Optional[Any]:
    """Return cached data if present and fresh enough, else None."""
    path = os.path.join(config.CACHE_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            wrapper = json.load(fh)
        saved = datetime.fromisoformat(wrapper["_saved_at"])
        age = (datetime.now(timezone.utc) - saved).total_seconds()
        if age <= max_age_seconds:
            return wrapper["data"]
    except Exception:  # noqa: BLE001 - corrupt cache should just be ignored
        return None
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
