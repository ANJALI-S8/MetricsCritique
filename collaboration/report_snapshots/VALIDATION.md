# Validation — Layer 5

Generated: 2026-07-29 07:59 UTC

## Tables present

- `age_norm_params`: 5 rows
- `collection_effort`: 151 rows
- `comment_facts`: 76,520 rows
- `engagement_snapshots`: 41,506 rows
- `extract_meta`: 1 rows
- `half_life`: 39 rows
- `keyword_map`: 418 rows
- `metrics_daily`: 7,340 rows
- `metrics_topic_day`: 6,296 rows
- `post_embeddings`: 16,611 rows
- `post_embeddings_meta`: 16,611 rows
- `post_facts`: 20,753 rows
- `post_scores`: 20,753 rows
- `post_topic`: 20,753 rows
- `topic_dim`: 9 rows

## Read-only guarantee

Pipeline scripts under `scripts/0*.py` open Postgres with `readonly=True` and always `ROLLBACK`.
Analytics written only under `data/`.

## Metrics sanity

- metrics_daily rows: 7,340
- growth finite share: 99.5%
- growth mean: 0.0029  std: 0.4126
- momentum mean: 0.0007  std: 0.0602

## Null control (shuffled volume)

```json
{
  "ok": true,
  "real_mean_abs_growth": 0.14188500019874606,
  "shuffled_mean_abs_growth": 0.7679872972180027,
  "note": "Shuffled volume growth magnitude \u2014 if real >> shuffle, temporal structure exists."
}
```

## Sensitivity (EMA window correlation vs default 7/21)

```json
{
  "ema_5_14_corr_vs_default": 0.791555555940177,
  "ema_7_21_corr_vs_default": 0.7913461667807777,
  "ema_7_14_corr_vs_default": 0.7838728102940731
}
```

Momentum reasonably stable across tested EMA windows.

## Engagement snapshot accrual

- Distinct extract_ids: **2**
- Append path works (per-post half-life measurable prospectively).

## Hate/tone calibration

Hand-label sample exported to `/home/hariramanan/NTRO/EDA/reports/calibration_sample.csv`.
Columns `label_tone` (−1/0/1) and `label_hate` (0/1) are empty for human fill-in.
Until labels exist, **do not publish raw hate prevalence** — war/terror reporting over-fires.

### Scoring once labels exist

```python
# precision/recall sketch
import pandas as pd
lab = pd.read_csv('reports/calibration_sample.csv')
lab = lab.dropna(subset=['label_hate'])
pred = (lab['hate_score'] >= threshold).astype(int)
# compare to lab['label_hate']
```

## Spot-check: top intensity_z cells

| day | topic | platform | V | intensity_z | momentum |
|---|---|---|---:|---:|---:|
| 2026-07-22 | t002 | facebook | 15 | 22.47 | 0.210 |
| 2026-07-23 | t002 | facebook | 9 | 21.61 | 0.058 |
| 2026-06-22 | unassigned | telegram | 3 | 21.18 | 0.034 |
| 2026-07-21 | t002 | facebook | 4 | 20.83 | 0.045 |
| 2026-07-27 | t004 | reddit | 134 | 20.32 | 0.710 |
| 2026-06-10 | unassigned | telegram | 2 | 19.89 | 0.147 |
| 2026-06-12 | t002 | telegram | 6 | 18.55 | 0.028 |
| 2026-06-01 | unassigned | telegram | 1 | 18.54 | -0.005 |

