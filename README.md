# Daily 10

A small dashboard for working through the NIFTY 500, ten stocks a day.

- The list is the NIFTY 500 **ordered by market cap, largest first**, with relative volume, turnover and delivery % alongside. You can re-sort by any of them.
- Tap **Get today's 10** for the next ten on the list. For each you choose **Invest** (just type the number of shares; the price is pre-filled with the current rate), **Ignore**, or, if you already hold it, **Sell**.
- A stock you've added is "reviewed" and drops off the list for the rest of the **cycle**. At ten a day all 500 take about 50 days, then you start a new cycle. When a stock you still hold comes round again, its row shows "You hold 100 · avg …" with **Sell** next to Invest and Ignore.
- **Sell** all or part. From Today it sells from the whole holding, oldest purchase first; from Portfolio you can sell from one specific purchase. The portfolio shows open positions with unrealised P&L, a list of sales with realised P&L, and totals. P&L is before brokerage and taxes.
- Counters: today's batch (invested / ignored / to decide), cycle progress (x/500, day n of 50), invested this cycle, open positions with money deployed.
- "Current rate" is the latest end-of-day close from NSE. There are no live prices.

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

(The market cap file, `kind=mcap`, is covered in the next section.)

Upload the last ~20 trading days (oldest first) so relative volume has history; with fewer than 5 earlier sessions the list ranks by turnover instead. After that, one upload a day keeps it current if the automatic download stays blocked.

## Market cap

NSE's daily bhavcopy has prices but no share counts, so market cap needs one more NSE file: the **MCAP** report (NSE circular 16/2024), which lists each security's issue size (shares outstanding). Share counts change rarely, so the server stores them and computes `market cap = close × shares` from each day's close.

Until it is loaded the page says so and orders by turnover (large caps still rise to the top). To load it, download the MCAP file from NSE's All Reports page (nseindia.com → Resources → All Reports, Capital Market daily section) and upload it once; repeat every few months, or after big corporate actions:

```bash
curl -X POST "$URL/api/admin/upload?kind=mcap" -H "Authorization: Bearer $PASSCODE" \
     --data-binary @MCAP24102026.csv
```

The file needs a `Symbol` column and an `Issue Size` column (or `Market Cap` with `Close Price`, from which shares are derived). Check the result against a number you know: Reliance should be around ₹18–20 lakh crore.

To make it refresh by itself, copy the file's download link from NSE and set the Railway variable `MCAP_URL` to that link with the date replaced by `{ddmmyyyy}`, for example `https://…/MCAP{ddmmyyyy}.csv`. The server then re-reads it at most weekly. I could not confirm NSE's exact file location from the build environment, so this one is for you to set.

## Updating an existing deployment

Merging to `main` redeploys on Railway. Your database on the volume upgrades itself in place (it gains an "ignored" flag), and nothing is deleted.

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
