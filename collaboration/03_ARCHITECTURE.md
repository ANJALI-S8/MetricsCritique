# Architecture

## Data flow

```
┌─────────────────────────────────────┐
│  Postgres shaktidb_social (LIVE)    │
│  READ-ONLY: SELECT only             │
│  REPEATABLE READ snapshot           │
│  always ROLLBACK                    │
└─────────────────┬───────────────────┘
                  │ Layer 0
                  ▼
┌─────────────────────────────────────┐
│  Foundation analytics               │
│  data/parquet/*                     │
│  data/analytics.duckdb              │
│  (posts, scores, topics, embeddings)│
└─────────────────┬───────────────────┘
                  │
     ┌────────────┼────────────┐
     ▼            ▼            ▼
  Layer 1      Layer 2      Layer 3
  diagnostics  ontology     NLP scores
     │            │            │
     └────────────┼────────────┘
                  ▼
         Layer 4 v2  07_metrics_v2
                  ▼
         data/metrics_stage/
                  ▼
         08_metrics_db → data/metrics.duckdb   ← authoritative signals
                  ▼
         UI/build_dashboard → metrics.json
                  ▼
         UI/index.html (interactive shell)
```

v1 scripts `05`/`06` remain for forensics only.

## Scripts

| Script | Layer | Writes |
|--------|-------|--------|
| `scripts/run_eda.py` | Descriptive (Claude) | `reports/EDA_Report.html`, figures, JSON |
| `scripts/01_extract_base.py` | 0 | parquet + analytics.duckdb base |
| `scripts/02_eda_diagnostics.py` | 1 | `METRIC_READINESS.md`, `age_norm_params` |
| `scripts/03_ontology.py` | 2 | `topic_dim`, `post_topic`, … |
| `scripts/04_nlp_scores.py` | 3 | `post_scores` |
| `scripts/05_metrics.py` | 4 **v1 legacy** | superseded for decisions |
| `scripts/06_validate.py` | 5 **v1 legacy** | historical validation |
| `scripts/07_metrics_v2.py` | 4 **v2** | `data/metrics_stage/*` |
| `scripts/08_metrics_db.py` | 4 **v2 publish** | `data/metrics.duckdb` |
| `scripts/review/stat_review.py` | Review | `reports/stat_review_results.json` |
| `UI/build_dashboard.py` | UI snapshot | `UI/data/metrics.json`, `UI/index.html` |

### Shared library (`scripts/lib/`)

| Module | Role |
|--------|------|
| `config.py` | Paths, `SHAKTI_PG_*` env, excluded platforms, embedding model id |
| `pg.py` | `read_only_snapshot()`, `q` / `q1` |
| `duck.py` | DuckDB connect, parquet write/append, table replace, views |
| `stats_v2.py` | NB, BH-FDR, EB, AR(1), Kleinberg/CUSUM, self-checks |

## Local tables (DuckDB / parquet)

### Layer 0

| Table | Description |
|-------|-------------|
| `post_facts` | Deduped posts; unified engagement slots; `has_engagement` flag |
| `comment_facts` | YouTube comments (~76k) |
| `post_embeddings` | Vectors as parquet list&lt;float&gt; (+ meta table without vectors) |
| `engagement_snapshots` | Append-only `(post_uid, extract_id, observed_at, likes, comments, shares, views)` |
| `collection_effort` | Platform-day: rows ingested, active channels/keywords |
| `extract_meta` | extract_id, snapshot_ts, counts |

**Dedup key:** `(platform, native_id)` with fallback `(platform, md5(text), author)`.

### Layer 1

| Table | Description |
|-------|-------------|
| `age_norm_params` | Per-platform fit: `log1p(E) = a + b·log1p(age)` |

### Layer 2

| Table | Description |
|-------|-------------|
| `topic_dim` | topic_id, domain, topic, size, top_terms, exemplars |
| `post_topic` | post_uid → topic_id, confidence, assign_method |
| `keyword_map` | keyword → optional domain/topic hint |

### Layer 3

| Table | Description |
|-------|-------------|
| `post_scores` | tone, hate, readability, lang_resolved, validity flags, model ids |

### Layer 4 v1 (legacy — do not use for decisions)

| Table | Description |
|-------|-------------|
| `metrics_daily` (in analytics.duckdb) | Old robust-z panel — defects D1–D5 |

### Layer 4 v2 (authoritative — `data/metrics.duckdb`)

| Table | Description |
|-------|-------------|
| `metrics_daily` | topic×platform×day: V, V_rate, growth, accel*, surprise, FDR q, burst, novelty, tone_eb, … |
| `metrics_topic_day` | Platform rolled up |
| `tag_metrics_daily` | Hashtag grain (secondary; low coverage) |
| `burst_episodes` | Detected burst windows + onset |
| `persistence` | AR(1) φ, half-life, Ljung–Box |
| `alert_candidates` | FDR-gated alert feed |
| `metric_meta` | In-band formulas / gates / caveats |
| `null_control`, `sensitivity_checks` | Validation artifacts |

### UI

| Artifact | Description |
|----------|-------------|
| `UI/data/metrics.json` | Snapshot export of metrics.duckdb for the browser |
| `UI/index.html` | Interactive shell (filters re-render charts; not live DB) |

## Metric definitions (summary)

**Authoritative:** `docs/METRICS_V2_SPEC.md` and `metric_meta` table.  
v1 formulas in old `METRICS_SPEC.md` are historical only.

| Family | v2 approach |
|--------|-------------|
| Intensity | NB exceedance surprise + BH q (`alert_flag`) |
| Growth / accel | Effort-normalised log-rate; causal SavGol for publishable accel |
| Half-life | AR(1) on series + episode-scoped decay |
| Tone / hate | Empirical-Bayes shrink; hate uncalibrated |
| Virality | Age-band percentile within platform |
| Emergence | Rank combo of surprise, accel, novelty, speed |
| Reach | Proxy only |

## Security / safety rules

1. Never write to `shaktidb_social`.
2. Prefer env vars for credentials (`SHAKTI_PG_*`). Note: legacy `run_eda.py` still has inline password — new scripts use env with lab defaults.
3. Do not publish raw hate prevalence without calibration labels.
4. Label reach as proxy in any external report.
5. Alert on **topic_id** primarily — not hashtags alone (~18% coverage).
6. UI shows a **rebuild snapshot**, not live production.
