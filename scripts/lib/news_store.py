"""Derived store for the news engine (DuckDB). Source systems are never written.

Reliability model
  * every write path is an UPSERT keyed on a natural key -> reruns cannot create duplicates
  * a stage's work for a day is committed in ONE transaction together with its `day_stage`
    row, so a crash leaves either (data + status) or neither
  * `run_log` records each run; a run left 'running' by a crash is marked 'abandoned' by the
    next run that acquires the lock
  * an exclusive file lock (`NEWS_LOCK`) prevents overlapping runs (DuckDB is single-writer)
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

import duckdb
import pandas as pd

from . import news_config as C

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS kv (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS run_log (
  run_id VARCHAR PRIMARY KEY, mode VARCHAR, scope VARCHAR, date_from DATE, date_to DATE,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, status VARCHAR,
  params_json VARCHAR, summary_json VARCHAR, error VARCHAR);

CREATE TABLE IF NOT EXISTS day_stage (
  stage VARCHAR, scope VARCHAR, day DATE, status VARCHAR, n_in BIGINT, n_out BIGINT,
  run_id VARCHAR, updated_at TIMESTAMPTZ, error VARCHAR,
  PRIMARY KEY (stage, scope, day));

CREATE TABLE IF NOT EXISTS pending_label (
  source_table VARCHAR, article_id BIGINT, first_seen_at TIMESTAMPTZ, last_checked_at TIMESTAMPTZ,
  attempts INTEGER, published_at TIMESTAMPTZ, PRIMARY KEY (source_table, article_id));

CREATE TABLE IF NOT EXISTS taxonomy_node (
  code VARCHAR PRIMARY KEY, tier VARCHAR, name VARCHAR, parent VARCHAR, sort_order INTEGER,
  is_catch_all BOOLEAN, in_scope BOOLEAN, taxonomy_version VARCHAR, loaded_at TIMESTAMPTZ);

-- articles that were seen but are NOT used for trends, with the reason (never silently dropped)
CREATE TABLE IF NOT EXISTS excluded_article (
  source_table VARCHAR, article_id BIGINT, reason VARCHAR, published_at TIMESTAMPTZ,
  run_id VARCHAR, seen_at TIMESTAMPTZ, PRIMARY KEY (source_table, article_id));

CREATE TABLE IF NOT EXISTS article_fact (
  source_table VARCHAR, article_id BIGINT, os_id VARCHAR, os_index VARCHAR,
  published_at TIMESTAMPTZ, day DATE, ingested_at TIMESTAMPTZ, lag_hours DOUBLE,
  date_confidence VARCHAR, date_source VARCHAR, date_precision VARCHAR,
  has_label BOOLEAN, domain_id VARCHAR, topic_id VARCHAR, category_id VARCHAR, subtopic_id VARCHAR,
  category_conf DOUBLE, subtopic_conf DOUBLE, taxonomy_version VARCHAR,
  content_hash VARCHAR, is_duplicate BOOLEAN, dup_of VARCHAR,
  source_domain VARCHAR, url VARCHAR, title VARCHAR, gdelt_event_id VARCHAR,
  cluster_id VARCHAR, cluster_sim DOUBLE, cluster_note VARCHAR,
  run_id VARCHAR, processed_at TIMESTAMPTZ,
  PRIMARY KEY (source_table, article_id));

CREATE TABLE IF NOT EXISTS article_geo (
  source_table VARCHAR, article_id BIGINT,
  ev_country_iso VARCHAR, ev_adm1_code VARCHAR, ev_place VARCHAR, ev_lat DOUBLE, ev_lon DOUBLE,
  ev_precision VARCHAR, geo_source VARCHAR, geo_inconsistent BOOLEAN, geo_snapped BOOLEAN,
  loc_status VARCHAR, country_code_unmapped BOOLEAN, mention_countries_iso VARCHAR[],
  PRIMARY KEY (source_table, article_id));

CREATE TABLE IF NOT EXISTS event_cluster (
  event_id VARCHAR PRIMARY KEY, first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ,
  n_articles INTEGER, n_publishers INTEGER, n_copies INTEGER,
  domain_id VARCHAR, topic_id VARCHAR, category_id VARCHAR,
  rep_title VARCHAR, rep_url VARCHAR, rep_source_table VARCHAR, rep_article_id BIGINT,
  loc_country_iso VARCHAR, loc_adm1_code VARCHAR, loc_precision VARCHAR, loc_share DOUBLE,
  n_located INTEGER, loc_status VARCHAR, loc_confidence VARCHAR,
  centroid_sum BLOB, is_open BOOLEAN, is_event BOOLEAN,
  created_run VARCHAR, updated_run VARCHAR, updated_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS event_metrics (
  event_id VARCHAR PRIMARY KEY, computed_at TIMESTAMPTZ, as_of TIMESTAMPTZ,
  n_articles INTEGER, n_publishers INTEGER, n_copies INTEGER, syndication_share DOUBLE,
  first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ, duration_hours DOUBLE, active_days INTEGER,
  peak_day DATE, articles_24h INTEGER, articles_prev_24h INTEGER,
  growth_24h DOUBLE, accel_6h DOUBLE, publisher_pie DOUBLE, cross_source_ratio DOUBLE,
  half_life_days DOUBLE, half_life_ok BOOLEAN, persistence_note VARCHAR,
  support_ok BOOLEAN, support_note VARCHAR);

CREATE TABLE IF NOT EXISTS event_aoi_test (
  aoi_id VARCHAR, event_id VARCHAR, kind VARCHAR, as_of TIMESTAMPTZ,
  age_hours DOUBLE, n_articles_at_age INTEGER, n_publishers_at_age INTEGER,
  n_ref INTEGER, stratum VARCHAR, p DOUBLE, q DOUBLE, effect DOUBLE,
  support_ok BOOLEAN, support_note VARCHAR, alert_flag BOOLEAN,
  PRIMARY KEY (aoi_id, event_id, kind));

CREATE TABLE IF NOT EXISTS event_development (
  event_id VARCHAR, dev_id VARCHAR, aoi_id VARCHAR, detected_asof TIMESTAMPTZ,
  n_new INTEGER, n_new_publishers INTEGER, shift DOUBLE, shift_p DOUBLE, shift_q DOUBLE,
  coherence DOUBLE, rep_title VARCHAR, alert_flag BOOLEAN, member_keys VARCHAR,
  PRIMARY KEY (dev_id, aoi_id));

CREATE TABLE IF NOT EXISTS aoi_def (
  aoi_id VARCHAR PRIMARY KEY, kind VARCHAR, key VARCHAR, name VARCHAR, mode VARCHAR,
  min_precision VARCHAR, adhoc BOOLEAN, iso2 VARCHAR, parent_aoi VARCHAR, geometry_json VARCHAR,
  registered_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS aoi_day (
  aoi_id VARCHAR, day DATE, n_articles INTEGER, n_publishers INTEGER, n_events_started INTEGER,
  n_events_active INTEGER, n_located_event INTEGER, n_mention_only INTEGER,
  n_scope_unlocated INTEGER, n_scope_total INTEGER, in_window BOOLEAN,
  PRIMARY KEY (aoi_id, day));

CREATE TABLE IF NOT EXISTS node_aoi_day (
  aoi_id VARCHAR, level VARCHAR, node_code VARCHAR, day DATE, in_window BOOLEAN,
  V DOUBLE, exposure DOUBLE, V_rate DOUBLE, share_V DOUBLE,
  growth DOUBLE, growth_7d DOUBLE, accel DOUBLE, accel_smooth DOUBLE, momentum DOUBLE,
  baseline_mu DOUBLE, baseline_theta DOUBLE, baseline_days INTEGER, baseline_mean DOUBLE,
  baseline_dist VARCHAR, baseline_method VARCHAR,
  surprise DOUBLE, surprise_p DOUBLE, surprise_q DOUBLE, alert_flag BOOLEAN,
  burst_state DOUBLE, burst_intensity DOUBLE, burst_method VARCHAR, burst_support_ok BOOLEAN,
  burst_episode_id VARCHAR, n_publishers INTEGER, publisher_pie DOUBLE,
  support_n DOUBLE, support_ok BOOLEAN,
  PRIMARY KEY (aoi_id, level, node_code, day));

CREATE TABLE IF NOT EXISTS node_aoi_series (
  aoi_id VARCHAR, level VARCHAR, node_code VARCHAR, total_v DOUBLE, n_days INTEGER,
  ar1_phi DOUBLE, ar1_half_life_days DOUBLE, ar1_ljung_box_p DOUBLE, ar1_n_obs INTEGER,
  ar1_fit_ok BOOLEAN, PRIMARY KEY (aoi_id, level, node_code));

CREATE TABLE IF NOT EXISTS burst_episode (
  episode_id VARCHAR PRIMARY KEY, aoi_id VARCHAR, level VARCHAR, node_code VARCHAR,
  start_day DATE, peak_day DATE, end_day DATE, peak_V DOUBLE, total_V DOUBLE, n_days INTEGER,
  level_k INTEGER, burst_method VARCHAR, onset_slope DOUBLE, half_life_days DOUBLE, decay_ok BOOLEAN);

CREATE TABLE IF NOT EXISTS alert (
  alert_id VARCHAR PRIMARY KEY, kind VARCHAR, aoi_id VARCHAR, entity_type VARCHAR, entity_id VARCHAR,
  day DATE, as_of TIMESTAMPTZ, p DOUBLE, q DOUBLE, effect DOUBLE, severity VARCHAR,
  status VARCHAR, rationale VARCHAR, support_json VARCHAR,
  first_run VARCHAR, last_run VARCHAR, first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ,
  retracted_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS corpus_day (
  day DATE PRIMARY KEY, n_dated_total BIGINT, n_action_country BIGINT, n_action_geo BIGINT,
  n_mention_country BIGINT, source VARCHAR, measured_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS data_quality (
  run_id VARCHAR, key VARCHAR, value VARCHAR, recorded_at TIMESTAMPTZ, PRIMARY KEY (run_id, key));

CREATE TABLE IF NOT EXISTS metric_meta (
  metric_key VARCHAR PRIMARY KEY, display_name VARCHAR, family VARCHAR, formula VARCHAR,
  units VARCHAR, grain VARCHAR, direction VARCHAR, caveats VARCHAR, support_gate VARCHAR);
"""

PK = {  # table -> primary key columns, used by upsert()
    "kv": ["key"], "run_log": ["run_id"], "day_stage": ["stage", "scope", "day"],
    "pending_label": ["source_table", "article_id"], "taxonomy_node": ["code"],
    "excluded_article": ["source_table", "article_id"], "article_fact": ["source_table", "article_id"],
    "article_geo": ["source_table", "article_id"], "event_cluster": ["event_id"],
    "event_metrics": ["event_id"], "event_aoi_test": ["aoi_id", "event_id", "kind"],
    "event_development": ["dev_id", "aoi_id"], "aoi_def": ["aoi_id"], "aoi_day": ["aoi_id", "day"],
    "node_aoi_day": ["aoi_id", "level", "node_code", "day"],
    "node_aoi_series": ["aoi_id", "level", "node_code"], "burst_episode": ["episode_id"],
    "alert": ["alert_id"], "corpus_day": ["day"], "data_quality": ["run_id", "key"],
    "metric_meta": ["metric_key"],
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class LockBusy(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path | str = C.NEWS_DB, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        # TIMESTAMPTZ values are returned in the SESSION time zone. Pin it to UTC so every consumer (API,
        # reports, exports) sees UTC; without this a +05:30 machine showed UTC times 5.5h late.
        self.con.execute("SET TimeZone = 'UTC'")
        if not read_only:
            self.con.execute(DDL)
            self.set_kv("schema_version", str(SCHEMA_VERSION))

    # ----- low-level ----------------------------------------------------------
    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @contextmanager
    def tx(self):
        """One transaction. Everything inside commits together or not at all."""
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield self.con
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")

    def df(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def scalar(self, sql: str, params: Optional[list] = None):
        r = self.con.execute(sql, params or []).fetchone()
        return None if r is None else r[0]

    def upsert(self, table: str, df: pd.DataFrame) -> int:
        """INSERT OR REPLACE rows by primary key. Columns are matched BY NAME, so a frame may
        carry a subset of the table's columns only if the omitted ones are nullable (they are
        set NULL on replace - pass full rows when updating)."""
        if df is None or len(df) == 0:
            return 0
        unknown = set(df.columns) - set(self.columns(table))
        if unknown:
            raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
        dup = df.duplicated(subset=PK[table]).sum()
        if dup:
            raise ValueError(f"{table}: {dup} duplicate primary keys in the batch")
        self.con.register("_up", df)
        try:
            self.con.execute(f"INSERT OR REPLACE INTO {table} BY NAME SELECT * FROM _up")
        finally:
            self.con.unregister("_up")
        return len(df)

    def delete_where(self, table: str, where: str, params: Optional[list] = None) -> None:
        self.con.execute(f"DELETE FROM {table} WHERE {where}", params or [])

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self.con.execute(f"SELECT column_name FROM information_schema.columns "
                                               f"WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()]

    # ----- key/value ------------------------------------------------------------
    def get_kv(self, key: str) -> Optional[str]:
        return self.scalar("SELECT value FROM kv WHERE key = ?", [key])

    def set_kv(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", [key, value, now_utc()])

    # ----- runs ------------------------------------------------------------------
    def start_run(self, mode: str, scope: str, date_from, date_to, params: dict) -> str:
        run_id = f"{now_utc():%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
        # a crash leaves a 'running' row behind; the lock proves nobody is actually running
        self.con.execute("UPDATE run_log SET status='abandoned', finished_at=? WHERE status='running'",
                         [now_utc()])
        self.con.execute("INSERT INTO run_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         [run_id, mode, scope, date_from, date_to, now_utc(), None, "running",
                          json.dumps(params, default=str), None, None])
        return run_id

    def finish_run(self, run_id: str, status: str, summary: dict, error: Optional[str] = None) -> None:
        self.con.execute("UPDATE run_log SET status=?, finished_at=?, summary_json=?, error=? WHERE run_id=?",
                         [status, now_utc(), json.dumps(summary, default=str), error, run_id])

    # ----- per-day stage checkpoints -----------------------------------------------
    def mark_stage(self, stage: str, scope: str, day: dt.date, status: str, run_id: str,
                   n_in: int = 0, n_out: int = 0, error: Optional[str] = None) -> None:
        self.con.execute("INSERT OR REPLACE INTO day_stage VALUES (?,?,?,?,?,?,?,?,?)",
                         [stage, scope, day, status, n_in, n_out, run_id, now_utc(), error])

    def stage_done(self, stage: str, scope: str, day: dt.date) -> bool:
        return self.scalar("SELECT status FROM day_stage WHERE stage=? AND scope=? AND day=?",
                           [stage, scope, day]) == "done"

    def record_quality(self, run_id: str, key: str, value) -> None:
        self.con.execute("INSERT OR REPLACE INTO data_quality VALUES (?,?,?,?)",
                         [run_id, key, json.dumps(value, default=str), now_utc()])


@contextmanager
def run_lock(path: Path = C.NEWS_LOCK, wait: bool = False):
    """Exclusive advisory lock so two runs never overlap. Released automatically on process
    death, so a crashed run cannot wedge the scheduler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(f"another news run holds {path}")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} since={now_utc().isoformat()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
