import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from db import ApiError, Store  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Store(Path(self.tmp.name) / "marks.db")
        self.cycle = self.db.state("2026-10-27")["cycles"][0]["id"]

    def invested(self, symbol="AAA", qty=100, price=50.0):
        pid = self.db.add_pick(self.cycle, symbol, "2026-10-27")
        self.db.set_invested(pid, price, qty, "2026-10-27")
        return pid

    def pick(self, pid):
        return next(p for p in self.db.state("2026-10-27")["picks"] if p["id"] == pid)

    def fails(self, status, fn, *a):
        with self.assertRaises(ApiError) as cm:
            fn(*a)
        self.assertEqual(cm.exception.status, status)

    def test_first_read_creates_exactly_one_cycle(self):
        self.assertEqual(len(self.db.state("2026-10-27")["cycles"]), 1)
        self.assertEqual(len(self.db.state("2026-10-27")["cycles"]), 1)

    def test_duplicate_pick_in_cycle_is_rejected_but_allowed_in_next_cycle(self):
        self.db.add_pick(self.cycle, "AAA", "2026-10-27")
        self.fails(409, self.db.add_pick, self.cycle, "aaa", "2026-10-28")
        nxt = self.db.start_cycle("2026-12-20")["id"]
        self.db.add_pick(nxt, "AAA", "2026-12-20")

    def test_rejects_bad_input(self):
        self.fails(400, self.db.add_pick, self.cycle, "bad symbol!", "2026-10-27")
        self.fails(400, self.db.add_pick, self.cycle, "AAA", "27/10/2026")
        pid = self.db.add_pick(self.cycle, "BBB", "2026-10-27")
        self.fails(400, self.db.set_invested, pid, 0, 10, "2026-10-27")
        self.fails(400, self.db.set_invested, pid, 10, 1.5, "2026-10-27")
        self.fails(400, self.db.set_invested, pid, float("nan"), 10, "2026-10-27")
        self.fails(400, self.db.set_invested, pid, True, 10, "2026-10-27")

    def test_partial_sells_track_open_quantity(self):
        pid = self.invested(qty=100)
        self.db.add_sell(pid, 30, 60.0, "2026-10-28")
        self.db.add_sell(pid, 70, 55.0, "2026-10-29")
        self.assertEqual([s["qty"] for s in self.pick(pid)["sells"]], [30, 70])
        self.fails(400, self.db.add_sell, pid, 1, 55.0, "2026-10-30")  # nothing left

    def test_cannot_oversell(self):
        pid = self.invested(qty=10)
        self.fails(400, self.db.add_sell, pid, 11, 50.0, "2026-10-28")

    def test_cannot_sell_uninvested_pick(self):
        pid = self.db.add_pick(self.cycle, "CCC", "2026-10-27")
        self.fails(409, self.db.add_sell, pid, 1, 50.0, "2026-10-28")

    def test_cannot_shrink_buy_below_sold_quantity(self):
        pid = self.invested(qty=100)
        self.db.add_sell(pid, 40, 60.0, "2026-10-28")
        self.fails(400, self.db.set_invested, pid, 50.0, 39, "2026-10-27")
        self.db.set_invested(pid, 50.0, 40, "2026-10-27")

    def test_pick_with_sales_cannot_be_removed_or_uninvested_until_sales_undone(self):
        pid = self.invested()
        sid = self.db.add_sell(pid, 10, 60.0, "2026-10-28")
        self.fails(409, self.db.remove_pick, pid)
        self.fails(409, self.db.clear_invested, pid)
        self.db.remove_sell(sid)
        self.db.clear_invested(pid)
        self.assertEqual(self.pick(pid)["status"], "picked")
        self.db.remove_pick(pid)

    def test_undoing_a_sale_restores_quantity(self):
        pid = self.invested(qty=10)
        sid = self.db.add_sell(pid, 10, 60.0, "2026-10-28")
        self.db.remove_sell(sid)
        self.db.add_sell(pid, 10, 61.0, "2026-10-29")
        self.fails(404, self.db.remove_sell, 9999)


class DecisionAndHoldingTests(StoreTests):
    """Reuses StoreTests' setUp and helpers (its own tests run again, which is harmless)."""

    def test_ignore_and_change_of_mind(self):
        pid = self.db.add_pick(self.cycle, "AAA", "2026-10-27")
        self.db.set_ignored(pid, True)
        self.assertEqual(self.pick(pid)["status"], "ignored")
        self.db.set_invested(pid, 50.0, 10, "2026-10-27")  # changing your mind clears the ignore
        self.assertEqual(self.pick(pid)["status"], "invested")
        self.fails(409, self.db.set_ignored, pid, True)    # can't ignore something you bought
        self.db.clear_invested(pid)
        self.assertEqual(self.pick(pid)["status"], "picked")
        self.db.set_ignored(pid, True)
        self.db.set_ignored(pid, False)
        self.assertEqual(self.pick(pid)["status"], "picked")

    def test_existing_database_without_ignored_column_is_upgraded_in_place(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(path)
        con.executescript("""
            create table cycles (id integer primary key autoincrement, started_on text not null);
            create table picks (id integer primary key autoincrement, cycle_id integer not null references cycles (id) on delete cascade,
              symbol text not null, picked_on text not null, status text not null default 'picked' check (status in ('picked', 'invested')),
              entry_price real, qty integer, invested_on text, unique (cycle_id, symbol));
            create table sells (id integer primary key autoincrement, pick_id integer not null references picks (id) on delete cascade,
              qty integer not null, price real not null, sold_on text not null);
            insert into cycles (started_on) values ('2026-10-01');
            insert into picks (cycle_id, symbol, picked_on, status, entry_price, qty, invested_on)
              values (1, 'OLD', '2026-10-01', 'invested', 10.0, 5, '2026-10-01');
        """)
        con.commit(); con.close()
        db = Store(path)
        picks = db.state("2026-10-27")["picks"]
        self.assertEqual([(p["symbol"], p["status"], p["qty"]) for p in picks], [("OLD", "invested", 5)])
        db.set_ignored(db.add_pick(1, "NEW", "2026-10-27"), True)
        Store(path)  # opening twice must not try to add the column again

    def lots(self):
        """Two lots of AAA: 100 @ 50 in cycle 1, then 100 @ 80 in cycle 2."""
        a = self.invested("AAA", qty=100, price=50.0)
        nxt = self.db.start_cycle("2026-12-20")["id"]
        b = self.db.add_pick(nxt, "AAA", "2026-12-20")
        self.db.set_invested(b, 80.0, 100, "2026-12-20")
        return a, b

    def test_sell_by_symbol_uses_oldest_lot_first_and_spans_lots(self):
        a, b = self.lots()
        made = self.db.sell_holding("AAA", 130, 90.0, "2026-12-21")
        self.assertEqual([(m["pickId"], m["qty"]) for m in made], [(a, 100), (b, 30)])
        self.assertEqual(sum(s["qty"] for s in self.pick(a)["sells"]), 100)
        self.assertEqual(sum(s["qty"] for s in self.pick(b)["sells"]), 30)
        again = self.db.sell_holding("AAA", 70, 95.0, "2026-12-22")  # only lot b has anything left
        self.assertEqual([(m["pickId"], m["qty"]) for m in again], [(b, 70)])

    def test_sell_by_symbol_cannot_exceed_total_holding_and_changes_nothing_on_failure(self):
        a, b = self.lots()
        self.fails(400, self.db.sell_holding, "AAA", 201, 90.0, "2026-12-21")
        self.assertEqual(self.pick(a)["sells"], [])
        self.assertEqual(self.pick(b)["sells"], [])
        self.fails(400, self.db.sell_holding, "NOPE", 1, 90.0, "2026-12-21")

    def test_stock_can_be_picked_again_next_cycle_while_still_held(self):
        self.lots()
        self.assertEqual(len([p for p in self.db.state("2026-12-21")["picks"] if p["symbol"] == "AAA"]), 2)


if __name__ == "__main__":
    unittest.main()
