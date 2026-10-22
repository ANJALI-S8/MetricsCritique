"""Statistics: calibration, no look-ahead, support gating, and AOI-SPECIFIC baselines."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from news_testutil import fact_row, geo_row, populate
from lib import news_aoi as A
from lib import news_config as C
from lib import news_geo as G
from lib import news_metrics as M
from lib import news_store as NSt

P = dict(C.PARAMS)
rng = np.random.default_rng(11)


# ---- series_panel -------------------------------------------------------------------
def test_null_calibration_near_nominal():
    E = np.full(30, 3000.0)
    rej = tests = 0
    for _ in range(250):
        d, _ = M.series_panel(rng.poisson(20, 30).astype(float), E, P)
        ok = d["support_ok"].to_numpy()
        rej += int((d.loc[ok, "surprise_p"] < 0.05).sum())
        tests += int(ok.sum())
    assert tests > 4000 and 0.03 < rej / tests < 0.085, rej / tests    # ~5% at p<.05 on a flat series


def test_planted_spike_detected_and_burst_found():
    V = rng.poisson(20, 30).astype(float)
    V[22] = 95
    d, eps = M.series_panel(V, np.full(30, 3000.0), P)
    assert d.loc[22, "surprise_p"] < 1e-6 and d.loc[22, "support_ok"]
    assert any(e["start_i"] <= 22 <= e["end_i"] for e in eps)


def test_collector_ramp_is_not_a_trend():
    # volume rises 3x purely because exposure rose 3x -> must not alert
    E = np.linspace(1000, 3000, 30)
    rej = 0
    for _ in range(100):
        d, _ = M.series_panel(rng.poisson(E * 0.01).astype(float), E, P)
        ok = d["support_ok"].to_numpy()
        rej += int((d.loc[ok, "surprise_p"] < 0.01).sum())
    assert rej <= 8                                                     # ~1% of ~2300 tests, not a flood


def test_baseline_has_no_lookahead():
    V = rng.poisson(15, 25).astype(float)
    a, _ = M.series_panel(V, np.full(25, 1000.0), P)
    V2 = V.copy(); V2[-1] = 500
    b, _ = M.series_panel(V2, np.full(25, 1000.0), P)
    np.testing.assert_allclose(a["baseline_mu"].to_numpy()[:-1], b["baseline_mu"].to_numpy()[:-1], equal_nan=True)


def test_thin_support_gives_null_not_zero():
    V = np.array([0, 1, 0, 0, 2, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1], dtype=float)
    d, _ = M.series_panel(V, np.full(len(V), 500.0), P)
    assert not d["support_ok"].any()
    for c in ("surprise", "surprise_p", "growth", "accel_smooth", "momentum"):
        assert d[c].isna().all(), c                                     # NULL, never 0


def test_hurlbert_pie():
    assert M.hurlbert_pie([10]) == 0.0
    assert np.isnan(M.hurlbert_pie([1])) and np.isnan(M.hurlbert_pie([]))   # N<2: undefined, not 0
    assert M.hurlbert_pie([5, 5]) == pytest.approx(10 / 9 * 0.5)
    assert M.hurlbert_pie([9, 1]) < M.hurlbert_pie([5, 5])


# ---- AOI panels ---------------------------------------------------------------------
@pytest.fixture
def st(tmp_path):
    s = NSt.Store(tmp_path / "m.duckdb")
    yield s
    s.close()


def build(st, n_days=20, spike_day=None, gap_day=None, with_unlabelled=0, with_d00=0):
    """AOI X (IN): topic T01 ~30/day + topic T02 ~30/day. Country Y (BD): topic T01 declining, large."""
    facts, geos, i = [], [], 0
    for d in range(n_days):
        for topic, base in (("D01.T01", 30), ("D01.T02", 30)):
            n = rng.poisson(base)
            if spike_day is not None and d == spike_day and topic == "D01.T01":
                n = 150
            if gap_day is not None and d == gap_day:
                n = 0
            for _ in range(n):
                i += 1
                facts.append(fact_row(i, d, topic=topic, cat=topic + ".C01", publisher=f"p{i % 11}.com"))
                geos.append(geo_row(i, "IN", adm1="IND-1"))
        if gap_day is None or d != gap_day:
            for _ in range(with_unlabelled):
                i += 1
                facts.append(fact_row(i, d, has_label=False)); geos.append(geo_row(i, "IN"))
            for _ in range(with_d00):
                i += 1
                facts.append(fact_row(i, d, domain="D00", topic="D00.T01", cat="D00.T01.C01")); geos.append(geo_row(i, "IN"))
        for _ in range(int(400 - 15 * d)):                       # other country: big and falling
            i += 1
            facts.append(fact_row(i, d, topic="D01.T01", cat="D01.T01.C01", publisher=f"q{i % 5}.com"))
            geos.append(geo_row(i, "BD"))
    populate(st, facts, geos)


AOI_X = G.AOI("country", "IN", "India", iso2="IN")
AOI_G = G.AOI("country", "BD", "Bangladesh", iso2="BD")


def test_aoi_baseline_is_the_aois_own_not_filtered_global(st):
    build(st, spike_day=17)
    summ = M.compute_aoi_panel(st, AOI_X, P, "r")
    assert summ["supported"] and summ["n_series_ts"] >= 2
    got = st.df("SELECT day, V, exposure, baseline_mu FROM node_aoi_day WHERE aoi_id = ? AND level='topic' "
                "AND node_code='D01.T01' ORDER BY day", [AOI_X.aoi_id])
    fr = A.aoi_frame(st, AOI_X)
    fr = fr[fr.topic_id == "D01.T01"]
    # exposure is the AOI's own labelled-article total (T01 + T02), NOT global volume
    tot = A.aoi_frame(st, AOI_X).groupby("day").size()
    assert ((got["exposure"] + got["V"]).to_numpy() == tot.sort_index().to_numpy()).all()   # leave-one-out exposure
    # the baseline equals what the pure function gives on the AOI's own counts ...
    V = fr.groupby("day").size().reindex(tot.index).fillna(0).to_numpy(float)
    want, _ = M.series_panel(V, tot.to_numpy(float) - V, P)
    np.testing.assert_allclose(got["baseline_mu"].to_numpy(), want["baseline_mu"].to_numpy(), equal_nan=True)
    # ... and is NOT what a global series (India + Bangladesh) would give
    allf = st.df("SELECT day, count(*) n FROM article_fact WHERE topic_id='D01.T01' AND NOT is_duplicate GROUP BY 1 ORDER BY 1")
    alle = st.df("SELECT day, count(*) n FROM article_fact WHERE NOT is_duplicate AND has_label GROUP BY 1 ORDER BY 1")
    glob, _ = M.series_panel(allf.n.to_numpy(float), alle.n.to_numpy(float), P)
    assert not np.allclose(got["baseline_mu"].to_numpy()[-5:], glob["baseline_mu"].to_numpy()[-5:], equal_nan=True)
    # the local spike is a statistically justified alert in the AOI
    row = got.iloc[17]
    assert pd.Timestamp(row.day) == pd.Timestamp("2026-10-18") and row.V >= 100
    al = st.df("SELECT * FROM alert WHERE aoi_id = ? AND kind='node_trend' AND status='active'", [AOI_X.aoi_id])
    assert len(al) >= 1 and (al.q < 0.05).all() and (al.effect >= P["alert_min_ratio"]).all()


def test_global_decline_does_not_hide_local_spike_and_vice_versa(st):
    build(st, spike_day=17)
    M.compute_aoi_panel(st, AOI_X, P, "r")
    M.compute_aoi_panel(st, AOI_G, P, "r")
    x = st.scalar("SELECT count(*) FROM alert WHERE aoi_id = ? AND status='active'", [AOI_X.aoi_id])
    g = st.scalar("SELECT count(*) FROM alert WHERE aoi_id = ? AND status='active'", [AOI_G.aoi_id])
    assert x >= 1 and g == 0           # Bangladesh's smooth decline is its own baseline's business


def test_alerts_idempotent_and_retracted_when_revised(st):
    build(st, spike_day=17)
    M.compute_aoi_panel(st, AOI_X, P, "r1")
    ids1 = set(st.df("SELECT alert_id FROM alert WHERE status='active'").alert_id)
    M.compute_aoi_panel(st, AOI_X, P, "r2")
    ids2 = set(st.df("SELECT alert_id FROM alert WHERE status='active'").alert_id)
    assert ids1 == ids2 and st.scalar("SELECT count(*) FROM alert") == len(ids1)     # no duplicates on rerun
    assert st.scalar("SELECT min(first_run) FROM alert") == "r1"                      # first detection preserved
    # late data revises the spike away (e.g. duplicates removed / re-labelled) -> alert RETRACTED, never deleted
    st.con.execute("DELETE FROM article_fact WHERE day = ? AND topic_id='D01.T01' AND article_id % 5 <> 0", [dt.date(2026, 10, 18)])
    M.compute_aoi_panel(st, AOI_X, P, "r3")
    rows = st.df("SELECT alert_id, day, status, retracted_at FROM alert")
    assert set(rows.alert_id) >= ids1                                                  # history kept, nothing deleted
    rows["day"] = pd.to_datetime(rows["day"])
    revised = rows[rows.day == pd.Timestamp("2026-10-18")]
    assert len(revised) >= 1 and (revised.status == "retracted").all() and revised.retracted_at.notna().all()
    unrelated = rows[rows.day != pd.Timestamp("2026-10-18")]
    assert (unrelated.status == "active").all()                                        # other days untouched


def test_data_gap_day_is_null_not_zero(st):
    build(st, gap_day=10)
    M.compute_aoi_panel(st, AOI_X, P, "r")
    d = st.df("SELECT day, V, exposure, support_ok, surprise, growth FROM node_aoi_day WHERE aoi_id=? AND level='topic' "
              "AND node_code='D01.T01' ORDER BY day", [AOI_X.aoi_id])
    d["day"] = pd.to_datetime(d["day"])
    gap = d[d.day == pd.Timestamp("2026-10-11")].iloc[0]
    assert pd.isna(gap.V) and not gap.support_ok and pd.isna(gap.surprise) and pd.isna(gap.growth)
    assert d[d.day != pd.Timestamp("2026-10-11")].V.notna().all()


def test_unlabelled_and_out_of_scope_are_not_in_node_series(st):
    build(st, with_unlabelled=40, with_d00=40)
    summ = M.compute_aoi_panel(st, AOI_X, P, "r")
    assert summ["n_unlabelled_or_out_of_scope"] > 0
    ex = st.df("SELECT day, exposure FROM node_aoi_day WHERE aoi_id=? AND level='topic' AND node_code='D01.T01' ORDER BY day",
               [AOI_X.aoi_id])
    lab = st.df("SELECT day, count(*) n FROM article_fact f JOIN article_geo g USING (source_table, article_id) "
                "WHERE g.ev_country_iso='IN' AND f.has_label AND f.domain_id <> 'D00' GROUP BY 1 ORDER BY 1")
    v = st.df("SELECT day, V FROM node_aoi_day WHERE aoi_id=? AND level='topic' AND node_code='D01.T01' ORDER BY day", [AOI_X.aoi_id])
    assert ((ex.exposure + v.V).to_numpy() == lab.n.to_numpy()).all()   # only labelled, in-scope articles count
    assert st.scalar("SELECT count(*) FROM node_aoi_day WHERE node_code LIKE 'D00%'") == 0


def test_unsupported_aoi_reports_reason_and_writes_nothing(st):
    populate(st, [fact_row(1, 0)], [geo_row(1, "IN")])
    s = M.compute_aoi_panel(st, AOI_X, P, "r")
    assert not s["supported"] and "labelled in-scope" in s["reason"]
    assert st.scalar("SELECT count(*) FROM node_aoi_day") == 0


def test_alert_requires_support_effect_and_fdr():
    base = {"aoi_id": "a", "level": "topic", "node_code": "n", "day": dt.date(2026, 10, 9), "V": 12.0, "exposure": 100.0,
            "baseline_mu": 11.0, "baseline_days": 10, "surprise_p": 0.001, "surprise_q": 0.02, "alert_flag": True,
            "support_ok": True}
    assert M.node_alerts(pd.DataFrame([base]), P) == []                                    # 1.1x < 2x effect floor
    assert M.node_alerts(pd.DataFrame([{**base, "V": 40.0}]), P)                           # real effect
    assert M.node_alerts(pd.DataFrame([{**base, "V": 40.0, "support_ok": False}]), P) == []
    assert M.node_alerts(pd.DataFrame([{**base, "V": 40.0, "alert_flag": False}]), P) == []


def test_aoi_day_when_nothing_mentions_the_country(st):
    """Regression: an empty 'mention' frame used to lose its `day` column (pandas treats [] as a column list)."""
    build(st)                                           # synthetic geo rows carry no mention countries
    d = M.compute_aoi_day(st, AOI_X, P, None)
    assert len(d) == 20 and (d["n_mention_only"] == 0).all() and d["n_articles"].gt(0).all()
