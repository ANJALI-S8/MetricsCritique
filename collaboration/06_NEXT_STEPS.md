# Next steps (prioritized)

## P0 — Correctness / publishability

1. **Hand-label calibration sample**  
   - `reports/calibration_sample.csv`  
   - Fill `label_tone` (−1/0/1), `label_hate` (0/1).  
   - Only then flip `hate_calibrated` / publish hate-related figures.

2. **Re-embed YouTube videos (bge-m3) when GPU is free**  
   - Improves ontology + novelty.  
   - Then: `03_ontology.py` → `07` → `08` → `UI/build_dashboard.py`.

3. ~~Guard sparse robust-z~~ — **Done in v2**. Do not re-open v1 z-metrics.

## P1 — Operational

4. **Daily cron for `01_extract_base.py`**  
   - Accrue `engagement_snapshots` for future per-post half-life.

5. **Human review of topic cards**  
   - Stable names/domains for briefings (`topic_dim` / ONTOLOGY.md).

6. **Scheduled metrics refresh**  
   - Cron or manual: `07` → `08` → `build_dashboard.py` after extract growth.

## P2 — Productization

7. ~~Interactive dashboard~~ — **Done** (snapshot + filters). UX polish optional.  
8. **Notification service** — consume `alert_candidates` / `metrics.json` `derived.alerts`; **topic_id primary** (`docs/ALERTING_KEYING.md`).  
9. Multilingual NER (people/orgs/locations/weapons) — deferred.  
10. Comment-level sub-topics — deferred (comment embeddings not pulled).  
11. Remove hardcoded credentials from legacy `run_eda.py`.

## Explicit non-goals (unless requirements change)

- Live UI ↔ production DB  
- Writing analytics tables back to Postgres  
- Hashtag-only alerting as sole key  
- WhatsApp / dead Snapchat-Pinterest time series without collector fixes  

## New agent session checklist

1. Read `collaboration/README.md`, then `07_METRICS_V2.md` + `08_UI_DASHBOARD.md` + `09_ADOPTION.md`.  
2. Prefer `metrics.duckdb` over v1 tables.  
3. Keep production **read-only**.  
