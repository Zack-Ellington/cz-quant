# How well do Kalshi prices predict two-outcome events?

Events closing 2023-11-15 to 2026-11-15. Each event is mutually exclusive with exactly two binary markets that settled yes/no; one side per event is picked by a fixed coin flip, so each event counts once. Price = hourly YES bid/ask midpoint (spread ≤ 10¢). Markets with under 100 contracts traded are dropped. Tests are two-sided t-tests at α = 0.02.

**Events analyzed: 4,596.** Dropped: no usable quote 58, volume < 100 15.

| Category | Events |
|---|---:|
| Sports | 4,596 |

Largest series: KXNCAAWBGAME (4,595), KXNCAAWBRACE (1).

## Touched a level: all touches before close

A market touched 90% at the first hour its midpoint reached ≥ 90% (for levels under 50%: fell to ≤ the level). *Avg price* is the midpoint at that hour; if prices are accurate, *Won* should equal it.

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| ≤ 5% | 1,713 | 2.1% | 1.3% | -0.7% | -1.4% to -0.1% | -2.69 | 0.0071 | 0.1333 | yes (raw only) |
| ≤ 10% | 1,898 | 4.2% | 3.5% | -0.7% | -1.7% to 0.2% | -1.77 | 0.0768 | 1.0000 | no |
| ≤ 15% | 2,005 | 6.2% | 5.2% | -0.9% | -2.1% to 0.2% | -1.90 | 0.0576 | 0.8647 | no |
| ≤ 20% | 2,133 | 8.2% | 7.7% | -0.5% | -1.8% to 0.8% | -0.84 | 0.4031 | 1.0000 | no |
| ≤ 25% | 2,239 | 10.4% | 10.2% | -0.2% | -1.7% to 1.2% | -0.37 | 0.7128 | 1.0000 | no |
| ≤ 30% | 2,338 | 12.8% | 12.4% | -0.4% | -1.9% to 1.1% | -0.61 | 0.5404 | 1.0000 | no |
| ≤ 35% | 2,449 | 15.1% | 14.7% | -0.4% | -2.0% to 1.2% | -0.60 | 0.5471 | 1.0000 | no |
| ≤ 40% | 2,548 | 17.3% | 17.1% | -0.2% | -1.8% to 1.4% | -0.26 | 0.7950 | 1.0000 | no |
| ≤ 45% | 2,670 | 19.9% | 19.8% | -0.1% | -1.8% to 1.6% | -0.13 | 0.8986 | 1.0000 | no |
| ≥ 50% | 2,763 | 78.1% | 77.9% | -0.2% | -1.9% to 1.5% | -0.27 | 0.7845 | 1.0000 | no |
| ≥ 55% | 2,640 | 80.4% | 80.6% | 0.2% | -1.5% to 1.8% | 0.22 | 0.8236 | 1.0000 | no |
| ≥ 60% | 2,537 | 82.6% | 82.7% | 0.0% | -1.6% to 1.6% | 0.02 | 0.9833 | 1.0000 | no |
| ≥ 65% | 2,431 | 85.0% | 85.3% | 0.3% | -1.3% to 1.8% | 0.39 | 0.6954 | 1.0000 | no |
| ≥ 70% | 2,319 | 87.4% | 88.2% | 0.7% | -0.7% to 2.2% | 1.17 | 0.2425 | 1.0000 | no |
| ≥ 75% | 2,219 | 89.6% | 90.5% | 0.9% | -0.5% to 2.3% | 1.56 | 0.1187 | 1.0000 | no |
| ≥ 80% | 2,118 | 91.8% | 93.0% | 1.3% | -0.0% to 2.5% | 2.31 | 0.0211 | 0.3595 | no |
| ≥ 85% | 2,028 | 93.7% | 94.7% | 1.0% | -0.1% to 2.1% | 2.03 | 0.0429 | 0.6857 | no |
| ≥ 90% | 1,900 | 95.9% | 96.6% | 0.7% | -0.2% to 1.7% | 1.72 | 0.0856 | 1.0000 | no |
| ≥ 95% | 1,693 | 98.1% | 98.8% | 0.7% | 0.1% to 1.3% | 2.70 | 0.0070 | 0.1333 | yes (raw only) |

## Touched a level: touches at least 6h before close

A market touched 90% at the first hour its midpoint reached ≥ 90% (for levels under 50%: fell to ≤ the level). *Avg price* is the midpoint at that hour; if prices are accurate, *Won* should equal it.

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| ≤ 5% | 515 | 3.6% | 3.3% | -0.3% | -2.1% to 1.6% | -0.32 | 0.7474 | 1.0000 | no |
| ≤ 10% | 829 | 6.2% | 4.8% | -1.4% | -3.1% to 0.3% | -1.89 | 0.0589 | 1.0000 | no |
| ≤ 15% | 1,043 | 8.2% | 7.0% | -1.2% | -3.0% to 0.6% | -1.54 | 0.1231 | 1.0000 | no |
| ≤ 20% | 1,217 | 10.0% | 9.3% | -0.7% | -2.6% to 1.2% | -0.87 | 0.3830 | 1.0000 | no |
| ≤ 25% | 1,387 | 12.0% | 11.8% | -0.2% | -2.2% to 1.7% | -0.29 | 0.7710 | 1.0000 | no |
| ≤ 30% | 1,554 | 14.0% | 14.0% | 0.1% | -1.9% to 2.0% | 0.08 | 0.9399 | 1.0000 | no |
| ≤ 35% | 1,716 | 16.0% | 15.7% | -0.3% | -2.2% to 1.7% | -0.31 | 0.7579 | 1.0000 | no |
| ≤ 40% | 1,861 | 18.0% | 17.8% | -0.2% | -2.1% to 1.8% | -0.20 | 0.8377 | 1.0000 | no |
| ≤ 45% | 2,020 | 20.1% | 20.2% | 0.2% | -1.8% to 2.1% | 0.18 | 0.8564 | 1.0000 | no |
| ≥ 50% | 2,153 | 77.5% | 77.1% | -0.4% | -2.3% to 1.5% | -0.48 | 0.6294 | 1.0000 | no |
| ≥ 55% | 2,001 | 79.7% | 79.5% | -0.2% | -2.1% to 1.8% | -0.19 | 0.8502 | 1.0000 | no |
| ≥ 60% | 1,858 | 81.6% | 81.2% | -0.4% | -2.4% to 1.6% | -0.48 | 0.6289 | 1.0000 | no |
| ≥ 65% | 1,700 | 83.7% | 83.9% | 0.3% | -1.7% to 2.2% | 0.30 | 0.7650 | 1.0000 | no |
| ≥ 70% | 1,531 | 85.8% | 86.7% | 0.9% | -1.0% to 2.9% | 1.10 | 0.2697 | 1.0000 | no |
| ≥ 75% | 1,377 | 87.7% | 89.2% | 1.5% | -0.5% to 3.4% | 1.78 | 0.0749 | 1.0000 | no |
| ≥ 80% | 1,222 | 89.5% | 90.8% | 1.3% | -0.6% to 3.2% | 1.55 | 0.1221 | 1.0000 | no |
| ≥ 85% | 1,045 | 91.5% | 92.5% | 1.0% | -0.9% to 2.9% | 1.26 | 0.2082 | 1.0000 | no |
| ≥ 90% | 812 | 94.0% | 94.6% | 0.6% | -1.2% to 2.5% | 0.78 | 0.4330 | 1.0000 | no |
| ≥ 95% | 494 | 96.8% | 97.0% | 0.2% | -1.6% to 2.0% | 0.25 | 0.8021 | 1.0000 | no |

## Price 24h before close

2,310 events had a quote within 24h before that point. Brier score 0.1019 (0 = perfect, 0.25 = coin flip).

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 0%–10% | 575 | 4.3% | 3.1% | -1.2% | -2.8% to 0.5% | -1.61 | 0.1087 | 0.8693 | no |
| 10%–20% | 248 | 13.9% | 10.9% | -3.0% | -7.6% to 1.5% | -1.56 | 0.1211 | 0.8693 | no |
| 20%–30% | 145 | 24.4% | 23.4% | -1.0% | -9.3% to 7.3% | -0.28 | 0.7827 | 1.0000 | no |
| 30%–40% | 100 | 34.9% | 35.0% | 0.1% | -11.3% to 11.5% | 0.02 | 0.9835 | 1.0000 | no |
| 40%–50% | 96 | 44.1% | 35.4% | -8.7% | -20.2% to 2.8% | -1.78 | 0.0780 | 0.7018 | no |
| 50%–60% | 92 | 54.6% | 48.9% | -5.7% | -18.2% to 6.7% | -1.09 | 0.2803 | 1.0000 | no |
| 60%–70% | 123 | 65.3% | 62.6% | -2.7% | -13.0% to 7.6% | -0.61 | 0.5419 | 1.0000 | no |
| 70%–80% | 117 | 74.3% | 71.8% | -2.5% | -12.3% to 7.3% | -0.61 | 0.5454 | 1.0000 | no |
| 80%–90% | 287 | 85.3% | 87.1% | 1.8% | -2.8% to 6.4% | 0.90 | 0.3693 | 1.0000 | no |
| 90%–100% | 527 | 95.5% | 97.0% | 1.4% | -0.3% to 3.2% | 1.92 | 0.0555 | 0.5553 | no |

Slope of outcome on price: **1.032** (SE 0.013); t-test of slope = 1: t = 2.43, p = 0.0153 → **significantly different from 1** at α = 0.02. Slope > 1 means favorites win more often than priced and longshots less (favorite-longshot bias); slope < 1 means prices are too extreme.

## Price 7d before close

50 events had a quote within 48h before that point. Brier score 0.0384 (0 = perfect, 0.25 = coin flip).

| Level | n | Avg price | Won | Won − price | 98% CI | t | p | Holm p | Miscalibrated at 0.02? |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 0%–10% | 18 | 3.1% | 0.0% | -3.1% | -4.0% to -2.1% | -8.26 | <0.0001 | <0.0001 | **yes (Holm)** |
| 10%–20% | 2 | 17.2% | 0.0% | -17.2% | -72.9% to 38.4% | -9.86 | 0.0644 | 0.0977 | no |
| 20%–30% | 2 | 22.2% | 100.0% | 77.8% | 53.9% to 101.6% | 103.67 | 0.0061 | 0.0184 | **yes (Holm)** |
| 30%–40% | 0 | – | – | – | – to – | – | – | – | too few |
| 40%–50% | 0 | – | – | – | – to – | – | – | – | too few |
| 50%–60% | 1 | 55.5% | 0.0% | -55.5% | – to – | – | – | – | too few |
| 60%–70% | 1 | 69.5% | 100.0% | 30.5% | – to – | – | – | – | too few |
| 70%–80% | 2 | 77.2% | 100.0% | 22.7% | -32.9% to 78.4% | 13.00 | 0.0489 | 0.0977 | no |
| 80%–90% | 6 | 87.2% | 100.0% | 12.8% | 9.7% to 16.0% | 13.57 | <0.0001 | 0.0002 | **yes (Holm)** |
| 90%–100% | 18 | 97.4% | 100.0% | 2.6% | 1.1% to 4.1% | 4.47 | 0.0003 | 0.0013 | **yes (Holm)** |

Slope of outcome on price: **1.054** (SE 0.042); t-test of slope = 1: t = 1.28, p = 0.2068 → not significantly different from 1 at α = 0.02. Slope > 1 means favorites win more often than priced and longshots less (favorite-longshot bias); slope < 1 means prices are too extreme.

## By category (price 24h before close)

| Category | n | Brier | Slope | p (slope = 1) | Significant? |
|---|---:|---:|---:|---:|---|
| Sports | 2,310 | 0.1019 | 1.032 | 0.0153 | yes |
