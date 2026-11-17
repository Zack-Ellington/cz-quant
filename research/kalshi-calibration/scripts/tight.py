import csv, math, numpy as np
from scipy import stats
fee=lambda p: math.ceil(round(0.07*p*(1-p)*100,6))/100
rows=[r for r in csv.DictReader(open(__import__("sys").argv[1])) if r["kind"].startswith("touch")]
print("1-cent-spread quotes only (half-spread 0.5c)")
for k in sorted({r["kind"] for r in rows}, key=lambda s:int(s[6:])):
    s=[r for r in rows if r["kind"]==k and float(r["ask"])-float(r["bid"])<=0.0101]
    if len(s)<30: continue
    y=np.array([float(r["y"]) for r in s]); b=np.array([float(r["bid"]) for r in s]); a=np.array([float(r["ask"]) for r in s])
    fav=int(k[6:])>=50
    net=(y-a) if fav else (b-y); px=a if fav else 1-b
    nf=net-np.array([fee(p) for p in px])
    print(f"{k:9s} n={len(s):6,d} gap={((y-(a+b)/2) if fav else ((a+b)/2-y)).mean()*100:+.2f} net={net.mean()*100:+.2f} p={stats.ttest_1samp(net,0).pvalue:.4f} net+fee={nf.mean()*100:+.2f} p={stats.ttest_1samp(nf,0).pvalue:.4f}")
