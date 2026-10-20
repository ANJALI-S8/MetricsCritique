"""Read-only Postgres helpers. Never issues DDL/DML against production."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Sequence

import psycopg2
import psycopg2.extensions

from .config import PG


@contextmanager
def read_only_snapshot() -> Iterator[tuple[psycopg2.extensions.connection, psycopg2.extensions.cursor, datetime]]:
    """Open a REPEATABLE READ, read-only connection and yield (conn, cur, snapshot_ts).

    Always ROLLBACKs on exit — never commits.
    """
    conn = psycopg2.connect(
        host=PG["host"],
        port=PG["port"],
        user=PG["user"],
        password=PG["password"],
        dbname=PG["dbname"],
        connect_timeout=PG["connect_timeout"],
    )
    conn.set_session(readonly=True, autocommit=False)
    cur = conn.cursor()
    try:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
        # Belt-and-suspenders: session already readonly, reinforce at DB level if supported
        try:
            cur.execute("SET default_transaction_read_only = on;")
        except Exception:
            pass
        snapshot_ts = datetime.now(timezone.utc)
        yield conn, cur, snapshot_ts
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        cur.close()
        conn.close()


def q(cur, sql: str, params: Optional[Sequence[Any]] = None) -> tuple[list[str], list[tuple]]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    return cols, rows


def q1(cur, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[tuple]:
    cur.execute(sql, params)
    return cur.fetchone()
