"""SQLite storage for cycles, picks and sells. Standard library only.

A pick is a stock you reviewed in a cycle. Once you invest it carries an entry price, quantity
and date. Sells are separate rows so a position can be closed in pieces:
    open quantity = bought quantity - sum(sells.qty)
"""
from __future__ import annotations

import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

SCHEMA = """
create table if not exists cycles (
  id integer primary key autoincrement,
  started_on text not null
);
create table if not exists picks (
  id integer primary key autoincrement,
  cycle_id integer not null references cycles (id) on delete cascade,
  symbol text not null,
  picked_on text not null,
  status text not null default 'picked' check (status in ('picked', 'invested')),
  entry_price real,
  qty integer,
  invested_on text,
  unique (cycle_id, symbol),
  check (status = 'picked' or (entry_price > 0 and qty > 0 and invested_on is not null))
);
create table if not exists sells (
  id integer primary key autoincrement,
  pick_id integer not null references picks (id) on delete cascade,
  qty integer not null check (qty > 0),
  price real not null check (price > 0),
  sold_on text not null
);
create index if not exists sells_pick on sells (pick_id);
"""

SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
MAX_PRICE, MAX_QTY = 10_000_000, 1_000_000_000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _date(value, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ApiError(400, f"{field} must be a date like 2026-10-27") from None


def _price(value, field: str = "price") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= MAX_PRICE:
        raise ApiError(400, f"{field} must be a positive number")
    return round(float(value), 2)


def _qty(value, field: str = "qty") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_QTY:
        raise ApiError(400, f"{field} must be a whole number above zero")
    return value


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("pragma journal_mode = wal")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("pragma foreign_keys = on")
        try:
            with c:  # commit or roll back
                yield c
        finally:
            c.close()

    # ---------------------------------------------------------------- read ----

    def state(self, today: str) -> dict:
        """Everything the page needs. The first cycle is created on first read, dated `today`."""
        with self._conn() as c:
            c.execute("begin immediate")  # two tabs loading at once must not both create the first cycle
            cycles = [{"id": r["id"], "startedOn": r["started_on"]} for r in c.execute("select * from cycles order by id")]
            if not cycles:
                c.execute("insert into cycles (started_on) values (?)", (_date(today, "today"),))
                cycles = [{"id": r["id"], "startedOn": r["started_on"]} for r in c.execute("select * from cycles order by id")]
            sells: dict[int, list] = {}
            for r in c.execute("select * from sells order by id"):
                sells.setdefault(r["pick_id"], []).append({"id": r["id"], "qty": r["qty"], "price": r["price"], "soldOn": r["sold_on"]})
            picks = [{
                "id": r["id"], "cycleId": r["cycle_id"], "symbol": r["symbol"], "pickedOn": r["picked_on"],
                "status": r["status"], "entryPrice": r["entry_price"], "qty": r["qty"], "investedOn": r["invested_on"],
                "sells": sells.get(r["id"], []),
            } for r in c.execute("select * from picks order by id")]
        return {"cycles": cycles, "picks": picks}

    # --------------------------------------------------------------- write ----

    def start_cycle(self, today: str) -> dict:
        today = _date(today, "date")
        with self._conn() as c:
            cur = c.execute("insert into cycles (started_on) values (?)", (today,))
            return {"id": cur.lastrowid, "startedOn": today}

    def add_pick(self, cycle_id, symbol, picked_on) -> int:
        symbol = str(symbol or "").upper()
        if not SYMBOL_RE.match(symbol):
            raise ApiError(400, "invalid symbol")
        picked_on = _date(picked_on, "pickedOn")
        with self._conn() as c:
            if not c.execute("select 1 from cycles where id = ?", (cycle_id,)).fetchone():
                raise ApiError(404, "cycle not found")
            try:
                return c.execute("insert into picks (cycle_id, symbol, picked_on) values (?, ?, ?)", (cycle_id, symbol, picked_on)).lastrowid
            except sqlite3.IntegrityError:
                raise ApiError(409, f"{symbol} is already picked in this cycle") from None

    def _pick(self, c, pick_id):
        row = c.execute("select * from picks where id = ?", (pick_id,)).fetchone()
        if not row:
            raise ApiError(404, "pick not found")
        sold = c.execute("select coalesce(sum(qty), 0) from sells where pick_id = ?", (pick_id,)).fetchone()[0]
        return row, sold

    def remove_pick(self, pick_id: int) -> None:
        with self._conn() as c:
            _, sold = self._pick(c, pick_id)
            if sold:
                raise ApiError(409, "This stock has sales recorded. Undo them first.")
            c.execute("delete from picks where id = ?", (pick_id,))

    def set_invested(self, pick_id: int, entry_price, qty, invested_on) -> None:
        entry_price, qty, invested_on = _price(entry_price, "entryPrice"), _qty(qty), _date(invested_on, "investedOn")
        with self._conn() as c:
            _, sold = self._pick(c, pick_id)
            if qty < sold:
                raise ApiError(400, f"Quantity can't be below the {sold} already sold.")
            c.execute("update picks set status = 'invested', entry_price = ?, qty = ?, invested_on = ? where id = ?",
                      (entry_price, qty, invested_on, pick_id))

    def clear_invested(self, pick_id: int) -> None:
        with self._conn() as c:
            _, sold = self._pick(c, pick_id)
            if sold:
                raise ApiError(409, "This stock has sales recorded. Undo them first.")
            c.execute("update picks set status = 'picked', entry_price = null, qty = null, invested_on = null where id = ?", (pick_id,))

    def add_sell(self, pick_id: int, qty, price, sold_on) -> int:
        qty, price, sold_on = _qty(qty), _price(price), _date(sold_on, "soldOn")
        with self._conn() as c:
            row, sold = self._pick(c, pick_id)
            if row["status"] != "invested":
                raise ApiError(409, "Mark the stock as invested before selling it.")
            open_qty = row["qty"] - sold
            if qty > open_qty:
                raise ApiError(400, f"Only {open_qty} left to sell.")
            return c.execute("insert into sells (pick_id, qty, price, sold_on) values (?, ?, ?, ?)", (pick_id, qty, price, sold_on)).lastrowid

    def remove_sell(self, sell_id: int) -> None:
        with self._conn() as c:
            if not c.execute("delete from sells where id = ?", (sell_id,)).rowcount:
                raise ApiError(404, "sale not found")
