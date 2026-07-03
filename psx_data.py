"""
psx_data.py
===========
Builds the *current* PSX stock universe so the dashboard's dropdowns are always
up to date — because PSX keeps adding (and occasionally removing) listings.

What it produces (cached to psx_cache/universe.json with a timestamp):

    {
      "generated_at": "...Z",
      "source": "live" | "fallback" | "mixed",
      "counts": {...},
      "symbols": [ {symbol, name, sector, isETF, isDebt}, ... ],   # everything
      "indices": {
          "KSE100": ["OGDC","HBL", ...],
          "KSE50":  [...],
          "KSE30":  [...],
          "KMI30":  [...],
          "ALLSHR": [...]
      },
      "sectors": { "OIL & GAS EXPLORATION COMPANIES": ["OGDC","PPL",...], ... }
    }

Run it on its own to refresh / inspect:

    python psx_data.py            # refresh + print a summary
    python psx_data.py --json     # dump the full universe as JSON
"""

from __future__ import annotations
import sys
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

import config
import utils


# ---------------------------------------------------------------------------
# Fallback snapshot — used only if the live scrape fails, so the UI never
# ends up with an empty dropdown. The live path above is always preferred.
# (A representative cross-section of the board; the live scrape returns the
#  complete, current set.)
# ---------------------------------------------------------------------------
_FALLBACK_SYMBOLS: List[Dict] = [
    # symbol, name, sector
    ("OGDC",  "Oil & Gas Development Company",      "OIL & GAS EXPLORATION COMPANIES"),
    ("PPL",   "Pakistan Petroleum",                "OIL & GAS EXPLORATION COMPANIES"),
    ("POL",   "Pakistan Oilfields",                "OIL & GAS EXPLORATION COMPANIES"),
    ("MARI",  "Mari Petroleum",                    "OIL & GAS EXPLORATION COMPANIES"),
    ("PSO",   "Pakistan State Oil",                "OIL & GAS MARKETING COMPANIES"),
    ("APL",   "Attock Petroleum",                  "OIL & GAS MARKETING COMPANIES"),
    ("SHEL",  "Shell Pakistan",                    "OIL & GAS MARKETING COMPANIES"),
    ("HBL",   "Habib Bank",                        "COMMERCIAL BANKS"),
    ("UBL",   "United Bank",                       "COMMERCIAL BANKS"),
    ("MCB",   "MCB Bank",                          "COMMERCIAL BANKS"),
    ("BAHL",  "Bank AL Habib",                     "COMMERCIAL BANKS"),
    ("MEBL",  "Meezan Bank",                       "COMMERCIAL BANKS"),
    ("BAFL",  "Bank Alfalah",                      "COMMERCIAL BANKS"),
    ("NBP",   "National Bank of Pakistan",         "COMMERCIAL BANKS"),
    ("FABL",  "Faysal Bank",                       "COMMERCIAL BANKS"),
    ("AKBL",  "Askari Bank",                       "COMMERCIAL BANKS"),
    ("LUCK",  "Lucky Cement",                      "CEMENT"),
    ("DGKC",  "D.G. Khan Cement",                  "CEMENT"),
    ("MLCF",  "Maple Leaf Cement",                 "CEMENT"),
    ("FCCL",  "Fauji Cement",                      "CEMENT"),
    ("PIOC",  "Pioneer Cement",                    "CEMENT"),
    ("CHCC",  "Cherat Cement",                     "CEMENT"),
    ("KOHC",  "Kohat Cement",                      "CEMENT"),
    ("ENGRO", "Engro Corporation",                 "FERTILIZER"),
    ("FFC",   "Fauji Fertilizer",                  "FERTILIZER"),
    ("EFERT", "Engro Fertilizers",                 "FERTILIZER"),
    ("FFBL",  "Fauji Fertilizer Bin Qasim",        "FERTILIZER"),
    ("FATIMA","Fatima Fertilizer",                 "FERTILIZER"),
    ("HUBC",  "Hub Power Company",                 "POWER GENERATION & DISTRIBUTION"),
    ("KAPCO", "Kot Addu Power",                    "POWER GENERATION & DISTRIBUTION"),
    ("NPL",   "Nishat Power",                      "POWER GENERATION & DISTRIBUTION"),
    ("NCPL",  "Nishat Chunian Power",              "POWER GENERATION & DISTRIBUTION"),
    ("PKGP",  "Pakgen Power",                      "POWER GENERATION & DISTRIBUTION"),
    ("SYS",   "Systems Limited",                   "TECHNOLOGY & COMMUNICATION"),
    ("AVN",   "Avanceon",                          "TECHNOLOGY & COMMUNICATION"),
    ("NETSOL","NetSol Technologies",               "TECHNOLOGY & COMMUNICATION"),
    ("TRG",   "TRG Pakistan",                      "TECHNOLOGY & COMMUNICATION"),
    ("AIRLINK","Air Link Communication",           "TECHNOLOGY & COMMUNICATION"),
    ("PTC",   "Pakistan Telecommunication",        "TECHNOLOGY & COMMUNICATION"),
    ("NESTLE","Nestle Pakistan",                   "FOOD & PERSONAL CARE PRODUCTS"),
    ("UNILEVER","Unilever Pakistan Foods",         "FOOD & PERSONAL CARE PRODUCTS"),
    ("NATF",  "National Foods",                    "FOOD & PERSONAL CARE PRODUCTS"),
    ("FCEPL", "Frieslandcampina Engro",            "FOOD & PERSONAL CARE PRODUCTS"),
    ("COLG",  "Colgate-Palmolive",                 "FOOD & PERSONAL CARE PRODUCTS"),
    ("PKGS",  "Packages Limited",                  "PAPER & BOARD"),
    ("PSEL",  "Packages Securities",               "PAPER & BOARD"),
    ("INDU",  "Indus Motor Company",               "AUTOMOBILE ASSEMBLER"),
    ("HCAR",  "Honda Atlas Cars",                  "AUTOMOBILE ASSEMBLER"),
    ("PSMC",  "Pak Suzuki Motor",                  "AUTOMOBILE ASSEMBLER"),
    ("MTL",   "Millat Tractors",                   "AUTOMOBILE ASSEMBLER"),
    ("GHGL",  "Ghani Glass",                       "GLASS & CERAMICS"),
    ("TPLP",  "TPL Properties",                    "REAL ESTATE INVESTMENT TRUST"),
    ("ILP",   "Interloop",                         "TEXTILE COMPOSITE"),
    ("NML",   "Nishat Mills",                      "TEXTILE COMPOSITE"),
    ("GATM",  "Gul Ahmed Textile",                 "TEXTILE COMPOSITE"),
    ("KTML",  "Kohinoor Textile",                  "TEXTILE COMPOSITE"),
    ("SearleC","The Searle Company",               "PHARMACEUTICALS"),
    ("AGP",   "AGP Limited",                       "PHARMACEUTICALS"),
    ("HINOON","Highnoon Laboratories",             "PHARMACEUTICALS"),
    ("GLAXO", "GlaxoSmithKline Pakistan",          "PHARMACEUTICALS"),
    ("ABOT",  "Abbott Laboratories",               "PHARMACEUTICALS"),
    ("EFUG",  "EFU General Insurance",             "INSURANCE"),
    ("AICL",  "Adamjee Insurance",                 "INSURANCE"),
    ("JLICL", "Jubilee Life Insurance",            "INSURANCE"),
    ("ISL",   "International Steels",              "ENGINEERING"),
    ("ASTL",  "Amreli Steels",                     "ENGINEERING"),
    ("MUGHAL","Mughal Iron & Steel",               "ENGINEERING"),
    ("THALL", "Thal Limited",                      "ENGINEERING"),
    ("SAZEW", "Sazgar Engineering",                "AUTOMOBILE ASSEMBLER"),
]

# A recent KSE-100 constituent snapshot (fallback only).
_FALLBACK_KSE100 = [
    "OGDC", "PPL", "POL", "MARI", "PSO", "APL", "SHEL",
    "HBL", "UBL", "MCB", "BAHL", "MEBL", "BAFL", "NBP", "FABL", "AKBL",
    "LUCK", "DGKC", "MLCF", "FCCL", "PIOC", "CHCC", "KOHC",
    "ENGRO", "FFC", "EFERT", "FFBL", "FATIMA",
    "HUBC", "KAPCO", "NPL", "NCPL", "PKGP",
    "SYS", "AVN", "NETSOL", "TRG", "AIRLINK", "PTC",
    "NESTLE", "NATF", "FCEPL", "COLG", "PKGS",
    "INDU", "HCAR", "PSMC", "MTL", "SAZEW",
    "ILP", "NML", "GATM", "KTML",
    "SearleC", "AGP", "HINOON", "GLAXO", "ABOT",
    "EFUG", "AICL", "JLICL", "ISL", "ASTL", "MUGHAL", "THALL", "GHGL",
]

_FALLBACK_KSE30 = [
    "OGDC", "PPL", "MARI", "PSO", "HBL", "UBL", "MCB", "MEBL", "BAHL",
    "LUCK", "ENGRO", "FFC", "EFERT", "HUBC", "SYS", "PKGS", "INDU",
    "NML", "DGKC", "MLCF", "FCCL", "POL", "BAFL", "NBP", "TRG", "PPL",
    "EFUG", "ILP", "AIRLINK", "MTL",
]

_FALLBACK_KMI30 = [
    "OGDC", "PPL", "MARI", "MEBL", "LUCK", "ENGRO", "FFC", "EFERT",
    "HUBC", "SYS", "PKGS", "INDU", "NML", "DGKC", "MLCF", "FCCL", "POL",
    "TRG", "ILP", "AIRLINK", "MTL", "PIOC", "CHCC", "KOHC", "NATF",
    "GHGL", "THALL", "ISL", "MUGHAL", "AGP",
]


def _fallback_universe() -> dict:
    symbols = [
        {"symbol": s, "name": n, "sector": sec, "isETF": False, "isDebt": False}
        for (s, n, sec) in _FALLBACK_SYMBOLS
    ]
    kse100 = _FALLBACK_KSE100
    return {
        "generated_at": utils.now_iso(),
        "source": "fallback",
        "symbols": symbols,
        "indices": {
            "KSE100": kse100,
            "KSE50":  kse100[:50],
            "KSE30":  list(dict.fromkeys(_FALLBACK_KSE30)),
            "KMI30":  list(dict.fromkeys(_FALLBACK_KMI30)),
            "ALLSHR": [s["symbol"] for s in symbols],
        },
        "sectors": _group_by_sector(symbols),
        "counts": {"symbols": len(symbols), "KSE100": len(kse100)},
    }


def _group_by_sector(symbols: List[dict]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for row in symbols:
        out.setdefault(row["sector"] or "OTHER", []).append(row["symbol"])
    return out


# ---------------------------------------------------------------------------
# Live scraping
# ---------------------------------------------------------------------------
def fetch_all_symbols(session) -> Optional[List[dict]]:
    """
    PSX Data Portal publishes every listed symbol as JSON at /symbols.
    Shape per item (keys vary slightly over time, so we read defensively):
        {"symbol": "...", "name": "...", "sectorName": "...",
         "isETF": bool, "isDebt": bool}
    """
    data = utils.fetch(config.PSX_SYMBOLS_URL, session=session, as_json=True)
    if not isinstance(data, list) or not data:
        return None

    out: List[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sym = (item.get("symbol") or item.get("ticker") or "").strip()
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": (item.get("name") or item.get("companyName") or sym).strip(),
            "sector": (item.get("sectorName") or item.get("sector") or "OTHER").strip().upper(),
            "isETF": bool(item.get("isETF") or item.get("isEtf")),
            "isDebt": bool(item.get("isDebt")),
        })
    return out or None


def fetch_index_members(index_code: str, session, valid: set) -> Optional[List[str]]:
    """
    Scrape an index page (e.g. /indices/KSE100) and pull the constituent
    tickers out of it. PSX renders constituents in a table; rather than depend
    on a fragile column position, we extract anything that looks like a ticker
    and keep only those that are real listed symbols.
    """
    html = utils.fetch(config.PSX_INDEX_URL.format(index=index_code), session=session)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    found: List[str] = []
    seen = set()

    # Prefer links to /company/SYMBOL — the most reliable signal.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/company/" in href:
            sym = href.rsplit("/", 1)[-1].strip().upper()
            if sym and sym in valid and sym not in seen:
                seen.add(sym)
                found.append(sym)

    # Fallback: scan table cells for bare tickers.
    if not found:
        for cell in soup.find_all(["td", "th"]):
            token = cell.get_text(strip=True).upper()
            if token and token in valid and token not in seen:
                seen.add(token)
                found.append(token)

    return found or None


def build_universe(force_refresh: bool = False) -> dict:
    """
    Assemble the full universe. Cached to disk; refreshed when the cache is
    older than UNIVERSE_TTL_HOURS or force_refresh=True.
    """
    if not force_refresh:
        cached = utils.cache_read("universe.json", config.UNIVERSE_TTL_HOURS * 3600)
        if cached:
            return cached

    session = utils.make_session()
    print("[psx_data] refreshing stock universe from PSX ...")

    symbols = fetch_all_symbols(session)
    source = "live"
    if not symbols:
        print("[psx_data] live symbol list unavailable — using fallback snapshot")
        uni = _fallback_universe()
        utils.cache_write("universe.json", uni)
        return uni

    valid = {s["symbol"].upper() for s in symbols}

    indices: Dict[str, List[str]] = {}
    for label, code in config.INDICES.items():
        members = fetch_index_members(code, session, valid)
        if members:
            indices[label] = members
        else:
            source = "mixed"
    indices.setdefault("ALLSHR", [s["symbol"] for s in symbols])

    # Backfill any index we could not scrape with the fallback snapshot,
    # intersected with the live symbol set so it stays internally consistent.
    fb = _fallback_universe()["indices"]
    for label in config.INDICES:
        if label not in indices or not indices[label]:
            indices[label] = [s for s in fb.get(label, []) if s.upper() in valid] or fb.get(label, [])
            source = "mixed"

    uni = {
        "generated_at": utils.now_iso(),
        "source": source,
        "symbols": sorted(symbols, key=lambda r: r["symbol"]),
        "indices": indices,
        "sectors": _group_by_sector(symbols),
        "counts": {
            "symbols": len(symbols),
            **{k: len(v) for k, v in indices.items()},
        },
    }
    utils.cache_write("universe.json", uni)
    print(f"[psx_data] done — {len(symbols)} symbols, source={source}")
    return uni


def get_universe(force_refresh: bool = False) -> dict:
    """Public entry point used by the Flask app."""
    try:
        return build_universe(force_refresh=force_refresh)
    except Exception as exc:  # noqa: BLE001
        print(f"[psx_data] unexpected error ({exc}); serving fallback")
        return _fallback_universe()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    uni = build_universe(force_refresh=True)
    if "--json" in sys.argv:
        print(json.dumps(uni, ensure_ascii=False, indent=2))
    else:
        print("\n=== PSX UNIVERSE ===")
        print(f"generated_at : {uni['generated_at']}")
        print(f"source       : {uni['source']}")
        print(f"total symbols: {uni['counts'].get('symbols')}")
        for label in config.INDICES:
            print(f"  {label:7s}: {uni['counts'].get(label, 0)} members")
        print(f"sectors      : {len(uni['sectors'])}")
