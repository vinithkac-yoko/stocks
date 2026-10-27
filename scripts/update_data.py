#!/usr/bin/env python3
"""Build docs/data/latest.json: the NIFTY 500 universe ranked-ready with relative volume.

Sources (both free, published by NSE after market close):
  * NIFTY 500 constituents  - ind_nifty500list.csv
  * Daily "full bhavcopy"   - sec_bhavdata_full_DDMMYYYY.csv (price, volume, delivery %)

Relative volume = today's traded quantity / mean traded quantity of the previous
(up to) 20 sessions. Daily snapshots are kept in data/history.json so each run only
downloads sessions it has not seen before.

Standard library only. If NSE blocks the download, pass the files yourself:
  python scripts/update_data.py --bhav-file sec_bhavdata_full_24102026.csv --bhav-date 2026-10-24
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Where state lives. On a server, DATA_DIR (e.g. a mounted volume) holds everything; in a
# checkout the files sit under data/ and docs/data/ as before. Resolved per call so tests can
# point it elsewhere.
def _data_dir() -> Path | None:
    value = os.environ.get("DATA_DIR")
    return Path(value) if value else None


def universe_cache() -> Path:
    return (_data_dir() or ROOT / "data") / "nifty500.csv"


def history_path() -> Path:
    return (_data_dir() or ROOT / "data") / "history.json"


def output_path() -> Path:
    d = _data_dir()
    return d / "latest.json" if d else ROOT / "docs" / "data" / "latest.json"


def write_atomic(path: Path, text: str) -> None:
    """Readers (the web server) never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


UNIVERSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"

IST = timezone(timedelta(hours=5, minutes=30))
AVG_WINDOW = 20      # sessions averaged for relative volume
MIN_WINDOW = 5       # fewer prior sessions than this -> relative volume is unknown
KEEP_SESSIONS = 40   # sessions retained in history.json
LOOKBACK_DAYS = 60   # how far back to walk looking for sessions
RECENT_DAYS = 2      # a missing file this recent may just be unpublished, not a holiday

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def fetch(url: str, retries: int = 3) -> bytes | None:
    """Return the body, or None on 404. Raises after repeated network failures."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"could not fetch {url}: {last}")


def read_csv(text: str) -> list[dict[str, str]]:
    """NSE pads headers and values with spaces; normalise both."""
    rows = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = [h.strip().upper() for h in next(rows)]
    except StopIteration:
        return []
    return [{h: v.strip() for h, v in zip(header, row)} for row in rows if row]


def num(value: str | None) -> float | None:
    try:
        return float(value.replace(",", "")) if value not in (None, "", "-") else None
    except ValueError:
        return None


def load_universe(path: Path | None, prefer_cache: bool = False) -> dict[str, dict[str, str]]:
    """symbol -> {name, industry}. Order of preference: given file, fresh download, cache.

    prefer_cache puts the cache first, so ingesting a hand-supplied file makes no network calls.
    """
    text: str | None = None
    if path:
        text = path.read_text(encoding="utf-8")
    elif prefer_cache and universe_cache().exists():
        text = universe_cache().read_text(encoding="utf-8")
    else:
        try:
            body = fetch(UNIVERSE_URL)
            if body:
                text = body.decode("utf-8", "replace")
                write_atomic(universe_cache(), text)
        except RuntimeError as e:
            log(f"universe download failed ({e}); trying cache")
    if text is None and universe_cache().exists():
        text = universe_cache().read_text(encoding="utf-8")
    if text is None:
        raise SystemExit("No NIFTY 500 list available. Download ind_nifty500list.csv and pass --universe-file.")
    universe = {}
    for row in read_csv(text):
        sym = row.get("SYMBOL")
        if sym:
            universe[sym] = {"name": row.get("COMPANY NAME", sym), "industry": row.get("INDUSTRY", "")}
    if not universe:
        raise SystemExit("NIFTY 500 list parsed to zero symbols; check the file format.")
    return universe


def parse_bhav(text: str, universe: dict) -> dict[str, list]:
    """symbol -> [close, prev_close, volume, delivery_pct, turnover_lacs] for universe symbols."""
    out: dict[str, list] = {}
    for row in read_csv(text):
        sym = row.get("SYMBOL")
        if sym not in universe:
            continue
        # one row per series; prefer the plain equity series
        if sym in out and row.get("SERIES") != "EQ":
            continue
        close, prev, vol = num(row.get("CLOSE_PRICE")), num(row.get("PREV_CLOSE")), num(row.get("TTL_TRD_QNTY"))
        if close is None or vol is None:
            continue
        out[sym] = [close, prev, vol, num(row.get("DELIV_PER")), num(row.get("TURNOVER_LACS"))]
    return out


def load_history() -> dict:
    if history_path().exists():
        return json.loads(history_path().read_text(encoding="utf-8"))
    return {"days": {}, "holidays": []}


def collect_sessions(history: dict, universe: dict, today: date, want: int) -> None:
    days, holidays = history["days"], set(history["holidays"])
    d = today
    for _ in range(LOOKBACK_DAYS):
        iso = d.isoformat()
        # enough sessions AND we have walked back to what we already hold -> nothing newer to fetch
        if len(days) >= want and iso <= max(days):
            break
        if d.weekday() < 5 and iso not in days and iso not in holidays:
            body = fetch(BHAV_URL.format(ddmmyyyy=d.strftime("%d%m%Y")))
            if body is None:
                log(f"{iso}: no file")
                if (today - d).days > RECENT_DAYS:
                    holidays.add(iso)
            else:
                parsed = parse_bhav(body.decode("utf-8", "replace"), universe)
                if parsed:
                    days[iso] = parsed
                    log(f"{iso}: {len(parsed)} symbols")
        d -= timedelta(days=1)
    history["holidays"] = sorted(holidays)


def build_output(history: dict, universe: dict) -> dict:
    dates = sorted(history["days"])
    latest = dates[-1]
    prior = dates[:-1][-AVG_WINDOW:]
    stocks = []
    for sym, (close, prev, vol, deliv, turnover_lacs) in history["days"][latest].items():
        vols = [history["days"][d][sym][2] for d in prior if sym in history["days"][d]]
        avg = sum(vols) / len(vols) if len(vols) >= MIN_WINDOW else None
        stocks.append({
            "symbol": sym,
            "name": universe[sym]["name"],
            "industry": universe[sym]["industry"],
            "close": close,
            "prevClose": prev,
            "volume": vol,
            "turnoverCr": round(turnover_lacs / 100, 2) if turnover_lacs is not None else None,
            "avgVol20": round(avg) if avg else None,
            "relVol": round(vol / avg, 2) if avg else None,
            "delivPct": deliv,
        })
    return {
        "asOf": latest,
        "generatedAt": datetime.now(IST).isoformat(timespec="seconds"),
        "demo": False,
        "sessionsUsed": len(prior),
        "universeSize": len(universe),
        "stocks": stocks,
    }


def run(universe_file: Path | None = None, bhav_text: str | None = None,
        bhav_date: str | None = None, today: date | None = None) -> int:
    """Refresh history and latest.json. Returns 0 on success, 1 if nothing could be produced."""
    universe = load_universe(universe_file, prefer_cache=bhav_text is not None)
    history = load_history()
    today = today or datetime.now(IST).date()

    if bhav_text is not None:
        if not bhav_date:
            raise ValueError("bhav_text needs bhav_date")
        parsed = parse_bhav(bhav_text, universe)
        if not parsed:
            log("Uploaded file matched no NIFTY 500 symbols; leaving output untouched.")
            return 1
        history["days"][bhav_date] = parsed
    else:
        # one more than the window so the newest session still has a full 20 to compare against
        collect_sessions(history, universe, today, want=AVG_WINDOW + 1)

    if not history["days"]:
        log("No sessions available; leaving output untouched.")
        return 1
    for old in sorted(history["days"])[:-KEEP_SESSIONS]:
        del history["days"][old]

    out = build_output(history, universe)
    if not out["stocks"]:
        log("Latest session matched zero universe symbols; leaving output untouched.")
        return 1

    write_atomic(history_path(), json.dumps(history, separators=(",", ":")))
    write_atomic(output_path(), json.dumps(out, separators=(",", ":")))
    log(f"asOf {out['asOf']}: {len(out['stocks'])}/{len(universe)} stocks, {out['sessionsUsed']} prior sessions")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe-file", type=Path, help="local ind_nifty500list.csv instead of downloading")
    ap.add_argument("--bhav-file", type=Path, help="local sec_bhavdata_full csv to ingest (needs --bhav-date)")
    ap.add_argument("--bhav-date", help="YYYY-MM-DD session date of --bhav-file")
    ap.add_argument("--today", help="YYYY-MM-DD, override 'today' (IST)")
    args = ap.parse_args()
    if args.bhav_file and not args.bhav_date:
        ap.error("--bhav-file needs --bhav-date")
    return run(
        universe_file=args.universe_file,
        bhav_text=args.bhav_file.read_text(encoding="utf-8") if args.bhav_file else None,
        bhav_date=args.bhav_date,
        today=date.fromisoformat(args.today) if args.today else None,
    )


if __name__ == "__main__":
    sys.exit(main())
