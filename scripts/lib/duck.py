"""Local DuckDB + parquet helpers for the analytics layer."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

from .config import DATA_DIR, DUCKDB_PATH, PARQUET_DIR


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    ensure_dirs()
    return duckdb.connect(str(DUCKDB_PATH), read_only=read_only)


def write_parquet(df: pd.DataFrame, name: str) -> Path:
    """Write a dataframe as parquet under data/parquet/<name>/part-0.parquet."""
    ensure_dirs()
    out_dir = PARQUET_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "part-0.parquet"
    df.to_parquet(path, index=False)
    return path


def append_parquet(df: pd.DataFrame, name: str) -> Path:
    """Append rows to a multi-part parquet directory (engagement_snapshots)."""
    ensure_dirs()
    out_dir = PARQUET_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("part-*.parquet"))
    next_idx = 0
    if existing:
        # part-0.parquet → 0
        idxs = []
        for p in existing:
            try:
                idxs.append(int(p.stem.split("-")[1]))
            except (IndexError, ValueError):
                pass
        next_idx = (max(idxs) + 1) if idxs else len(existing)
    path = out_dir / f"part-{next_idx}.parquet"
    df.to_parquet(path, index=False)
    return path


def register_parquet_views(
    con: duckdb.DuckDBPyConnection,
    names: Optional[list[str]] = None,
    *,
    skip_if_table: bool = True,
) -> None:
    """Create or replace views over parquet dirs for convenient SQL.

    If a TABLE with the same name already exists and skip_if_table is True,
    leave the table alone (tables from replace_table_from_df take precedence).
    """
    if names is None:
        names = [p.name for p in PARQUET_DIR.iterdir() if p.is_dir()] if PARQUET_DIR.exists() else []
    existing_tables = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='BASE TABLE'"
        ).fetchall()
    }
    for name in names:
        if skip_if_table and name in existing_tables:
            continue
        glob = str(PARQUET_DIR / name / "*.parquet")
        # Drop leftover view of same name if any
        con.execute(f"DROP VIEW IF EXISTS {name};")
        con.execute(
            f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{glob}', union_by_name=true);"
        )


def replace_table_from_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    con.execute(f"DROP TABLE IF EXISTS {table};")
    con.register("_tmp_df", df)
    con.execute(f"CREATE TABLE {table} AS SELECT * FROM _tmp_df;")
    con.unregister("_tmp_df")
