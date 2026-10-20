# Metric Readiness — Layer 1 Diagnostics

Generated: 2026-07-29 07:24 UTC
Source extract_id: `20260729T072256Z_118e3e74`
Snapshot: `2026-07-29 07:22:56.789114+00:00`

## Deduped corpus

| Platform | Posts |
|---|---:|
| telegram | 14,595 |
| youtube_video | 2,305 |
| reddit | 2,107 |
| linkedin | 924 |
| instagram | 337 |
| facebook | 230 |
| snapchat | 177 |
| pinterest | 78 |
| **total** | **20,753** |

Telegram raw was ~23.3k → **14,595** after dedup (~37% removed). Matches plan finding #2.

## Scrape lag

| Platform | n | median days | p90 | mean | neg lag share |
|---|---:|---:|---:|---:|---:|
| linkedin | 924 | 4.2 | 10.6 | 5.1 | 0.0% |
| telegram | 14,595 | 3.5 | 21.6 | 10.0 | 0.0% |
| facebook | 230 | 0.7 | 1.1 | 0.6 | 0.0% |
| instagram | 336 | 0.6 | 0.9 | 0.6 | 0.0% |
| youtube_video | 2,079 | 0.4 | 11.1 | 13.7 | 0.0% |
| reddit | 1,907 | 0.1 | 0.7 | 0.6 | 0.0% |

Engagement is **age-confounded**. Age-normalisation curves are required before cross-age virality/intensity.

## Age-normalisation fits (`log1p(E) = a + b·log1p(age)`)

| Platform | n | R² | a | b | usable |
|---|---:|---:|---:|---:|---|
| telegram | 14,562 | 0.000 | 7.771 | -0.052 | yes |
| youtube_video | 2,176 | 0.015 | 8.163 | 0.138 | yes |
| linkedin | 924 | 0.005 | 2.374 | -0.168 | yes |
| instagram | 336 | 0.036 | 3.053 | 3.139 | yes |
| facebook | 230 | 0.004 | 1.566 | -0.653 | yes |

Stored in DuckDB table `age_norm_params`.

## Panel density (last 21d median posts/day)

| Platform | median daily volume | trustworthy for daily metrics? |
|---|---:|---|
| telegram | 616.0 | yes |
| linkedin | 32.0 | yes |
| reddit | 29.0 | yes |
| instagram | 9.0 | yes |
| facebook | 8.0 | yes |
| youtube_video | 6.0 | yes |
| snapchat | 2.0 | exclude (coverage) |

## Language mix (top codes)

- **telegram:** en=9336, unknown=1642, ru=946, zh-cn=859, uk=635
- **youtube_video:** (null)=2046, en=192, hi=46, my=6, bn=4
- **reddit:** (null)=2107
- **linkedin:** (null)=924
- **instagram:** (null)=337
- **facebook:** (null)=230
- **snapchat:** (null)=167, hi=5, en=5
- **pinterest:** (null)=78

## Readability coverage (≥25 words)

| Platform | n | median chars | median words | scorable % |
|---|---:|---:|---:|---:|
| snapchat | 177 | 0 | 0 | 4.5% |
| pinterest | 78 | 0 | 0 | 5.1% |
| youtube_video | 2,305 | 93 | 14 | 14.0% |
| reddit | 2,107 | 50 | 5 | 39.7% |
| telegram | 14,595 | 220 | 23 | 48.6% |
| instagram | 337 | 495 | 60 | 70.9% |
| facebook | 230 | 796 | 117 | 88.7% |
| linkedin | 924 | 842 | 119 | 94.8% |

## Per-metric readiness

| Metric | Status | Notes |
|---|---|---|
| Growth rate | `ready` | Deduped daily volume + share-of-volume; normalise by collection_effort. Cap long windows at 21d. |
| Momentum | `ready` | EMA_7 − EMA_21 on growth; ~6wk history → 21d cap is the max sensible long window. |
| Half-life / persistence | `partial` | Aggregate decay on topic×day volume ready after ontology. Per-post decay needs snapshot accrual (currently 2 extract_id(s)). |
| Intensity | `ready_after_age_norm` | Use age-norm params from this layer; robust z vs 28d baseline of entity. |
| Tone z-score | `needs_layer3` | Requires multilingual sentiment scores. |
| Virality | `partial` | Per-platform formulas + age-norm. Reddit excluded (no engagement). Snapchat/Pinterest excluded from TS. |
| Reach / author diversity | `ready` | Reach is a PROXY (Σviews or Σlog1p(E)). Diversity = Shannon / 1−HHI / distinct authors. |
| Readability | `partial` | Length-gate ≥25 words; coverage low on short posts. zh/ja N/A for syllable scores. |
| Ontology | `needs_layer2` | Embeddings present for 16,611 posts; youtube_video embeddings=0 → compute bge-m3 locally. |
| Hate-speech score | `needs_layer3_plus_calibration` | War/terror corpus → off-the-shelf toxicity will over-fire; calibrate before publishing. |

## Figures

- `reports/figures/layer1_volume_by_platform.png`
- `reports/figures/layer1_scrape_lag.png`
- `reports/figures/layer1_engagement_vs_age.png`
- `reports/figures/layer1_effort_vs_volume.png`
- `reports/figures/layer1_panel_density.png`
- `reports/figures/layer1_readability_coverage.png`
- `reports/figures/layer1_heavy_tails.png`

## Next

1. Layer 2 — ontology: embed youtube_videos with bge-m3, cluster, assign Telegram.
2. Layer 3 — multilingual tone / hate / readability scores.
3. Layer 4 — compute the 10 metrics at (topic × platform × day).

