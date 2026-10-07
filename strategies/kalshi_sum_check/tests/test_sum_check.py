import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pricing import maker_buy_price, maker_sell_price, parse_book, round_up_cent, taker_fee, maker_fee, walk  # noqa: E402
from sum_check import analyze_event  # noqa: E402

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def market(ticker, days=30, status="active"):
    return {
        "ticker": ticker,
        "status": status,
        "result": "",
        "expected_expiration_time": (NOW + timedelta(days=days)).isoformat(),
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


def book(yes, no):
    return {"yes_dollars": [[f"{p:.4f}", f"{q:.2f}"] for p, q in yes], "no_dollars": [[f"{p:.4f}", f"{q:.2f}"] for p, q in no]}


class PricingTests(unittest.TestCase):
    def test_parse_book_converts_no_bids_to_yes_asks(self):
        b = parse_book(book(yes=[(0.30, 10), (0.32, 5)], no=[(0.60, 7), (0.65, 4)]))
        self.assertEqual(b.yes_bids, [(0.32, 5), (0.30, 10)])
        self.assertEqual(b.yes_asks, [(0.35, 4), (0.40, 7)])

    def test_walk_partial_levels(self):
        f = walk([(0.35, 4), (0.40, 7)], 6)
        self.assertEqual(f.filled, 6)
        self.assertAlmostEqual(f.notional, 4 * 0.35 + 2 * 0.40)
        self.assertEqual(f.levels, 2)

    def test_fees(self):
        # 100 contracts at 0.50: 0.07 * 100 * 0.25 = 1.75
        self.assertAlmostEqual(taker_fee(100 * 0.25, "quadratic", 1), 1.75)
        # rounds up to the cent: 0.07 * 1 * 0.25 = 0.0175 -> 0.02
        self.assertAlmostEqual(taker_fee(0.25, "quadratic", 1), 0.02)
        self.assertEqual(maker_fee(25, "quadratic", 1), 0.0)
        self.assertAlmostEqual(maker_fee(25, "quadratic_with_maker_fees", 1), 0.44)
        self.assertAlmostEqual(taker_fee(25, "quadratic", 0.5), 0.88)
        self.assertEqual(round_up_cent(0.07), 0.07)  # no float-noise bump

    def test_maker_prices(self):
        pr = [{"start": "0", "end": "1", "step": "0.01"}]
        wide = parse_book(book(yes=[(0.30, 1)], no=[(0.60, 1)]))  # 0.30 / 0.40
        self.assertAlmostEqual(maker_buy_price(wide, pr), 0.31)
        self.assertAlmostEqual(maker_sell_price(wide, pr), 0.39)
        tight = parse_book(book(yes=[(0.30, 1)], no=[(0.69, 1)]))  # 0.30 / 0.31
        self.assertAlmostEqual(maker_buy_price(tight, pr), 0.30)
        self.assertAlmostEqual(maker_sell_price(tight, pr), 0.31)


class SumCheckTests(unittest.TestCase):
    def event(self, n=3):
        return {"event_ticker": "EV", "series_ticker": "S", "markets": [market(f"M{i}") for i in range(n)]}

    def test_long_arb_when_asks_sum_below_one(self):
        # Asks 0.30 each (NO bid 0.70) -> sum 0.90; bids 0.25 each -> sum 0.75.
        books = {f"M{i}": book(yes=[(0.25, 500)], no=[(0.70, 500)]) for i in range(3)}
        row, legs = analyze_event(self.event(), books, {"S": {"fee_type": "quadratic", "fee_multiplier": 1}}, 100, NOW)
        self.assertAlmostEqual(row["sum_ask"], 0.90)
        self.assertAlmostEqual(row["sum_bid"], 0.75)
        fee = 3 * round_up_cent(0.07 * 100 * 0.3 * 0.7)  # 1.47 each
        self.assertAlmostEqual(row["taker_long_net"], 100 - 90 - fee)
        self.assertAlmostEqual(row["taker_long_gross"], 10)
        self.assertAlmostEqual(row["taker_long_fees"], fee)
        self.assertLess(row["taker_short_net"], 0)
        self.assertEqual(row["best_taker"], "taker_long")
        self.assertAlmostEqual(row["days_to_settle"], 30)
        roi = row["taker_long_roi"]
        self.assertAlmostEqual(row["taker_long_apr"], roi * 365 / 30)

    def test_short_arb_when_bids_sum_above_one(self):
        books = {f"M{i}": book(yes=[(0.36, 500)], no=[(0.60, 500)]) for i in range(3)}  # bids 1.08, asks 1.20
        row, _ = analyze_event(self.event(), books, {}, 100, NOW)
        fee = 3 * round_up_cent(0.07 * 100 * 0.36 * 0.64)
        self.assertAlmostEqual(row["taker_short_net"], 108 - 100 - fee)
        # capital = cost of NO on every leg = 3 * 100 * 0.64 + fees
        self.assertAlmostEqual(row["taker_short_roi"], row["taker_short_net"] / (192 + fee))

    def test_walks_and_reports_thin_depth(self):
        books = {
            "M0": book(yes=[(0.2, 50)], no=[(0.70, 40), (0.60, 100)]),  # 40 @ .30 then .40
            "M1": book(yes=[(0.2, 50)], no=[(0.70, 500)]),
            "M2": book(yes=[(0.2, 50)], no=[(0.70, 500)]),
        }
        row, _ = analyze_event(self.event(), books, {}, 100, NOW)
        self.assertAlmostEqual(row["sum_ask"], (40 * 0.30 + 60 * 0.40) / 100 + 0.60)
        self.assertEqual(row["taker_short_size"], 50)
        self.assertIn("bid_depth_only_50", row["flags"])

    def test_leg_without_asks_disables_long(self):
        books = {"M0": book(yes=[(0.2, 50)], no=[]), "M1": book(yes=[(0.2, 50)], no=[(0.7, 9)])}
        row, _ = analyze_event(self.event(2), books, {}, 100, NOW)
        self.assertIsNone(row["taker_long_net"])
        self.assertIn("leg_without_ask", row["flags"])

    def test_flags_likely_non_exhaustive(self):
        books = {f"M{i}": book(yes=[(0.05, 50)], no=[(0.93, 50)]) for i in range(3)}  # mids sum 0.18
        row, _ = analyze_event(self.event(), books, {}, 10, NOW)
        self.assertIn("likely_non_exhaustive", row["flags"])

    def test_resolved_event_skipped(self):
        ev = self.event()
        ev["markets"][0]["result"] = "yes"
        self.assertIsNone(analyze_event(ev, {}, {}, 100, NOW))


if __name__ == "__main__":
    unittest.main()
