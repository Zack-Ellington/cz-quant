import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classifier import EXHAUSTIVE, GAP, NOT_EXHAUSTIVE, RISKY, UNKNOWN, classify  # noqa: E402

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def ev(*markets):
    return {"markets": [{"result": "", "latest_expiration_time": "2027-01-01T00:00:00Z", **m} for m in markets]}


def strike(st, lo=None, hi=None):
    return {"strike_type": st, "floor_strike": lo, "cap_strike": hi}


class ClassifierTests(unittest.TestCase):
    def test_numeric_ladder_that_tiles(self):
        e = ev(strike("less", hi=70), strike("between", 70, 71), strike("between", 72, 73), strike("greater", lo=73))
        self.assertEqual(classify(e, NOW)[0], EXHAUSTIVE)

    def test_numeric_ladder_with_gap_or_missing_tail(self):
        self.assertEqual(classify(ev(strike("less", hi=70), strike("between", 72, 73), strike("greater", lo=73)), NOW)[0], GAP)
        self.assertEqual(classify(ev(strike("between", 2, 2), strike("between", 3, 3)), NOW)[0], GAP)
        decimals = ev(strike("less", hi=0.1), strike("between", 0.2, 0.5), strike("greater", lo=0.5))
        self.assertEqual(classify(decimals, NOW)[0], EXHAUSTIVE)

    def test_tie_rule(self):  # KXSTATELEG-MISEN26 wording
        rules = {"strike_type": "custom", "rules_primary": "If the Democratic party wins the Michigan State Senate in 2026, then the market resolves to Yes.",
                 "rules_secondary": "If both parties are tied, then the market resolves to No."}
        self.assertEqual(classify(ev(rules, rules), NOW)[0], NOT_EXHAUSTIVE)

    def test_tie_leg_beats_tie_rule(self):  # KXMLBF3 wording: a tie pays the "Tie" leg
        rules = {"strike_type": "structured", "rules_secondary": 'If tied, the "Tie" strike will resolve to Yes and all teams resolve to No.'}
        e = ev({**rules, "yes_sub_title": "Milwaukee"}, {**rules, "yes_sub_title": "Los Angeles D"}, {**rules, "yes_sub_title": "Tie"})
        self.assertEqual(classify(e, NOW)[0], EXHAUSTIVE)

    def test_other_leg_with_deadline_is_risky(self):  # KXMODELHIGH-27-1550 wording
        r = {"strike_type": "custom", "rules_primary": "If a model by Alibaba is the first to hit 1550 on Text Arena before Jan 1, 2027, then the market resolves to Yes."}
        self.assertEqual(classify(ev({**r, "yes_sub_title": "Qwen"}, {**r, "yes_sub_title": "Other"}), NOW)[0], RISKY)
        self.assertEqual(classify(ev({**r, "yes_sub_title": "Qwen"}, {**r, "yes_sub_title": "No one"}), NOW)[0], EXHAUSTIVE)

    def test_resolve_to_zero_tie_rule(self):  # KXNFLGAME / KXUFCFIGHT wording
        nfl = {"strike_type": "structured", "rules_secondary": "If the game ends in a tie, the market will resolve to $0."}
        self.assertEqual(classify(ev({**nfl, "yes_sub_title": "Cleveland"}, {**nfl, "yes_sub_title": "Baltimore"}), NOW)[0], NOT_EXHAUSTIVE)

    def test_combat_sport_needs_a_draw_leg(self):  # KXBOXING-26DEC11FURYJOSHUA
        fight = {"series_ticker": "KXBOXING", "event_ticker": "KXBOXING-26DEC11FURYJOSHUA",
                 "markets": [{"strike_type": "structured", "yes_sub_title": n, "result": ""} for n in ("Tyson Fury", "Anthony Joshua")]}
        self.assertEqual(classify(fight, NOW)[0], NOT_EXHAUSTIVE)
        fight["markets"].append({"strike_type": "structured", "yes_sub_title": "Draw", "result": ""})
        self.assertEqual(classify(fight, NOW)[0], EXHAUSTIVE)

    def test_first_to_needs_a_none_leg(self):
        e = ev({"strike_type": "custom", "yes_sub_title": "Indiana"}, {"strike_type": "custom", "yes_sub_title": "Ohio St."})
        e["title"] = "Which team scores the first touchdown?"
        self.assertEqual(classify(e, NOW)[0], RISKY)
        e["markets"].append({"strike_type": "custom", "yes_sub_title": "No team", "result": ""})
        self.assertEqual(classify(e, NOW)[0], EXHAUSTIVE)

    def test_candidate_lists_are_flagged_for_approval(self):  # KXLEXMAYOR-26
        from classifier import CANDIDATE_REASON
        e = ev({"strike_type": "custom", "yes_sub_title": "Linda Gorton"}, {"strike_type": "custom", "yes_sub_title": "Raquel Carter"})
        e["title"] = "Lexington Mayor winner?"
        self.assertEqual(classify(e, NOW), (UNKNOWN, CANDIDATE_REASON))

    def test_catch_all_leg(self):
        e = ev({"strike_type": "custom", "yes_sub_title": "Smith"}, {"strike_type": "custom", "yes_sub_title": "Other"})
        self.assertEqual(classify(e, NOW)[0], EXHAUSTIVE)

    def test_party_and_long_dated(self):  # KXATTYGENRI-26 wording
        party = {"strike_type": "custom", "rules_primary": "If a representative of the Republican party is sworn in as the Attorney General of Rhode Island for the term beginning in 2027, then the market resolves to Yes."}
        self.assertEqual(classify(ev(party, party), NOW)[0], RISKY)
        far = {"strike_type": "custom", "latest_expiration_time": "2099-08-08T15:00:00Z"}
        self.assertEqual(classify(ev(far, far), NOW)[0], RISKY)
        self.assertEqual(classify(ev({"strike_type": "structured"}, {"strike_type": "structured"}), NOW)[0], UNKNOWN)


if __name__ == "__main__":
    unittest.main()
