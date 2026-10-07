import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from monitor import build_summary, thin, update_state  # noqa: E402


def row(ticker, ask, bid, net=None, flags="", fees=0.5):
    best = "taker_long" if net is not None else None
    return {
        "taker_long_gross": net + fees if net is not None else None, "taker_long_fees": fees if net is not None else None,
        "taker_short_gross": None,
        "event_ticker": ticker, "title": ticker, "series_ticker": "S", "category": "C", "n_legs": 2,
        "sum_ask": ask, "sum_bid": bid, "mid_sum": (ask + bid) / 2, "flags": flags,
        "best_taker": best, "taker_long_net": net, "best_edge_per_set": net / 100 if net else None,
        "best_roi": 0.02 if net else None, "best_apr": 0.5 if net else None, "days_to_settle": 30.0,
    }


class MonitorTests(unittest.TestCase):
    def test_counts_extremes_and_return(self):
        state = {}
        update_state(state, [row("A", 0.98, 0.95, net=1.0), row("B", 1.05, 1.02)], "2026-01-01T00:00:00Z", 100)
        p = update_state(state, [row("A", 1.01, 0.97), row("B", 1.04, 1.03)], "2026-01-01T00:15:00Z", 100)
        a, b = state["events"]["A"], state["events"]["B"]
        self.assertEqual((a["obs"], a["under"], a["over"]), (2, 1, 0))
        self.assertEqual((b["obs"], b["under"], b["over"]), (2, 0, 2))
        self.assertEqual((a["ask_lo"], a["ask_hi"]), (0.98, 1.01))
        self.assertEqual(state["extremes"]["ask_lo"]["value"], 0.98)
        self.assertEqual(state["extremes"]["bid_hi"]["value"], 1.03)
        self.assertEqual((p["under"], p["over"], p["opps"]), (0, 1, 0))
        self.assertEqual(state["history"][0]["net"], 1.0)
        self.assertAlmostEqual(state["history"][0]["cap"], 50.0)  # net / roi
        self.assertEqual((state["history"][0]["gross"], state["history"][0]["fees"]), (1.5, 0.5))

    def test_counts_edge_eaten_by_fees(self):
        state = {}
        p = update_state(state, [row("A", 0.995, 0.97, net=-0.2, fees=0.7)], "2026-01-01T00:00:00Z", 100)
        self.assertEqual((p["opps"], p["fee_eaten"], p["fee_eaten_gross"]), (0, 1, 0.5))

    def test_non_exhaustive_excluded_from_totals(self):
        state = {}
        p = update_state(state, [row("X", 0.2, 0.1, net=70.0, flags="likely_non_exhaustive")], "2026-01-01T00:00:00Z", 100)
        self.assertEqual((p["under"], p["under_exh"], p["opps"]), (1, 0, 0))
        self.assertEqual(state["extremes"], {})
        self.assertEqual(build_summary(state)["ever_under"], 0)

    def test_thin_keeps_ends(self):
        t = thin(list(range(1000)), 10)
        self.assertEqual((len(t), t[0], t[-1]), (10, 0, 999))


if __name__ == "__main__":
    unittest.main()
