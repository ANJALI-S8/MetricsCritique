# EDA Collaboration Pack — ShaktiDB Social Metrics

**Purpose:** Single place for anyone continuing this work (Claude, Grok, human) to understand what happened, what was decided, what was built, and what remains.

**Last updated:** 2026-07-29 (metrics v2 landed + dynamic UI shell)  
**Workspace:** `/home/hariramanan/NTRO/EDA`

---

## Start here (5 minutes)

| Order | Doc | What it gives you |
|------:|-----|-------------------|
| 1 | [01_SESSION_HISTORY.md](01_SESSION_HISTORY.md) | Full Claude → Grok timeline |
| 2 | [02_DECISIONS.md](02_DECISIONS.md) | Four locked design choices — **do not re-open lightly** |
| 3 | [03_ARCHITECTURE.md](03_ARCHITECTURE.md) | Foundation + metrics v2 + UI data flow |
| 4 | [04_CURRENT_STATE.md](04_CURRENT_STATE.md) | Counts, v2 quality gates, gaps |
| 5 | [05_HOW_TO_RUN.md](05_HOW_TO_RUN.md) | Env, pipeline, dashboard serve |
| 6 | [06_NEXT_STEPS.md](06_NEXT_STEPS.md) | Prioritized open work |
| 7 | [07_METRICS_V2.md](07_METRICS_V2.md) | v2 stack, defects D1–D5, alerting |
| 8 | [08_UI_DASHBOARD.md](08_UI_DASHBOARD.md) | Dynamic UI model (snapshot data, interactive filters) |
| 9 | [09_ADOPTION.md](09_ADOPTION.md) | Is this correct / adoptable for social media? |

### Authoritative live paths (under `../`)

| Path | Role |
|------|------|
| `data/metrics.duckdb` | **Metrics v2 DB** (query for analysis / alerts) |
| `data/analytics.duckdb` | Foundation extract (posts, scores, topics) |
| `docs/METRICS_V2_SPEC.md` | Statistical contract |
| `docs/STATISTICAL_REVIEW.md` | Empirical D1–D5 closure |
| `docs/ALERTING_KEYING.md` | **topic_id primary**, hashtag secondary |
| `UI/index.html` + `UI/data/metrics.json` | Interactive dashboard over a **data snapshot** |
| `UI/README.md` | How to serve the UI |

### Source archives

| File | Origin |
|------|--------|
| [00_ORIGINAL_CLAUDE_PLAN.md](00_ORIGINAL_CLAUDE_PLAN.md) | Claude metric-readiness plan |
| [00_CLAUDE_CHAT_EXPORT.md](00_CLAUDE_CHAT_EXPORT.md) | Claude chat export |
| [report_snapshots/](report_snapshots/) | Early Layer report copies |

---

## One-line summary

We turned a **live-ingesting** multi-platform OSINT social DB (`shaktidb_social`) into a **local analytics layer** (parquet + DuckDB) with a **statistically redesigned metrics v2** panel and an **interactive dashboard** over a rebuildable snapshot — production Postgres stays **read-only**.

---

## Quick status

| Item | Status |
|------|--------|
| Schema + readiness (Claude) | Done |
| Descriptive EDA HTML | Done |
| Design decisions locked | Done |
| Layers 0–3 (extract, diagnostics, ontology, NLP) | Done |
| Layer 4 **v1** (`05_metrics.py`) | Done — **superseded for decisions** |
| Metrics **v2** (NB surprise, FDR, acceleration, gates) | **Done** → `metrics.duckdb` |
| Statistical review docs | **Done** |
| Dashboard (interactive shell + snapshot JSON) | **Done** |
| Alerting keying note (topic vs hashtag) | **Done** |
| Hand-label hate/tone calibration | **Not done** |
| Daily extract cron | **Not done** |
| Notification service | **Not built** (data ready) |
| Live UI ↔ DB connection | **Out of scope by design** |

---

## Tree map

```
EDA/
  collaboration/          ← you are here
  docs/                   ← METRICS_V2_SPEC, STATISTICAL_REVIEW, ALERTING_KEYING
  scripts/
    01…04 foundation
    05…06 metrics v1 (legacy)
    07_metrics_v2.py  08_metrics_db.py
    review/stat_review.py
    lib/{config,pg,duck,stats_v2}.py
  data/
    analytics.duckdb      ← posts, embeddings meta, scores, topics
    metrics.duckdb        ← metrics only (authoritative for signals)
    metrics_stage/        ← parquet staging from 07
    parquet/  parquet_metrics/
  UI/
    index.html            ← slim interactive shell (~42 KB)
    data/metrics.json     ← snapshot fetched by the UI (~4 MB)
    assets/dash.js|css
    build_dashboard.py
    README.md
  reports/
```
