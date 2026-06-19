# PSX·SCORE — Pakistan Stock Exchange Fundamental Scorer

Pick any PSX-listed stock, hit **Analyze**, and get a **0–100 fundamental health score**
with a full metric-by-metric breakdown, multi-year trend charts, and side-by-side
comparison — all from **public PSX data** that is scraped **live each time you run it**.

Higher score = more fundamentally solid. *This is fundamental analysis only — not a
price prediction and not investment advice.*

![mode](https://img.shields.io/badge/data-live%20scrape-00E5A0) ![lang](https://img.shields.io/badge/python-3.10%2B-FFB627) ![ui](https://img.shields.io/badge/UI-light%20%2B%20dark-8794AE)

---

## What it does

- **Live scrape** of public PSX pages (company profile, financial statements, price
  history) plus annual-report PDF parsing as a fallback gap-filler — no paid APIs.
- **Auto-updating stock universe.** The KSE-100 / KSE-50 / KSE-30 / KMI-30 and the full
  listed-symbol list are rebuilt from PSX on launch, so newly listed companies show up
  automatically.
- **Smart scoring.** Nine equally-weighted fundamentals (revenue growth, profit margin, EPS
  growth, debt/equity, ROE, current ratio, cash-flow quality, dividend consistency, and
  **share-price trend**).
  Banks & financials are scored with a **separate model** (debt/equity is dropped,
  capital adequacy is added).
  Every metric's **source document** and **date** are shown in the explainer modal.
- **Honest about data.** Missing figures are dropped and weights re-normalised; you see a
  **coverage %** and a **data-confidence %** so you know how complete the picture is.
- **Visual everything.** Animated health gauge, staggered metric bars, multi-year trend
  charts (1Y / 3Y / 5Y / 10Y), price sparkline.
  Full **light & dark mode**.

> **DEMO vs LIVE** — open `dashboard.html` on its own and it runs in **DEMO** mode with a
> few bundled sample companies so you can see exactly how it looks and behaves. Launch it
> through `run.bat` (which starts the Python engine) and it flips to **LIVE** mode and
> scrapes real PSX data on every Analyze.

---

## How it works (architecture)

```
   run.bat  ──pulls latest code──►  GitHub repo (your public repo)
      │
      ▼
   analyzer.py  (local Flask engine on 127.0.0.1:5000)
      │  scrapes on demand
      ▼
   dps.psx.com.pk  (public PSX data + annual-report PDFs)
      │
      ▼
   dashboard.html  (opens in your browser, talks to the local engine)
```

Everything runs **locally on your machine**. The only reason you stay online is that the
scrape happens **live at analysis time** so the numbers are always current. The code
itself lives on **GitHub**, and `run.bat` re-downloads it on every launch — so when you
push an update, everyone who runs the `.bat` gets it automatically.

### Files

| File | Role |
|------|------|
| `dashboard.html` | The whole front-end (UI, gauge, charts, animations). Works live or in demo. |
| `analyzer.py`    | Flask server: serves the dashboard + `/api/analyze`, `/api/stocks`. |
| `psx_data.py`    | Builds & caches the live PSX stock universe (the auto-updating list). |
| `scraper.py`     | Scrapes a company's profile, financial tables, price history, and report PDFs. |
| `scorer.py`      | Pure scoring logic (no network): turns scraped data into the 0–100 score. |
| `utils.py`       | Shared helpers: HTTP session, robust number parsing, caching. |
| `config.py`      | All settings: ports, PSX endpoints, scoring weights, your GitHub details. |
| `requirements.txt` | Python dependencies. |
| `run.bat`        | Windows one-click launcher (installs Python if needed, pulls code, starts app). |

---

## Setup (one time, ~5 minutes)

You need a free **GitHub** account.

### 1. Create a public repo and upload the files
1. On GitHub: **New repository** → name it e.g. `psx-score` → **Public** → Create.
2. **Add file → Upload files** → drag in **all** the files listed above (every `.py`,
   `dashboard.html`, `requirements.txt`, and `run.bat`) → **Commit**.

### 2. Point the launcher at your repo
Open **`run.bat`** in Notepad and edit the three lines near the top:
```bat
set "GITHUB_USER=your-github-username"
set "GITHUB_REPO=psx-score"
set "GITHUB_BRANCH=main"
```
Save. (If your default branch is `master`, put that instead of `main`.)

### 3. Run it
Double-click **`run.bat`**. On first launch it will:
- check for Python (and offer to install it via `winget` if it's missing),
- download the latest code from your repo,
- install the Python packages,
- start the local engine and **open your browser** at `http://127.0.0.1:5000`.

Keep the black launcher window open while you use the app. Close it (or `Ctrl+C`) when done.

### 4. Share it
Send friends **just the `run.bat` file**. As long as your repo is public, their copy
pulls your code and runs the same way. When you improve something, push to GitHub — they
get it on their next launch, no re-sharing needed.

---

## Using the app

1. **Choose an index** chip (All / KSE-100 / KSE-50 / KSE-30 / KMI-30) to narrow the list.
2. **Type a symbol or company name** (e.g. `OGDC`, `HBL`, `LUCK`) and pick it.
3. Hit **Analyze**. Watch the gauge sweep to the score.
4. Read the **breakdown** — green = strong, amber = watch, red = weak — each with the raw
   figure and its weight.
5. Explore **trends**: switch the metric and the 1Y/3Y/5Y/10Y window; check the price
   sparkline.
6. Toggle **light/dark** with the sun/moon button (your choice is remembered).

---

## Scoring, in brief

Each metric is scored 0–10 against sensible thresholds, then **all metrics are
weighted equally** and the average is scaled to 0–100.

| Metric | Weight (general) | Good when |
|--------|:---:|-----------|
| Revenue growth (multi-yr CAGR) | 11.1% | strong, sustained growth |
| Net profit margin | 11.1% | high & stable |
| EPS growth (CAGR) | 11.1% | rising earnings per share |
| Debt / equity | 11.1% | low leverage |
| Return on equity | 11.1% | efficient use of capital |
| Current ratio | 11.1% | healthy short-term liquidity |
| Cash-flow quality (OCF vs net profit) | 11.1% | profits backed by real cash |
| Dividend consistency | 11.1% | regular payouts |
| Share-price trend (12-month) | 11.1% | upward momentum |

**Banks / financials** use a model where debt/equity is removed, **capital adequacy** is
added, and there are 8 equally-weighted metrics — because a bank's balance sheet doesn't
read like an industrial company's.

Verdict bands: **Rock Solid** (85+), **Strong** (70+), **Decent** (55+), **Mixed** (40+),
**Fragile** (25+), **Weak** (below 25).

---

## Known challenges & notes

- **PSX can change its page layout.** The scraper is defensive (it tries multiple table
  shapes and falls back to PDF parsing), but if PSX restructures heavily, some figures may
  go missing — the app will tell you via a lower coverage/confidence score rather than
  guessing. If that happens, the mapping lives in `scraper.py` (`LINE_ITEMS`) and is easy
  to extend.
- **First analysis of a session is slower** because it warms up the stock list and pulls
  fresh statements; repeat analyses are cached for ~30 minutes.
- **Rate-limiting / blocks.** Heavy repeated scraping can get throttled by PSX. The app
  rotates user-agents and backs off, but be reasonable.
- **Windows-first.** `run.bat` targets Windows. On macOS/Linux you can run it manually:
  `pip install -r requirements.txt` then `python analyzer.py`.
- **Not advice.** This scores *current fundamentals only*. It does not predict prices and
  is not a recommendation to buy or sell anything.

---

*Built for learning and exploring PSX fundamentals. Data belongs to its respective
sources; this tool only reads public information.*
