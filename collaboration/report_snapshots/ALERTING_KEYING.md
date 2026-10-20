# Alerting keying decision

## Problem

A natural first notifier keys on **hashtags** (“flag intensity by hashtag”). Measured coverage on this corpus:

| Source | Coverage |
|--------|----------|
| Structured `hashtags` field | LinkedIn + Instagram only — **empty** for Telegram, YouTube, Reddit, Facebook |
| Inline `#tags` mined from text | Telegram ~15.6%, Reddit ~2.7%, higher on IG/LI where the field already exists |
| **Union (any tag)** | **~17.9% of posts** overall |

Telegram is the dominant source (~14.6k of ~20k posts). Hashtag-only alerting is **blind to ~85% of collected material**.

## Decision (recommended)

| Priority | Key | Why |
|----------|-----|-----|
| **1 — primary** | `topic_id` × `platform` × `day` | Ontology covers ~79% of posts; remainder is explicit `unassigned`. Available for all platforms. |
| **2 — secondary** | `tag_id` (hashtag) | Use `tag_metrics_daily` where tags exist; never sole key. |
| **3 — secondary** | scraper `keyword` | Present on keyword-driven platforms; **null on Telegram**. |
| future | NER entities | Not built yet. |

## What to read from the metrics DB

```sql
-- intensity / unusual volume (FDR-controlled)
SELECT * FROM alert_candidates
WHERE metric_key = 'surprise'
ORDER BY day DESC, surprise_q;

-- "situation appearing" composite
SELECT * FROM alert_candidates
WHERE metric_key = 'emergence_score';

-- or live cells
SELECT * FROM v_metrics_live
WHERE alert_flag OR emergence_flag
ORDER BY day DESC, surprise DESC;
```

Also: `UI/data/metrics.json` → `derived.alerts` for a file-based consumer.

## Thresholds

- Use **`surprise_q` (BH FDR)** — not raw z or raw surprise alone.  
- Default α = 0.05 (see `run_params.fdr`).  
- Severity bands are already assigned in `alert_candidates.severity` (`high` / `medium`).  
- Require `support_ok` / non-null `support_n` — never alert on gated cells.

## Explicit non-goals

- Hashtag-only production alerts  
- Publishing hate-based alerts until calibration (`hate_calibrated = false`)  
- Cross-platform raw virality comparisons without within-platform standardisation  

## Implemented support

- `tag_metrics_daily` + `tag_dim` built for secondary facets  
- Primary series: `metrics_daily`, `metrics_topic_day`, `alert_candidates`  
- Coverage stats: `data/metrics_stage/run_params.json` → `hashtag_coverage`
