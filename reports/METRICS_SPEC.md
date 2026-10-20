# Metrics Specification — Layer 4

Generated: 2026-07-29 07:59 UTC
Grain: **(topic_id × platform × posted_day)** with rollup to (topic × day).
Smoothing α = 1.0; EMA spans 7/21; baseline window = 28d (capped by ~6wk history).
Excluded platforms from TS: pinterest, snapchat, whatsapp

## Formulas

| # | Metric | Formula | Caveats |
|---|---|---|---|
| 1 | Growth rate | `g = ln((V_t+α)/(V_{t-1}+α))`; also `growth_share`, `growth_7d` | Report share-based growth to strip collector ramp-up |
| 2 | Momentum | `EMA_7(g) − EMA_21(g)` | Long window capped at 21d |
| 3 | Half-life | Post-peak fit `V = V_peak·e^{−λ(t−t_peak)}` → `t½=ln2/λ`; require R²≥0.3 | Aggregate only; per-post needs engagement_snapshots accrual |
| 4 | Intensity | Robust-z of `0.5·log1p(V)+0.5·log1p(E)` vs trailing 28d | Age-norm residuals used in E where fitted |
| 5 | Tone z-score | Entity-day mean tone vs trailing 28d robust-z | Requires Layer 3; null if unscored |
| 6 | Virality | Platform amp ratio → within-platform robust-z | Reddit excluded; cross-platform only after standardisation |
| 7 | Reach / diversity | `reach_proxy = Σviews` else `Σlog1p(E)`; Shannon entropy; `1−HHI`; distinct authors | Reach is a **proxy** — no follower counts |
| 8 | Readability | Language-appropriate, length-gated mean + coverage | Low coverage on short posts |
| 9 | Ontology | `topic_id` from Layer 2 | `unassigned` retained |
| 10 | Hate-speech | Daily mean + prevalence (≥0.5 raw) + robust-z | **Uncalibrated** — war corpus over-fires |

## Output tables

- `metrics_daily` — full panel
- `metrics_topic_day` — platform rollup
- `half_life` — one row per topic×platform

## Snapshot stats

- Rows: 7,340
- Topics: 9
- Platforms: facebook, instagram, linkedin, reddit, telegram, youtube_video
- Date range: 2011-06-22 00:00:00+00:00 → 2026-07-28 00:00:00+00:00
- Half-life fits accepted: 6 / 39

### Sample: top momentum cells (recent window)

| day | topic | platform | V | growth | momentum | intensity_z |
|---|---|---|---:|---:|---:|---:|
| 2026-07-27 | t004 | reddit | 134 | 3.807 | 0.710 | 20.32 |
| 2026-06-25 | t004 | reddit | 94 | 3.455 | 0.651 | 10.81 |
| 2026-07-27 | t002 | reddit | 72 | 4.290 | 0.615 | 12.46 |
| 2026-07-27 | t007 | reddit | 349 | 2.247 | 0.530 | 3.12 |
| 2026-07-23 | t007 | instagram | 124 | 1.332 | 0.456 | 3.75 |
| 2026-07-26 | unassigned | reddit | 21 | 3.091 | 0.455 | 8.91 |
| 2026-06-22 | t002 | facebook | 13 | 2.639 | 0.414 | 3.25 |
| 2026-07-05 | t003 | reddit | 10 | 2.398 | 0.395 | 2.11 |
| 2026-06-12 | unassigned | youtube_video | 29 | 2.303 | 0.395 | 2.46 |
| 2026-06-26 | t001 | telegram | 11 | 2.485 | 0.389 | 2.56 |

## Virality platform formulas

| Platform | Formula |
|---|---|
| telegram | forwards / views |
| instagram, facebook | shares / (likes+comments+shares) |
| youtube_video | (likes+comments) / views |
| linkedin | comments / reactions |
| reddit | excluded (no engagement) |

