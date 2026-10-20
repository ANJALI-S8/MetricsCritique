#!/usr/bin/env python3
"""
Ingest data/metrics.duckdb and data/geo_ontology.duckdb into the delivery
Postgres (docker-compose in this folder) for another team to query directly.

Read-only against both source DuckDB files. Idempotent: each run does a full
schema-level refresh (DROP/CREATE), so re-running after the geo-ontology
risk/theme reclassification fix lands will pick up the corrected data.

Usage:
    docker compose up -d          # starts the delivery Postgres (see .env)
    python3 ingest.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
EDA_ROOT = HERE.parent


def load_env() -> dict:
    env = {}
    for line in (HERE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


ENV = load_env()
PG_DSN = (
    f"host=localhost port={ENV['POSTGRES_PORT']} "
    f"dbname={ENV['POSTGRES_DB']} user={ENV['POSTGRES_USER']} "
    f"password={ENV['POSTGRES_PASSWORD']}"
)

# (source db, schema in Postgres, table, verdict, caveat)
MANIFEST = [
    ("metrics.duckdb", "metrics_v2", "metrics_daily", "SOUND", "Layer-4 metrics v2. 46/46 statistical self-checks independently verified. Topic x platform x day grain."),
    ("metrics.duckdb", "metrics_v2", "metrics_topic_day", "SOUND", "Platform roll-up of metrics_daily."),
    ("metrics.duckdb", "metrics_v2", "tag_metrics_daily", "SOUND", "Hashtag facet. Hashtags cover only ~25% of posts -- use topic_id as the primary key, not tags, for anything comprehensive."),
    ("metrics.duckdb", "metrics_v2", "tag_dim", "SOUND", "Hashtag dimension table."),
    ("metrics.duckdb", "metrics_v2", "topic_dim", "PARTIAL", "Original ontology, coarsened by a corpus-size-scaling bug (3 clusters, one holds ~92% of posts). Superseded by geo_ontology.topic_dim_v2 below, which is a real (though not perfect) fix."),
    ("metrics.duckdb", "metrics_v2", "platform_dim", "SOUND", "All 8 platforms present (snapchat/pinterest included, support-gated)."),
    ("metrics.duckdb", "metrics_v2", "alert_candidates", "SOUND", "FDR q<0.05 gated alerts, topic x platform x day."),
    ("metrics.duckdb", "metrics_v2", "burst_episodes", "SOUND", ""),
    ("metrics.duckdb", "metrics_v2", "persistence", "SOUND", "AR(1) half-life / volatility per topic x platform series."),
    ("metrics.duckdb", "metrics_v2", "metric_meta", "SOUND", "Documents every metric's formula, units, direction -- read this first."),
    ("metrics.duckdb", "metrics_v2", "run_meta", "SOUND", "Run parameters, window, extract lineage."),
    ("metrics.duckdb", "metrics_v2", "null_control", "SOUND", "Statistical validation artifact (temporal-structure null control)."),
    ("metrics.duckdb", "metrics_v2", "sensitivity_checks", "SOUND", "Parameter-sensitivity validation artifact."),
    ("metrics.duckdb", "metrics_v2", "v_alerts", "SOUND", "View: same as alert_candidates."),
    ("metrics.duckdb", "metrics_v2", "v_metrics_live", "SOUND", "View: metrics_daily filtered to support_ok=true."),

    ("geo_ontology.duckdb", "geo_ontology", "topic_dim_v2", "PARTIAL", "Superseded by topic_dim_v3 below (fixed-k=33 clustering) -- kept for comparison/forensics. 3->11 clusters, largest-cluster share 85-94%->23-29% (real ~3x fix). One residual cluster (~25.7% of corpus) is NOT topically coherent."),
    ("geo_ontology.duckdb", "geo_ontology", "post_topic_v2", "PARTIAL", "Assignments for topic_dim_v2 above. Superseded by post_topic_v3."),
    ("geo_ontology.duckdb", "geo_ontology", "topic_dim_v3", "PARTIAL (substantial improvement)", "Fixed-k=33 cuML KMeans clustering on the same bge-m3 embeddings, real content read and named per cluster (not just top-terms). Largest cluster only 5.66% of corpus (vs v2's 23-29%, vs the original bug's 85-94%) -- no mega-cluster at all. K=33 also revealed real internal structure v2's 11 clusters hid: what was one 'Middle-East' mega-topic split into 6 genuine sub-narratives (tactical strike tickers, Gulf alerts, Hormuz/energy angle, Israel-US intel angle, opinion/diplomacy, etc), Russia-Ukraine similarly split into 4. ~5-6 of the 33 are marked coherent=false: two are data artifacts (cluster g011 is almost entirely dead/404 LinkedIn pages; g027 is generic LinkedIn career content mistagged with geopolitical keywords), the rest are loose multi-theme miscellany rather than a single sharp topic -- see the `coherent` column and `description` per row."),
    ("geo_ontology.duckdb", "geo_ontology", "post_topic_v3", "PARTIAL (substantial improvement)", "Assignments for topic_dim_v3. 21,228 posts assigned via KMeans (assign_method='kmeans_core'); the 2,128 posts with no embedding are explicitly 'unassigned' (assign_method='no_embedding'), never silently dropped."),
    ("geo_ontology.duckdb", "geo_ontology", "entity_dim", "PARTIAL", "Base entity extraction verified good (2x25-sample precision checks: 25/25 and 25/25). BUT only ~1.5% of mentions have a detailed subtype (political_leader/military_leader/diplomat/ministry/SOE/armed_group/port/etc) -- 98.5% are generic PERSON/ORG/LOCATION only. A subtype fix was in progress but not complete as of this ingest."),
    ("geo_ontology.duckdb", "geo_ontology", "entity_mentions", "PARTIAL", "Same caveat as entity_dim. Also: ~2.5% of gazetteer-matched entities have an entity_type inconsistent with their subtype (e.g. a named political leader tagged as ORG instead of PERSON)."),
    ("geo_ontology.duckdb", "geo_ontology", "risk_labels", "PARTIAL (fix applied, not yet re-verified)", "The reclassification fix (bi-encoder recall net + multilingual NLI final-decision ensemble, scripts/12_risk_theme_classification.py) completed and this table was regenerated from it (11,733 rows, down from 20,777 -- more conservative/precise). The fix's own calibration sample scored 72%/46% precision/recall (lenient) vs the old flat-threshold's 29-45%. This has NOT yet been independently re-verified with a fresh hand-judged sample the way the original UNSOUND finding was -- treat as improved-but-not-yet-proven until that check happens."),
    ("geo_ontology.duckdb", "geo_ontology", "theme_labels", "PARTIAL (fix applied, not yet re-verified)", "Same fix and same caveat as risk_labels (7,876 rows, down from 24,566)."),
    ("geo_ontology.duckdb", "geo_ontology", "entity_metrics_daily", "SOUND methodology / PARTIAL inputs", "Timeline-metrics math independently verified (46/46 self-checks, 0 mismatches vs re-derived NB baselines and BH q-values across 10,762 cells). But built from entity_dim's sparse subtypes -- will regenerate once the entity-subtype fix lands."),
    ("geo_ontology.duckdb", "geo_ontology", "risk_metrics_daily", "SOUND methodology / PARTIAL inputs", "Same verified math as entity_metrics_daily. Regenerated from the fixed risk_labels above (11_geo_timeline_metrics.py re-run, 46/46 self-checks still pass) -- inherits that table's PARTIAL/not-yet-re-verified status rather than the original UNSOUND."),
    ("geo_ontology.duckdb", "geo_ontology", "theme_metrics_daily", "SOUND methodology / PARTIAL inputs", "Same caveat as risk_metrics_daily."),
    ("geo_ontology.duckdb", "geo_ontology", "geo_alert_candidates", "SOUND methodology / PARTIAL inputs", "FDR-gated alerting math is sound (490->463 alerts after regeneration, reflecting the more precise labels). Alerts on risk/theme dimensions inherit the PARTIAL/not-yet-re-verified label status; entity-dimension alerts rest on the most solid ground -- filter on dimension_type if precision matters for your use."),
]


def main() -> int:
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{PG_DSN}' AS pg (TYPE postgres)")

    metrics_path = str(EDA_ROOT / "data" / "metrics.duckdb")
    geo_path = str(EDA_ROOT / "data" / "geo_ontology.duckdb")
    con.execute(f"ATTACH '{metrics_path}' AS metrics (READ_ONLY)")
    con.execute(f"ATTACH '{geo_path}' AS geo (READ_ONLY)")

    for schema in ("metrics_v2", "geo_ontology"):
        con.execute(f"DROP SCHEMA IF EXISTS pg.{schema} CASCADE")
        con.execute(f"CREATE SCHEMA pg.{schema}")

    now = datetime.now(timezone.utc).isoformat()
    counts = {}
    for src_db, schema, table, verdict, caveat in MANIFEST:
        alias = "metrics" if src_db == "metrics.duckdb" else "geo"
        print(f"  {schema}.{table} ...", end=" ", flush=True)
        con.execute(f'CREATE TABLE pg.{schema}."{table}" AS SELECT * FROM {alias}.main."{table}"')
        n = con.execute(f'SELECT COUNT(*) FROM pg.{schema}."{table}"').fetchone()[0]
        counts[f"{schema}.{table}"] = n
        print(f"{n:,} rows")

    con.execute("DROP SCHEMA IF EXISTS pg.public CASCADE")
    con.execute("CREATE SCHEMA IF NOT EXISTS pg.public")
    con.execute("""
        CREATE TABLE pg.public._delivery_manifest (
            source_db TEXT, schema_name TEXT, table_name TEXT,
            verdict TEXT, caveat TEXT, row_count BIGINT, ingested_at TEXT
        )
    """)
    for src_db, schema, table, verdict, caveat in MANIFEST:
        con.execute(
            "INSERT INTO pg.public._delivery_manifest VALUES (?,?,?,?,?,?,?)",
            [src_db, schema, table, verdict, caveat, counts[f"{schema}.{table}"], now],
        )

    print(f"\nDone. {len(MANIFEST)} tables ingested across 2 schemas + _delivery_manifest.")
    print("Read docs/METRICS_V2_SPEC.md and docs/GEOPOLITICAL_ONTOLOGY_SPEC.md in the source")
    print("repo for full detail behind the verdicts in _delivery_manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
