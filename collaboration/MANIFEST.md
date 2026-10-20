# Collaboration pack manifest

**Last updated:** 2026-07-30, latest (risk/theme remediation completed; fixed-k=33 cuML
re-clustering added as `topic_dim_v3`/`post_topic_v3`, all 33 clusters named from direct
text sampling; delivery Postgres refreshed with both. See `04_CURRENT_STATE.md` top
section and `docs/GEOPOLITICAL_ONTOLOGY_SPEC.md` §7 for full detail. The independent
re-verification of the fixed risk/theme labels against a fresh hand-judged sample is
still open — do not treat that fix as proven, only improved.)

## Documents in this folder

| File | Description |
|------|-------------|
| `README.md` | Index / start here |
| `00_ORIGINAL_CLAUDE_PLAN.md` | Full Claude metric-readiness plan |
| `00_CLAUDE_CHAT_EXPORT.md` | Claude session chat export |
| `01_SESSION_HISTORY.md` | Phases A–F timeline |
| `02_DECISIONS.md` | Four locked design decisions |
| `03_ARCHITECTURE.md` | Foundation + metrics v2 + UI flow |
| `04_CURRENT_STATE.md` | Counts, v2 quality, dashboard model, geo-ontology status |
| `05_HOW_TO_RUN.md` | Full runbook |
| `06_NEXT_STEPS.md` | Prioritized open work |
| `07_METRICS_V2.md` | v2 stack, D1–D5, alerting |
| `08_UI_DASHBOARD.md` | Dynamic UI / snapshot data model |
| `09_ADOPTION.md` | Correctness & social-media adoption |
| `MANIFEST.md` | This file |
| `report_snapshots/` | Early Layer report copies (v1 era) |

### Geo-ontology layer (2026-07-30, separate DB, additive)

| File | Description |
|------|-------------|
| `../docs/GEO_ONTOLOGY_CONTEXT.md` | Shared environment/taxonomy reference every geo-ontology agent reads (entity types, risk categories, themes, known bugs) |
| `../docs/GEOPOLITICAL_ONTOLOGY_SPEC.md` | As-built record + verdict-by-subsystem verification, including the live remediation state (§6) |
| `../data/geo_ontology.duckdb` | Entities/risk/theme/timeline tables — separate from `data/metrics.duckdb`, additive, never overwrites it |
| `../scripts/09_ontology_v2_cluster_fix.py`, `10_entity_extraction.py`, `11_geo_timeline_metrics.py`, `12_risk_theme_classification.py` | Committed, reproducible pipeline scripts for this layer |
| `../scripts/notify_poller.py` | Reference notification poller (no delivery channel wired — logs only) |

### Delivery database (2026-07-30, separate handoff artifact)

| File | Description |
|------|-------------|
| `../delivery/docker-compose.yml`, `.env` | Isolated Postgres container (own port/network/volume) for handing metrics + geo-ontology tables to another team as plain SQL, no DuckDB/Python dependency on their side |
| `../delivery/ingest.py` | Idempotent DuckDB→Postgres copy, with a `MANIFEST` list carrying a verdict + caveat per table (SOUND/PARTIAL/UNSOUND) into a `_delivery_manifest` table the receiving team can query directly |
| `../delivery/README.md` | Connection info + schema/caveat guide for the receiving team (host left blank — not yet provided) |

## Not duplicated here (live under `EDA/`)

| Path | Why live |
|------|----------|
| `data/analytics.duckdb` | Foundation extract (large) |
| `data/metrics.duckdb` | Authoritative metrics v2 |
| `data/metrics_stage/**`, `parquet/**`, `parquet_metrics/**` | Staging / mirrors |
| `UI/data/metrics.json` | Dashboard snapshot (~4 MB) |
| `docs/*.md` | Specs and review (canonical) |
| `reports/**` | Generated reports / figures |
| `scripts/**` | Code of record |
| `.venv/**` | Local environment |

## Pointer from project root

See also `../README.md` (operator runbook) and `../UI/README.md` (dashboard serve).
