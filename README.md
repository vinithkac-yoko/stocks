# Daily 10

A small dashboard for working through the NIFTY 500, ten stocks a day.

- Every evening it downloads NSE's free end-of-day file and ranks the NIFTY 500 by **relative volume** (today's volume ÷ 20-day average). Turnover and raw shares are one tap away.
- Each day you add 10 stocks from the ranked list to **Today's batch**, look at them however you like, and mark the ones you **invest** in (entry price, quantity, date).
- A stock you've added is "reviewed" and drops out of the list for the rest of the **cycle**. After about 50 days all 500 are done and you start a new cycle. Your portfolio carries over.
- Counters: today's batch (x/10, invested), cycle progress (x/500, day n of 50), invested this cycle, open positions with capital deployed and unrealised P&L.

There is deliberately no charting or analysis: that part is yours.

## Set up (about 10 minutes)

1. **Merge this branch** into `main`.
2. **Turn on Pages:** repo *Settings → Pages → Build and deployment → Source: GitHub Actions*.
3. **Load real data:** *Actions → Update NSE data and deploy → Run workflow*. The first run downloads about 21 sessions of history, so relative volume works from day one. The site URL appears in the run summary. The page shows a yellow "Demo data" banner until this has succeeded once.
4. **Optional, to sync phone and laptop:** without this, marks are saved in the browser on each device separately.
   1. Create a free project at [supabase.com](https://supabase.com).
   2. *SQL Editor* → paste `supabase/schema.sql` → Run.
   3. *Authentication → Users → Add user*: any email you like, and your **passcode as the password**. Tick "Auto confirm".
   4. *Authentication → Sign In / Providers*: turn **off** "Allow new users to sign up". The page is public, so this is what stops others creating accounts.
   5. Fill in `docs/config.js` (`supabaseUrl`, `supabaseAnonKey`, `loginEmail`) and commit. The anon key is designed to be public; row level security keeps your rows private.

   The passcode screen is a real login: the passcode is that user's Supabase password. Pick something longer than 6 characters.

## Daily use

Open the site after about 7 pm IST (the workflow runs at 7:00 pm and again at 9:00 pm). Pick 10, decide, mark invested. That's it.

## If NSE blocks the download

NSE sometimes refuses requests from cloud servers. If the workflow fails at "Fetch NSE data", run it from your own computer instead:

```bash
python scripts/update_data.py            # needs only Python 3, no packages
git add data docs/data && git commit -m "Update NSE data" && git push
```

Or download the files by hand and feed them in:

```bash
python scripts/update_data.py --universe-file ind_nifty500list.csv \
    --bhav-file sec_bhavdata_full_24102026.csv --bhav-date 2026-10-24
```

## Run locally

```bash
python -m http.server -d docs 8000      # then open http://localhost:8000
python -m unittest discover -s tests    # data pipeline tests
```

## Data notes

- Source files: `ind_nifty500list.csv` (constituents) and `sec_bhavdata_full_DDMMYYYY.csv` (daily prices, volume, delivery %), both from `nsearchives.nseindia.com`. They are end-of-day only, no live prices.
- Relative volume needs at least 5 earlier sessions; until then the list falls back to turnover.
- Portfolio P&L uses the latest close, not live prices.
- `scripts/make_demo_data.py` regenerates the fake demo file; real runs overwrite it.
