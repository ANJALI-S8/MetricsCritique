# Statistical Review — Appendix

Companion to `docs/STATISTICAL_REVIEW.md`. Machine-readable source of truth:
`reports/stat_review_results.json` (sections D1–D5, B, C, MT, E).

v2 operational params and self-checks: `data/metrics_stage/run_params.json`.

---

## Constants (review harness)

| Name | Value |
|------|------:|
| seed | 20260729 |
| WINDOW_START | 2026-06-14 |
| BASELINE_WIN (review recommend) | 21d |
| SCAN_L | 3d |
| N_SHUFFLE_NULL | 200 |
| N_PERM_BURST | 2000 |
| N_INJECT | 400 |
| TS_EXCLUDE | snapchat, pinterest, whatsapp |

---

## D1 — max |z| by metric (v1)

| metric | max \|z\| |
|--------|----------:|
| intensity_z | 143.1 |
| tone_z | 82.0 |
| hate_z | ~4.1×10⁴ |
| virality_z | ~9×10⁹ |

intensity_z exactly 0.0: **5,976 / 7,160** non-null (83.5%) — degenerate MAD path.

---

## D2 — panel summary (v1)

| Statistic | Value |
|-----------|------:|
| n_cells | 7,340 |
| V≤2 pct | 91.28% |
| V=0 cells | 6,166 |
| V≥5 in window | 447 |
| distinct days | 5,516 |
| posts/cell | 2.77 |
| Kish n_eff | 133.4 |
| modellable series | 25 |

---

## D3 — pre-window mass by platform (v1)

| platform | pre-window cells | sum V |
|----------|-----------------:|------:|
| youtube_video | 5,458 | 282 |
| telegram | 392 | 583 |
| reddit | 156 | 6 |

Baseline contamination (in-window cells): mean fraction of trailing-28d baseline that is pre-window ≈ **0.146**; cells with ≥50% pre-window baseline: **164**.

---

## D4 — corrected null (review)

Apples-to-apples unweighted per-group means, in-window, 200 shuffles:

| | real mean \|Δ log\| | shuffled mean \|Δ log\| |
|--|--------------------:|------------------------:|
| corrected null | 0.535 | 1.009 |

Interpretation: shuffled changes more → temporal structure present.

v2 stage null_control: 8/8 series `temporal_structure_supported=true`.

---

## D5 — isolation experiment

| Path | ema 5/14 | ema 7/21 | ema 7/14 |
|------|---------:|---------:|---------:|
| Drop leading growth (v1 bug) | 0.7916 | **0.7913** | 0.7839 |
| Keep lead alignment | 0.9849 | **1.0** | 0.9973 |

Self-test after fix: `corr(default, default) = 1.0`.

---

## B — Poisson vs NB (25 modellable series)

| Metric | Value |
|--------|------:|
| NB wins AIC | 24/25 |
| NB wins BIC | 24/25 |
| LR p&lt;0.05 | 24 |
| Cameron–Trivedi p&lt;0.05 | 14 |
| θ median | 0.546 |
| θ IQR | 0.27–0.89 |
| Pearson dispersion median | 7.40 |

---

## C — noise amplification

| Ratio | mean |
|-------|-----:|
| sd(accel)/sd(growth) | 1.55 |
| iid theory √3 | 1.73 |
| implied noise share of accel | ~0.75 |
| acf1(accel) mean | −0.50 (theory −2/3 for iid 2nd diff) |

SavGol reduces noise vs raw 2nd difference (see bake-off in JSON section C).

---

## MT — effective tests / day

| Estimator | Value |
|-----------|------:|
| M cells tested / day (raw) | 22 |
| Nyholt–Cheverud M_eff | 17.8 |
| Participation ratio | 4.23 |
| Li–Ji | 9.63 |
| PCs for 95% var | 12 |
| First PC variance share | 0.435 |

FDR: BH within day (v2 default); BY available as sensitivity variant.

---

## E — extra defect notes (abbrev.)

1. **E1 timezone:** ~18% of posts would shift calendar day under IST; LinkedIn 100% midnight UTC timestamps.  
2. **E2 effort join:** `collection_effort` is scrape-day; cannot join naively to posted_day (v2 uses observed authors/channels).  
3. **E3 ramp:** collector effort variance can dominate narrative variance on raw V.  
4. **E4 virality_z:** self-inclusion + epsilon pathologies in v1.  
5. **E5 growth_share epsilon** inconsistent with growth α.  
6. **E9 half-life:** R² on wrong scale; absurd t½ accepted.  
7. **E11 age norm:** R² near 0 — abandoned in v2 for percentiles.  
8. **E12 snapshots:** insufficient multi-extract history for per-post decay.  
9. **E14 seasonality:** no weekly seasonality detected → no DOW term required in baselines yet.

---

## v2 self-checks (all true at last run)

Including: no look-ahead in trailing baselines; causal SavGol no future leakage; NB recovers μ/θ; D1 single/two-post not extreme while real spike is; BH monotone; EB shrinks n=1; AR(1) recovers φ; Kleinberg/CUSUM routing; permutation D4 direction.

Full map: `data/metrics_stage/run_params.json` → `self_checks`.

---

## Artifacts

| Path | Role |
|------|------|
| `reports/stat_review_results.json` | Full harness dump |
| `data/metrics_stage/run_params.json` | v2 params + diagnostics |
| `data/metrics.duckdb` | Published metrics |
| `reports/METRICS_V2_SPEC_RESULTS.md` | Publish report from 08 |
| `UI/index.html` | Offline dashboard (real mode) |
