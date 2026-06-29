# PSX·SCORE — Fundamental Health Analyzer (Desktop)

Score any **Pakistan Stock Exchange** listed company's fundamentals from **0 to 100**,
with an animated health gauge, a per-metric breakdown (each metric shows a plain-English
explainer **and the exact source document + date** it came from), a data-confidence reading,
and 1 / 3 / 5 / 10-year historical trend charts. Built for the **laptop / desktop** browser.

It runs **live**: every time you hit Analyze it scrapes fresh PSX data on the spot.

---

## How your colleagues use it — just the `.bat`

You share **one file: `run.bat`**. A colleague double-clicks it and, the first time, it:

1. checks for Python and installs it automatically (via `winget`) if it's missing,
2. downloads the latest app code from your GitHub repo,
3. installs the Python packages,
4. starts the local engine and opens the dashboard at `http://127.0.0.1:5000`.

After the first run it launches in a few seconds. They keep the small black window open while
using the app and close it when done. No App Store, no APK — just the `.bat`.

> When you improve something, push to GitHub. Everyone's `run.bat` pulls the new version on its
> next launch — you never have to re-send anything.

---

## One-time setup for YOU (the owner)

1. **Push all the files below to your public repo** `github.com/Umairshahid01/psx-score`
   (replace the current contents — see "Files" below). The repo must stay **Public** so the
   `.bat` can download from it.
2. Confirm the three lines near the top of `run.bat` match your repo:
   ```bat
   set "GITHUB_USER=Umairshahid01"
   set "GITHUB_REPO=psx-score"
   set "GITHUB_BRANCH=main"
   ```
3. Send colleagues **`run.bat`**. Done.

To test it yourself: double-click `run.bat`, or run the engine directly:
```bash
pip install -r requirements.txt
python api.py        # then open http://127.0.0.1:5000
```

---

## Live vs Demo

The dashboard checks for the local engine on load:

- **LIVE** (green badge) — the engine is running (via `run.bat` / `python api.py`), so it scrapes
  real PSX data for the **full** stock universe on every Analyze.
- **DEMO** (gold badge) — if you just open `dashboard.html` on its own with no engine, it falls
  back to 7 bundled sample companies (OGDC, LUCK, SYS, HBL, ENGRO, KTML, SAZEW) so you can see how
  it looks. The `.bat` always gives colleagues LIVE mode.

---

## Files (push all of these to the repo)

**Run with the `.bat`**

| File | Role |
|------|------|
| `run.bat` | **The only file you share.** Installs Python if needed, pulls the code, runs the engine, opens the dashboard. |
| `api.py` | Local engine (Flask): serves `dashboard.html` + `/api/health`, `/api/stocks`, `/api/analyze`. |
| `config.py`, `utils.py`, `psx_data.py`, `scraper.py`, `scorer.py` | PSX scraping + 0–100 scoring logic. |
| `requirements.txt` | Python packages the engine needs. |
| `dashboard.html` | The desktop dashboard UI (this is what the engine serves). |

**Editing / rebuilding the UI**

| File | Role |
|------|------|
| `dashboard_template.html` | Source for the UI. Edit this, then run `python build.py`. |
| `demo_bundle.json` | The bundled demo dataset (used for the DEMO fallback). |
| `build.py` | Rebuilds `dashboard.html` = `dashboard_template.html` + `demo_bundle.json`. |

> The old mobile files (`index.html`, `index_template.html`, `manifest.json`, `sw.js`, icons)
> are not used by this desktop build. You can leave them in the repo or delete them — the `.bat`
> only downloads the files listed above.

---

## Scoring, in brief

Nine checks, **equally weighted**, each scored 0–10 against sensible thresholds and summed to
0–100: revenue growth, net-profit margin, EPS growth, debt-to-equity, return on equity, current
ratio, cash-flow quality, dividend consistency, and 12-month share-price trend. Banks use a model
where debt/equity is swapped for capital adequacy. Each metric shows the **source document and
date** behind its number, and a **data-confidence** reading tells you how complete the picture is.

---

## Realistic notes for sharing via `.bat`

- **Windows only.** The `.bat` is a Windows launcher.
- **Internet required** — the whole point is live scraping at analysis time.
- **First run is slower** (~2–3 min) while Python and packages install; later runs are quick.
- A one-time **Windows SmartScreen** ("More info → Run anyway") and a **firewall "Allow access"**
  prompt for Python are normal; it only listens on the colleague's own machine.
- **Locked-down / corporate laptops** may block `winget`, Python installs, or outbound scraping.
  On those, the colleague may need IT to allow it, or you can host `api.py` online instead and
  share a URL.

*Not investment advice.*
