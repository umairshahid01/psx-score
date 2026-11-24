# PSX·SCORE 5.0 — Pakistan Stock Exchange Stock Health Analyzer

Pick any PSX-listed company, hit **Fundamental** or **Technical**, and get a
**0–100 health score** with a full metric-by-metric breakdown, multi-year trend
charts and a chart-based outlook — all from **public PSX data scraped live**.

Higher score = more fundamentally solid. *Fundamental analysis only — not a
price prediction, and never investment advice.*

![mode](https://img.shields.io/badge/data-live%20scrape-19E39B) ![lang](https://img.shields.io/badge/python-3.10%2B-FFC247) ![ui](https://img.shields.io/badge/UI-rebuilt%20in%20v5.0-3DD9FF)

---

## What changed in this build

### The dashboard was rebuilt from scratch — "The Floor"

`dashboard.html` is a new file, not a reskin. The design language is the
instruments of an exchange: a running tape, an opening-bell boot, a segmented
**dial** with its own printed scale, order-book style metric bars, and a
**depth ladder** for the trade plan.

Three defects from the old build are gone:

| Was | Now |
|---|---|
| The trade-plan strip printed every label at its exact price, so Buy-1 and the live price — 0.5% apart — printed on top of each other, and the figures ran into the words beside them. | The **dot** keeps the true price position; only the **card** is nudged apart, with a drawn leader joining the two. Verified: 0 overlaps, 14 px minimum gap. |
| The score ring was a thin arc with a number in the middle. | A 240° instrument: verdict bands, major and minor ticks, a scale printed **outside** the arc, and a marker riding on the arc — so nothing overlaps the score. |
| The dashboard polled `/api/progress` ~2.4 times a second for as long as a panel was open. | One poller, 900 ms, which **stops itself** the moment the engine reports `done`. |

Everything that carries text now sits on an **opaque** surface, and no
animation runs over a word or a figure.

### Accuracy: a plan is now graded, not just printed

The engine could hand back a **0.2 : 1** plan styled exactly like a 3 : 1 plan.
Both are arithmetically correct; only one is worth taking. `predictor.py` now
derives a **quality grade** from the numbers it already computed — no level is
moved, nothing is invented — and the dashboard leads with it:

> **Pays too little for the risk** · 0.43 : 1
> Target 1 is only 3.3% above Buy 1 while the stop sits 7.9% below it, so this
> plan risks 2.3 rupees to make 1 … as it stands at Buy 1, this is a watch,
> not a trade.

### Speed

* **Dead URLs are remembered.** StockAnalysis has no `/dividend/` page for a
  large slice of PSX names. Every scan was re-requesting those same URLs to be
  told 404 again. A 404/410 is now cached for 6 hours and answered locally.
  Transient failures (403, 5xx, timeouts) are still retried for real.
* **Progress polling** cut from ~2.4 req/s to one request every 900 ms that
  stops on completion.
* **The OCR console flood is gone** — the engine was always shared, but the
  library re-registered its loggers and printed one INFO line per page.

### One source of truth

The browser does **no financial modelling**. It never derives a score, never
fabricates a level, never fills a gap — when a field is missing it draws
`N/A` with the engine's own reason attached. If `analyzer.py` is not running,
the page says so and tells you how to start it, instead of inventing a demo.

---

## v5.0 — the tool works again, and it looks like a different product

### 1. The fix: PSX added a request token, and the app didn't send it

On **24 September 2026** PSX put a token check in front of every data endpoint
on `dps.psx.com.pk`. The portal's own JavaScript reads a value out of each HTML
page and sends it as an `X-Req-Id` header on every AJAX call:

```html
<script>window.__ps = {"lc":"en","tz":"Asia/Karachi","_k":"<token>","rv":"1"};</script>
```
```js
var ps = window.__ps;
ps && ps._k && $.ajaxSetup({headers: {"X-Req-Id": ps._k}});
```

Without that header a data route answers **403 with an empty body**; without
`X-Requested-With: XMLHttpRequest` it answers with PSX's **HTML 404 page**.
This app sent neither, so:

| Endpoint | What happened | What broke |
|---|---|---|
| `GET /symbols` | 404 / 403 | the stock list collapsed to the ~70-name built-in snapshot |
| `GET /timeseries/eod/{SYM}` | 403 | no price, no chart, no EMA/RSI, no trade plan |

HTML pages were never gated, which is why the app still *looked* alive: the
company page loaded, a profile appeared, and everything numeric was blank.

**`utils.py` now carries a proper token layer.** It scrapes `_k` from a PSX
page, caches it process-wide for 150 s (the token rotates every few minutes),
attaches it per-request rather than on the shared session, and on a 403
throws it away, fetches a fresh one and retries once — outside the normal
retry budget. If no token can be found at all it sends the request anyway, so
the app keeps working the day PSX drops the check. Twelve scraping threads
starting at once produce **one** token fetch, not twelve.

### 2. `doctor.py` — run this first when something looks wrong

```
python doctor.py              # full check
python doctor.py LUCK         # ...using LUCK as the test company
python doctor.py --quick      # skip the slow end-to-end test
```
or just **`PSX 5.0.bat doctor`**.

It checks Python and every package, loads each module, obtains the request
token, hits **every PSX endpoint one at a time** with its real HTTP status,
tests StockAnalysis, then runs a complete scrape + score + technical pass and
reports field coverage — in plain language, with the fix for each failure.

### 3. Three places the tool used to state things it had not earned

These are honesty fixes, and they change numbers you may have seen before:

- **"Not Rated" is now a real state.** With no readable fundamentals the score
  used to fall to `0.0` and the verdict table happily returned
  *"Weak — fundamentals look poor on current data"*. That is a judgement the
  tool had not earned: the truth was that the scrape came back empty. A score
  is now only reported when at least **25 %** of the model's weight had real
  figures behind it. Below that the gauge goes grey, shows a dash, and says
  exactly why.
- **The technical verdict no longer blends an invented number.** It used to
  read `fundamental.score || 50`, so a company with unreadable accounts was
  quietly handed a neutral 50 that then carried **45 %** of the final verdict —
  a pretty chart plus zero financials could come out *"Looking Strong"*. Now an
  unrated business drops out of the blend entirely, the verdict is computed
  from the chart alone, and a line under the scores says so.
- **The stay-away list no longer punishes a scraping failure.** A company whose
  fundamentals could not be read scored 0 and fell straight into
  *"5 to Stay Away From"*. Those companies are now excluded from ranking
  altogether — a short list is acceptable, a libellous one is not.

### 4. Two ways the app could get stuck, now closed

- **The loading screen could hang for ever.** The boot page only finished when
  the ranking scan returned *at least one* candidate. A legitimately empty
  result left the user staring at it. Any terminal scan status now releases the
  app and explains what happened, and a hard **45-second watchdog** releases it
  regardless of what else goes wrong.
- **A wider net when PSX is down.** If `/symbols` cannot be reached, the
  universe is rebuilt from the full **StockAnalysis PSX listing** (several
  hundred companies) before falling back to the small built-in snapshot — and
  the UI says which source it is on.

### 5. The interface is new, not repainted

`dashboard.html` was rebuilt: new boot sequence, new app shell, new layout,
new stylesheet. Nothing of the v2–v4 design remains.

- **New boot sequence.** The wordmark draws itself and a candlestick strip
  builds across the screen as the real pipeline reports in. The ring and bar
  show genuine progress; the console line names the actual stage.
- **New app shell** — a glass app bar with a live market strip, and a
  **command palette on `Ctrl`/`⌘`+`K`** (or `/`) that searches every listed
  company from anywhere.
- **New workbench.** A sticky bar carries the company identity, the
  Fundamental / Technical tabs and Export, so you always know where you are.
- **NEW · How the score was built.** Every metric's *actual* push on the final
  number — a stacked strip showing each one's share of the points earned, and a
  diverging bar per metric showing how far above or below neutral it sits.
  Nothing hidden inside an average.
- **NEW · Fundamentals radar.** The shape of a company across all eleven
  metrics at a glance, with group averages underneath.
- **NEW · Every line item side by side.** A small-multiple grid — one mini
  chart per financial series, so the whole history reads in one glance.
- **NEW · Where every number came from.** Each source, how many figures it
  supplied, and a link to check it — plus an explicit row for what nobody
  published, which is dropped from the score rather than estimated.
- Full light and dark themes, a real reduced-motion mode that stops
  *everything*, and a print stylesheet.

### Verified, not assumed

The rebuild ships with its own test rigs (not required to run the app):
a simulator of PSX's exact 404/403/200 token behaviour including rotation, and
a headless-browser suite that drives the real UI. Current status: **21/21**
token-layer checks, **32/32** interface checks, **0** uncaught errors, no
horizontal overflow at 390 px, and a full-stack run of `analyzer.py` against
the simulator in LIVE mode.

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
- **NEW — Prediction tab.** A second **Prediction** button next to Analyze runs a
  rules-based outlook engine modelled on how seasoned PSX analysts read a chart:
  trend structure (higher-highs / higher-lows), **EMA-21 / EMA-89 / 200-MA** as dynamic
  supports, **support & resistance clusters**, **Fibonacci retracement** of the last
  rally, **RSI(14) divergence**, **volumes**, and a defined **Buy-1 / Buy-2 /
  Stop-loss / Target** trade plan with a reward-vs-risk ratio and a 2–3% portfolio-risk
  sizing reminder. Results are shown with a **candlestick chart** (EMA overlays,
  floors/ceilings, Fib levels, plan markers, RSI panel with divergence lines) and a set
  of plain-English "explain it like I'm 5" story cards, with the **final verdict — and
  its disclaimer — pinned at the top of the page**.
  ⚠️ *The prediction is educational guidance only. It is **not** a purchase or sell
  call, and never financial advice.*

> **DEMO vs LIVE** — open `dashboard.html` on its own and it runs in **DEMO** mode with a
> few bundled sample companies so you can see exactly how it looks and behaves. Launch it
> through `PSX 5.0.bat` (which starts the Python engine) and it flips to **LIVE** mode and
> scrapes real PSX data on every Analyze.

---

## How it works (architecture)

```
   PSX 5.0.bat ──pulls latest code──►  GitHub repo (your public repo)
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
itself lives on **GitHub**, and `PSX 5.0.bat` re-downloads it on every launch — so when you
push an update, everyone who runs the `.bat` gets it automatically.

### Files

## v4.1 — About the company, and exports that match the screen

- **“About” in the Company Profile card.** Every Fundamentals view now opens with the
  company's **name in full**, its **official PSX category** (Insurance, Cement, …), a short
  plain-language description of **what the business actually does**, and **3–5 of its top
  products** where a source names them.
  Nothing here is written by the tool. The description and product list are lifted from
  real, citable pages — the **PSX company page**, the **StockAnalysis (S&P Global) profile**,
  or the **issuer's own website** — and each line carries a clickable link so you can check
  it yourself. When none of those publish a description, the card says so plainly instead of
  inventing a sentence, and a sector name is never turned into a guessed product list.

- **Export, on both tabs.** An **Export** button sits on the Fundamental and the Technical
  tab. Click it and pick:
  - **PDF** — a comprehensive report: a landscape *At a glance* summary, then a full page
    per company covering About, the key snapshot, the fundamental score with its complete
    metric table (value, reading, how it was worked out, source), and the technical read
    with the trade plan, RSI, volume, Wyckoff season and the pros/cons.
  - **Excel** — one workbook, **two sheets**: `Fundamental` and `Technical`, with frozen
    headers, auto-filters and colour-coded buy / stop / target columns.

- **Bulk export from the landing page.** A **“Export a list of stocks in one go”** panel
  lets you build a list without opening each company:
  - type a **symbol *or* a company name**, pick it, type the next one, and so on; or
  - import a **CSV / Excel** file that is simply a column of stock names — **no header
    needed**.
  **Symbols and names work identically.** `LUCK`, `Lucky Cement`, `Lucky Cement Limited`
  and `lucky cement ltd.` all resolve to the same company: case, punctuation, `&` vs `and`
  and legal suffixes (Limited / Ltd / Corporation / Pvt …) are all ignored. Anything
  genuinely ambiguous (typing just `Bank`) or unrecognised is reported back to you rather
  than silently guessed. Then export the whole list as PDF or Excel, same as a single stock.

- **Every export carries the must-have columns first**, in this order:
  Stock Symbol · Company Name · Stock Category (PSX Sector) · The energy meter (RSI) ·
  How loud is the crowd? (volume) · Chart Health · Business Health · OverAll Outlook ·
  The market's season (Wyckoff) · 🟢 Buy zone 1 (first nibble) · 🟢 Buy zone 2 (deeper dip) ·
  🛑 Stop-loss (the safety rope) · 🎯 Target 2 (if Target 1 breaks) — on **both** sheets, so
  each is independently useful. Everything else follows behind them.

- **Exports can never disagree with the screen.** The browser sends the exact figures it
  rendered; `exporter.py` only lays them out — it recomputes nothing. Anything a source did
  not publish stays blank (a dash), never filled in.

- **A note on the PDF.** The standard PDF fonts carry no emoji and no `₨` glyph, so the PDF
  writes `Rs` and conveys the same meaning with colour (green buy zones, red stop-loss, blue
  targets). The Excel file keeps the emoji headings exactly as specified.

## v4.0 — Range-driven rankings, Blue-Chip list & the Material Information engine

- **Landing page, rebuilt around YOU.** A short hero, the **In Depth Analysis —
  Individual Stock** button right on top, and then the star of the page: a golden
  **“What's your investment range ?” slider** spanning the actual KSE-100 share-price
  span, with crisp price bubbles that follow your thumbs. Hit **Generate Lists** and
  three ranked columns appear for exactly that price range:
  **Top Blue-Chip Stocks · Top 5 Recommended · 5 to Stay Away From.**
- **Honest, thresholded lists — strictly KSE-100.** Every list is capped at 5 but
  NEVER padded: a Top pick must score ≥ 60/100 blended, a stay-away must score ≤ 48,
  and a blue chip must earn a ≥ 60 blue-chip grade. If only 2 stocks qualify in your
  range, you see 2; if none, the empty state says so plainly.
- **Blue-chip model, grounded in PSX reality.** Researched from Pakistani financial
  press & broker usage: blue chips are large, seasoned, dividend-paying KSE-100
  heavyweights (think OGDC, HBL, MCB, UBL, ENGRO, FFC, LUCK, HUBC, PPL). The engine
  scores those exact observable traits — fundamental strength, real cash dividends,
  KSE-30 grade liquidity, a conservative balance sheet, a long trading history — from
  the SAME scraped analysis, never from a hard-coded list.
- **Stay-away cards now argue like they mean it.** Weakness-only bullets
  (`fundamental_flaws` + `technical_warnings`) replaced the old contradictory
  positives; stay-away names get no growth section and no trade plan — “the plan IS
  to stay away.”
- **NEW: Material Information section (replaces Announcements).** Built from scratch:
  only filings literally titled *“Material Information”* are taken, each PDF is
  downloaded and read **end-to-end**, and the 1–2 sentence verdict is **EXTRACTED
  from the document's own words** — never assumed. Scanned letters (most PSX MI
  filings have no text layer) are read via an **OCR fallback** (`pypdfium2` +
  `pytesseract`); if a document truly cannot be read, the card honestly says
  *UNREAD — no verdict given (we refuse to guess)* instead of inventing one.
  > Optional, for scanned-letter OCR on Windows: install the free Tesseract engine
  > (`winget install UB-Mannheim.TesseractOCR` or from
  > github.com/UB-Mannheim/tesseract) — everything else works without it.
- **Speed.** The KSE-100 ranking scan runs on **12 parallel workers** with zero
  artificial delays, starts **before** anything else at launch, and any previous
  scan on disk is served **instantly** while the fresh one builds — so the landing
  page is interactive within seconds; the slider unlocks the moment data lands.
- **Search workbench polish.** One professional row (search field + Fundamental +
  Technical), dropdown scrollbar bug fixed (the console panel was clipping the
  list), and search always covers **every listed company** — index tabs removed.
- **Alive to the touch.** Money-themed micro-interactions everywhere: ripples on
  every primary control, a coin-burst on Generate, medal coin-flips on hover,
  smooth view transitions, pulsing price bubbles — all disabled automatically for
  reduced-motion users.

## v3.7 — Insider Transactions, cleaner banking model & Wyckoff story card

- **Banks now score on 7 fundamentals, equal weight.** The Capital Adequacy
  metric was removed from the banking model — PSX and StockAnalysis do not
  reliably publish it, and this tool never shows estimated values, so the
  health score is now built purely from the seven fundamentals that are
  always readable from real filings.
- **NEW: Insider Transactions slab in the Fundamentals view.** The tool
  scans the company's actual PSX filings and pulls out every insider share
  transaction — directors, sponsors, CEO/CFO/CXOs, company secretary,
  substantial shareholders, buy-backs. A show/hide list presents each filing
  with its date, a BOUGHT / SOLD / SEE-FILING tag, and a **clickable link to
  the real PSX document**. A verdict sits on top with honest logic: insiders
  buying = quietly bullish (people with the best information putting their
  own money in); insiders selling = caution, especially near highs; both =
  no clear signal; direction not stated in the notice titles = nothing is
  assumed, open the filings; none found = "Scanned PSX — no insider
  transactions in the last 12 months" with a link so you can verify.
- **The Catalyst Check section was removed** from the Technical Analysis
  view, and the verdict returns to two factors: **Chart Health 55% +
  Business Health 45%** (the Wyckoff cycle still feeds the chart evidence).
- **"The Story, Told Simply" gained a Wyckoff card** — the market's season
  (quiet collecting → the climb → quiet selling → the fall) explained in the
  same friendly card format, computed live from the stock's own prices.

## v3.6 — Wyckoff market cycle & the source-linked data engine

- **Wyckoff Market Cycle analysis** in the Technical Analysis (Prediction) view.
  A new engine detects the stock's most recent **consolidation box**, reads the
  trend that led into it, where price sits in the 52-week range, and whether
  volumes **dried up** inside the box — then places the stock on the classic
  cycle: **Accumulation → Advancing → Distribution → Decline** (plus the
  golden **breakout-from-the-base** signal). A dedicated chart below the
  candle chart draws the price line, volume bars, the detected box, the
  breakout line, and a breakout/breakdown arrow, with a 4-season strip
  showing "NOW", plain-English story cards, and honest notes ("the bigger
  the base, the higher in the space").
  The identical algorithm runs in Python (`predictor.wyckoff`) and in the
  dashboard's JavaScript, verified to produce byte-identical results.
- **NEW `catalyst.py`** — scans the company's **actual announcements from
  the PSX Data Portal** (company page first, portal-wide announcements page
  as fallback) and classifies each one. Nothing is assumed or invented, and
  every item carries a clickable link to the real filing. In v3.7 this
  engine powers the Insider Transactions slab.

## v3.5 — Real progress bar

- Both analysis buttons now show a **progress bar with a live percentage** under
  the loading animation. It reports **genuine pipeline progress**, not a fake
  animation: the scraper and scorer publish their actual stages (contacting
  PSX → parsing tables → price history / statistics / statements received →
  merging → scoring) through the new `GET /api/progress?symbol=X` endpoint,
  and the dashboard polls it every ~0.4 s, tweening the bar smoothly toward the
  latest real value with a bounded creep between stages so it is always visibly
  alive. Stage text replaces the rotating messages the moment real stages
  arrive; cache hits complete instantly at 100 %.

## v3.4 — ROIC tier-0, version stamping & prewarm hygiene

- **ROIC now has a tier-0:** S&P Global publishes "Return on Capital (ROIC)" on
  the StockAnalysis statistics page, computed from the company's own filed
  statements. It is captured in the same single fetch that already succeeds for
  virtually every PSX stock, and used directly (with the exact-identity ladder
  from v3.3 as fallback). ROIC is now effectively never N/A for a real company.
- **Version stamping:** the console banner, `/api/health` and the dashboard
  footer all show `v3.4.0`. PSX.bat re-downloads the code from GitHub at every
  launch — **if the banner doesn't say v3.4.0, GitHub is still serving old
  files** (this is exactly what happened with the "ROIC still N/A" report: the
  log showed v3.2 fingerprints — dead price endpoints, old label counts).
- **Prewarm hygiene:** sustainability reports, FAQs, rating press releases,
  AGM notices, presentations etc. are blacklisted — never downloaded, never
  parsed (the log showed a mobile-account FAQ PDF being fetched for figures).
- **KSE-50 404 eliminated:** PSX publishes no KSE-50 index page; it is now
  derived live as the top 50 of KSE-100 with zero network calls.

## v3.3 — Performance overhaul, ROIC guarantee & real-world PDF fixes

- **Analysis is now seconds, not minutes.** The PSX company page is fetched once
  and re-used by every parser (it was being downloaded 4-5×); all independent
  sources (price history, StockAnalysis statistics, annual statements, dividends)
  are fetched **in parallel**; the three dead PSX price endpoints that were
  burning retries on every search are removed; and 4xx responses now **fail
  fast** (retries are reserved for transient 5xx/network errors).
- **Annual-report PDFs never block a request anymore.** Deep fetching moved to a
  **background queue** — the persistent store is merged instantly, missing
  symbols are queued, and an incomplete symbol enters a 24-hour cooldown instead
  of re-downloading the same 30 MB report on every click. Per-symbol locks stop
  the prewarm and user requests from fetching the same document concurrently,
  and the prewarm pauses while a user is actively analyzing.
- **ROIC is now effectively guaranteed** for every real operating company, via a
  ladder of EXACT accounting identities (never estimates): EBIT = reported
  operating profit, or PBT + interest expense − interest income; tax = reported,
  or PBT − net profit; invested capital = avg(debt + equity), year-end, or the
  textbook total assets − current liabilities. The metric note states exactly
  which construction was used.
- **Bug fixed:** StockAnalysis labels rows "Income Tax Expense" / "Total Current
  Liabilities" etc. — the old exact-match map silently missed them and starved
  ROIC. Label matching is now variant-aware, with growth/margin/ratio rows
  excluded.
- **Bug fixed:** image-only (scanned/graphic) annual reports — like Lucky
  Cement's — are detected up-front, recorded, and never re-downloaded. Optional
  OCR can be enabled via `config.DEEPDATA["ocr"]` + `pip install
  rapidocr-onnxruntime`. PDF parsing also early-exits once the required field
  set is complete.

## v3.2 — Deep official-filings engine, ETF gating & UI polish

- **`deepdata.py` (new):** when PSX tables and StockAnalysis still leave gaps, the
  tool now goes to the primary sources themselves — the company's exchange-hosted
  filed reports **and its own official website** (discovered from its PSX page and
  politely crawled for annual-report PDFs). A multi-year PDF parser reads the
  "Six Years at a Glance" financial tables, and every figure is stored in a
  **persistent per-symbol store** (`psx_cache/deepdata/`) with exact provenance
  (document, URL, page). KPI info tabs link straight to the source PDF.
- **Background pre-warm:** every run, a rate-limited background worker keeps
  walking the whole PSX universe (companies only), persisting progress, so
  coverage of all listed companies grows across runs without blocking anyone.
  Track it at `GET /api/deep-status`. Tune or disable in `config.DEEPDATA`.
- **Still zero estimation:** only numbers physically printed in official
  documents are stored or merged, and only into fields that are missing.
- **ETF gating:** ETFs are baskets, not operating companies — they file no
  financial statements, so both analysis buttons now show a clear notice
  instead of a meaningless 0-score.
- Header renamed to **Stock Health Analyzer**; source links tidied.


## v3.1 — Strict original-data scoring & Technical Analysis chart controls

- **Buttons renamed:** *Analyze* → **Fundamental Analysis**, *Prediction* → **Technical Analysis**.
- **New 11-metric financial story** (all equally weighted, top-to-bottom):
  Revenue Growth → Profit Margin → EPS Growth → **ROIC** → ROE → Debt/Equity →
  Current Ratio → **Cash & Equivalents** → Cash Flow Quality → **Dividend Yield** → **P/E Ratio**.
  Banks use an adapted 8-metric model with Capital Adequacy.
- **ZERO-ESTIMATION POLICY:** every figure is scraped from an original source
  (PSX company page, the company's filed statements via StockAnalysis/S&P Global,
  or PSX end-of-day prices). ROIC uses the real reported operating profit and the
  company's actual tax rate — `NOPAT ÷ average invested capital` — never a proxy.
  If any input is missing, the metric shows **N/A** and the remaining metrics are
  **re-weighted**; nothing is ever guessed.
- **Every metric's ⓘ info tab** now shows the exact **formula**, a plain-English
  explanation of the calculation, and a **clickable link to the source page** so
  you can verify the numbers yourself.
- **Technical Analysis candle chart:** mouse-wheel / trackpad-pinch **zoom**
  (anchored at the cursor), **drag to pan**, **double-click to reset**, and an
  **⛶ Expand** toggle for a full-height chart.



| File | Role |
|------|------|
| `dashboard.html` | The whole front-end — rebuilt from scratch. Reads only the engine's JSON; no client-side modelling, no demo data. |
| `analyzer.py`    | Flask server: serves the dashboard + `/api/analyze`, `/api/stocks`. |
| `psx_data.py`    | Builds & caches the live PSX stock universe (the auto-updating list). |
| `scraper.py`     | Scrapes a company's profile, financial tables, price history, and report PDFs. |
| `scorer.py`      | Pure scoring logic (no network): 11-metric financial story, strict original-data policy (N/A + re-weighting, never estimation), per-metric source links. |
| `deepdata.py`    | Deep official-filings fetcher: website discovery, IR crawl, multi-year PDF parser, persistent provenance store, background pre-warm. |
| `predictor.py`   | Prediction engine (no network): trend structure, EMAs, S/R, Fibonacci, RSI divergence, trade plan. Mirrored in JS inside the dashboard so DEMO mode behaves identically. |
| `utils.py`       | Shared helpers: the **PSX request-token layer** (the v5.0 fix), HTTP session with retry/backoff, robust number parsing, caching. |
| `about.py`       | v4.1: builds the sourced **About** block — full name, PSX category, business description and top products. Never invents; cites every field. |
| `exporter.py`    | v4.1: builds the **PDF report** and the two-sheet **Excel workbook** from the figures the dashboard sends. Recomputes nothing. |
| `config.py`      | All settings: ports, PSX endpoints, scoring weights, your GitHub details. |
| `requirements.txt` | Python dependencies. |
| `PSX 5.0.bat`    | Windows one-click launcher. `PSX 5.0.bat doctor` runs the self-diagnostic. |
| `doctor.py`      | v5.0: the self-diagnostic — tests every data source and says which one is failing. |

---

## Setup (one time, ~5 minutes)

You need a free **GitHub** account.

### 1. Create a public repo and upload the files
1. On GitHub: **New repository** → name it e.g. `psx-score` → **Public** → Create.
2. **Add file → Upload files** → drag in **all** the files listed above (every `.py`,
   `dashboard.html`, `requirements.txt`, and `PSX 5.0.bat`) → **Commit**.

### 2. Point the launcher at your repo
Open **`PSX 5.0.bat`** in Notepad and edit the three lines near the top:
```bat
set "GITHUB_USER=your-github-username"
set "GITHUB_REPO=psx-score"
set "GITHUB_BRANCH=main"
```
Save. (If your default branch is `master`, put that instead of `main`.)

### 3. Run it
Double-click **`PSX 5.0.bat`**. On first launch it will:
- check for Python (and offer to install it via `winget` if it's missing),
- download the latest code from your repo,
- install the Python packages,
- start the local engine and **open your browser** at `http://127.0.0.1:5000`.

Keep the black launcher window open while you use the app. Close it (or `Ctrl+C`) when done.

### 4. Share it
Send friends **just the `PSX 5.0.bat` file**. As long as your repo is public, their copy
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
- **Windows-first.** `PSX 5.0.bat` targets Windows. On macOS/Linux you can run it manually:
  `pip install -r requirements.txt` then `python analyzer.py`.
- **Not advice.** This scores *current fundamentals only*. It does not predict prices and
  is not a recommendation to buy or sell anything.

---

*Built for learning and exploring PSX fundamentals. Data belongs to its respective
sources; this tool only reads public information.*
