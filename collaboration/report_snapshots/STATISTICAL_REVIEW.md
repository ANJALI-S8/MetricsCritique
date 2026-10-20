# Statistical Review — Layer-4 Metrics (v1 defects → v2 closure)

**Status:** empirical review complete (seed `20260729`)  
**Harness:** `scripts/review/stat_review.py` → `reports/stat_review_results.json`  
**Contract:** `docs/METRICS_V2_SPEC.md`  
**v2 publish:** `data/metrics.duckdb` (via `07_metrics_v2.py` + `08_metrics_db.py`)

This document verifies the five defects called out against v1 (`analytics.duckdb.metrics_daily`, 7,340 cells), states root causes, and records whether Metrics v2 closes them.

---

## Executive summary

| Defect | Verdict on v1 | Closed by v2? | Evidence |
|--------|---------------|---------------|----------|
| **D1** robust-z explosion | **Confirmed** | **Yes** | v1 max \|z\|=143 on V≤2; v2 max surprise=3.11, **0** non-null surprise on V≤2 |
| **D2** panel 91% sparse | **Confirmed** | **Mitigated** | v1 6,700/7,340 V≤2; v2 1,522 cells, 345 support-gated publishable |
| **D3** 82% outside collection window | **Confirmed** | **Yes** | v1 6,006 pre-2026-06 cells; v2 `in_window` + trends only on 2026-06-14→07-28 |
| **D4** null-control inverted | **Confirmed** (logic bug) | **Yes** | Corrected: real \|Δ\| &lt; shuffled \|Δ\| ⇒ temporal structure; all 8 v2 series support structure |
| **D5** sensitivity self-test fails | **Confirmed** (code bug) | **Yes** | Root cause: drop of leading 0 growth; v2 `SELFTEST_default_vs_default` corr = **1.0** |

**Distributional assumption:** Negative Binomial strongly preferred over Poisson (24/25 series by AIC/BIC; median θ≈0.55). v2 NB exceedance is the correct family.

**Alerting:** Prefer **topic×platform×day** with FDR q-values; hashtags cover only ~18% of posts (Telegram ~16%). See `docs/ALERTING_KEYING.md`.

---

## D1 — Robust-z on sparse cells

### v1 evidence (review harness)

| V bucket | cells | max \|intensity_z\| | frac \|z\|&gt;5 |
|----------|------:|--------------------:|--------------:|
| 0 | 6,166 | **143.1** | 0.3% |
| 1–2 | 534 | 19.9 | 7.1% |
| 3–4 | 136 | 21.2 | 7.4% |
| 5–9 | 167 | 21.6 | 3.0% |
| 10+ | 337 | 22.5 | 3.3% |

Also: max \|tone_z\|≈82, max \|hate_z\|≈4e4, max \|virality_z\|≈9e9 — scale statistics on near-zero MAD.

**Silent zero bug:** 83.5% of non-null `intensity_z` are exactly **0.0** because `robust_z()` returned 0 when baseline MAD and std were both 0 — so a series of zeros followed by a spike could look “normal.”

Top-50 by intensity_z: **29** have V≤2; **28** outside collection window → leaderboards were mostly artifacts.

### v2 closure (`run_params.d1_before_after`)

| | v1 | v2 |
|--|---:|---:|
| cells | 7,340 | 1,522 |
| V≤2 cells | 6,700 (91%) | 882 |
| non-null “intensity-like” on V≤2 | many extreme z | **0** non-null surprise |
| max extreme metric | \|z\|=143 | surprise=3.11 (p≈7.8e-4) |
| support gate | none | baseline_days≥14 ∧ baseline_mean≥0.5 ∧ V≥3 |

**Verdict:** D1 closed. Publish **NB surprise** (`−log10 P(X≥V)`), never MAD-z on counts.

---

## D2 — Panel sparsity

### v1

- 91.3% cells V≤2; only **447** cells with V≥5 *and* in collection window  
- 20,297 posts / 7,340 cells ≈ **2.8 posts/cell**  
- Kish effective n ≈ 133; modellable series (len≥20, vol≥20) ≈ **25**

### v2

- Continuous daily grid only where needed; support gate leaves **345** cells with non-null surprise  
- Time-series metrics are honest about evidence (`support_ok=false` → NULL)

**Verdict:** Grain remains fine for OSINT facets, but **gated**. Do not interpret null cells as “quiet” without reading `support_ok`.

---

## D3 — Window pollution

### v1

- Panel spans **2011-06-22 → 2026-07-28** (5,516 distinct days)  
- **6,006 / 7,340** cells pre-2026-06 (mostly YouTube `published_at` backfill)  
- Trailing baselines for in-window cells: mean **14.6%** of baseline days are pre-window; **164** cells ≥50% contaminated; median baseline median all-time = **0** (zero-fill)

### v2

- `window_start = 2026-06-14`, `window_end = 2026-07-28`  
- Growth / accel / burst / surprise use **in_window** only  
- Historical posts retained for cross-section (tone, ontology), flagged, not in trends

**Verdict:** D3 closed for trend metrics.

---

## D4 — Null control direction

### v1 bug

Reported `real_mean_abs_growth=0.142` vs `shuffled=0.768` with note “if real ≫ shuffle, structure exists” and `ok: true`. The **observation was the opposite of the stated pass condition** — but actually *correctly* indicates a smooth (autocorrelated) series. The criterion was inverted; the flag was meaningless.

### Corrected test (review + v2)

- Expect **shuffled \|Δ\| > real \|Δ\|** under temporal structure  
- Primary evidence: Ljung–Box + lag-1..7 ACF CIs + permutation of burst statistic  

v2 `null_control` (8 densest series): **8/8** `temporal_structure_supported`; mean \|Δ\|_real ≈ 19 vs shuffled ≈ 52 (on volume scale used in stage).

**Verdict:** D4 closed (logic fixed; structure present on dense Telegram/ME series).

---

## D5 — Sensitivity harness bug

### v1 symptom

`ema_7_21_corr_vs_default ≈ 0.791` — but **7/21 is the default**, so self-correlation must be **1.0**. All three “stability” numbers (~0.78–0.79) were untrustworthy.

### Root cause (isolated by review)

In `06_validate.py` sensitivity recomputation, growth was built as:

```text
growth = log((v[1:]+1)/(v[:-1]+1))   # length n-1, drops day 0
```

then aligned against production `momentum` which includes a leading value. **Dropping the leading 0.0** (or misaligning series) produced corr≈0.79 even for identical EMA params.

Restoring alignment → `corr(default, default) = 1.0`. Corrected pairwise stability (in-window series):

| Variant | mean corr vs default 7/21 |
|---------|--------------------------:|
| 5/14 | ~0.985 |
| 7/21 self | **1.0** |
| 7/14 | ~0.997 |

### v2

Sensitivity table includes mandatory:

`SELFTEST_default_vs_default` → corr_momentum = **1.0**, corr_surprise = **1.0**, corr_accel_smooth = **1.0**.

Real sensitivity (supported cells):

| Variant | corr_momentum | corr_surprise | corr_accel_smooth |
|---------|--------------:|--------------:|------------------:|
| ema_5_14 | 0.983 | 1.0 | 1.0 |
| ema_10_28 | 0.989 | 1.0 | 1.0 |
| baseline_21 | 1.0 | 0.990 | 1.0 |
| baseline_35 | 1.0 | 0.997 | 1.0 |
| savgol_7 | 1.0 | 1.0 | **0.456** |

**Note:** smoothed acceleration is **parameter-sensitive** to Savitzky–Golay window (expected). Treat `accel_smooth` as a family member with documented window, not an invariant.

**Verdict:** D5 closed; v1 VALIDATION sensitivity numbers should be ignored.

---

## B — Count model choice

- **24/25** modellable series: NB beats Poisson on AIC and BIC; LR p&lt;0.05 on 24  
- Median dispersion θ ≈ **0.55** (heavy overdispersion; Pearson dispersion median ≈ 7.4)  
- Zero inflation: plain NB already explains most zeros at this grain  

→ F1 (NB exceedance) is empirically justified. Low θ reduces power — surprise will be conservative; that is preferable to D1 false alarms.

---

## C — Acceleration family

- Raw 2nd differences amplify noise (~1.55× sd vs growth; ~75% of accel variance is differencing noise)  
- Causal Savitzky–Golay is the publishable curvature metric  
- Kleinberg used where series long enough; Poisson CUSUM fallback otherwise (see `burst_method` column)  
- Novelty needs embeddings; YouTube videos still largely unembedded — novelty coverage incomplete for that platform  

---

## Multiple testing & alerts

- Effective independent tests/day ≪ raw cell count (Nyholt ~18, participation ratio ~4)  
- v2 applies BH within day on mid-p exceedance p-values (`fdr_alpha=0.05`)  
- Published `alert_candidates`: **34** (11 high, 23 medium) at handoff  

Dashboard + `UI/data/metrics.json` expose these for a future notifier.

---

## Additional defects found (section E) — partially addressed in v2

| ID | Issue | v2 status |
|----|--------|-----------|
| E2 | `collection_effort` is scrape-day grain, not posted-day | v2 uses **observed** active channels per platform-day (corr 0.77 with reported where both exist) |
| E3 | Collector ramp dominates raw volume | F2 log-rate / V_rate |
| E4 | virality_z pathologies | F9 age-band percentile |
| E9 | half-life R² gate weak | F7 AR(1) + episode-scoped decay |
| E11 | age-curve R²≈0 | parametric age-norm abandoned |
| E12 | engagement_snapshots single extract pair | still immature for per-post half-life |
| E1 | timezone day boundaries | documented; not remapped |

Full tables: `docs/STATISTICAL_APPENDIX.md` and `reports/stat_review_results.json`.

---

## Recommendations (actionable)

1. **Use `data/metrics.duckdb` for all analysis and alerting** — not v1 `metrics_daily` in analytics.duckdb.  
2. **Key alerts on `topic_id` (+ platform + day)** via `alert_candidates` / `surprise_q` / `emergence_score`. Hashtags secondary only.  
3. **Do not publish `hate_*`** until calibration labels exist (`hate_calibrated=false`).  
4. Re-run `07` → `08` after each Layer-0 extract if the window end moves.  
5. When GPU free: embed YouTube videos to improve novelty/ontology coverage.  
6. Cron Layer-0 extracts so `engagement_snapshots` accrues multi-observation history.

---

## Reproduction

```bash
cd /home/hariramanan/NTRO/EDA
source .venv/bin/activate
python scripts/review/stat_review.py --json reports/stat_review_results.json
# v2 pipeline
python scripts/07_metrics_v2.py   # if stage stale
python scripts/08_metrics_db.py
python UI/build_dashboard.py
```
