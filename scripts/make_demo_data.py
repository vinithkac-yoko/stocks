#!/usr/bin/env python3
"""Write docs/data/latest.json with 500 obviously fake stocks (DEMO001...), flagged demo=true.

Only there so the dashboard has something to show before the first real NSE update.
The real update (scripts/update_data.py) overwrites the file.
"""
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "data" / "latest.json"
INDUSTRIES = ["Financial Services", "Information Technology", "Healthcare", "Automobile", "Capital Goods",
              "Chemicals", "Consumer Goods", "Metals & Mining", "Power", "Realty"]

rng = random.Random(7)
stocks = []
for i in range(1, 501):
    close = round(rng.uniform(40, 4500), 2)
    prev = round(close * rng.uniform(0.95, 1.05), 2)
    avg = rng.randint(50_000, 8_000_000)
    rel = round(max(0.15, rng.lognormvariate(0, 0.55)), 2)
    vol = int(avg * rel)
    stocks.append({
        "symbol": f"DEMO{i:03d}",
        "name": f"Demo Company {i} Ltd",
        "industry": INDUSTRIES[i % len(INDUSTRIES)],
        "close": close,
        "prevClose": prev,
        "volume": vol,
        "turnoverCr": round(vol * close / 1e7, 2),
        "avgVol20": avg,
        "relVol": rel,
        "delivPct": round(rng.uniform(15, 80), 2),
    })

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps({
    "asOf": "2026-10-26", "generatedAt": "2026-10-27T00:00:00+05:30", "demo": True,
    "sessionsUsed": 20, "universeSize": 500, "stocks": stocks,
}, separators=(",", ":")), encoding="utf-8")
print(f"wrote {OUT} ({len(stocks)} fake stocks)")
