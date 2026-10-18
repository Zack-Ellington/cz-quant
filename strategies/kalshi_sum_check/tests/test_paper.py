import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from feeds import KalshiWSFeed, OrderBookStore  # noqa: E402
from paper_ws import best_quote, quote, rebuild_consumed, settle_trade  # noqa: E402
from pricing import parse_book, round_up_cent  # noqa: E402


def book(yes, no):
    return parse_book({"yes_dollars": [[f"{p:.4f}", f"{q:.2f}"] for p, q in yes],
                       "no_dollars": [[f"{p:.4f}", f"{q:.2f}"] for p, q in no]})


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.updates = []
        self.store = OrderBookStore(on_update=self.updates.append)
        self.feed = KalshiWSFeed(self.store, credentials=("key", object()))

    def msg(self, **m):
        return self.feed.handle(json.dumps(m))

    def test_snapshot_then_deltas(self):
        self.msg(type="subscribed", id=1, msg={"channel": "orderbook_delta", "sid": 7})
        self.msg(type="orderbook_snapshot", sid=7, seq=1,
                 msg={"market_ticker": "M", "yes_dollars_fp": [["0.4000", "10.00"]], "no_dollars_fp": [["0.5500", "5.00"]]})
        b = self.store.book("M")
        self.assertEqual((b.yes_bids, b.yes_asks), ([(0.4, 10.0)], [(0.45, 5.0)]))
        self.msg(type="orderbook_delta", sid=7, seq=2, msg={"market_ticker": "M", "price_dollars": "0.5500", "delta_fp": "-5.00", "side": "no"})
        self.msg(type="orderbook_delta", sid=7, seq=3, msg={"market_ticker": "M", "price": 42, "delta": 3, "side": "yes"})  # legacy cents
        b = self.store.book("M")
        self.assertEqual((b.yes_bids, b.yes_asks), ([(0.42, 3.0), (0.4, 10.0)], []))
        self.assertEqual(self.updates, ["M", "M", "M"])

    def test_sequence_gap_requests_resubscribe(self):
        self.msg(type="orderbook_snapshot", sid=7, seq=1, msg={"market_ticker": "M", "yes": [[40, 10]], "no": []})
        self.assertEqual(self.msg(type="orderbook_delta", sid=7, seq=3, msg={"market_ticker": "M", "price": 40, "delta": 1, "side": "yes"}), 7)

    def test_paper_fills_survive_a_flickering_level(self):
        self.store.snapshot("M", [(0.40, 10)], [(0.55, 5)])  # YES ask 0.45 x5
        self.store.consume("M", "ask", 0.45, 3)
        self.assertEqual(self.store.book("M").yes_asks, [(0.45, 2.0)])
        self.store.snapshot("M", [(0.40, 10)], [(0.54, 5)])  # level briefly gone...
        self.store.snapshot("M", [(0.40, 10)], [(0.55, 5)])  # ...and back: same liquidity
        self.assertEqual(self.store.book("M").yes_asks, [(0.45, 2.0)])

    def test_paper_fills_expire_after_the_level_is_gone_long_enough(self):
        store = OrderBookStore(refill_seconds=0)
        store.snapshot("M", [(0.40, 10)], [(0.55, 5)])
        store.consume("M", "ask", 0.45, 3)
        store.snapshot("M", [(0.40, 10)], [(0.54, 5)])  # gone for >= 0 s: forgotten
        store.snapshot("M", [(0.40, 10)], [(0.55, 5)])
        self.assertEqual(store.book("M").yes_asks, [(0.45, 5.0)])

    def test_restart_rebuilds_fills_from_open_trades(self):
        # KXCAELECTION-2614 was bought 30 sets at 0.10 / 0.88 in three runs in a row.
        trade = {"side": "long", "legs": [{"ticker": "A", "levels": [[0.10, 30]]}, {"ticker": "B", "levels": [[0.88, 30]]}]}
        store = OrderBookStore()
        rebuild_consumed(store, {"t1": trade})
        store.snapshot("A", [], [(0.90, 30)])  # YES ask 0.10 x30
        store.snapshot("B", [], [(0.12, 30)])  # YES ask 0.88 x30
        self.assertEqual((store.book("A").yes_asks, store.book("B").yes_asks), ([], []))


class QuoteTests(unittest.TestCase):
    T = ["A", "B", "C"]

    def test_long_quote_matches_fee_math(self):
        books = [book([(0.25, 500)], [(0.70, 500)])] * 3  # asks 0.30 each
        q = quote("E", self.T, books, "long", 100, "quadratic", 1, 30, 0.05)
        fee = round_up_cent(0.07 * 100 * 0.3 * 0.7)
        self.assertAlmostEqual(q.capital, 90 + 3 * fee)
        self.assertAlmostEqual(q.net, 100 - 90 - 3 * fee)
        self.assertAlmostEqual(q.hurdle, 1.05 ** (30 / 365) - 1)

    def test_short_quote_capital_and_payout(self):
        books = [book([(0.36, 500)], [(0.60, 500)])] * 3  # bids 0.36 each, sum 1.08
        q = quote("E", self.T, books, "short", 100, "quadratic", 1, 30, 0.05)
        fee = round_up_cent(0.07 * 100 * 0.36 * 0.64)
        self.assertEqual(q.payout, 200)
        self.assertAlmostEqual(q.capital, 3 * 64 + 3 * fee)
        self.assertAlmostEqual(q.net, 8 - 3 * fee)

    def test_risk_free_hurdle_rejects_thin_long_dated_edge(self):
        # Asks sum to 0.99: ~1% before fees. Fine over 30 days, not over 2 years.
        books = [book([(0.30, 500)], [(0.67, 500)]), book([(0.30, 500)], [(0.67, 500)]), book([(0.30, 500)], [(0.67, 500)])]
        q, why = best_quote("E", self.T, books, "quadratic", 0, 30, 0.05, 500, 1e9, 0.10)
        self.assertIsNotNone(q)
        self.assertGreater(q.apr, 0.05)
        q, why = best_quote("E", self.T, books, "quadratic", 0, 730, 0.05, 500, 1e9, 0.10)
        self.assertIsNone(q)
        self.assertEqual(why, "below_risk_free")

    def test_sizing_stops_where_deeper_levels_lose(self):
        # Leg A: 50 @ 0.30 then 1000 @ 0.40. B, C: deep at 0.30. Past 50 sets each set costs 1.00.
        a = book([(0.2, 9)], [(0.70, 50), (0.60, 1000)])
        bc = book([(0.2, 9)], [(0.70, 1000)])
        q, _ = best_quote("E", self.T, [a, bc, bc], "quadratic", 0, 10, 0.05, 500, 1e9, 0.10)
        self.assertEqual((q.side, q.sets), ("long", 50))

    def test_capital_limit_shrinks_size(self):
        books = [book([(0.25, 500)], [(0.70, 500)])] * 3
        q, _ = best_quote("E", self.T, books, "quadratic", 1, 10, 0.05, 500, 100.0, 0.10)
        self.assertLessEqual(q.capital, 100.0)
        self.assertGreater(q.sets, 90)


class SettlementTests(unittest.TestCase):
    # KXEBASKETBALLGAME-26OCT151725BARRTATU as Kalshi reported it on Oct 15.
    POS = {"side": "long", "sets": 201, "capital": 199.95,
           "legs": [{"ticker": "BARR"}, {"ticker": "TATU"}]}

    def mk(self, status="finalized", tatu_value="1.0000", tatu_result="yes", **extra):
        return {"BARR": {"status": status, "result": "no", "settlement_value_dollars": "0.0000"},
                "TATU": {"status": status, "result": tatu_result, "settlement_value_dollars": tatu_value}, **extra}

    def test_real_settlement_verifies(self):
        st = settle_trade(self.POS, self.mk())
        self.assertEqual((st["payout"], st["mismatches"]), (201.0, []))
        self.assertAlmostEqual(st["pnl"], 1.05)

    def test_waits_for_finalized(self):
        self.assertIsNone(settle_trade(self.POS, self.mk(status="determined")))

    def test_value_disagreeing_with_result_is_flagged_and_value_wins(self):
        st = settle_trade(self.POS, self.mk(tatu_value="0.5000"))
        self.assertEqual(st["payout"], 100.5)
        self.assertEqual({m["issue"] for m in st["mismatches"]}, {"value_vs_result", "payout_differs"})

    def test_no_side_pays_one_minus_value(self):
        st = settle_trade({**self.POS, "side": "short", "capital": 0.5}, self.mk())
        self.assertEqual((st["payout"], st["mismatches"]), (201.0, []))

    def test_added_market_that_wins_is_flagged(self):
        st = settle_trade(self.POS, self.mk(tatu_result="no", tatu_value="0.0000",
                                            NEW={"status": "finalized", "result": "yes", "settlement_value_dollars": "1.0000"}))
        self.assertEqual(st["payout"], 0.0)
        self.assertIn("added_market_won", {m["issue"] for m in st["mismatches"]})



class ReserveTests(unittest.TestCase):
    def test_priority_needs_both_short_and_high_apr(self):
        from paper_ws import Config, is_priority
        c = Config()
        self.assertTrue(is_priority(0.2, 5.0, c))
        self.assertFalse(is_priority(0.2, 0.5, c))   # fast but under 100% APR
        self.assertFalse(is_priority(30, 5.0, c))    # high APR but slow

    def test_reserve_need_shrinks_as_priority_trades_use_it(self):
        from paper_ws import Config, reserve_need
        c = Config()
        self.assertAlmostEqual(reserve_need(1000, 0, c), 300)
        self.assertAlmostEqual(reserve_need(1000, 200, c), 100)  # $200 of the reserve is in a game
        self.assertAlmostEqual(reserve_need(1000, 450, c), 0)    # priority trades beyond the reserve


if __name__ == "__main__":
    unittest.main()
