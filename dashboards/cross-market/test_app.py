"""Unfilled-opportunity classification from recorded strategy events."""
import app


def scan(t, net=None):
    basket = {"net": net, "sets": 16, "edge": "0.02"} if net else None
    return {"effective_at": f"2100-01-01T00:00:{t:02d}+00:00", "data": {"kind": "scan_complete", "basket": basket}}


def event(t, **data):
    return {"effective_at": f"2100-01-01T00:00:{t:02d}+00:00", "data": data}


LEGS = [{"ticker": "PMUS:m", "limit": "0.17"}, {"ticker": "K-KC", "limit": "0.73"}]


def test_an_expired_leg_is_reported_with_its_venue_price_and_fill():
    missed = app.missed_opportunities([
        scan(0, "1.20"), event(1, kind="basket_execution", context={"intended_legs": LEGS},
                               outcomes=[{"venue": "polymarket_us", "ticker": "PMUS:m", "asked": "17",
                                          "filled": "0", "status": "canceled"}]),
        scan(15)], cooldown=300)
    assert len(missed) == 1 and missed[0]["max_net"] == "1.20"
    assert missed[0]["errors"] == ["IOC_CANCELED Polymarket US: filled 0/17 @ 17.00c"]


def test_a_filled_basket_is_not_missed():
    outcomes = [{"venue": "polymarket_us", "ticker": "PMUS:m", "asked": "17", "filled": "17", "status": "filled"},
                {"venue": "kalshi", "ticker": "K-KC", "asked": "17", "filled": "17", "status": "filled"}]
    assert app.missed_opportunities([scan(0, "1.20"), event(1, kind="basket_execution",
                                     context={"intended_legs": LEGS}, outcomes=outcomes), scan(15)], 300) == []


def test_refusals_carry_their_detail_and_consecutive_scans_merge():
    missed = app.missed_opportunities([
        scan(0, "0.30"), event(1, kind="reject", reason="basket_preflight", detail="kalshi: insufficient_cash"),
        scan(15, "0.50"), scan(30)], cooldown=300)
    assert len(missed) == 1 and missed[0]["max_net"] == "0.50" and missed[0]["duration_s"] == 15
    assert missed[0]["errors"] == ["BASKET_PREFLIGHT: kalshi: insufficient_cash"]


def test_an_unrecorded_miss_says_so_and_a_recent_attempt_means_cooldown():
    assert app.missed_opportunities([scan(0, "0.3"), scan(15)], 300)[0]["errors"][0].startswith("NO_ATTEMPT_RECORDED")
    missed = app.missed_opportunities([
        scan(0, "0.3"), event(1, kind="basket_execution", context={}, outcomes=[]), scan(5),
        scan(20, "0.4"), scan(35)], cooldown=300)
    assert missed[-1]["errors"][0].startswith("COOLDOWN: 19s after the last attempt")
