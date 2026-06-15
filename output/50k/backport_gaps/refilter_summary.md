# Refilter to structural-only working set

clean fixes:  structural=1097, mixed=309, deletion=398, total=1804

## RQ2 (gap rate)

| Metric | All clean fixes | Structural only | Δ |
|---|---:|---:|---:|
| status=ok commits        | 1789 | 1085 | -704 |
| total release branches   | 10862 | 7195 | -3667 |
| gap branches             | 4776 | 2606 | -2170 |
| already-fixed branches   | 1711 | 1363 | -348 |
| inapplicable branches    | 4375 | 3226 | -1149 |
| **gap rate (of all)**    | **44.0%** | **36.2%** | -7.8pp |
| **gap rate (actionable)**| **73.6%** | **65.7%** | -8.0pp |
| repos with ≥1 gap        | 510 | 301 | -209 |

## RQ3 (true backports)

| Refined status | All clean fixes | Structural only |
|---|---:|---:|
| inconclusive | 126 | 94 |
| independent_prior_fix | 106 | 106 |
| never_had_it | 81 | 73 |
| same_day_fix | 1038 | 842 |
| timed_out | 118 | 74 |
| true_backport | 242 | 174 |
| **TRUE backports**       | **242** | **174** |
| repos with TRUE backports | 52 | 36 |
