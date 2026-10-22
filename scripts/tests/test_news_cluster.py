"""Event clustering: planted events are recovered, the time gate works, reruns are idempotent."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from news_testutil import DIM, FakeCtxParts, UTC, fact_row, geo_row, populate, unit
from lib import news_cluster as K
from lib import news_config as C
from lib import news_ingest as I
from lib import news_store as NSt

rng = np.random.default_rng(7)


def centers(n):
    return unit(rng.normal(size=(n, DIM)))


def near(center, k, noise=0.15):
    return unit(center + noise * unit(rng.normal(size=(k, DIM))))


def test_valid_vector_rules():
    v, why = K.valid_vector(np.ones(DIM))
    assert why is None and abs(np.linalg.norm(v) - 1) < 1e-5
    assert K.valid_vector(None)[1] == "missing_embedding"
    assert K.valid_vector(np.ones(DIM - 1))[1] == "invalid_vector"
    assert K.valid_vector(np.zeros(DIM))[1] == "invalid_vector"
    bad = np.ones(DIM); bad[3] = np.nan
    assert K.valid_vector(bad)[1] == "invalid_vector"


def test_planted_events_recovered():
    c = centers(3)
    cl = K.OnlineClusterer(0.80, 72)
    truth, got = [], []
    t0 = dt.datetime(2026, 10, 10, tzinfo=UTC).timestamp()
    order = rng.permutation(60)
    for j, k in enumerate(order):
        e = k % 3
        v = near(c[e], 1)[0]
        eid, sim, created = cl.assign(v, t0 + j * 600, K.event_id_for("scraped_articles", int(k)))
        truth.append(e); got.append(eid)
    ct = pd.crosstab(pd.Series(got, name="cluster"), pd.Series(truth, name="truth"))
    assert len(ct) == 3 and (ct.max(axis=1) == ct.sum(axis=1)).all()      # three pure clusters


def test_time_gate_splits_same_topic_far_apart_in_time():
    c = centers(1)[0]
    cl = K.OnlineClusterer(0.80, 72)
    t0 = dt.datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    a, _, c1 = cl.assign(near(c, 1)[0], t0, "ev_a")
    b, _, c2 = cl.assign(near(c, 1)[0], t0 + 3600, "ev_b")
    far, _, c3 = cl.assign(near(c, 1)[0], t0 + 10 * 86400, "ev_c")
    assert c1 and not c2 and b == a        # joins within the gap
    assert c3 and far != a                 # 10 days later = a new event (same topic recurring)


def test_dissimilar_article_founds_new_cluster():
    c = centers(2)
    cl = K.OnlineClusterer(0.80, 72)
    t = dt.datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    cl.assign(near(c[0], 1)[0], t, "ev_a")
    _, sim, created = cl.assign(near(c[1], 1)[0], t + 60, "ev_b")
    assert created and sim == 1.0


def test_event_ids_deterministic():
    assert K.event_id_for("scraped_articles", 5) == K.event_id_for("scraped_articles", 5)
    assert K.event_id_for("scraped_articles", 5) != K.event_id_for("scraped_articles_trusted", 5)


# ---- DB-level -------------------------------------------------------------------
@pytest.fixture
def world(tmp_path):
    st = NSt.Store(tmp_path / "c.duckdb")
    c = centers(3)
    facts, geos, vecs = [], [], {}
    i = 0
    # event 0: 6 articles / 5 publishers, located in India; event 1: 3 articles / 1 publisher (NOT an event);
    # event 2: 2 articles (below minimum); 1 duplicate of article 0
    spec = [(0, 6, [f"p{j}.com" for j in range(5)] + ["p0.com"], "IN"), (1, 3, ["same.com"] * 3, "BD"), (2, 2, ["x.com", "y.com"], None)]
    for e, n, pubs, ctry in spec:
        for k in range(n):
            i += 1
            facts.append(fact_row(i, 0, publisher=pubs[k], hour=8 + k))
            geos.append(geo_row(i, country=ctry, adm1="IND-1" if ctry == "IN" else None))
            vecs[f"scraped_articles-{i}"] = near(c[e], 1)[0].tolist()
    facts.append(fact_row(100, 0, dup=True, hour=20)); geos.append(geo_row(100, country="IN"))
    facts.append(fact_row(101, 0, hour=21)); geos.append(geo_row(101, country="IN"))   # no vector in OS
    populate(st, facts, geos)
    osc = FakeCtxParts.OS(vecs)
    ctx = I.Ctx(st, FakeCtxParts.PG(), osc, None, I.Scope("t", ["IN"]), "r1", now=dt.datetime(2026, 10, 20, tzinfo=UTC))
    yield st, ctx
    st.close()


def test_cluster_new_articles_end_to_end(world):
    st, ctx = world
    res = K.cluster_new_articles(ctx)
    assert res["clustered"] == 11 and res["no_vector"] == 1
    cl = st.df("SELECT * FROM event_cluster")
    assert len(cl) == 3
    by = cl.sort_values("n_articles", ascending=False).reset_index(drop=True)
    assert by.n_articles.tolist() == [6, 3, 2]
    ev = by.iloc[0]
    assert ev.is_event and ev.n_publishers == 5                      # 6 articles, 5 distinct publishers
    assert ev.loc_status == "located" and ev.loc_country_iso == "IN" and ev.loc_adm1_code == "IND-1"
    assert not by.iloc[1].is_event                                   # 3 articles but ONE publisher
    assert not by.iloc[2].is_event                                   # 2 articles
    assert by.iloc[2].loc_status == "unlocated"                      # no located member -> not guessed
    d = st.df("SELECT article_id, cluster_id, cluster_note FROM article_fact WHERE article_id IN (100, 101) ORDER BY article_id")
    assert d.cluster_id.isna().all()
    assert dict(zip(d.article_id, d.cluster_note)) == {100: "duplicate", 101: "missing_embedding"}


def test_duplicates_are_not_clustered_and_counted_as_copies(world):
    st, ctx = world
    st.con.execute("UPDATE article_fact SET dup_of = 'scraped_articles-1' WHERE article_id = 100")
    K.cluster_new_articles(ctx)
    assert st.scalar("SELECT cluster_id FROM article_fact WHERE article_id = 100") is None
    K.refresh_cluster_stats(ctx)
    assert st.scalar("SELECT n_copies FROM event_cluster WHERE event_id = ?", [K.event_id_for("scraped_articles", 1)]) == 1


def test_rerun_is_idempotent_and_ids_stable(world):
    st, ctx = world
    K.cluster_new_articles(ctx)
    before = st.df("SELECT event_id, n_articles FROM event_cluster ORDER BY 1")
    r2 = K.cluster_new_articles(ctx)
    assert r2["clustered"] == 0 and r2["no_vector"] == 0             # nothing selected again (permanent bad vectors included)
    pd.testing.assert_frame_equal(before, st.df("SELECT event_id, n_articles FROM event_cluster ORDER BY 1"))


def test_late_article_joins_existing_open_cluster(world):
    st, ctx = world
    K.cluster_new_articles(ctx)
    c = np.array(ctx.osc.vectors["scraped_articles-1"])
    ctx.osc.vectors["scraped_articles-500"] = unit(c + 0.05 * unit(rng.normal(size=DIM))).tolist()
    populate(st, [fact_row(500, 0, hour=23, publisher="late.com")], [geo_row(500, country="IN")])
    K.cluster_new_articles(ctx)
    assert st.scalar("SELECT cluster_id FROM article_fact WHERE article_id = 500") == K.event_id_for("scraped_articles", 1)
    assert st.scalar("SELECT n_articles FROM event_cluster WHERE event_id = ?", [K.event_id_for("scraped_articles", 1)]) == 7
    assert st.scalar("SELECT count(*) FROM event_cluster") == 3      # updated, not duplicated


def test_ambiguous_location_when_members_disagree(tmp_path):
    st = NSt.Store(tmp_path / "a.duckdb")
    c = centers(1)[0]
    facts = [fact_row(i, 0, hour=8 + i, publisher=f"p{i}.com") for i in range(1, 5)]
    geos = [geo_row(1, "IN"), geo_row(2, "BD"), geo_row(3, "PK"), geo_row(4, "NP")]
    populate(st, facts, geos)
    vecs = {f"scraped_articles-{i}": near(c, 1)[0].tolist() for i in range(1, 5)}
    ctx = I.Ctx(st, FakeCtxParts.PG(), FakeCtxParts.OS(vecs), None, I.Scope("t"), "r", now=dt.datetime(2026, 10, 20, tzinfo=UTC))
    K.cluster_new_articles(ctx)
    r = st.df("SELECT loc_status, loc_country_iso FROM event_cluster").iloc[0]
    assert r.loc_status == "ambiguous" and r.loc_country_iso is None
    st.close()


def _loc_world(tmp_path, countries):
    st = NSt.Store(tmp_path / "l.duckdb")
    c = centers(1)[0]
    facts = [fact_row(i + 1, 0, hour=8 + i, publisher=f"p{i}.com") for i in range(len(countries))]
    geos = [geo_row(i + 1, cc) for i, cc in enumerate(countries)]
    populate(st, facts, geos)
    vecs = {f"scraped_articles-{i + 1}": near(c, 1)[0].tolist() for i in range(len(countries))}
    ctx = I.Ctx(st, FakeCtxParts.PG(), FakeCtxParts.OS(vecs), None, I.Scope("t"), "r", now=dt.datetime(2026, 10, 20, tzinfo=UTC))
    K.cluster_new_articles(ctx)
    r = st.df("SELECT loc_status, loc_country_iso, loc_confidence FROM event_cluster").iloc[0]
    st.close()
    return r


@pytest.mark.parametrize("countries,status,conf", [
    (["IN"] * 5, "located", "high"),                       # unanimous, >=3 located
    (["IN", "IN", "IN", "BD"], "located", "medium"),       # 3-of-4 majority: the Delhi-University/Dhaka pattern
    (["IN", "IN"], "located", "medium"),
    (["IN"], "located", "low"),                            # a single GDELT point
    ([None, None, None], "unlocated", None)])
def test_location_confidence_tiers(tmp_path, countries, status, conf):
    r = _loc_world(tmp_path, countries)
    assert r.loc_status == status
    assert (r.loc_confidence == conf) or (conf is None and pd.isna(r.loc_confidence))
