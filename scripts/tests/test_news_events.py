"""Event metrics, the AOI-specific emerging-event test, and development detection."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from news_testutil import UTC, fact_row, geo_row, populate, unit
from lib import news_cluster as K
from lib import news_config as C
from lib import news_events as EV
from lib import news_geo as G
from lib import news_ingest as I
from lib import news_store as NSt

P = dict(C.PARAMS)
rng = np.random.default_rng(21)
AOI = G.AOI("country", "IN", "India", iso2="IN")
T_END = dt.datetime(2026, 10, 14, tzinfo=UTC)          # evaluation: end of 2026-10-13


@pytest.fixture
def st(tmp_path):
    s = NSt.Store(tmp_path / "e.duckdb")
    yield s
    s.close()


class Builder:
    """Creates clusters of articles directly in the store (no OpenSearch)."""

    def __init__(self, st, domain="D01"):
        self.st, self.i, self.f, self.g, self.domain = st, 1000, [], [], domain

    def cluster(self, cid, day, hour, pubs, country="IN", gap_h=1.0):
        for k, pub in enumerate(pubs):
            self.i += 1
            h = hour + k * gap_h
            d = day + int(h // 24)
            self.f.append(fact_row(self.i, d, hour=int(h % 24), publisher=pub, cluster=cid, domain=self.domain))
            self.g.append(geo_row(self.i, country, adm1="IND-1"))

    def commit(self):
        populate(self.st, self.f, self.g)
        ids = sorted({r["cluster_id"] for r in self.f})
        self.st.upsert("event_cluster", pd.DataFrame({"event_id": ids, "is_open": False, "is_event": False}))
        ctx = I.Ctx(self.st, None, None, None, I.Scope("t"), "r", now=dt.datetime(2026, 10, 20, tzinfo=UTC))
        K.refresh_cluster_stats(ctx)


def reference_population(b, n=120, pubs_per=(1, 1, 2)):
    """n ordinary clusters spread over days 1-9: mostly 1-2 articles from 1-2 publishers."""
    for j in range(n):
        k = int(rng.choice(pubs_per))
        b.cluster(f"ref{j}", 1 + j % 9, int(rng.integers(0, 20)), [f"r{j}_{m}.com" for m in range(k)])


def test_outlier_event_raises_one_statistically_justified_alert(st):
    b = Builder(st)
    reference_population(b)
    b.cluster("big", 12, 8, [f"outlet{m}.com" for m in range(8)])       # 8 publishers within 8h, on day 12
    b.commit()
    r = EV.emerging_tests(st, AOI, P, [T_END], "r1")
    assert r["alerts"] == 1
    t = st.df("SELECT * FROM event_aoi_test WHERE event_id = 'big'").iloc[0]
    assert t.support_ok and t.q < 0.05 and t.p <= 1 / 50 and t.n_ref >= P["emerging_min_ref"]
    assert t.n_publishers_at_age >= P["emerging_min_publishers"] and t.stratum.startswith("domain:")
    al = st.df("SELECT * FROM alert WHERE kind='emerging_event'")
    assert len(al) == 1 and al.q[0] < 0.05 and "publishers" in al.rationale[0]


def test_common_event_is_not_an_alert(st):
    b = Builder(st)
    reference_population(b, n=70, pubs_per=(1,))
    for j in range(70):      # in THIS AOI, 6-publisher events within hours are the norm
        b.cluster(f"norm{j}", 1 + j % 9, int(rng.integers(0, 12)), [f"n{j}_{m}.com" for m in range(6)])
    b.cluster("cand", 12, 8, [f"c{m}.com" for m in range(6)])
    b.commit()
    r = EV.emerging_tests(st, AOI, P, [T_END], "r1")
    assert r["alerts"] == 0
    t = st.df("SELECT * FROM event_aoi_test WHERE event_id = 'cand'").iloc[0]
    assert t.support_ok and t.p > 0.3                       # judged against ITS AOI's own population


def test_thin_reference_is_null_with_reason_never_a_number(st):
    b = Builder(st)
    reference_population(b, n=10)                           # < emerging_min_ref
    b.cluster("big", 12, 8, [f"o{m}.com" for m in range(8)])
    b.commit()
    r = EV.emerging_tests(st, AOI, P, [T_END], "r1")
    t = st.df("SELECT * FROM event_aoi_test WHERE event_id = 'big'").iloc[0]
    assert r["alerts"] == 0 and not t.support_ok and pd.isna(t.p) and pd.isna(t.q)
    assert "reference clusters" in t.support_note


def test_emerging_is_idempotent(st):
    b = Builder(st)
    reference_population(b)
    b.cluster("big", 12, 8, [f"o{m}.com" for m in range(8)])
    b.commit()
    EV.emerging_tests(st, AOI, P, [T_END], "r1")
    EV.emerging_tests(st, AOI, P, [T_END], "r2")
    EV.emerging_tests(st, AOI, P, [T_END + dt.timedelta(hours=6)], "r3")
    assert st.scalar("SELECT count(*) FROM alert WHERE kind='emerging_event'") == 1
    assert st.scalar("SELECT count(*) FROM event_aoi_test WHERE event_id='big'") == 1
    assert st.scalar("SELECT first_run FROM alert WHERE kind='emerging_event'") == "r1"


def test_no_lookahead_candidate_uses_only_data_up_to_T(st):
    b = Builder(st)
    reference_population(b)
    # articles arrive hour by hour; at T only the first 3 publishers exist yet
    b.cluster("late", 13, 0, [f"o{m}.com" for m in range(10)], gap_h=2)
    b.commit()
    EV.emerging_tests(st, AOI, P, [dt.datetime(2026, 10, 14, 4, tzinfo=UTC)], "r1")     # 4h after first article
    t = st.df("SELECT * FROM event_aoi_test WHERE event_id='late'").iloc[0]
    assert t.n_publishers_at_age <= 3 and t.n_articles_at_age <= 3


def test_out_of_scope_domain_never_alerts(st):
    b = Builder(st, domain="D00")
    reference_population(b)
    b.cluster("sport", 12, 8, [f"o{m}.com" for m in range(8)])
    b.commit()
    assert EV.emerging_tests(st, AOI, P, [T_END], "r1")["alerts"] == 0


def test_event_is_assigned_to_an_aoi_only_by_its_located_members(st):
    b = Builder(st)
    b.cluster("in_india", 5, 8, [f"a{m}.com" for m in range(4)], country="IN")
    b.cluster("elsewhere", 5, 8, [f"b{m}.com" for m in range(4)], country="BD")
    b.cluster("nowhere", 5, 8, [f"c{m}.com" for m in range(4)], country=None)
    b.commit()
    from lib import news_aoi as A
    ev = A.aoi_events(st, AOI, P)
    assert set(ev.event_id) == {"in_india"}                 # unlocated events belong to NO event-mode AOI


# ---- event metrics ------------------------------------------------------------------------------------
def test_event_metrics_null_when_support_thin(st):
    b = Builder(st)
    b.cluster("thin", 12, 20, ["a.com", "b.com", "c.com"], gap_h=1)
    b.commit()
    EV.compute_event_metrics(st, P, dt.datetime(2026, 10, 14, tzinfo=UTC))
    r = st.df("SELECT * FROM event_metrics WHERE event_id='thin'").iloc[0]
    assert pd.isna(r.growth_24h) and pd.isna(r.accel_6h) and pd.isna(r.half_life_days)     # NULL, not 0
    assert r.n_articles == 3 and r.n_publishers == 3 and r.cross_source_ratio == 1.0


def test_event_metrics_growth_and_diversity(st):
    b = Builder(st)
    asof = dt.datetime(2026, 10, 14, 23, 30, tzinfo=UTC)
    b.cluster("grow", 12, 0, [f"p{m % 6}.com" for m in range(4)], gap_h=2)                   # 2026-10-13, early
    b.cluster("grow2", 13, 0, [f"q{m}.com" for m in range(12)], gap_h=1)                     # 2026-10-14: busy last 24h
    b.commit()
    EV.compute_event_metrics(st, P, asof)
    r = st.df("SELECT * FROM event_metrics WHERE event_id='grow2'").iloc[0]
    assert r.articles_24h == 12 and r.articles_prev_24h == 0 and r.growth_24h == pytest.approx(np.log(13.0))
    assert 0.9 < r.publisher_pie <= 1.0 and r.duration_hours == pytest.approx(11.0)


# ---- developments --------------------------------------------------------------------------------------
def test_development_test_calibration_and_power():
    D = 1024
    u = lambda x: x / np.linalg.norm(x, axis=-1, keepdims=True)
    flags_null = flags_planted = 0
    N = 150
    for _ in range(N):
        core = u(rng.normal(size=D))
        old = u(core + 0.5 * u(rng.normal(size=(12, D))))
        new_same = u(core + 0.5 * u(rng.normal(size=(4, D))))
        other = u(core + 2.0 * u(rng.normal(size=D)))
        new_diff = u(other + 0.5 * u(rng.normal(size=(4, D))))
        a = EV.development_stats(old, new_same, P["dev_envelope_pct"])
        b = EV.development_stats(old, new_diff, P["dev_envelope_pct"])
        flags_null += a["p"] < 0.05 and a["outside_envelope"]
        flags_planted += b["p"] < 0.05 and b["outside_envelope"]
    assert flags_null / N < 0.04            # false positives controlled when nothing changed
    assert flags_planted / N > 0.95         # a genuinely different sub-story is found


def test_medium_confidence_location_cannot_raise_an_alert(st):
    """Regression for the pilot's 'Delhi University' event: 3 of 4 located members said Dhaka (a GDELT
    geocoding error) and it raised a Bangladesh alert. A majority is not enough for a geographic alert."""
    b = Builder(st)
    reference_population(b)
    ids = [f"outlet{m}.com" for m in range(8)]
    # 8 articles, 8 publishers; located: 3 in IN + 1 in BD (share 0.75 -> medium) and 4 unlocated
    b.cluster("dirty", 12, 8, ids, country=None)
    for k, cc in enumerate(["IN", "IN", "IN", "BD"]):
        b.g[-8 + k] = geo_row(b.f[-8 + k]["article_id"], cc, adm1="IND-1" if cc == "IN" else None)
    b.commit()
    conf = st.scalar("SELECT loc_confidence FROM event_cluster WHERE event_id='dirty'")
    assert conf == "medium"
    assert EV.emerging_tests(st, AOI, P, [T_END], "r1")["alerts"] == 0
    # ... while the same event with a unanimous location does alert
    b2 = Builder(st)
    b2.i = 5000
    b2.cluster("clean", 12, 8, [f"x{m}.com" for m in range(8)])
    b2.commit()
    assert EV.emerging_tests(st, AOI, P, [T_END], "r2")["alerts"] == 1
