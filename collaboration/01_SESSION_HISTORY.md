# Session history

## Phase A — Claude (planning + descriptive EDA)

**Source:** `EDA/ClaudeChat.md` (if present) and Claude plan  
`~/.claude/plans/whimsical-enchanting-marshmallow.md`  
→ archived here as [`00_ORIGINAL_CLAUDE_PLAN.md`](00_ORIGINAL_CLAUDE_PLAN.md)

### What Claude did

1. **Connected to live Postgres** `shaktidb_social` (`<PG_HOST>:13366`).
2. **Inventoried schema** — 10 base tables + `social_posts_unified` / `youtube_comments_unified`.
3. **Mapped 10 target metrics** to available columns and gaps:
   - growth rate / momentum / half-life / intensity  
   - tone z-score / virality / reach–author diversity  
   - readability / ontology / hate-speech score  
4. **Profiled blocking facts** (duplicates, no post history, keyword-less Telegram, scrape lag, multilingual, engagement gaps, short text, embedding gaps).
5. **Asked four design questions**; user locked recommended options (see [02_DECISIONS.md](02_DECISIONS.md)).
6. **Drafted the 5-layer metric-readiness plan** (local parquet/DuckDB; prod read-only).
7. **Ran descriptive EDA** → `scripts/run_eda.py` producing:
   - `reports/EDA_Report.html`
   - `reports/figures/*.png` (26 charts)
   - `reports/_raw_results.json` / `_R_full.json`
8. **Did not implement** Layers 0–5 or write `KEY_FINDINGS.md` (script mentioned it; file was missing).

### Claude plan layers (design only at end of Claude session)

| Layer | Intent |
|-------|--------|
| 0 | Read-only extract → clean local base + engagement snapshots |
| 1 | Metric-readiness diagnostics |
| 2 | Ontology (embeddings + clusters) |
| 3 | NLP scoring (tone, hate, readability) |
| 4 | Ten metrics at topic×platform×day |
| 5 | Validation + calibration |

---

## Phase B — Grok (execution, 2026-07-29)

**Trigger:** User: “EDA/ClaudeChat.md is the chat from claude. I want you to plan and take it forward.”

### What Grok did

1. Recovered full Claude plan from `~/.claude/plans/whimsical-enchanting-marshmallow.md`.
2. Wrote execution plan; user approved.
3. Scaffolded project:
   - `EDA/.venv` + `requirements.txt`
   - `scripts/lib/{config,pg,duck}.py`
   - `README.md`
   - `reports/KEY_FINDINGS.md` from existing EDA JSON
4. **Implemented and ran** Layers 0–5 (see table below).
5. Handled infrastructure realities:
   - L40S occupied by **vLLM** (~43 GB) during ontology/NLP → YouTube re-embed skipped; NLP ran on **CPU** (~28 min for 18k texts).
   - Torch/transformers reused from Entity_extraction venv via `PYTHONPATH`.
6. Relabelled clusters after first TF-IDF pass produced stopword-heavy labels.

### Layer execution log

| Layer | Script | Outcome |
|-------|--------|---------|
| 0 | `01_extract_base.py` | Success. Telegram 23,325 → **14,595** deduped. 20,753 post_facts. Snapshots append-only (2 extract_ids after re-runs). |
| 1 | `02_eda_diagnostics.py` | Success. `METRIC_READINESS.md`, age_norm_params, layer1 figures. |
| 2 | `03_ontology.py` | Success with caveat. UMAP+HDBSCAN → **8 clusters**, ~21% unassigned. **0** new YouTube embeddings (CUDA OOM / GPU held). Relabel improved domain names. |
| 3 | `04_nlp_scores.py` | Success on CPU. 18,299 scored; tone mean ≈ −0.27; raw hate mean ≈ 0.04. |
| 4 | `05_metrics.py` | Success. **7,340** `metrics_daily` rows + half_life + topic_day rollup. |
| 5 | `06_validate.py` | Success. `VALIDATION.md`, `calibration_sample.csv` (200 stratified, labels empty). |

### Models used (Layer 2–3)

| Role | Model |
|------|--------|
| Embeddings (existing in DB + target for YT) | `BAAI/bge-m3` (1024-d) |
| Sentiment / tone | `cardiffnlp/twitter-xlm-roberta-base-sentiment` |
| Toxicity / hate | `unitary/multilingual-toxic-xlm-roberta` |

### Notable fixes during build

- DuckDB view vs table name clash → `register_parquet_views(skip_if_table=True)`.
- Sentiment tokenizer / protobuf → `use_fast=False` + install `protobuf`, `sentencepiece`.
- Cluster labels dominated by stopwords → TF-IDF `stop_words=english`, `max_df=0.4`, drop URL tokens.
- GPU OOM path → smaller batch + CPU fallback when free VRAM &lt; 4 GB.

---

## Phase C — This collaboration pack

User asked to put “all this that has happened so far” under `EDA/collaboration/` so future work can continue without re-deriving context.

---

## Phase D — Claude Metrics v2 (session limit)

Claude reviewed v1 output (not just the pipeline), found **five defects** (robust-z explosion, 91% sparse panel, 82% out-of-window cells, inverted null control, sensitivity self-test bug), wrote:

- `docs/METRICS_V2_SPEC.md`, `docs/IDEATION.md`
- `scripts/lib/stats_v2.py`, `07_metrics_v2.py`, `08_metrics_db.py`
- `scripts/review/stat_review.py`
- `UI/build_dashboard.py` + fixture + assets
- Ran `07` successfully → full `data/metrics_stage/`

Three parallel agents (metrics DB publish, statistical review docs, dashboard build) **failed on API session limit** before landing.

Also: hashtag coverage measured at ~15–18% → recommendation to key alerts on **topic_id**, not hashtag alone.

---

## Phase E — Grok continuation (post session limit)

1. Ran `08_metrics_db.py` → `data/metrics.duckdb` (1,522 daily cells, 34 alerts).  
2. Fixed UI NaN JSON serialization; first dashboard build in **real** mode (data embedded).  
3. Ran `stat_review.py` → `reports/stat_review_results.json`.  
4. Wrote `docs/STATISTICAL_REVIEW.md`, `STATISTICAL_APPENDIX.md`, `ALERTING_KEYING.md`.  
5. Documented adoption assessment (v2 adoptable as OSINT decision-support; not hashtag-only product).

---

## Phase F — Dynamic UI (snapshot data, interactive shell)

**Clarification from user:** “Dynamic” means **interactive UI**, **not** live queries to Postgres/DuckDB while browsing.

1. Split dashboard into:
   - slim `UI/index.html` shell (~42 KB) + `assets/dash.js` / `dash.css`
   - full snapshot `UI/data/metrics.json` (~4 MB)
2. `dash.js` **fetches** `metrics.json`, then filters re-scope all charts client-side.
3. Rebuild snapshot after metrics change: `python UI/build_dashboard.py`.
4. Serve with static HTTP (`cd UI && python3 -m http.server 8877`) — required so the shell can load JSON.
5. Optional offline mode: `--static-embed` still embeds data for `file://`.
6. Explicitly **not** built: live API that re-reads DuckDB on every page load (rejected for this phase).

See [08_UI_DASHBOARD.md](08_UI_DASHBOARD.md).