# EDA metrics delivery — connection info for the receiving team

A live Postgres database containing the ShaktiDB social-media analytics: Metrics v2
(verified, SOUND) plus the geo-ontology layer (entities/risk/theme/timeline — mixed
verification status, see below). This is a **copy for querying**, not the source of
truth — the source is `data/metrics.duckdb` / `data/geo_ontology.duckdb` in this repo.

## Connect

```
host:     <this machine's address> (ask the sender — not written here on purpose)
port:     25432
database: eda_delivery
user:     eda_delivery
password: see .env in this folder
```

```
psql -h <host> -p 25432 -U eda_delivery -d eda_delivery
```

## Read this first: `public._delivery_manifest`

Every ingested table has a row here with a `verdict` (SOUND / PARTIAL / UNSOUND) and a
`caveat` explaining exactly what was checked and what wasn't. Query it before trusting
any table:

```sql
SELECT schema_name, table_name, verdict, caveat FROM public._delivery_manifest
ORDER BY verdict, schema_name, table_name;
```

**Short version**: everything in `metrics_v2` is SOUND (46/46 independent statistical
self-checks passed) except `topic_dim`, which is superseded by `geo_ontology.topic_dim_v2`.
In `geo_ontology`: entity data and the topic-clustering fix are real improvements with
one documented residual defect each (PARTIAL); **`risk_labels` and `theme_labels` are
UNSOUND** — measured 12–45% precision across three independent samples, confirmed recall
gaps, do not treat as ground truth. Everything downstream of them (`risk_metrics_daily`,
`theme_metrics_daily`, the risk/theme portion of `geo_alert_candidates`) has sound
*math* but unsound *inputs* — the alerting/statistical methodology is independently
verified (0 mismatches on recomputation), but will only be trustworthy once the
labels are fixed. **Entity-dimension alerts in `geo_alert_candidates` are more reliable
than risk/theme-dimension alerts right now** — filter on `dimension_type`.

A reclassification fix for `risk_labels`/`theme_labels` (bi-encoder + multilingual NLI
ensemble) was running in the background as of this delivery. Re-run `ingest.py` after
it completes to refresh this database with corrected data.

## Schemas

- `metrics_v2` — the core, fully verified pipeline: growth, momentum, intensity,
  virality, tone, reach, author diversity, readability, FDR-gated alerts. Grain:
  topic × platform × day (`metrics_daily`), rolled up (`metrics_topic_day`), and by
  hashtag (`tag_metrics_daily` — hashtags cover only ~25% of posts, so prefer
  `topic_id` over tags for anything comprehensive). Start with `metric_meta` for
  every metric's formula/units/direction, and `platform_dim`/`topic_dim` for dimensions.
- `geo_ontology` — entities (`entity_dim`, `entity_mentions`: political/military
  leaders, diplomats, business executives, ministries, state-owned enterprises,
  armed groups, international orgs, ports, border towns, economic zones, pipelines,
  railways — though only ~1.5% of mentions have a specific subtype so far, see
  manifest), risk categories and thematic dimensions (`risk_labels`, `theme_labels` —
  UNSOUND, see above), and timeline metrics/alerts for all three (`entity_metrics_daily`,
  `risk_metrics_daily`, `theme_metrics_daily`, `geo_alert_candidates`).

## Refreshing

```
cd delivery
docker compose up -d      # if the container isn't already running
python3 ingest.py         # idempotent full refresh, safe to re-run any time
```

`ingest.py` drops and recreates both schemas from the current state of the two source
DuckDB files each run — it does not touch production Postgres (`shaktidb_social`),
which stays read-only throughout this entire project.

## Further detail

Full verification write-ups, formulas, and caveats live in the source repo:
`docs/METRICS_V2_SPEC.md`, `docs/STATISTICAL_REVIEW.md`, `docs/GEOPOLITICAL_ONTOLOGY_SPEC.md`,
`docs/GEO_ONTOLOGY_CONTEXT.md`, and `collaboration/04_CURRENT_STATE.md` for the running
narrative of what's been built, verified, and is still open.
