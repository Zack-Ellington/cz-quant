"""Does the calibration gap survive paying the spread (and Kalshi's taker fee)?

For each touch level, the trade the bias suggests:
  favorites (level >= 50%): buy YES at the ask       -> pnl = y - ask
  longshots (level <  50%): buy NO at 1 - bid        -> pnl = bid - y
Taker fee per contract = ceil(0.07 * P * (1 - P) * 100) / 100 (Kalshi general schedule).
Writes per-observation rows to spread_obs.csv so the slow candle read happens once.
"""
import csv, json, math, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from scipy import stats

sys.path.insert(0, "src")
from calibration.analysis import THRESHOLDS, HOUR, holm, ttest  # noqa

ALPHA = 0.02
data = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data")
out_csv = Path(sys.argv[1])
markets = json.loads((data / "qualifying.json").read_text(encoding="utf-8"))


def fee(p):
    return math.ceil(round(0.07 * p * (1 - p) * 100, 6)) / 100


def one(m):
    if m["volume"] < 100:
        return []
    f = data / "candles" / f"{m['ticker']}.json"
    if not f.exists():
        return []
    rows = []
    found = set()
    y = 1.0 if m["result"] == "yes" else 0.0
    snap = None
    for row in json.loads(f.read_text(encoding="utf-8")):
        t, bid, ask = int(row[0]), row[1], row[2]
        if t > m["close_ts"]:
            break
        if bid is None or ask is None or ask < bid - 1e-9 or ask - bid > 0.10 + 1e-9 or (bid <= 0 and ask >= 1):
            continue
        p = (bid + ask) / 2
        if t <= m["close_ts"] - 24 * HOUR and t > m["close_ts"] - 48 * HOUR:
            snap = (t, bid, ask, p)
        for x in THRESHOLDS:
            if x in found:
                continue
            if (x >= 0.5 and p >= x - 1e-9) or (x < 0.5 and p <= x + 1e-9):
                found.add(x)
                rows.append([m["series"], f"touch_{int(round(100 * x))}", x, y, bid, ask, p,
                             (m["close_ts"] - t) / HOUR])
    if snap:
        t, bid, ask, p = snap
        rows.append([m["series"], "snap24", -1, y, bid, ask, p, (m["close_ts"] - t) / HOUR])
    return rows


if not out_csv.exists():
    with ThreadPoolExecutor(16) as ex, out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["series", "kind", "level", "y", "bid", "ask", "mid", "hours_left"])
        for i, rows in enumerate(ex.map(one, markets, chunksize=64)):
            w.writerows(rows)
            if i % 10000 == 0:
                print(i, flush=True)

obs = list(csv.DictReader(out_csv.open()))
print(f"{len(obs):,} observations")


def report(rows, label):
    print(f"\n=== {label} ===")
    print(f"{'level':7s} {'n':>7s} {'mid':>6s} {'won':>6s} {'gap':>6s} {'½spr':>6s} {'fee':>5s} "
          f"{'net(spread)':>11s} {'p':>7s} {'net(spr+fee)':>12s} {'p':>7s}  %obs gap>½spread")
    res = []
    for kind in [f"touch_{int(round(100 * x))}" for x in THRESHOLDS] + ["snap24_fav", "snap24_long"]:
        if kind.startswith("snap24"):
            sub = [r for r in rows if r["kind"] == "snap24"
                   and ((float(r["mid"]) >= 0.5) == kind.endswith("fav"))]
            fav = kind.endswith("fav")
        else:
            sub = [r for r in rows if r["kind"] == kind]
            fav = float(sub[0]["level"]) >= 0.5 if sub else True
        if len(sub) < 30:
            continue
        y = np.array([float(r["y"]) for r in sub]); bid = np.array([float(r["bid"]) for r in sub])
        ask = np.array([float(r["ask"]) for r in sub]); mid = (bid + ask) / 2
        if fav:
            gap, cost, px = y - mid, ask - mid, ask
            net = y - ask
        else:
            gap, cost, px = mid - y, mid - bid, 1 - bid
            net = bid - y
        fees = np.array([fee(p) for p in px])
        net_f = net - fees
        p1 = stats.ttest_1samp(net, 0).pvalue
        p2 = stats.ttest_1samp(net_f, 0).pvalue
        res.append([kind, len(sub), mid.mean(), y.mean(), gap.mean(), cost.mean(), fees.mean(),
                    net.mean(), p1, net_f.mean(), p2, (cost < gap.mean()).mean()])
    # Holm on net-of-spread+fee p
    order = sorted(range(len(res)), key=lambda i: res[i][10]); run = 0
    for k, i in enumerate(order):
        run = max(run, min(1, (len(res) - k) * res[i][10])); res[i].append(run)
    for r in res:
        k, n, m, w, g, c, fe, n1, p1, n2, p2, share, ph = r
        flag = " <== profitable (Holm)" if ph < ALPHA and n2 > 0 else (" (raw +)" if p2 < ALPHA and n2 > 0 else "")
        print(f"{k:12s} {n:7,d} {m*100:6.1f} {w*100:6.1f} {g*100:+6.2f} {c*100:6.2f} {fe*100:5.2f} "
              f"{n1*100:+11.2f} {p1:7.4f} {n2*100:+12.2f} {p2:7.4f}  {share*100:5.1f}%{flag}")


report(obs, "All touches (pts per contract; gap measured in the direction of the trade)")
report([r for r in obs if float(r["hours_left"]) >= 6], "Touches >= 6h before close")
for s in ["KXNCAAWBGAME", "KXBOXING", "KXLOLGAME", "KXDOTA2GAME"]:
    report([r for r in obs if r["series"] == s], s)
