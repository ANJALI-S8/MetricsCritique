"""Read-only enforcement, configuration hygiene, and date reliability. No network."""
import datetime as dt

import pytest

from news_testutil import UTC, doc  # noqa: F401  (also sets sys.path)
from lib import news_config as C
from lib import news_sources as NS


# ---- PostgreSQL guard -------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "DELETE FROM tmc.article_label", "update raw.users set a=1", "INSERT INTO x VALUES (1)",
    "select 1; drop table x", "SELECT 1; SELECT 2", "create table t(a int)", "TRUNCATE tmc.run_log",
    "with x as (select 1) delete from t", "copy t to '/tmp/x'", "ALTER TABLE t ADD c int",
    "GRANT ALL ON t TO u", "  -- comment\n delete from t", "set role postgres"])
def test_pg_guard_blocks_writes(sql):
    with pytest.raises(PermissionError):
        NS.assert_read_only_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM tmc.article_label WHERE x = 'update me'",
    "WITH a AS (SELECT 1) SELECT * FROM a;", NS.LABEL_SQL, NS.TAXONOMY_SQL])
def test_pg_guard_allows_selects(sql):
    NS.assert_read_only_sql(sql)


# ---- OpenSearch whitelist ---------------------------------------------------
@pytest.fixture
def os_client(monkeypatch):
    monkeypatch.setenv("NEWS_OS_URL", "https://example.invalid:9200")
    monkeypatch.setenv("NEWS_OS_USER", "u")
    monkeypatch.setenv("NEWS_OS_PASSWORD", "p")

    class Boom:
        auth = None
        verify = True

        def request(self, *a, **k):
            raise AssertionError("a non-whitelisted call reached the network layer")
    return NS.ReadOnlyOpenSearch(session=Boom())


@pytest.mark.parametrize("method,path", [
    ("PUT", "/full-articles-000002/_doc/1"), ("DELETE", "/full-articles"), ("POST", "/_bulk"),
    ("POST", "/full-articles/_delete_by_query"), ("POST", "/full-articles/_update_by_query"),
    ("POST", "/full-articles/_search/scroll"), ("DELETE", "/_search/scroll"), ("POST", "/_reindex"),
    ("POST", "/full-articles/_doc"), ("POST", "/full-articles/_pit"), ("GET", "/_cluster/settings")])
def test_os_blocks_everything_but_search_count_mapping(os_client, method, path):
    with pytest.raises(PermissionError):
        os_client._call(method, path, {})


def test_os_uses_ids_query_not_mget(os_client, monkeypatch):
    calls = []
    monkeypatch.setattr(os_client, "_call", lambda m, p, b=None, timeout=0: calls.append((m, p)) or {"hits": {"hits": []}})
    os_client.fetch_by_ids(["a-1"], include_embedding=False)
    assert calls and all(p.endswith("/_search") for _, p in calls)   # _mget fails on the 2-index alias


# ---- configuration: credentials from the environment, never defaults ---------
def test_no_credential_defaults(monkeypatch):
    for k in ("NEWS_PG_URL", "SOURCE_DATABASE_URL", "NEWS_PG_HOST", "NEWS_PG_PORT", "NEWS_PG_USER",
              "NEWS_PG_PASSWORD", "NEWS_PG_DB", "NEWS_OS_URL", "OPENSEARCH_URL", "NEWS_OS_USER",
              "OPENSEARCH_USER", "NEWS_OS_PASSWORD", "OPENSEARCH_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(C.ConfigError):
        C.pg_settings()
    with pytest.raises(C.ConfigError):
        C.os_settings()


def test_pg_url_parsing(monkeypatch):
    monkeypatch.setenv("NEWS_PG_URL", "postgresql+psycopg://news-media:p%40ss@10.0.0.1:13355/shaktidb_news")
    s = C.pg_settings()
    assert (s["host"], s["port"], s["user"], s["password"], s["dbname"]) == \
           ("10.0.0.1", 13355, "news-media", "p@ss", "shaktidb_news")


def test_no_secret_literals_in_source():
    import re
    root = C.EDA_ROOT / "scripts"
    bad = []
    for p in list(root.glob("lib/news_*.py")) + list(root.glob("2*_news*.py")):
        t = p.read_text()
        if re.search(r"(password|passwd)\s*=\s*['\"][^'\"]+['\"]", t, re.I) or re.search(r"postgres(ql)?://\w+:[^@\s]+@", t):
            bad.append(p.name)
    assert not bad, f"possible hardcoded credential in {bad}"


# ---- document normalisation + date reliability ---------------------------------
NOW = dt.datetime(2026, 10, 20, tzinfo=UTC)


def test_normalise_extracts_event_and_mentions_separately():
    d = doc(1, fips="IN", mentions=("IN", "CH"))
    assert d["action_country_fips"] == "IN" and d["mention_countries_fips"] == ["CH", "IN"]
    assert d["published_at"].tzinfo is not None


def test_missing_date_is_not_substituted():
    d = doc(2, published=None)
    ok, why = NS.validate_published_at(d, NOW, C.PARAMS)
    assert not ok and why == "missing_published_at"
    assert "scraped_at" not in d and d["published_at"] is None     # nothing was filled in


@pytest.mark.parametrize("published,conflict,reason", [
    ("2026-10-25T00:00:00+00:00", False, "future_published_at"),
    ("1990-04-08T07:00:00+00:00", False, "implausibly_old"),
    ("2026-10-10T00:00:00+00:00", True, "date_conflict")])
def test_invalid_dates_are_excluded_with_reason(published, conflict, reason):
    ok, why = NS.validate_published_at(doc(3, published=published, conflict=conflict), NOW, C.PARAMS)
    assert not ok and why == reason


def test_valid_date_ok():
    assert NS.validate_published_at(doc(4), NOW, C.PARAMS) == (True, "ok")


def test_dedupe_hits_prefers_newest_index_and_records_dups():
    h1 = {"_id": "scraped_articles-1", "_index": "full-articles-000001", "_source": {"v": 1}}
    h2 = {"_id": "scraped_articles-1", "_index": "full-articles-000002", "_source": {"v": 2}}
    out = NS.dedupe_hits([h1, h2, {"_id": "scraped_articles-2", "_index": "full-articles-000001", "_source": {}}])
    assert len(out) == 2
    assert out["scraped_articles-1"]["_source"]["v"] == 2
    assert out["scraped_articles-1"]["_dup_indices"] == ["full-articles-000001", "full-articles-000002"]


# ---- connection resilience --------------------------------------------------------------
class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None):
        if self.conn.kill:
            raise NS.psycopg2.OperationalError("server closed the connection unexpectedly")
        self.conn.executed.append(sql)
    def fetchall(self): return [{"x": 1}]


class _Conn:
    def __init__(self, kill=False):
        self.kill, self.executed, self.closed, self.rollbacks = kill, [], False, 0
    def set_session(self, **k): self.session = k
    def cursor(self, cursor_factory=None): return _Cur(self)
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True


def test_pg_reconnects_after_server_drops_idle_connection(monkeypatch):
    monkeypatch.setenv("NEWS_PG_URL", "postgresql://u:p@h:1/d")
    monkeypatch.setattr(NS.time, "sleep", lambda s: None)
    conns = [_Conn(kill=True), _Conn()]
    monkeypatch.setattr(NS.psycopg2, "connect", lambda **k: conns.pop(0))
    pg = NS.ReadOnlyPG()
    assert pg.query("SELECT 1") == [{"x": 1}]            # first connection was dead; second served it
    assert pg._conn.rollbacks >= 1                        # the read txn is ended after every query


def test_pg_gives_up_with_clear_error(monkeypatch):
    monkeypatch.setenv("NEWS_PG_URL", "postgresql://u:p@h:1/d")
    monkeypatch.setattr(NS.time, "sleep", lambda s: None)
    monkeypatch.setattr(NS.psycopg2, "connect", lambda **k: _Conn(kill=True))
    with pytest.raises(RuntimeError, match="unavailable"):
        NS.ReadOnlyPG().query("SELECT 1")


def test_pg_close_never_raises_on_dead_connection(monkeypatch):
    monkeypatch.setenv("NEWS_PG_URL", "postgresql://u:p@h:1/d")
    c = _Conn()
    c.rollback = lambda: (_ for _ in ()).throw(NS.psycopg2.OperationalError("gone"))
    monkeypatch.setattr(NS.psycopg2, "connect", lambda **k: c)
    NS.ReadOnlyPG().close()                               # the exact failure seen in the pilot log
