"""Gap (won - price) by series and sport family, vs. the rest of the sample."""
import csv, math, re, sys
from collections import defaultdict
import numpy as np
from scipy import stats

ALPHA = 0.02
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))

FAMILIES = [
    ("Tennis set winner", r"SETWINNER"),
    ("Tennis doubles", r"^KX(ITF|ITFW|ATP|WTA|ATPCHALLENGER|WTACHALLENGER)DOUBLES"),
    ("Tennis singles", r"^KX(ITF|ITFW|ATP|WTA|ATPCHALLENGER|WTACHALLENGER)MATCH"),
    ("Table tennis", r"^KXTT"),
    ("Esports", r"^KX(CS2|LOL|DOTA2|VALORANT|R6|COD|OW|RL|EBASKETBALL|ESOCCER)"),
    ("Baseball", r"^KX(MLB|NPB|KBO|LMB|NCAABB|SLB|GBL)"),
    ("Basketball", r"^KX(NBA|WNBA|NCAAMB|NCAAWB|LNBP|NBL|EUROLEAGUE|BBL|ACB)"),
    ("Hockey", r"^KX(NHL|AHL|KHL|SHL)"),
    ("Cricket", r"^KX(T20|WT20|ODI|IPL|TEST)"),
    ("Volleyball", r"^KX(NCAAWV|VTB|VOLLEY)"),
    ("Soccer", r"^KX(LIGAPRO|EPL|LALIGA|MLS|UCL|SERIEA|BUNDES|LIGUE|EFL|COPA|USOPENCUP|FRA)"),
    ("Football", r"^KX(NFL|NCAAF|CFL|UFL)"),
    ("Combat sports", r"^KX(BOXING|UFC|MMA)"),
    ("Elections", r"^(KX)?(HOUSE|SENATE|GOV|LTGOV)"),
]


def family(series: str, category: str) -> str:
    for name, pat in FAMILIES:
        if re.search(pat, series):
            return name
    return "Other " + (category or "")


def obs(rs, level):
    k = f"touch_{level}"
    return [(1.0 if r["result"] == "yes" else 0.0, float(r[k])) for r in rs if r[k]]


def gap(o):
    if len(o) < 2:
        return None
    d = np.array([y - p for y, p in o])
    return d.mean(), d.std(ddof=1) / math.sqrt(len(d)), len(d)


def welch(a, b):
    da = np.array([y - p for y, p in a]); db = np.array([y - p for y, p in b])
    if len(da) < 30:
        return math.nan
    return stats.ttest_ind(da, db, equal_var=False).pvalue


def table(key_fn, min_n, label):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    groups = {g: rs for g, rs in groups.items() if len(rs) >= min_n}
    out = []
    for side, level in [("fav", 80), ("long", 20)]:
        allo = obs(rows, level)
        for g, rs in groups.items():
            o = obs(rs, level)
            rest = obs([r for r in rows if key_fn(r) != g], level)
            gg = gap(o)
            if not gg:
                continue
            m, se, n = gg
            p0 = 2 * stats.t.sf(abs(m / se), n - 1) if se > 0 else math.nan  # gap vs 0
            out.append([g, side, level, n, float(np.mean([p for _, p in o])), m, se, p0, welch(o, rest)])
    # Holm across all "differs from rest" tests in this table
    valid = sorted([x for x in out if not math.isnan(x[8])], key=lambda x: x[8])
    run = 0
    for i, x in enumerate(valid):
        run = max(run, min(1, (len(valid) - i) * x[8])); x.append(run)
    for x in out:
        if len(x) == 9:
            x.append(math.nan)
    print(f"\n=== {label} (groups with >= {min_n} events) ===")
    overall = {lvl: gap(obs(rows, lvl)) for lvl in (80, 20)}
    print(f"overall gap: touched>=80% {overall[80][0]*100:+.2f} pts, touched<=20% {overall[20][0]*100:+.2f} pts")
    print(f"{'group':28s} side lvl     n  price   gap   98%CI          p(gap=0)  p(vs rest) Holm")
    q = stats.norm.ppf(1 - ALPHA / 2)
    for x in sorted(out, key=lambda x: (x[1], -abs(x[5]))):
        g, side, lvl, n, pr, m, se, p0, pw, ph = x
        flag = " <== differs" if ph < ALPHA else (" (raw)" if pw < ALPHA else "")
        print(f"{g[:28]:28s} {side:4s} {lvl:3d} {n:6d} {pr*100:5.1f} {m*100:+6.2f}  [{(m-q*se)*100:+5.1f},{(m+q*se)*100:+5.1f}]  "
              f"{p0:8.4f}  {pw:8.4f}  {ph:6.4f}{flag}")


table(lambda r: family(r["series"], r["category"]), 300, "Sport family")
table(lambda r: r["series"], 300, "Series")
