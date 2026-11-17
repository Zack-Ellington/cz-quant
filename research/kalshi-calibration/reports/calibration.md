# How well do Kalshi prices predict two-outcome events?

Events closing 2026-05-14 to 2026-11-13. Each event is mutually exclusive with exactly two binary markets that settled yes/no; one side per event is picked by a fixed coin flip, so each event counts once. Price = hourly YES bid/ask midpoint (spread ≤ 10¢). Markets with under 100 contracts traded are dropped. Tests are two-sided t-tests at α = 0.02.

**Events analyzed: 113,242.** Dropped: no usable quote 17,208, volume < 100 7,469.

| Category | Events |
|---|---:|
| Sports | 112,431 |
| Elections | 784 |
| Entertainment | 22 |
| Crypto | 2 |
| Financials | 2 |
| Politics | 1 |

Largest series: KXITFMATCH (16,255), KXITFWMATCH (15,281), KXCS2MAP (6,875), KXATPCHALLENGERMATCH (6,536), KXCS2GAME (5,735), KXATPSETWINNER (4,674), KXWTASETWINNER (4,282), KXTTELITEMATCH (3,682), KXLOLMAP (2,982), KXITFDOUBLES (2,717).

## Touched a level: all touches before close

A market touched 90% at the first hour its midpoint reached ≥ 90% (for levels under 50%: fell to ≤ the level). *Avg price* is the midpoint at that hour; if prices are accurate, *Won* should equal it.

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| ≤ 5% | 20,476 | 1.9% | 1.2% | -0.6% | -0.8% to -0.5% | -8.32 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≤ 10% | 27,597 | 4.3% | 3.5% | -0.8% | -1.1% to -0.6% | -7.55 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≤ 15% | 33,047 | 6.9% | 6.1% | -0.7% | -1.0% to -0.4% | -5.73 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≤ 20% | 38,053 | 9.7% | 9.2% | -0.6% | -0.9% to -0.2% | -3.99 | <0.0001 | 0.0004 | **yes (Holm)** |
| ≤ 25% | 43,100 | 12.8% | 12.4% | -0.5% | -0.8% to -0.1% | -3.18 | 0.0015 | 0.0074 | **yes (Holm)** |
| ≤ 30% | 48,290 | 16.2% | 15.8% | -0.4% | -0.7% to 0.0% | -2.25 | 0.0243 | 0.0972 | no |
| ≤ 35% | 53,919 | 19.8% | 19.5% | -0.3% | -0.6% to 0.1% | -1.58 | 0.1151 | 0.3452 | no |
| ≤ 40% | 60,030 | 23.6% | 23.5% | -0.1% | -0.5% to 0.3% | -0.70 | 0.4855 | 0.9711 | no |
| ≤ 45% | 66,354 | 27.4% | 27.3% | -0.1% | -0.5% to 0.3% | -0.67 | 0.5050 | 0.9711 | no |
| ≥ 50% | 72,904 | 69.0% | 70.0% | 1.0% | 0.7% to 1.4% | 6.49 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 55% | 66,489 | 72.5% | 73.6% | 1.1% | 0.7% to 1.4% | 6.52 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 60% | 59,843 | 76.4% | 77.4% | 1.1% | 0.7% to 1.4% | 6.49 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 65% | 53,822 | 80.2% | 81.2% | 1.0% | 0.6% to 1.3% | 6.01 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 70% | 48,308 | 83.8% | 84.9% | 1.0% | 0.7% to 1.4% | 6.51 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 75% | 43,146 | 87.2% | 88.2% | 1.0% | 0.6% to 1.3% | 6.44 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 80% | 38,114 | 90.3% | 91.3% | 0.9% | 0.6% to 1.3% | 6.70 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 85% | 32,952 | 93.2% | 94.0% | 0.8% | 0.5% to 1.1% | 6.32 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 90% | 27,498 | 95.8% | 96.6% | 0.8% | 0.5% to 1.0% | 7.20 | <0.0001 | <0.0001 | **yes (Holm)** |
| ≥ 95% | 20,685 | 98.3% | 98.8% | 0.6% | 0.4% to 0.7% | 7.43 | <0.0001 | <0.0001 | **yes (Holm)** |

## Touched a level: touches at least 6h before close

A market touched 90% at the first hour its midpoint reached ≥ 90% (for levels under 50%: fell to ≤ the level). *Avg price* is the midpoint at that hour; if prices are accurate, *Won* should equal it.

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| ≤ 5% | 2,105 | 3.4% | 2.5% | -1.0% | -1.7% to -0.2% | -2.83 | 0.0047 | 0.0474 | yes (raw only) |
| ≤ 10% | 5,281 | 7.0% | 5.8% | -1.2% | -1.9% to -0.4% | -3.69 | 0.0002 | 0.0041 | **yes (Holm)** |
| ≤ 15% | 8,107 | 9.8% | 8.8% | -1.0% | -1.7% to -0.2% | -3.09 | 0.0020 | 0.0278 | yes (raw only) |
| ≤ 20% | 11,470 | 12.8% | 12.2% | -0.6% | -1.3% to 0.1% | -1.91 | 0.0559 | 0.3916 | no |
| ≤ 25% | 15,531 | 16.0% | 15.6% | -0.5% | -1.1% to 0.2% | -1.67 | 0.0953 | 0.5715 | no |
| ≤ 30% | 20,259 | 19.4% | 19.1% | -0.3% | -0.9% to 0.4% | -0.99 | 0.3208 | 1.0000 | no |
| ≤ 35% | 25,844 | 22.8% | 22.8% | -0.0% | -0.6% to 0.6% | -0.05 | 0.9581 | 1.0000 | no |
| ≤ 40% | 32,471 | 26.4% | 26.4% | 0.0% | -0.5% to 0.6% | 0.14 | 0.8915 | 1.0000 | no |
| ≤ 45% | 39,839 | 29.9% | 29.9% | 0.0% | -0.5% to 0.5% | 0.15 | 0.8781 | 1.0000 | no |
| ≥ 50% | 45,961 | 66.4% | 67.0% | 0.6% | 0.1% to 1.1% | 2.90 | 0.0038 | 0.0416 | yes (raw only) |
| ≥ 55% | 38,775 | 69.5% | 70.2% | 0.7% | 0.2% to 1.2% | 3.09 | 0.0020 | 0.0278 | yes (raw only) |
| ≥ 60% | 31,429 | 73.0% | 73.7% | 0.7% | 0.2% to 1.3% | 2.96 | 0.0031 | 0.0370 | yes (raw only) |
| ≥ 65% | 24,839 | 76.6% | 77.3% | 0.7% | 0.1% to 1.3% | 2.70 | 0.0070 | 0.0596 | yes (raw only) |
| ≥ 70% | 19,307 | 80.1% | 80.9% | 0.8% | 0.1% to 1.4% | 2.72 | 0.0066 | 0.0596 | yes (raw only) |
| ≥ 75% | 14,658 | 83.5% | 84.5% | 1.0% | 0.3% to 1.7% | 3.44 | 0.0006 | 0.0095 | **yes (Holm)** |
| ≥ 80% | 10,715 | 86.7% | 87.8% | 1.1% | 0.4% to 1.8% | 3.51 | 0.0004 | 0.0076 | **yes (Holm)** |
| ≥ 85% | 7,540 | 89.7% | 90.9% | 1.1% | 0.4% to 1.9% | 3.42 | 0.0006 | 0.0096 | **yes (Holm)** |
| ≥ 90% | 4,719 | 92.8% | 94.2% | 1.4% | 0.6% to 2.2% | 3.99 | <0.0001 | 0.0013 | **yes (Holm)** |
| ≥ 95% | 1,877 | 96.6% | 97.0% | 0.4% | -0.6% to 1.3% | 0.93 | 0.3522 | 1.0000 | no |

## Price 24h before close

29,504 events had a quote within 24h before that point. Brier score 0.1985 (0 = perfect, 0.25 = coin flip).

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 0%–10% | 1,415 | 4.6% | 3.7% | -0.9% | -2.0% to 0.3% | -1.75 | 0.0810 | 0.6484 | no |
| 10%–20% | 1,502 | 15.1% | 13.5% | -1.6% | -3.7% to 0.4% | -1.84 | 0.0665 | 0.5985 | no |
| 20%–30% | 2,470 | 25.3% | 24.1% | -1.1% | -3.1% to 0.9% | -1.33 | 0.1841 | 0.9205 | no |
| 30%–40% | 3,727 | 35.2% | 35.0% | -0.2% | -2.0% to 1.6% | -0.22 | 0.8239 | 1.0000 | no |
| 40%–50% | 5,462 | 44.9% | 46.0% | 1.2% | -0.4% to 2.7% | 1.74 | 0.0824 | 0.6484 | no |
| 50%–60% | 5,665 | 54.8% | 54.5% | -0.2% | -1.8% to 1.3% | -0.36 | 0.7164 | 1.0000 | no |
| 60%–70% | 3,901 | 64.4% | 64.4% | -0.0% | -1.8% to 1.7% | -0.06 | 0.9499 | 1.0000 | no |
| 70%–80% | 2,497 | 74.4% | 75.9% | 1.5% | -0.5% to 3.4% | 1.70 | 0.0886 | 0.6484 | no |
| 80%–90% | 1,596 | 84.6% | 85.0% | 0.3% | -1.8% to 2.4% | 0.35 | 0.7246 | 1.0000 | no |
| 90%–100% | 1,269 | 95.3% | 96.8% | 1.6% | 0.4% to 2.7% | 3.24 | 0.0012 | 0.0124 | **yes (Holm)** |

Slope of outcome on price: **1.028** (SE 0.009); t-test of slope = 1: t = 3.16, p = 0.0016 → **significantly different from 1** at α = 0.02. Slope > 1 means favorites win more often than priced and longshots less (favorite-longshot bias); slope < 1 means prices are too extreme.

## Price 7d before close

2,098 events had a quote within 48h before that point. Brier score 0.1169 (0 = perfect, 0.25 = coin flip).

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 0%–10% | 463 | 3.9% | 0.9% | -3.1% | -4.1% to -2.1% | -7.14 | <0.0001 | <0.0001 | **yes (Holm)** |
| 10%–20% | 152 | 14.7% | 13.8% | -0.9% | -7.5% to 5.7% | -0.31 | 0.7594 | 1.0000 | no |
| 20%–30% | 122 | 24.9% | 26.2% | 1.3% | -8.1% to 10.7% | 0.34 | 0.7376 | 1.0000 | no |
| 30%–40% | 160 | 34.9% | 31.9% | -3.1% | -11.8% to 5.7% | -0.82 | 0.4135 | 1.0000 | no |
| 40%–50% | 151 | 44.9% | 43.7% | -1.2% | -10.7% to 8.4% | -0.28 | 0.7772 | 1.0000 | no |
| 50%–60% | 173 | 54.7% | 48.6% | -6.2% | -15.1% to 2.8% | -1.62 | 0.1075 | 0.8599 | no |
| 60%–70% | 125 | 64.6% | 64.8% | 0.2% | -9.9% to 10.3% | 0.05 | 0.9636 | 1.0000 | no |
| 70%–80% | 156 | 75.3% | 72.4% | -2.8% | -11.2% to 5.6% | -0.79 | 0.4318 | 1.0000 | no |
| 80%–90% | 157 | 84.9% | 88.5% | 3.6% | -2.3% to 9.5% | 1.44 | 0.1525 | 1.0000 | no |
| 90%–100% | 439 | 96.1% | 98.4% | 2.3% | 0.9% to 3.7% | 3.91 | 0.0001 | 0.0010 | **yes (Holm)** |

Slope of outcome on price: **1.046** (SE 0.013); t-test of slope = 1: t = 3.47, p = 0.0005 → **significantly different from 1** at α = 0.02. Slope > 1 means favorites win more often than priced and longshots less (favorite-longshot bias); slope < 1 means prices are too extreme.

## By category (price 24h before close)

| Category | n | Brier | Slope | p (slope = 1) | Significant? |
|---|---:|---:|---:|---:|---|
| Sports | 28,746 | 0.2027 | 1.026 | 0.0076 | yes |
| Elections | 742 | 0.0384 | 1.041 | <0.0001 | yes |
| Entertainment | 12 | 0.0110 | 1.117 | 0.0376 | no |
| Financials | 2 | – | – | – | too few |
| Crypto | 1 | – | – | – | too few |
| Politics | 1 | – | – | – | too few |
