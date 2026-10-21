"""Strictly read-only access to the production news PostgreSQL and OpenSearch.

Enforcement is layered, because "read-only" must not rest on one mechanism:

  PostgreSQL  - session opened `readonly=True`, `default_transaction_read_only=on`, a
                statement timeout, REPEATABLE READ, and always ROLLBACK. Additionally the
                client refuses any statement that is not a single SELECT/WITH query.
  OpenSearch  - a whitelist client: only `POST /<index>/_search`, `POST /<index>/_count`
                and `GET /<index>/_mapping` are reachable. Anything else (index, update,
                delete, bulk, scroll, PIT, reindex, ...) raises PermissionError before a
                request is made. Paging uses `search_after`, so no server-side cursor is
                ever created.

The derived data lives in a separate DuckDB file (news_store.py); nothing is written back.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg2
import psycopg2.extras
import requests

from . import news_config as C

# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------
_SELECT_ONLY = re.compile(r"^\s*(with\b|select\b)", re.I)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|"
    r"vacuum|reindex|cluster|refresh|comment|lock|listen|notify|set\s+role)\b", re.I)


def assert_read_only_sql(sql: str) -> None:
    """Raise PermissionError unless `sql` is one SELECT/WITH statement with no write verbs."""
    body = sql.strip().rstrip(";")
    if ";" in body:
        raise PermissionError("multiple statements are not allowed")
    if not _SELECT_ONLY.match(body):
        raise PermissionError("only SELECT/WITH statements are allowed")
    # strip string literals before looking for verbs so '...update...' text is harmless
    scrub = re.sub(r"'(?:[^']|'')*'", "''", body)
    if _FORBIDDEN.search(scrub):
        raise PermissionError("statement contains a write/DDL keyword")


class ReadOnlyPG:
    """Read-only client. Each query runs in its own short read-only transaction that is rolled back
    immediately (never idle-in-transaction, which servers kill), and a dropped connection is
    re-established transparently - a long run's PostgreSQL reads must survive minutes spent in
    OpenSearch-heavy stages."""

    def __init__(self, statement_timeout_s: int = 120, retries: int = 3):
        self._timeout = statement_timeout_s
        self._retries = retries
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        s = C.pg_settings()
        self._conn = psycopg2.connect(
            host=s["host"], port=s["port"], user=s["user"], password=s["password"],
            dbname=s["dbname"], connect_timeout=30, application_name="news_engine_ro",
            options=f"-c default_transaction_read_only=on -c statement_timeout={self._timeout * 1000}",
        )
        self._conn.set_session(readonly=True, autocommit=False)

    def _drop(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
        assert_read_only_sql(sql)
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    rows = [dict(r) for r in cur.fetchall()]
                self._conn.rollback()      # end the read-only txn now; nothing is ever committed
                return rows
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last = e
                self._drop()
                time.sleep(min(20, 2 ** attempt))
        raise RuntimeError(f"PostgreSQL unavailable after {self._retries} attempts: {last}")

    def close(self) -> None:
        try:
            if self._conn is not None and not self._conn.closed:
                self._conn.rollback()
        except Exception:
            pass
        finally:
            self._drop()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# --------------------------------------------------------------------------
# OpenSearch
# --------------------------------------------------------------------------
_ALLOWED = (("POST", re.compile(r"^/[\w\-\*,]+/_search$")),
            ("POST", re.compile(r"^/[\w\-\*,]+/_count$")),
            ("GET", re.compile(r"^/[\w\-\*,]+/_mapping$")))

DOC_FIELDS = [
    "article_id", "source_table", "published_at", "published_at_confidence",
    "published_at_source", "published_at_precision", "published_at_conflict",
    "ingested_at", "source_domain", "url", "title", "content_hash", "embedding_model",
    "gdelt.globaleventid", "gdelt.action_country", "gdelt.action_location",
    "gdelt.action_geo", "gdelt.event_root_code", "gdelt.quad_class",
    "gkg.locations_country_code",
]


class ReadOnlyOpenSearch:
    def __init__(self, session: Optional[requests.Session] = None):
        s = C.os_settings()
        self._base = s["url"]
        self._s = session or requests.Session()
        self._s.auth = (s["user"], s["password"])
        self._s.verify = s["verify"]
        if not s["verify"]:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.index = C.OPENSEARCH_INDEX

    def _call(self, method: str, path: str, body: Optional[dict] = None, timeout: int = 120) -> dict:
        if not any(method == m and rx.match(path) for m, rx in _ALLOWED):
            raise PermissionError(f"OpenSearch call not permitted (read-only): {method} {path}")
        last: Exception | None = None
        for attempt in range(C.PARAMS["os_max_retries"]):
            try:
                r = self._s.request(method, self._base + path, json=body, timeout=timeout)
                if r.status_code >= 500 or r.status_code == 429:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, RuntimeError) as e:
                last = e
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"OpenSearch request failed after retries: {last}")

    def search(self, body: dict) -> dict:
        return self._call("POST", f"/{self.index}/_search", body)

    def count(self, query: dict) -> int:
        return int(self._call("POST", f"/{self.index}/_count", {"query": query})["count"])

    def mapping(self) -> dict:
        return self._call("GET", f"/{self.index}/_mapping")

    # ----- retrieval --------------------------------------------------------
    @staticmethod
    def _src(include_embedding: bool) -> dict:
        inc = list(DOC_FIELDS) + (["embedding"] if include_embedding else [])
        return {"includes": inc}

    def scan(self, query: dict, *, include_embedding: bool = False,
             page_size: Optional[int] = None) -> Iterator[list[dict]]:
        """Yield pages of raw hits for `query`, ordered by (published_at, source_table, article_id).

        search_after paging: stateless, no PIT/scroll. A doc present in both backing indices
        appears twice with identical sort keys; callers dedupe by `_id` (see `dedupe_hits`).
        """
        size = page_size or C.PARAMS["os_page_size"]
        after = None
        while True:
            body = {"size": size, "query": query, "_source": self._src(include_embedding),
                    "sort": [{"published_at": "asc"}, {"source_table": "asc"}, {"article_id": "asc"}]}
            if after:
                body["search_after"] = after
            hits = self.search(body)["hits"]["hits"]
            if not hits:
                return
            yield hits
            after = hits[-1]["sort"]
            if len(hits) < size:
                return

    def fetch_by_ids(self, os_ids: Sequence[str], *, include_embedding: bool = True,
                     batch: int = 200) -> dict[str, dict]:
        """`ids` query (NOT `_mget`, which fails on the two-index alias). Returns {_id: hit}.

        If an id exists in both backing indices, the copy in the highest-numbered index wins
        and the disagreement is recorded in hit['_dup_indices'].
        """
        out: dict[str, dict] = {}
        for i in range(0, len(os_ids), batch):
            chunk = list(os_ids[i:i + batch])
            hits = self.search({"size": len(chunk) * 3, "_source": self._src(include_embedding),
                                "query": {"ids": {"values": chunk}}})["hits"]["hits"]
            out.update(dedupe_hits(hits))
        return out


def dedupe_hits(hits: Iterable[dict]) -> dict[str, dict]:
    """Collapse duplicate `_id`s across the backing indices (newest index wins)."""
    by: dict[str, list[dict]] = {}
    for h in hits:
        by.setdefault(h["_id"], []).append(h)
    out = {}
    for k, v in by.items():
        v.sort(key=lambda h: h.get("_index", ""))
        best = v[-1]
        if len(v) > 1:
            best = dict(best)
            best["_dup_indices"] = [h.get("_index") for h in v]
        out[k] = best
    return out


# --------------------------------------------------------------------------
# Document normalisation and date validation (pure, unit-testable)
# --------------------------------------------------------------------------
def _parse_ts(v: Any) -> Optional[dt.datetime]:
    if not v:
        return None
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def _g(d: dict, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def normalise_hit(hit: dict) -> dict:
    """Flatten one OpenSearch hit into the fields the engine uses. No imputation."""
    s = hit.get("_source", {})
    geo = _g(s, "gdelt", "action_geo") or {}
    cc = _g(s, "gkg", "locations_country_code") or []
    if isinstance(cc, str):
        cc = [cc]
    try:
        article_id = int(s.get("article_id"))
    except (TypeError, ValueError):
        article_id = None
    return {
        "os_id": hit.get("_id"),
        "os_index": hit.get("_index"),
        "dup_indices": hit.get("_dup_indices"),
        "source_table": s.get("source_table"),
        "article_id": article_id,
        "published_at": _parse_ts(s.get("published_at")),
        "date_confidence": s.get("published_at_confidence"),
        "date_source": s.get("published_at_source"),
        "date_precision": s.get("published_at_precision"),
        "date_conflict": bool(s.get("published_at_conflict")) if s.get("published_at_conflict") is not None else None,
        "ingested_at": _parse_ts(s.get("ingested_at")),
        "source_domain": (s.get("source_domain") or "").lower() or None,
        "url": s.get("url"),
        "title": s.get("title"),
        "content_hash": s.get("content_hash"),
        "gdelt_event_id": _g(s, "gdelt", "globaleventid"),
        "action_country_fips": _g(s, "gdelt", "action_country"),
        "action_location": _g(s, "gdelt", "action_location"),
        "action_lat": geo.get("lat") if isinstance(geo, dict) else None,
        "action_lon": geo.get("lon") if isinstance(geo, dict) else None,
        "cameo_root": _g(s, "gdelt", "event_root_code"),
        "mention_countries_fips": sorted({c for c in cc if c}),
        "embedding": s.get("embedding"),
    }


def validate_published_at(doc: dict, now: dt.datetime, P: dict) -> tuple[bool, str]:
    """(usable_for_trends, reason). `scraped_at` is never consulted."""
    t = doc.get("published_at")
    if t is None:
        return False, "missing_published_at"
    if t.year < P["min_valid_year"]:
        return False, "implausibly_old"
    if t > now + dt.timedelta(days=P["max_future_days"]):
        return False, "future_published_at"
    if P["exclude_date_conflict"] and doc.get("date_conflict"):
        return False, "date_conflict"
    return True, "ok"


# --------------------------------------------------------------------------
# PostgreSQL queries used by the engine
# --------------------------------------------------------------------------
LABEL_SQL = """
SELECT DISTINCT ON (l.source_table, l.article_id)
       l.source_table, l.article_id, l.model_version, l.taxonomy_version, l.published_at,
       l.os_ingested_at, l.domain_id, l.topic_id, l.category_id, l.subtopic_id,
       l.category_confidence, l.subtopic_confidence, l.content_hash, l.url
FROM tmc.article_label l
JOIN unnest(%s::text[], %s::bigint[]) AS k(source_table, article_id)
  ON l.source_table = k.source_table AND l.article_id = k.article_id
ORDER BY l.source_table, l.article_id, l.classified_at DESC NULLS LAST
"""

TAXONOMY_SQL = ("SELECT code, tier, name, parent, sort_order, is_catch_all, in_scope, taxonomy_version "
                "FROM tmc.taxonomy_node")


def fetch_labels(pg: ReadOnlyPG, keys: Sequence[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """Labels for (source_table, article_id) keys. Missing keys are simply absent (unlabelled)."""
    out: dict[tuple[str, int], dict] = {}
    step = C.PARAMS["pg_batch"]
    for i in range(0, len(keys), step):
        chunk = keys[i:i + step]
        rows = pg.query(LABEL_SQL, ([k[0] for k in chunk], [int(k[1]) for k in chunk]))
        for r in rows:
            out[(r["source_table"], int(r["article_id"]))] = r
    return out


def fetch_taxonomy(pg: ReadOnlyPG) -> list[dict]:
    return pg.query(TAXONOMY_SQL)
