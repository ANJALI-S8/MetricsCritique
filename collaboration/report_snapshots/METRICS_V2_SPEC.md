# Metrics v2 — Statistical Redesign & Schema Contract

Status: **contract** — Layer-4 v2. Supersedes `reports/METRICS_SPEC.md` (v1).
Owner: this document is the shared interface between the metrics builder, the
statistical-review pass, and the dashboard UI.

---

## 1. Why v2 exists — five defects in v1

Measured directly against `data/analytics.duckdb` (v1 output, 7,340 cells):

| # | Defect | Evidence | Root cause |
|---|--------|----------|-----------|
| D1 | **Robust-z explodes on sparse cells** | `max |intensity_z| = 143.1` on cells with **V ≤ 2**; `intensity_z = 18.5` for a **single post**; 0.9% of V≤2 cells exceed \|z\|>5 | median/MAD z on counts. When MAD → 0 the denominator collapses. Gaussian scale statistics applied to low counts. |
| D2 | **Panel is 91.3% trivially sparse** | 6,700 / 7,340 cells have V ≤ 2. Only **447 cells** have V ≥ 5 inside the real collection window | Grain (topic × platform × day) is finer than the data supports: 20,753 posts / 7,340 cells ≈ 2.8 posts/cell |
| D3 | **82% of the panel is outside the collection window** | 6,006 / 7,340 cells are pre-2026-06; panel spans 2011-06-22 → 2026-07-28 (5,516 distinct days) | YouTube backfill has real `published_at` going back to 2011. A 2011 video is a *corpus member*, not a point in a live time series. It pollutes every trailing baseline. |
| D4 | **Null-control conclusion contradicts its own criterion** | `real_mean_abs_growth = 0.142` vs `shuffled = 0.768`; doc states "if real >> shuffle, temporal structure exists" — the observed result is the *opposite* of the stated pass condition, yet reported `ok: true` | Wrong test framing. A temporally smooth series has **lower** \|Δ\| than its shuffle. The observation actually *confirms* autocorrelation; the stated criterion is inverted. |
| D5 | **Sensitivity harness bug** | `ema_7_21_corr_vs_default = 0.7913` — but 7/21 **is** the default, so this must be exactly 1.0 | The harness is not comparing what it claims; recomputation path differs from the production path. All three "stability" numbers (~0.78–0.79) are therefore untrustworthy. |

Two further design gaps:

- **No acceleration metric at all.** v1 has growth (1st difference) and momentum
  (EMA difference), but nothing measuring *how fast a scenario is emerging*.
- **Half-life is weak**: only **6 of 39** post-peak exponential fits reached R² ≥ 0.3.

---

## 2. Statistical fixes

### F1 — Count data gets count models (replaces robust-z)
Volume is a count with overdispersion. Replace Gaussian robust-z with a
**Negative Binomial exceedance surprise**:

```
baseline: V ~ NegBinom(μ_cp, θ_cp)   fitted per (topic × platform) on a trailing window
surprise  = −log10 P(X ≥ V_obs | baseline)
p_value   = P(X ≥ V_obs | baseline)
```

Bounded, interpretable as evidence, and correctly unimpressed by V = 1 on a small
baseline. Falls back to Poisson when θ is unidentifiable. NB is fitted by MLE;
if the fit fails, use method-of-moments θ.

### F2 — Exposure offset kills the collector-ramp confound
Telegram went 422 → 1,393 posts/day while active channels went 18 → 24. Model the
**rate**, not the count, using a log-exposure offset (standard epidemiological form):

```
log E[V] = log(effort) + η       effort = active channels (or active keywords) that day
V_rate   = V / effort
```
All growth/acceleration metrics are computed on the **effort-normalised log rate**,
with raw-count versions retained alongside for transparency.

### F3 — Support gating (no number without evidence)
Emit `NULL` + `support_ok = false` rather than a fake number when:
`baseline_days < 14` **or** `baseline_mean < 0.5` **or** `V_obs < 3` for z-like measures.
This is what prevents D1 from recurring.

### F4 — Window partitioning
`in_window = day BETWEEN 2026-06-14 AND max(day)`. Time-series metrics (growth,
acceleration, momentum, burst, persistence) are computed **only** where
`in_window = true`. The historical YouTube backfill is retained for cross-sectional
metrics (tone, readability, ontology) and explicitly flagged, never used for trends.

### F5 — Empirical-Bayes shrinkage for sparse means (tone, hate, virality)
Cell means from 1–2 posts are noise. Shrink toward the parent (topic, then platform,
then global) mean:
```
θ̂_EB = (n·x̄ + k·μ_parent) / (n + k)        k estimated from between/within variance
```
Report `*_eb` as the publishable value, keeping the raw `*_mean` and `n` visible.

### F6 — Multiple-testing control (directly enables alerting)
The panel produces hundreds of daily tests. Convert surprise p-values to
**Benjamini–Hochberg q-values** per day, and set `alert_flag = q < 0.05`. This gives
the notification system a **calibrated false-discovery rate** instead of an arbitrary
z threshold.

### F7 — Persistence via AR(1), not post-peak curve fitting
```
Δlog-rate series → AR(1):  x_t = c + φ·x_{t−1} + ε
half_life = ln(0.5) / ln(φ)      (defined for 0 < φ < 1)
volatility = σ(log-returns)
Ljung–Box Q  → autocorrelation significance
```
Uses the whole series with a proper standard error, instead of discarding 33 of 39
cells for weak R². Post-peak exponential decay is retained but scoped to **detected
burst episodes** rather than whole series.

### F8 — Corrected null control & sensitivity
- Null control restated: shuffling destroys autocorrelation, so
  `mean|Δ|_shuffled > mean|Δ|_real` **is** the signature of temporal structure.
  Add **Ljung–Box** and **lag-1..7 ACF with CIs** as the primary evidence, plus a
  permutation test on the *burst statistic* (does observed max burst exceed the 95th
  percentile of shuffled maxima?).
- Sensitivity harness must reproduce `corr(default, default) = 1.0` as a self-test
  before any other window comparison is reported. **Fix D5 before publishing stability claims.**

### F9 — Virality as an age-conditional percentile
Engagement is a single snapshot at wildly varying post age (0.6 → 150 days) and the
v1 age-curve R² was low. Stop pretending a parametric age normalisation works:
report virality as an **empirical percentile within (platform × age-band)**, which is
distribution-free and age-honest.

### F10 — Better reach proxy
No follower counts exist anywhere. Improvement over v1: for Telegram, a channel's
**median views** is a defensible audience-size estimate, so report
`reach_audience_norm = post_views / channel_median_views` alongside the raw proxy.
Always labelled a proxy.

---

## 3. Acceleration — "rate of popping up of a scenario"

Treated as a **family**, not one number, because "popping up" has three distinct senses.

| Key | Name | Definition | Sense captured |
|-----|------|-----------|----------------|
| `accel` | Acceleration | 2nd difference of effort-normalised log rate: `a_t = g_t − g_{t−1}` | Raw curvature |
| `accel_smooth` | Smoothed acceleration | Savitzky–Golay (window 5, order 2) derivative, or local quadratic fit | Curvature with noise controlled — 2nd differences double the noise, so the smoothed form is the **publishable** one |
| `burst_state`, `burst_intensity` | Kleinberg burst | 2-state HMM (baseline vs burst) over the count series; yields state, level, start/end | Canonical "topic is bursting" detection |
| `onset_slope` | Onset steepness | Logistic/Gompertz fit to a burst's **rising limb**; max slope + inflection day | How *explosively* it arrived |
| `days_to_threshold` | Time-to-threshold | Days from first appearance to first crossing k posts/day | Speed of emergence from zero |
| `novelty` | Semantic novelty | Cosine distance of the day's post centroid from the topic's trailing centroid (bge-m3) | A **new sub-narrative inside an existing topic** — invisible to volume alone |
| `emergence_score` | Composite | Rank-combination of `surprise`, `accel_smooth`, `novelty`, inverse `days_to_threshold`; FDR-gated | The single alerting number |

`emergence_score` is the metric the notification system should key on for
"a situation is appearing", while `surprise`/`alert_flag` covers "a situation is
unusually intense".

---

## 4. Hashtag grain — a coverage warning that must be stated

The notification system is intended to flag intensity **by hashtag**. Measured coverage:

| Source | Coverage |
|--------|----------|
| `hashtags` field populated | linkedin 502/924, instagram 237/337 — **and zero for telegram, youtube, reddit, facebook** |
| Inline `#tag` mined from text | telegram 1,883/14,595 (12.9%), youtube 414/2,305, linkedin 502, instagram 235, facebook 142, reddit 66/2,107 (3.1%) |

**Hashtags cover roughly 15% of the corpus and ~13% of Telegram — the dominant source.**
A hashtag-keyed alerting system would therefore be blind to ~85% of collected material.

**Recommendation:** key alerts on **`topic_id` (ontology)** as primary — it covers 79%
of posts with the rest explicitly `unassigned` — with **hashtag and keyword as
secondary facets**, and NER entities as a future third. `tag_metrics_daily` is still
built (union of the `hashtags` field + mined inline tags, normalised) so hashtag
alerting works where hashtags exist, but it must not be the only key.

---

## 5. Schema contract — `data/metrics.duckdb` (metrics only)

Separate from `data/analytics.duckdb` (which keeps post-level data). This DB holds
**only metrics + the dimensions needed to read them**, per the "local DB has only the
metrics" requirement. Production Postgres stays **read-only**; nothing is written back.

### Dimensions
```
topic_dim(topic_id, domain, topic, label, n_posts, description)
tag_dim(tag_id, tag, n_posts, platforms, first_seen, last_seen)
platform_dim(platform, has_engagement, in_ts_scope, notes)
metric_meta(metric_key, display_name, family, formula, units, grain,
            direction, higher_is_worse, caveats, support_gate)
run_meta(run_id, created_at, source_extract_id, window_start, window_end, params_json)
```

### Facts
```
metrics_daily(day, topic_id, platform, in_window,
  V, V_rate, effort,
  growth, growth_share, growth_7d,
  accel, accel_smooth, momentum,
  surprise, surprise_p, surprise_q, alert_flag,
  burst_state, burst_intensity, burst_episode_id,
  novelty, days_to_threshold, emergence_score,
  tone_mean, tone_eb, tone_n,
  hate_mean, hate_eb, hate_prevalence, hate_calibrated,
  virality_pct, reach_proxy, reach_audience_norm,
  author_entropy, author_1_hhi, n_authors, coordination_flag,
  readability_mean, readability_coverage,
  support_n, support_ok)

metrics_topic_day(...)      -- same measures, platform rolled up
tag_metrics_daily(day, tag_id, platform, V, V_rate, growth, accel_smooth,
                  surprise, surprise_q, alert_flag, tone_eb, hate_eb, emergence_score,
                  support_n, support_ok)
burst_episodes(episode_id, topic_id, platform, start_day, peak_day, end_day,
               peak_V, total_V, onset_slope, inflection_day,
               decay_lambda, half_life_days, r2, level)
persistence(topic_id, platform, ar1_phi, phi_se, half_life_days, volatility,
            ljung_box_q, ljung_box_p, n_obs)
alert_candidates(day, topic_id, platform, tag_id, metric_key, value,
                 surprise_q, severity, rationale)
```

`metric_meta` is mandatory: every metric ships its formula, units, direction and
caveats in-band, so the dashboard and the future notification service read semantics
from the data rather than hardcoding them.

---

## 6. Non-negotiables

1. Production Postgres **read-only** — no DDL/DML, ever.
2. No metric published without its support gate; `support_ok = false` ⇒ `NULL`, not a guess.
3. `hate_*` stays **uncalibrated and unpublishable** until `reports/calibration_sample.csv`
   is hand-labelled — a war/terror corpus over-fires toxicity models. Dashboard must
   render it visibly gated.
4. Every z-like or p-like metric carries its `support_n`.
5. Alert thresholds come from **FDR q-values**, not raw z cutoffs.
