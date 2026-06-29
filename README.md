# PSX·SCORE — Fundamental Health Analyzer

Score any **Pakistan Stock Exchange** listed company's fundamentals from **0 to 100**,
with an animated health gauge, a per-metric breakdown (each with a plain-English explainer
and the exact source document it came from), and 1/3/5/10-year historical trend charts.

**One file. Two layouts.** `index.html` is a single, self-contained, fully responsive web
app. The same file lays itself out for a phone or a laptop automatically — if it works in a
desktop browser it works in a mobile browser, because it's the same HTML. Just share the
file; there's nothing to install.

---

## Two ways to run it

| | How | What you get |
|---|---|---|
| **Share mode (DEMO)** | Send `index.html` to a colleague. They double-click it (or you host it). | Runs offline in **DEMO** mode with 7 fully-loaded sample companies (OGDC, LUCK, SYS, HBL, ENGRO, KTML, SAZEW). No backend, no setup. |
| **Live mode** | Launch through `run.bat` (local) **or** host `api.py` on Render/Railway. | Flips to **LIVE** mode and scrapes real PSX data on every Analyze, for the full stock universe. |

The app decides automatically: on load it pings the backend at `/api/health`. If it answers,
you get the green **LIVE** badge; if not, it shows the gold **DEMO** badge and uses the data
embedded at the bottom of the file. No flags to set.

> **Hosting the file elsewhere and want LIVE mode?** Open `index.html`, find `const API_BASE="";`
> near the top of the script, and set it to your backend URL
> (e.g. `const API_BASE="https://psx-score.onrender.com";`). Leave it blank for same-origin.

---

## Sharing with colleagues (the simple path)

1. Send them **`index.html`** (WhatsApp, email, Drive — anything).
2. They open it. On a laptop it opens wide; on a phone it opens as a single column.
3. They search a sample ticker and hit **Analyze**.

That's it. No Python, no `.bat`, no app store.

---

## Hosting it online (optional, for LIVE data for everyone)

The repo already includes a cloud backend (`api.py`) and deploy config.

1. Push the repo to GitHub.
2. Go to **render.com** → New Web Service → pick the repo → it reads `render.yaml` → **Apply**.
3. You get a URL like `https://psx-score.onrender.com` that serves the app **and** the live engine.
4. Share that URL. On HTTPS, browsers also offer **Install** (adds an icon to the home screen / desktop).

---

## What changed in this version

The previous separate **mobile PWA** and **desktop** front-ends are now **one responsive
`index.html`**. Mobile behaviour is unchanged; tablet (≥720px) and desktop (≥1024px) layers were
added on top — a two-column score/company header, a two-column metric grid, a taller trend
chart, hover states for pointers, and a centered dialog (instead of a bottom sheet) for the
explainer popups on large screens. Pinch-zoom is now allowed for accessibility.

---

## Files

**Updated this round**

| File | Role |
|------|------|
| `index.html` | **The app.** Single responsive file (desktop + mobile), with demo data embedded. This is the one you share. |
| `index_template.html` | Source template (same file, minus the embedded data blob). Edit this, then rebuild. |
| `manifest.json` | PWA manifest (install metadata; orientation no longer locked to portrait). |
| `sw.js` | Service worker — caches the app shell when hosted (skipped harmlessly on `file://`). |
| `README.md` | This file. |

**Build helper**

| File | Role |
|------|------|
| `build.py` | Rebuilds `index.html` = `index_template.html` with `demo_bundle.json` injected. Run after editing the template. |
| `demo_bundle.json` | The embedded demo dataset (universe + 7 analyses). |
| `gen_icons.py` | Regenerates the PNG icons. |
| `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`, `favicon-32.png` | App / tab icons. |

**Backend (only used for LIVE mode)**

| File | Role |
|------|------|
| `api.py` | Flask engine: serves the app + `/api/health`, `/api/stocks`, `/api/analyze`. |
| `config.py`, `utils.py`, `psx_data.py`, `scraper.py`, `scorer.py` | Scraping + scoring modules. |
| `requirements.txt`, `runtime.txt`, `Procfile`, `render.yaml` | Cloud deploy config. |

---

## Rebuilding after an edit

Edit `index_template.html`, then:

```bash
python build.py        # writes index.html with the demo data injected
```

---

## Scoring, in brief

Nine checks, **equally weighted**, each scored 0–10 against sensible thresholds and summed to 0–100:
revenue growth, net-profit margin, EPS growth, debt-to-equity, return on equity, current ratio,
cash-flow quality, dividend consistency, and 12-month share-price trend. Banks use a model where
debt/equity is swapped for capital adequacy. Each metric shows the **source document and date**
behind its number, plus a **data-confidence** reading so you know how complete the picture is.

*Not investment advice.*
