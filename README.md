# PSX·SCORE Mobile — Progressive Web App

A mobile-optimized version of the PSX·SCORE fundamental analyzer that runs on **any phone** with **zero installation hassle**.

Friends open a URL → tap "Add to Home Screen" → an app icon appears like a native app. No App Store, no APK, no permissions.

---

## What's in this folder

| File | Purpose |
|---|---|
| `index.html` | The mobile PWA dashboard (single file, embeds demo data) |
| `manifest.json` | PWA manifest — makes it installable |
| `sw.js` | Service worker — caches app shell for instant launch + offline |
| `icon-192.png`, `icon-512.png` | App icons used on the home screen |
| `apple-touch-icon.png`, `favicon-32.png` | iOS / browser tab icons |
| `api.py` | Flask backend — same `/api/*` endpoints as the desktop version |
| `config.py`, `utils.py`, `psx_data.py`, `scraper.py`, `scorer.py` | Backend modules (unchanged from desktop) |
| `requirements.txt`, `runtime.txt`, `Procfile`, `render.yaml` | Cloud deployment config |
| `gen_icons.py`, `build.py` | Build scripts (only used when regenerating assets) |

---

## Deploy in 5 minutes (Render — recommended)

1. **Create a new GitHub repo** (or new branch in your existing one) and push every file in this folder.
2. Go to **https://render.com** → sign in with GitHub (free).
3. Click **New +** → **Web Service** → select your repo.
4. Render auto-detects `render.yaml` — just click **Apply**.
5. Wait ~3 min for first build. You'll get a URL like `https://psx-score-api.onrender.com`.
6. Send that URL to your friends.

That's it. The same URL serves the PWA dashboard **and** the live scraping backend.

> **Note on Render free tier:** the server sleeps after 15 min of inactivity. First request after a sleep takes ~30 sec to wake — subsequent requests are instant. For zero-cold-start, upgrade to the $7/mo plan or use Railway.

---

## Alternative: Deploy on Railway

1. Go to **https://railway.app** → sign in with GitHub.
2. **New Project** → **Deploy from GitHub repo** → pick your repo.
3. Railway reads `Procfile` automatically. Deploy.
4. Under **Settings** → **Networking**, click **Generate Domain**.

Railway has $5/month free credit, no sleep — better than Render for shared use.

---

## What the friend's experience looks like

### On Android (Chrome / Edge / Samsung Internet)
1. Friend opens the URL.
2. After a few seconds of browsing, Chrome shows an **"Install app"** banner at the bottom — or the built-in install prompt I wired into the header appears.
3. Tap **Install** → app icon appears on home screen → tap → opens fullscreen, no browser UI.

### On iPhone (Safari)
1. Friend opens the URL in **Safari** (Chrome on iOS doesn't support PWA install).
2. Tap the **Share** button (square with up arrow) → scroll → **Add to Home Screen**.
3. App icon appears on home screen → tap → opens fullscreen.

Both feel and look identical to a real installed app — splash screen, app icon, no browser bar, fullscreen layout.

---

## How it stays up-to-date

Same model as the desktop version:
- Edit `index.html` on GitHub → push → Render auto-rebuilds (~2 min).
- Service worker fetches the new shell on next visit; users get the update automatically.
- API logic (`scraper.py`, `scorer.py`) lives on the server — instant updates for everyone.

No re-installation needed, ever.

---

## Mobile-specific design changes vs. desktop

- **Layout:** single column, max 480px wide, all panels stack vertically.
- **Search dropdown:** inline below the input field instead of overlay.
- **Modals:** bottom sheet style (slide up from bottom) instead of centered overlay — natural on touch.
- **Gauge dial:** smaller and centered in the score card.
- **Trends chart:** 240px tall (vs 340 on desktop) to fit comfortably above the fold.
- **Tap targets:** all buttons ≥ 38px tall (Apple HIG / Material guidelines).
- **Sticky header** with the brand + theme toggle.
- **Safe area insets:** respects notches and home indicators on iOS.
- **No background canvas particles** — saves battery on mobile.
- **No ticker tape** — vertical space is at a premium.

---

## Local development / testing

```bash
pip install -r requirements.txt
python api.py
```

Open `http://localhost:5000` in your phone's browser (same WiFi as your laptop) — use your laptop's local IP instead of localhost.

To regenerate icons after editing `gen_icons.py`:
```bash
python gen_icons.py
```

To regenerate `index.html` after editing the template:
```bash
python build.py
```

---

## Sharing the install link

A clean URL like `https://psx-score-api.onrender.com` works on any phone. For an even simpler share, set up a custom domain on Render (free) or shorten via [is.gd](https://is.gd) / TinyURL.

WhatsApp message your friends:
> Hey — try this PSX stock analyzer I built: https://your-app.onrender.com
> Tap "Add to Home Screen" so it works like a real app 🚀

---

## Troubleshooting

**Install banner doesn't appear on Android**  
The PWA must be served over HTTPS — Render/Railway give you HTTPS automatically, so this is only an issue if you're testing locally. Use a tunnel like ngrok if needed.

**iOS users see no install prompt**  
That's normal — iOS only supports manual "Add to Home Screen" via Safari's Share menu. The dashboard works fine in Safari, just doesn't auto-prompt.

**Cold start takes 30s on first analyze (Render free)**  
Expected. Either upgrade Render's plan, switch to Railway, or set up an uptime pinger (e.g. [UptimeRobot](https://uptimerobot.com) free tier) hitting `/api/health` every 10 minutes.

**Friend wants offline access**  
The app shell is cached by the service worker, so it opens instantly even offline — but `/api/analyze` requires network. Demo mode kicks in automatically if the backend is unreachable, showing the 7 pre-cached stocks (OGDC, LUCK, SYS, HBL, ENGRO, KTML, SAZEW).
