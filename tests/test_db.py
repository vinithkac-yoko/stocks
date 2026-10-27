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


if __name__ == "__main__":
    unittest.main()
