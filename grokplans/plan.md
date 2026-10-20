# Plan: Take Forward ShaktiDB Metric-Readiness EDA (from Claude session)

## Where Claude left off

The session in `EDA/ClaudeChat.md` (full plan: `~/.claude/plans/whimsical-enchanting-marshmallow.md`) did **schema + readiness analysis**, locked design decisions, and started a **descriptive EDA**. It did **not** implement the 5-layer metrics pipeline.

### Already done
| Artifact | Status |
|---|---|
| Schema inventory + metric gap table | Done (in chat) |
| 10 blocking findings (dupes, lag, no post history, keyword-less Telegram, …) | Done (in plan) |
| 4 design decisions locked by user | Done |
| `scripts/run_eda.py` descriptive EDA | Done |
| `reports/EDA_Report.html` + 26 figures + `_raw_results.json` | Done |
| `reports/KEY_FINDINGS.md` | **Missing** (script mentions it; never written) |
| Layers 0–5 scripts, DuckDB, metrics | **Not started** |

### Corpus snapshot (from completed EDA)
~105K rows across platforms; Telegram + YouTube comments dominate.

| Table | Rows | Notes |
|---|---:|---|
| youtube_comments | 76,520 | embeddings present; short text |
| telegram_posts | 23,325 | ~37% URL dupes; no keywords; multi-lang |
| youtube_videos | 2,305 | **embeddings 100% NULL** |
| reddit_posts | 2,107 | no engagement fields |
| linkedin / ig / fb / snapchat / pinterest | smaller | snapchat/pinterest effectively unusable for time series |
| whatsapp_posts | 0 | empty |

Live DB (`<PG_HOST>:13366` / `shaktidb_social`) is **actively ingesting** — extracts must use `REPEATABLE READ` + read-only.

### Locked decisions (do not re-open)
1. **Entity** = derived topic clusters (ontology first — Telegram has no keywords).
2. **Half-life** = aggregate decay now + append-only local engagement snapshots for future per-post decay.
3. **Tone / hate** = local multilingual transformers on L40S.
4. **Store** = local parquet + DuckDB only. **Production Postgres is READ-ONLY** (no DDL/DML).

---

## Goal

Build the 5-layer pipeline so the 10 signal metrics become computable and documented:

growth rate · momentum · half-life (persistence/volatility) · intensity · tone z-score · virality · reach / author diversity · readability · ontology · hate-speech score

Grain: **(topic × platform × day)**, with rollups to (topic × day) and (channel × day).

---

## Architecture (from Claude plan, refined for execution)

```
Postgres (READ-ONLY, REPEATABLE READ)
        │
        ▼
Layer 0  01_extract_base.py     → data/analytics.duckdb + data/parquet/
Layer 1  02_eda_diagnostics.py  → METRIC_READINESS.md + charts
Layer 2  03_ontology.py         → topic_dim, post_topic (+ local bge-m3 for YT videos)
Layer 3  04_nlp_scores.py       → post_scores (tone, hate, readability, lang)
Layer 4  05_metrics.py          → metrics_daily + METRICS_SPEC.md
Layer 5  06_validate.py         → VALIDATION.md (sensitivity + null control + calibration path)
```

### Target tree
```
EDA/
  data/
    analytics.duckdb
    parquet/{post_facts,comment_facts,post_embeddings,engagement_snapshots,collection_effort}/
  scripts/
    run_eda.py              # existing descriptive EDA (keep)
    01_extract_base.py
    02_eda_diagnostics.py
    03_ontology.py
    04_nlp_scores.py
    05_metrics.py
    06_validate.py
    lib/                    # shared db, schema, config, formula helpers
  reports/
    EDA_Report.html         # existing
    KEY_FINDINGS.md         # write from existing JSON if still missing
    METRIC_READINESS.md
    ONTOLOGY.md
    METRICS_SPEC.md
    VALIDATION.md
  requirements.txt
  README.md
```

---

## Environment prep (before Layer 0)

**Present:** L40S 48GB, torch+CUDA, transformers, sentence-transformers, sklearn, psycopg2.

**Missing packages (install into a local venv under `EDA/.venv`):**
- `duckdb`, `pyarrow`, `pandas`
- `umap-learn`, `hdbscan`
- `textstat` (readability) — optional, can implement Flesch/LIX inline
- `fasttext` or `lingua` / `langdetect` for language fill-in

**Models (download on first run, cache under HF hub):**
- `BAAI/bge-m3` — must match DB embeddings (1024-d multilingual)
- Multilingual sentiment (e.g. `cardiffnlp/twitter-xlm-roberta-base-sentiment` or similar)
- Multilingual toxicity/hate (e.g. `textdetox/xlmr-large-toxicity-classifier` or `facebook/roberta-hate-speech-dynabench-r4-target` — pick one multilingual; document choice)
- Language ID model if declared language is sparse

Do **not** mix embedding spaces: only bge-m3 for clustering.

Credentials stay in env / local config file that is gitignored — never hardcode in committed scripts (current `run_eda.py` has inline password; new scripts should use env vars `SHAKTI_PG_*`).

---

## Phase-by-phase implementation

### Phase 0 — Project scaffolding (small)
1. Create `EDA/.venv` + `requirements.txt`; install deps.
2. Shared `scripts/lib/` with:
   - `config.py` (paths, DB DSN from env, read-only flag)
   - `pg.py` (connect with `default_transaction_read_only=on`, REPEATABLE READ, always ROLLBACK)
   - `duck.py` (open analytics.duckdb, register parquet)
3. Write `reports/KEY_FINDINGS.md` from existing `_raw_results.json` so the descriptive EDA is complete without re-querying Postgres.
4. Short `README.md` describing the pipeline and read-only rule.

### Phase 1 — Layer 0: Extract & clean base
`scripts/01_extract_base.py`

Single snapshot transaction → local tables:

| Table | Purpose |
|---|---|
| `post_facts` | Deduped posts: `(platform, native_id)` or text+author fallback; unified engagement slots + `has_engagement` flag |
| `comment_facts` | YouTube comments (~76K) |
| `post_embeddings` | Existing pgvector embeddings as parquet (~24.5K posts; **skip** comment embeddings) |
| `engagement_snapshots` | Append-only `(post_uid, extract_id, observed_at, likes, comments, shares, views)` |
| `collection_effort` | Platform-day: active channels/keywords, rows ingested |
| `extract_meta` | `extract_id`, snapshot_ts, row counts |

**Verification**
- Telegram `post_facts` ≈ 14.6K unique, not 23.3K
- No duplicate `post_uid`
- Reddit `has_engagement` = false for all
- Grep scripts: no `INSERT|UPDATE|CREATE|DROP|ALTER` on Postgres connection
- Second extract run **appends** snapshots (same post_uid, new extract_id)

### Phase 2 — Layer 1: Metric-readiness diagnostics
`scripts/02_eda_diagnostics.py` → `METRIC_READINESS.md` + figures

Must produce:
1. Dedup before/after volume (platform-day)
2. Scrape-lag distributions
3. **Engagement-vs-age curves** per platform on `log1p(E)` → age normaliser for virality/intensity (save fit params to DuckDB)
4. Collection-effort vs volume overlay
5. Panel density map (topic placeholder until Layer 2: use platform × day; re-run after ontology)
6. Language × platform matrix
7. Text-length floors for readability coverage
8. Heavy-tail rank plots → justify `log1p` + median/MAD z-scores

### Phase 3 — Layer 2: Ontology (hard dependency)
`scripts/03_ontology.py` → `ONTOLOGY.md`, `topic_dim`, `post_topic`

1. Embed missing **youtube_videos** (2,305) with **bge-m3** locally → local parquet only (never write back to Postgres).
2. Cluster L2-normalised embeddings: UMAP → HDBSCAN; k-means fallback; silhouette + seed stability.
3. Label clusters via discriminative TF-IDF + exemplar posts.
4. 2-level taxonomy: `domain → topic` (Conflict/ME, Conflict/RU-UA, India/Defence, Cyber, Markets, Tech/AI, …).
5. Map existing ~keywords into taxonomy; assign keyword-less Telegram by nearest centroid with **confidence + `unassigned` bucket** (never force-fit).
6. Optional multilingual NER (people/orgs/locs/weapons) if time permits; can be a follow-up after metrics land.
7. Manual spot-check ~20 cluster cards.

### Phase 4 — Layer 3: NLP scores
`scripts/04_nlp_scores.py` → `post_scores`

On L40S, batched:
- Language detection (fill unknown)
- Tone/sentiment continuous score
- Hate/toxicity probability
- Readability: Flesch/FK for English; LIX elsewhere; **not-applicable for zh/ja syllable-based**; length gate ≥ ~25 words

Every row: `model_name`, `model_version`, `scored_at`, `valid`.

**Domain caveat:** war/terror corpus → off-the-shelf toxicity over-fires on reported violence. Layer 5 must calibrate before publishing hate figures.

### Phase 5 — Layer 4: The 10 metrics
`scripts/05_metrics.py` → `metrics_daily` + `METRICS_SPEC.md`

Grain `(topic × platform × day)` with α=1 smoothing, `log1p`, robust z (median/MAD).

| Metric | Formula / notes |
|---|---|
| Growth rate | `g = ln((V_t+α)/(V_{t-1}+α))` + 7d/prior-7d; also on share-of-volume `s` |
| Momentum | `EMA_7(g) − EMA_21(g)` (21d cap given ~6wk history) |
| Half-life | Aggregate exponential decay post-peak + ACF/Hurst/vol of `g`; per-post only after snapshot accrual |
| Intensity | Robust-z of weighted `log1p(V)+log1p(E)` vs 28d entity baseline |
| Tone z-score | Entity-day mean tone vs trailing 28d baseline |
| Virality | Per-platform amp ratio → age-normalise → within-platform z; Reddit excluded / structural proxy |
| Reach / diversity | Reach = proxy (`Σviews` or `Σlog1p(E)`); Shannon entropy, 1−HHI, distinct authors |
| Readability | Length-gated; report scorable coverage |
| Ontology | From Layer 2 |
| Hate score | Prevalence above calibrated threshold + mean, z vs baseline |

Exclude Snapchat/Pinterest from time-series metrics (coverage gaps). Cap long windows at ~21d.

### Phase 6 — Layer 5: Validation
`scripts/06_validate.py` → `VALIDATION.md`

1. Deterministic re-run same `extract_id`
2. Sensitivity on windows 7/14/21d and α
3. Null control: shuffle timestamps → growth/momentum ≈ noise
4. Spot-check top-N posts per metric
5. **Calibration path**: export stratified ~200-post sample for hand-label (tone + hate); scripts compute P/R once labels exist (labels may be partial in first pass — document as TODO if user labels later)

### Phase 7 — Documentation & optional cron
- README: how to run full pipeline, env vars, read-only guarantee
- Optional: document daily cron for `01_extract_base.py` so engagement snapshots accrue (unlocks per-post half-life)

---

## Metric formulas (reference — full detail in METRICS_SPEC.md when built)

See Claude plan table (Layer 4). Key constraints already known:

- **No per-post engagement history** in Postgres → half-life is aggregate until local snapshots mature.
- **Collector ramp-up** confounds growth → always report share-of-volume and effort-normalised series.
- **Age confounds engagement** → Layer-1 age curve is required before virality/intensity comparisons.
- **Reach is a proxy** — no follower counts exist anywhere.

---

## Implementation order & parallelism

```
Phase 0 (scaffold)
    └─► Phase 1 (Layer 0 extract)     [needs DB]
            └─► Phase 2 (Layer 1 diagnostics)  [CPU]
            └─► Phase 3 (Layer 2 ontology)     [GPU for YT embeddings + clustering]
                    └─► Phase 4 (Layer 3 NLP)  [GPU]
                            └─► Phase 5 (Layer 4 metrics)
                                    └─► Phase 6 (validation)
```

Phases 2 and 3 can overlap after Layer 0 (diagnostics don't need topics; re-run density map after ontology).

**Estimated effort:** Layer 0–1 first day; Layer 2–3 GPU-heavy; Layer 4–5 formulas + docs.

---

## Verification checklist (gate each phase)

1. **Read-only:** only `SELECT` against Postgres; connection `default_transaction_read_only`; always `ROLLBACK`.
2. **Layer 0:** deduped counts match findings; second extract appends snapshots.
3. **Layer 1:** age-curve R² reported; dedup deltas ≈ 37% telegram / 36% reddit.
4. **Layer 2:** cluster stability threshold; Telegram assigned or `unassigned`; 20 cards inspected.
5. **Layer 3:** `valid=false` where lang unknown or below length floor; no hate numbers published pre-calibration.
6. **Layer 4:** re-run deterministic; shuffled control collapses growth/momentum.
7. **E2E:** clean `data/` → run 01…06 → open reports.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Live DB counts drift during long extract | Single REPEATABLE READ transaction; record extract_id |
| bge-m3 download large / offline later | Cache once; document model id+revision in post_embeddings metadata |
| Toxicity false positives on war reporting | Prevalence + calibrated threshold; never raw mean alone |
| Short history (~6 weeks) | Cap EMA/windows at 21d; sensitivity analysis flags fragile params |
| Telegram channel-based collection ≠ keyword collection | Ontology clusters + effort normalisation |
| Hardcoded DB password in `run_eda.py` | New scripts use env; leave old script as-is unless user wants cleanup |

---

## Out of scope (this pass)

- Writing anything back to `shaktidb_social`
- Full hand-labeling campaign (provide export + scoring hooks only)
- WhatsApp (empty table)
- Real-time dashboard / API
- Comment-level sub-topic clustering (comment embeddings not pulled)

---

## First actions after plan approval

1. Scaffold venv + `scripts/lib/` + `requirements.txt`.
2. Implement and run `01_extract_base.py` (read-only extract → DuckDB/parquet).
3. Write `KEY_FINDINGS.md` from existing EDA JSON.
4. Proceed Layer 1 → 2 → 3 → 4 → 5 in order above.

No re-litigation of the four locked decisions unless data makes a metric impossible — in that case document the blocker in `METRIC_READINESS.md` and degrade gracefully (e.g. Reddit virality structural proxy only).
