import math

import numpy as np
from scipy import stats

from calibration.analysis import bucket_of, holm, mids, slope_test, snapshot, touches, ttest
from calibration.pull import pick_side


def test_mids_skip_empty_wide_and_late_quotes():
    candles = [
        [0, 0.0, 1.0, None, 0],     # empty book
        [1, 0.40, 0.60, None, 0],   # 20c spread
        [2, 0.48, 0.52, 0.5, 10],
        [3, None, 0.52, None, 0],
        [4, 0.60, 0.62, 0.61, 5],
    ]
    assert mids(candles) == [(2, 0.5), (4, 0.61)]
    assert mids(candles, until=3) == [(2, 0.5)]


def test_touch_records_first_crossing_and_overshoot():
    path = [(0, 0.55), (1, 0.62), (2, 0.97), (3, 0.40), (4, 0.30)]
    t = touches(path, [0.6, 0.9, 0.95, 0.4, 0.3, 0.2])
    assert t[0.6] == (1, 0.62)
    assert t[0.9] == (2, 0.97)      # jumped past 90 and 95 in one hour
    assert t[0.95] == (2, 0.97)
    assert t[0.4] == (3, 0.40)
    assert t[0.3] == (4, 0.30)
    assert 0.2 not in t


def test_snapshot_uses_last_quote_within_age():
    path = [(0, 0.3), (10, 0.4), (20, 0.5)]
    assert snapshot(path, at=15, max_age=10) == 0.4
    assert snapshot(path, at=25, max_age=4) is None
    assert snapshot(path, at=-1, max_age=100) is None


def test_bucket_edges():
    assert bucket_of(0.0) == 0 and bucket_of(0.1) == 1 and bucket_of(0.95) == 9 and bucket_of(1.0) == 9


def test_ttest_matches_scipy():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.6, 0.9, 400)
    y = (rng.uniform(size=400) < p - 0.05).astype(float)
    t = ttest(y, p, alpha=0.02)
    ref = stats.ttest_1samp(y - p, 0)
    assert math.isclose(t.t, ref.statistic) and math.isclose(t.p, ref.pvalue)
    assert t.ci_low < t.diff < t.ci_high


def test_calibrated_data_rarely_rejected_and_biased_data_is():
    rng = np.random.default_rng(7)
    rejections = 0
    for _ in range(200):
        p = rng.uniform(0.85, 0.95, 300)
        y = (rng.uniform(size=300) < p).astype(float)
        rejections += ttest(y, p, 0.02).p < 0.02
    assert rejections / 200 < 0.06       # ~2% expected
    p = rng.uniform(0.85, 0.95, 3000)
    y = (rng.uniform(size=3000) < p - 0.05).astype(float)
    assert ttest(y, p, 0.02).p < 0.02


def test_holm_is_monotone_and_capped():
    tests = [ttest([1, 0, 1, 1], [0.5] * 4, 0.02) for _ in range(3)]
    tests[0].p, tests[1].p, tests[2].p = 0.01, 0.04, 0.03
    holm(tests)
    assert [round(t.p_holm, 4) for t in tests] == [0.03, 0.06, 0.06]


def test_slope_detects_longshot_bias_and_accepts_calibration():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.02, 0.98, 20000)
    y = (rng.uniform(size=p.size) < p).astype(float)
    assert slope_test(y, p).p > 0.02
    true = np.clip(0.5 + 1.2 * (p - 0.5), 0, 1)   # favorites underpriced
    y2 = (rng.uniform(size=p.size) < true).astype(float)
    s = slope_test(y2, p)
    assert s.slope > 1 and s.p < 0.02


def test_pick_side_is_stable_and_uses_both_sides():
    assert pick_side("E-1", ["E-1-B", "E-1-A"]) == pick_side("E-1", ["E-1-A", "E-1-B"])
    sides = {pick_side(f"E-{i}", ["A", "B"]) for i in range(50)}
    assert sides == {"A", "B"}
