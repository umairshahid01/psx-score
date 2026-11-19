# dev-tests — optional, not needed to run the app

These are the rigs used to verify v5.0. **Do not upload them to the repo the
launcher pulls from** — `PSX 5.0.bat` downloads a fixed file list and ignores
anything else, but keeping the repo to just the app files is tidier.

| File | What it does |
|---|---|
| `psx_sim.py` | A simulator of dps.psx.com.pk's post-2026-09-24 behaviour: 404 without `X-Requested-With`, 403 (empty body) without a valid `X-Req-Id`, 200 JSON with both — and a token that rotates, so refresh-and-retry is genuinely exercised. |
| `verify_fix.py` | Points the real `config`/`utils`/`psx_data`/`scraper` at the simulator and asserts the whole token path, including a stale token and 12 concurrent threads. `python verify_fix.py` |
| `verify_dashboard.py` | Static check of `dashboard.html`: every id the JS references exists, tag balance, CSS covers every class used, no leftovers from the old design. |
| `render_check.js` | Drives the real UI in headless Chrome (needs Playwright): boot sequence, both tabs, the command palette, modals, light theme and a 390 px mobile pass. |
| `dom_contract.py` | Extracts the ids/selectors/classes the JS depends on — used when reworking the markup. |

Run order that proves the engine end to end:

```
python dev-tests/verify_fix.py      # the token fix, against the simulator
python doctor.py                    # the real PSX, from your machine
```
