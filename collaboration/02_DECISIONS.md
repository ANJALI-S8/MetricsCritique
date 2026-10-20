# Locked design decisions

These were answered during the Claude session. **Do not re-open without new data that makes a metric impossible.**

| # | Question | Decision | Why |
|---|----------|----------|-----|
| 1 | Primary entity for metrics? | **Derived topic clusters** | Telegram (~68% of raw posts pre-dedup, still majority post-dedup) has **no keyword**. Keyword-only metrics would drop most of the corpus. |
| 2 | Half-life with no engagement history? | **Aggregate now + append-only local snapshot table** | Every post is observed once in Postgres (`distinct(key, scraped_at) == distinct(key)`). True per-post decay only becomes measurable as `engagement_snapshots` accrues locally. |
| 3 | Tone / hate scoring? | **Local multilingual transformers on L40S** | Corpus is multilingual (en/ru/zh/bn/uk/hi/…). English-only NLP invalid. GPU + HF available; no data should leave the network. |
| 4 | Where does analytics live? | **Local parquet + DuckDB only** | Scraper actively writes production DB; credentials are powerful. Pipeline is **SELECT-only** against Postgres. |

---

## Derived constraints (not separate user decisions, but binding)

| Constraint | Implication |
|------------|-------------|
| Production is READ-ONLY | No `INSERT/UPDATE/CREATE/DROP/ALTER` on Postgres. Missing YouTube embeddings computed **locally only**. |
| ~6 weeks effective history | Cap long windows (EMA slow, baselines) at ~21 days. Sensitivity analysis required. |
| Collector ramp-up confounds growth | Always report **share-of-volume** growth, not only raw `V`. Use `collection_effort`. |
| Engagement is age-confounded | Fit age curves before virality/intensity comparisons (`age_norm_params`). |
| Reach has no follower field | Reach is always a **proxy** (`Σviews` or `Σlog1p(E)`). Label it as such. |
| War/terror domain | Off-the-shelf toxicity **over-fires** on reported violence. Hate figures need hand-label calibration before publish. |
| Snapchat / Pinterest coverage | Exclude from time-series metrics. |
| Reddit engagement | All engagement fields null → exclude from virality; volume/diversity only. |

---

## Metric grain (locked by plan)

**Primary:** `(topic_id × platform × posted_day)`  
**Rollups:** `(topic_id × day)`, optionally `(channel × day)` later  
**Smoothing:** `α = 1` on volume ratios  
**Heavy tails:** `log1p` + robust z (median/MAD)
