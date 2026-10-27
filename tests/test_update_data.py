import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_data as ud  # noqa: E402

UNIVERSE = {
    "AAA": {"name": "Aaa Ltd", "industry": "Banks"},
    "BBB": {"name": "Bbb Ltd", "industry": "IT"},
}

HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
          "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n")


def bhav(aaa_vol, bbb_vol=1000):
    rows = [
        f"AAA, EQ, 01-Oct-2026, 100.0, 100, 105, 99, 104, 103.5, 102, {aaa_vol}, 250.50, 900, 400, 45.5",
        f"BBB, EQ, 01-Oct-2026, 50.0, 50, 51, 49, 50, 50.5, 50, {bbb_vol}, 10.00, 100, -, -",
        "NOTINDEX, EQ, 01-Oct-2026, 10, 10, 10, 10, 10, 10, 10, 5, 1, 1, 1, 20",
    ]
    return HEADER + "\n".join(rows) + "\n"


class ParseTests(unittest.TestCase):
    def test_parses_padded_csv_and_filters_to_universe(self):
        out = ud.parse_bhav(bhav(2000), UNIVERSE)
        self.assertEqual(set(out), {"AAA", "BBB"})
        self.assertEqual(out["AAA"], [103.5, 100.0, 2000.0, 45.5, 250.5])

    def test_dash_delivery_becomes_none(self):
        self.assertIsNone(ud.parse_bhav(bhav(2000), UNIVERSE)["BBB"][3])

    def test_prefers_eq_series_over_other_series(self):
        text = HEADER + (
            "AAA, BE, d, 1, 1, 1, 1, 1, 9, 1, 111, 1, 1, 1, 1\n"
            "AAA, EQ, d, 1, 1, 1, 1, 1, 7, 1, 222, 1, 1, 1, 1\n"
        )
        self.assertEqual(ud.parse_bhav(text, UNIVERSE)["AAA"][2], 222.0)


class OutputTests(unittest.TestCase):
    def history(self, n_prior, prior_vol, latest_vol):
        days = {f"2026-09-{i + 1:02d}": {"AAA": [100, 100, prior_vol, 40, 1], "BBB": [50, 50, 1000, None, 1]}
                for i in range(n_prior)}
        days["2026-10-01"] = {"AAA": [103.5, 100, latest_vol, 45.5, 250.5], "BBB": [50.5, 50, 1000, None, 10.0]}
        return {"days": days, "holidays": []}

    def test_relative_volume(self):
        out = ud.build_output(self.history(20, 1000, 3000), UNIVERSE)
        aaa = next(s for s in out["stocks"] if s["symbol"] == "AAA")
        self.assertEqual(out["asOf"], "2026-10-01")
        self.assertEqual(aaa["relVol"], 3.0)
        self.assertEqual(aaa["turnoverCr"], 2.5)
        self.assertEqual(out["sessionsUsed"], 20)

    def test_only_last_20_prior_sessions_are_averaged(self):
        h = self.history(25, 1000, 2000)
        for i in range(5):  # oldest five are wildly different; must be ignored
            h["days"][f"2026-09-{i + 1:02d}"]["AAA"][2] = 10_000_000
        aaa = next(s for s in ud.build_output(h, UNIVERSE)["stocks"] if s["symbol"] == "AAA")
        self.assertEqual(aaa["avgVol20"], 1000)

    def test_too_little_history_gives_unknown_relvol(self):
        out = ud.build_output(self.history(3, 1000, 3000), UNIVERSE)
        self.assertTrue(all(s["relVol"] is None for s in out["stocks"]))


MCAP = ("Trade Date, Symbol, Series, Security Name, Category, Face Value, Issue Size, Close Price, Market Cap(Rs.)\n"
        "01-Oct-2026, AAA, EQ, Aaa Ltd, Listed, 10, 2000000000, 103.5, 207000000000\n"
        "01-Oct-2026, BBB, EQ, Bbb Ltd, Listed, 1, 500000000, 50.5, 25250000000\n"
        "01-Oct-2026, ZZZ, EQ, Not In Index, Listed, 1, 100, 1, 100\n")


class McapTests(unittest.TestCase):
    def test_issue_size_column_is_shares(self):
        self.assertEqual(ud.parse_mcap(MCAP, UNIVERSE), {"AAA": 2_000_000_000, "BBB": 500_000_000})

    def test_shares_derived_from_mcap_and_price_when_issue_size_missing(self):
        text = "Symbol, Series, Close Price, Market Cap(Rs.)\nAAA, EQ, 100, 100000000000\n"
        self.assertEqual(ud.parse_mcap(text, UNIVERSE), {"AAA": 1_000_000_000})

    def test_unusable_file_gives_nothing(self):
        self.assertEqual(ud.parse_mcap("Symbol, Series\nAAA, EQ\n", UNIVERSE), {})
        self.assertEqual(ud.parse_mcap("", UNIVERSE), {})

    def test_market_cap_is_close_times_shares_in_crore(self):
        h = {"days": {"2026-10-01": {"AAA": [103.5, 100, 5000, 40, 55.0], "BBB": [50.5, 50, 1000, None, 10.0]}}, "holidays": []}
        out = ud.build_output(h, UNIVERSE, shares={"AAA": 2_000_000_000})
        by = {s["symbol"]: s for s in out["stocks"]}
        self.assertEqual(by["AAA"]["mcapCr"], 20700.0)  # 103.5 x 2e9 / 1e7
        self.assertIsNone(by["BBB"]["mcapCr"])           # no shares known for it
        self.assertTrue(out["mcap"]["loaded"])

    def test_no_shares_means_not_loaded(self):
        h = {"days": {"2026-10-01": {"AAA": [103.5, 100, 5000, 40, 55.0]}}, "holidays": []}
        out = ud.build_output(h, UNIVERSE, shares={})
        self.assertFalse(out["mcap"]["loaded"])


class CollectTests(unittest.TestCase):
    def run_collect(self, history, today, available):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            key = url.rsplit("_", 1)[1][:8]
            return bhav(1000).encode() if key in available else None

        with mock.patch.object(ud, "fetch", fake_fetch):
            ud.collect_sessions(history, UNIVERSE, today, want=3)
        return calls

    def test_skips_weekends_and_records_old_holidays(self):
        # Mon 2026-10-05 is "today"; Fri 10-02 is a holiday; Thu 10-01 and Wed 09-30 trade.
        h = {"days": {}, "holidays": []}
        self.run_collect(h, date(2026, 10, 5), {"05102026", "01102026", "30092026"})
        self.assertEqual(sorted(h["days"]), ["2026-09-30", "2026-10-01", "2026-10-05"])
        self.assertIn("2026-10-02", h["holidays"])

    def test_missing_today_is_not_recorded_as_holiday(self):
        h = {"days": {}, "holidays": []}
        self.run_collect(h, date(2026, 10, 5), {"02102026", "01102026", "30092026"})
        self.assertNotIn("2026-10-05", h["holidays"])

    def test_full_history_still_fetches_newer_sessions(self):
        h = {"days": {"2026-09-28": {"AAA": [1, 1, 1, 1, 1]}, "2026-09-29": {"AAA": [1, 1, 1, 1, 1]},
                      "2026-09-30": {"AAA": [1, 1, 1, 1, 1]}}, "holidays": []}
        self.run_collect(h, date(2026, 10, 1), {"01102026"})
        self.assertIn("2026-10-01", h["days"])

    def test_up_to_date_history_makes_no_requests(self):
        h = {"days": {"2026-09-29": {"AAA": [1, 1, 1, 1, 1]}, "2026-09-30": {"AAA": [1, 1, 1, 1, 1]},
                      "2026-10-01": {"AAA": [1, 1, 1, 1, 1]}}, "holidays": []}
        self.assertEqual(self.run_collect(h, date(2026, 10, 1), set()), [])


if __name__ == "__main__":
    unittest.main()
