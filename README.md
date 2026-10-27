# Daily 10

A small dashboard for working through the NIFTY 500, ten stocks a day.

- Every evening the server downloads NSE's free end-of-day file and ranks the NIFTY 500 by **relative volume** (today's volume ÷ 20-day average). Turnover and raw shares are one tap away.
- Each day you add 10 stocks from the ranked list to **Today's batch**, look at them however you like, and mark the ones you **invest** in (entry price, quantity, date).
- A stock you've added is "reviewed" and drops out of the list for the rest of the **cycle**. After about 50 days all 500 are done and you start a new cycle. Your portfolio carries over.
- **Sell** all or part of a position with an exit price. The portfolio shows open positions with unrealised P&L, a list of sales with realised P&L, and totals. P&L is before brokerage and taxes.
- Counters: today's batch (x/10), cycle progress (x/500, day n of 50), invested this cycle, open positions with money deployed.

There is deliberately no charting or analysis: that part is yours.

## Deploy on Railway

One Railway service does everything: it serves the page, keeps your marks in a SQLite file, and refreshes the NSE data itself. No other accounts are needed.

1. **Get the code on `main`.** Merge the pull request into `main`.
2. **Create the project.** On [railway.com](https://railway.com): *New Project → Deploy from GitHub repo*, authorise Railway's GitHub app if asked, and pick this repository. Railway finds the `Dockerfile` and builds it.
3. **Set your passcode.** Open the service → *Variables* → add `APP_PASSCODE` with a passcode of at least 8 characters. (Until it is set, the service refuses to start and the deploy shows as crashed. That is expected.)
4. **Add a volume. Don't skip this.** Without one, your picks and sales are erased on every deploy. On the project canvas, right-click (or press Ctrl/Cmd+K) and add a **Volume**, attach it to the service, and set the mount path to `/data`. The app finds it automatically. If you forget, the page shows a warning banner.
5. **Get a URL.** Service → *Settings → Networking → Generate Domain* (or the equivalent in Railway's current UI). Railway provides HTTPS.
6. **Redeploy** if the service had already crashed, then open the URL and enter your passcode.
7. **Check the data.** Until the first NSE download succeeds the page shows a yellow *Demo data* banner with made-up stocks. The download starts when the service boots and takes about a minute; it also runs every 30 minutes after 6:30 pm IST on trading days until the day's file appears. The service logs show a line like `asOf 2026-10-27: 497/500 stocks`.

Health check: `https://your-url/healthz` returns `ok`. The Railway config in `railway.json` already points at it.

### If the NSE download fails

NSE sometimes refuses requests from cloud servers. The page then shows *Last NSE update failed: …* and the logs say why. Fallback: download the files yourself and upload them with your passcode.

```bash
URL=https://your-url
PASSCODE='your passcode'

# 1. NIFTY 500 list: https://www.niftyindices.com (Indices → NIFTY 500 constituents CSV)
curl -X POST "$URL/api/admin/upload?kind=universe" -H "Authorization: Bearer $PASSCODE" \
     --data-binary @ind_nifty500list.csv

# 2. One bhavcopy per trading day: sec_bhavdata_full_DDMMYYYY.csv from NSE's "Reports → Daily" page
curl -X POST "$URL/api/admin/upload?kind=bhav&date=2026-10-24" -H "Authorization: Bearer $PASSCODE" \
     --data-binary @sec_bhavdata_full_24102026.csv
```

Upload the last ~20 trading days (oldest first) so relative volume has history; with fewer than 5 earlier sessions the list ranks by turnover instead. After that, one upload a day keeps it current if the automatic download stays blocked.

## Security

- Everything personal sits behind the passcode: the API returns 401 without a login cookie. The page, its scripts and the public NSE data do not.
- The passcode is compared in constant time, wrong guesses are throttled (5 per client, 30 overall per 15 minutes), and the login cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` over HTTPS.
- Changing `APP_PASSCODE` signs out every device.
- Keep the passcode long. Brute-force throttling slows guessing but is not a substitute for a strong passcode.

## Run locally

```bash
APP_PASSCODE=choose-something DISABLE_REFRESH=1 python server/app.py   # http://localhost:8000
python -m unittest discover -s tests
```

Local state goes in `./data` (git-ignored). Drop `DISABLE_REFRESH` to let it download from NSE, or use the upload commands above. `scripts/update_data.py` can also run on its own (it writes `docs/data/latest.json`), and `scripts/make_demo_data.py` regenerates the fake demo file.

## Data notes

- Source files: `ind_nifty500list.csv` (constituents) and `sec_bhavdata_full_DDMMYYYY.csv` (daily prices, volume, delivery %), both from `nsearchives.nseindia.com`. End-of-day only, no live prices.
- Portfolio P&L uses the latest close, not live prices.
- Back up your marks by copying `marks.db` off the volume if you ever need to; it is a plain SQLite file.
