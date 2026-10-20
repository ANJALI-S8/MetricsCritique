# Metrics v2 — stack status

## 2026-07-29 refresh: platform-inclusion fix + fresh extract

Removed the blanket snapchat/pinterest/whatsapp exclusion (`TS_EXCLUDE_PLATFORMS` is
now empty; see `collaboration/04_CURRENT_STATE.md` for the full write-up, including a
non-obvious second exclusion path fixed in `07_metrics_v2.py`'s `posted_at` handling and
a fresh-extraction bug in `01_extract_base.py` — both were required, not just a re-run,
to get every platform genuinely flowing through). New extract
`20260729T134230Z_bd348e9f`, new metrics run `20260729T145621Z_metrics_v2`.
`platform_dim` now has **8 rows** (was 7 — pinterest was entirely absent, snapchat was
present but marked excluded). Numbers below are from this refreshed run.

## Why v2 exists

v1 (`scripts/05_metrics.py` → `analytics.duckdb.metrics_daily`) had five defects:

| ID | Defect | v1 evidence | v2 closure |
|----|--------|-------------|------------|
| D1 | Robust-z explodes on sparse cells | max \|z\| = 143 on V≤2 | NB surprise; **0** non-null on V≤2; max surprise ≈ 3.11 |
| D2 | Panel 91% trivially sparse | 6,700/7,340 V≤2 | Support gates; 345 publishable cells |
| D3 | 82% cells outside collection window | YouTube backfill to 2011 | `in_window` 2026-06-14→07-28 for trends |
| D4 | Null-control logic inverted | ok:true vs own criterion | Shuffled \|Δ\| > real = structure; 8/8 series support |
| D5 | Sensitivity self-test broken | corr(7/21, default)≈0.79 | Self-test **1.0**; root cause was series misalignment |

Full write-up: `../docs/STATISTICAL_REVIEW.md`, `../docs/STATISTICAL_APPENDIX.md`,  
contract: `../docs/METRICS_V2_SPEC.md`.

## What to use

| For | Use |
|-----|-----|
| Analysis / alerts | **`data/metrics.duckdb`** |
| Interactive exploration | **`UI/`** over `UI/data/metrics.json` |
| Forensics only | v1 tables in `analytics.duckdb` |

## Pipeline

```
analytics.duckdb (posts, topics, scores)
        │
        ▼
  07_metrics_v2.py  →  data/metrics_stage/*.parquet
        │
        ▼
  08_metrics_db.py  →  data/metrics.duckdb  +  parquet_metrics/
        │
        ▼
  UI/build_dashboard.py  →  UI/data/metrics.json  +  UI/index.html shell
```

## Headline numbers (last published run — 2026-07-29 refresh, all platforms)

| Metric | Value |
|--------|------:|
| v1 cells | 7,340 |
| v2 cells (`metrics_daily`) | 941 |
| in-window | 486 |
| support_ok (surprise) | 211 |
| max surprise | 3.01 |
| non-null surprise on V≤2 | **0** |
| tag cells (`tag_metrics_daily`) | 26,257 |
| alert candidates | 28 (12 high / 16 medium) |
| hashtag post coverage | 25.5% |
| platforms in `platform_dim` | **8** (every platform in `post_facts`, incl. snapchat & pinterest) |
| self-test corr(default,default) | **1.0** |
| self_checks (stats_v2) | all true (46/46) |

Cell/alert counts are *lower* than the prior run despite more posts and platforms — this
is mechanical, not a regression: ontology this run produced only 3 real topic clusters
(down from 8, see `04_CURRENT_STATE.md`), and `metrics_daily`'s grain is
`topic_id × platform × day`, so far fewer distinct topic buckets means far fewer
possible cells even with more raw posts. The topic coarsening (flagged as needing a
revisit) is the more likely explanation than any change in support-gate behavior.

## Alerting

- **Primary key:** `topic_id` × platform × day (`alert_candidates`, FDR `surprise_q`)  
- **Secondary:** hashtags (`tag_metrics_daily`) — only ~25% coverage  
- **Do not** publish hate until calibration (`hate_calibrated=false`)  
- Detail: `../docs/ALERTING_KEYING.md`

## Commands

```bash
cd /home/hariramanan/NTRO/EDA && source .venv/bin/activate
python scripts/07_metrics_v2.py
python scripts/08_metrics_db.py
python UI/build_dashboard.py
python scripts/review/stat_review.py --json reports/stat_review_results.json
```

## Do not

- Trust v1 `intensity_z` / old VALIDATION sensitivity numbers  
- Alert on hashtags alone  
- Write to production Postgres  
- Treat the UI as live DB (it is a snapshot — see 08_UI_DASHBOARD.md)  
