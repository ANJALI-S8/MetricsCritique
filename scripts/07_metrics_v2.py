#!/usr/bin/env python3
"""Metrics v2 — statistically sound metrics layer (Layer 4 v2).

Implements `docs/METRICS_V2_SPEC.md`: defects D1-D5, fixes F1-F10, the
acceleration family (spec section 3) and the hashtag grain (section 4).

Reads `data/analytics.duckdb` **read-only** and writes nothing back to it.
Output goes to `data/metrics_stage/*.parquet`, which `08_metrics_db.py`
publishes into `data/metrics.duckdb`.

Every tunable lives in the PARAMS block below — one place to retune.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DATA_DIR, DUCKDB_PATH, NO_ENGAGEMENT_PLATFORMS, TS_EXCLUDE_PLATFORMS
from lib import stats_v2 as S

# ==========================================================================
# PARAMS — the single place to retune. A concurrent statistical review may
# recommend different defaults; nothing below this block hardcodes a number.
# ==========================================================================
PARAMS: dict = {
    # --- windowing (F4 / D3) -------------------------------------------
    "window_start": "2026-06-14",   # first day of the live collection window
    "window_end": None,             # None -> max observed posted day
    # --- count baseline (F1) ------------------------------------------
    "baseline_window": 28,          # trailing days used to fit the NB baseline
    "baseline_min_days": 14,        # F3 gate: minimum trailing observations
    "baseline_min_mean": 0.5,       # F3 gate: minimum trailing mean count
    "support_min_v": 3,             # F3 gate: minimum V for z-like measures
    "nb_use_exposure_offset": True,  # F2: rescale baseline mu by effort ratio
    "exposure_scale_cap": 3.0,       # F2 guard: max/min effort-ratio multiplier (see fit loop)
    "nb_use_drift_adjust": True,     # F2b: project baseline level along its own log-linear trend
    # --- multiple testing (F6) ----------------------------------------
    "fdr_method": "bh",            # 'bh' or 'by'
    "fdr_alpha": 0.05,
    "fdr_scope": "day",            # BH applied within each day
    "fdr_pvalue_kind": "midp",     # mid-p for FDR; exact p reported as surprise_p
    # --- growth / momentum / acceleration -----------------------------
    "alpha_count": 1.0,            # log smoothing constant on counts
    "ema_fast": 7,
    "ema_slow": 21,
    "growth_7d_lag": 7,
    "savgol_window": 5,
    "savgol_order": 2,
    "savgol_mode": "causal",       # 'causal' (no look-ahead) or 'centered'
    # --- burst detection ----------------------------------------------
    "burst_s": 2.0,                # burst rate multiplier lambda1 = s*lambda0
    "burst_gamma": 1.0,            # Kleinberg state-transition cost
    "burst_cusum_h": 5.0,          # CUSUM decision interval
    "burst_min_days_kleinberg": 21,
    "burst_min_total_kleinberg": 30,
    "burst_min_mean_kleinberg": 0.5,
    # --- emergence ----------------------------------------------------
    "threshold_k": 5.0,            # posts/day defining "emerged"
    "emergence_weights": {"surprise": 0.40, "accel_smooth": 0.25,
                          "novelty": 0.20, "speed": 0.15},
    "emergence_min_components": 2,
    "emergence_alert_pct": 0.90,   # emergence_score percentile for a candidate
    # --- novelty ------------------------------------------------------
    "novelty_window": 21,          # trailing days for the topic centroid
    "novelty_min_day_posts": 3,
    "novelty_min_base_posts": 20,
    # --- empirical Bayes (F5) -----------------------------------------
    "eb_min_parent_n": 30,
    # --- virality (F9) ------------------------------------------------
    "age_bands": [0.0, 0.5, 1.0, 2.0, 4.0, 7.0, 14.0, 30.0, 90.0],
    "virality_min_band_n": 20,
    # --- reach (F10) --------------------------------------------------
    "channel_median_min_posts": 3,
    # --- coordination -------------------------------------------------
    "coord_min_v": 10,
    "coord_max_diversity": 0.30,   # author_1_hhi = 1-HHI, low => concentrated
    "coord_min_dup_rate": 0.30,
    # --- persistence (F7) ---------------------------------------------
    "ar1_min_obs": 12,
    "ljung_box_lags": 7,
    # --- readability ---------------------------------------------------
    "readability_clip": [0.0, 100.0],
    # --- hashtag grain (section 4) ------------------------------------
    "tag_min_posts_dim": 5,        # appear in tag_dim
    "tag_min_posts_ts": 20,        # get a daily time series
    # --- null control (F8) --------------------------------------------
    "permutation_n": 499,
    # --- misc ----------------------------------------------------------
    "seed": S.RNG_SEED,
    "hate_calibrated": False,      # non-negotiable #3 — never flip without labels
}

STAGE_DIR = DATA_DIR / "metrics_stage"
TS_PLATFORMS_EXCLUDE = set(TS_EXCLUDE_PLATFORMS) | {"youtube_comment"}
HASHTAG_RE = re.compile(r"#(\w{2,60})", re.UNICODE)
LATIN_RE = re.compile(r"[a-z]")


def log(msg: str) -> None:
    print(f"[07] {msg}", flush=True)


# ==========================================================================
# helpers
# ==========================================================================
def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=1).mean()


def normalise_tag(raw: str) -> Optional[str]:
    """lowercase, strip '#'/punctuation, drop Latin diacritics."""
    if not isinstance(raw, str):
        return None
    s = unicodedata.normalize("NFKC", raw).strip().lstrip("#").lower()
    s = re.sub(r"[^\w]+", "", s, flags=re.UNICODE)
    if not s:
        return None
    # Strip combining marks only for majority-Latin tags: doing it for
    # Devanagari/Hebrew/Arabic would destroy matras and vowel points.
    letters = [c for c in s if c.isalpha()]
    if letters and sum(bool(LATIN_RE.match(c)) for c in letters) / len(letters) > 0.6:
        s = "".join(c for c in unicodedata.normalize("NFD", s)
                    if unicodedata.category(c) != "Mn")
    s = s.strip("_-")
    if len(s) < 2 or len(s) > 60 or s.isdigit():
        return None
    return s


def parse_hashtag_field(h) -> list[str]:
    if not isinstance(h, str) or not h.strip():
        return []
    t = h.strip()
    if t.startswith("["):
        try:
            v = json.loads(t)
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            pass
    return [p for p in re.split(r"[,\s]+", t) if p]


def tag_id_of(tag: str) -> str:
    return hashlib.sha1(tag.encode("utf-8")).hexdigest()[:12]


def norm_text(t) -> str:
    if not isinstance(t, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", "", t.lower(), flags=re.UNICODE)).strip()


def virality_ratio(platform: str, likes, comments, shares, views) -> float:
    """Platform amplification ratio (v1 formulas retained — they were sound)."""
    lk = 0.0 if pd.isna(likes) else float(likes)
    cm = 0.0 if pd.isna(comments) else float(comments)
    sh = 0.0 if pd.isna(shares) else float(shares)
    vw = 0.0 if pd.isna(views) else float(views)
    if platform == "telegram":
        return sh / (vw + 1.0) if (vw or sh) else np.nan
    if platform in ("instagram", "facebook"):
        d = lk + cm + sh
        return sh / (d + 1.0) if d else np.nan
    if platform == "youtube_video":
        return (lk + cm) / (vw + 1.0) if (vw or lk or cm) else np.nan
    if platform == "linkedin":
        return cm / (lk + 1.0) if (lk or cm) else np.nan
    if platform == "snapchat":
        return sh / (vw + 1.0) if (vw or sh) else np.nan
    return np.nan


# ==========================================================================
# load
# ==========================================================================
def load_source() -> dict:
    log(f"opening {DUCKDB_PATH} READ-ONLY")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        posts = con.execute(
            """
            SELECT pf.post_uid, pf.platform, pf.author_uid, pf.author_name, pf.is_verified,
                   pf.text, pf.lang, pf.posted_at, pf.scraped_at, pf.scrape_lag_days,
                   pf.keyword, pf.hashtags, pf.likes, pf.comments, pf.shares, pf.views,
                   pf.has_engagement,
                   COALESCE(pt.topic_id,'unassigned') AS topic_id, pt.confidence,
                   ps.tone_score, ps.tone_valid, ps.hate_score, ps.hate_valid,
                   ps.readability_score, ps.readability_valid
            FROM post_facts pf
            LEFT JOIN post_topic  pt USING (post_uid)
            LEFT JOIN post_scores ps USING (post_uid)
            """
        ).df()
        topic_dim = con.execute("SELECT * FROM topic_dim").df()
        effort_ce = con.execute("SELECT * FROM collection_effort").df()
        extract = con.execute("SELECT * FROM extract_meta").df()
        emb = con.execute(
            "SELECT post_uid, platform, embedding FROM post_embeddings"
        ).fetchnumpy()
        v1 = con.execute("SELECT * FROM metrics_daily").df()
    finally:
        con.close()

    # ---- integrity assertions (correctness requirements) -----------------
    n_dupe = int(posts["post_uid"].duplicated().sum())
    assert n_dupe == 0, f"post_uid double counting: {n_dupe} duplicate rows after join"
    neg_lag = int((posts["scrape_lag_days"] < 0).sum())
    if neg_lag:
        # Live collector clock skew, not a data-integrity failure: on the 2026-07-29
        # refresh this was 1 row at -0.0002 days (~19s) -- posted_at landed a hair after
        # scraped_at. A live, still-ingesting DB will occasionally produce this; crashing
        # the whole extract on it contradicts the "must not crash on live-data surprises"
        # principle applied everywhere else in this pipeline. WARN and null it out (below)
        # instead of hard-failing.
        log(f"WARN: {neg_lag} rows have negative scrape_lag_days (clock skew) -- "
            f"nulling scrape_lag_days for those rows rather than crashing")
    posts.loc[posts["scrape_lag_days"] < 0, "scrape_lag_days"] = np.nan

    posts["posted_at"] = pd.to_datetime(posts["posted_at"], utc=True, errors="coerce")
    posts["scraped_at"] = pd.to_datetime(posts["scraped_at"], utc=True, errors="coerce")
    # SPEC DEVIATION (documented): some platforms (e.g. pinterest — 0/78 posted_at in
    # the 2026-07 snapshot) carry no posted_at at all. Dropping those rows outright
    # would silently remove the whole platform from every downstream table (platform_dim,
    # metrics_daily, tag_metrics_daily), reintroducing by data-shape the same exclusion
    # this pipeline was explicitly changed to stop doing by platform name. Instead: fall
    # back to scraped_at (the day we observed the post) as the aggregation-grain "day"
    # only when posted_at is missing, and flag it via day_is_scrape_proxy so it is
    # auditable and never confused with a true publish date. scrape_lag_days is left
    # NaN for these rows (unknown, not fabricated) and support gates (baseline_days>=14,
    # min V, etc.) still apply, so this does not manufacture false precision -- it only
    # lets the platform's rows exist in the panel at all, as the support gates intend.
    posts["day_is_scrape_proxy"] = posts["posted_at"].isna() & posts["scraped_at"].notna()
    day_ts = posts["posted_at"].fillna(posts["scraped_at"])
    posts = posts[day_ts.notna()].copy()
    posts["day"] = day_ts.loc[posts.index].dt.date

    E = None
    if len(emb.get("post_uid", [])):
        mat = np.vstack([np.asarray(x, dtype=np.float32) for x in emb["embedding"]])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / np.maximum(norms, 1e-9)
        E = {"uid": np.asarray(emb["post_uid"]), "mat": mat,
             "index": {u: i for i, u in enumerate(emb["post_uid"])}}
    return {"posts": posts, "topic_dim": topic_dim, "effort_ce": effort_ce,
            "extract": extract, "emb": E, "v1": v1}


# ==========================================================================
# effort / exposure (F2)
# ==========================================================================
def build_effort(posts: pd.DataFrame, effort_ce: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Exposure offset per (platform, day).

    SPEC DEVIATION (documented): `collection_effort.active_channels` cannot serve
    as the offset on the posted-day grain. It is keyed on *scrape* day, covers
    only 8/45 telegram posted-days, and reports active_channels = 0 on 5 of
    those 8 days despite thousands of ingested rows. Splicing it with a fallback
    would inject scale discontinuities that look exactly like growth.

    We therefore use the *observed* number of distinct active channels/authors
    per platform-day, which is the same quantity ("active channels that day")
    measured on the correct grain with 100% coverage, and cross-check it against
    collection_effort where both exist (correlation recorded in run_meta).
    """
    obs = (
        posts.groupby(["platform", "day"], dropna=False)["author_uid"]
        .nunique()
        .reset_index(name="effort_obs")
    )
    ce = effort_ce.copy()
    if len(ce):
        ce["day"] = pd.to_datetime(ce["day"], errors="coerce").dt.date
        ce["effort_reported"] = ce["active_channels"].replace(0, np.nan).astype(float)
        ce = ce[["platform", "day", "effort_reported"]]
    else:
        ce = pd.DataFrame(columns=["platform", "day", "effort_reported"])
    eff = obs.merge(ce, on=["platform", "day"], how="left")
    eff["effort"] = eff["effort_obs"].astype(float).clip(lower=1.0)
    eff["effort_source"] = "observed_active_channels"

    both = eff.dropna(subset=["effort_reported"])
    diag = {"n_days_with_reported_effort": int(len(both)),
            "n_platform_days": int(len(eff))}
    if len(both) > 3:
        diag["corr_observed_vs_reported"] = float(
            np.corrcoef(both["effort_obs"], both["effort_reported"])[0, 1]
        )
        diag["median_ratio_reported_over_observed"] = float(
            (both["effort_reported"] / both["effort_obs"]).median()
        )
    per_plat = {}
    for p, g in both.groupby("platform"):
        if len(g) > 2:
            per_plat[p] = {"n": int(len(g)),
                           "corr": float(np.corrcoef(g["effort_obs"], g["effort_reported"])[0, 1])}
    diag["per_platform"] = per_plat
    return eff, diag


# ==========================================================================
# post-level derived fields
# ==========================================================================
def enrich_posts(posts: pd.DataFrame, P: dict) -> pd.DataFrame:
    p = posts.reset_index(drop=True).copy()
    p["E_sum"] = p[["likes", "comments", "shares", "views"]].fillna(0).sum(axis=1)
    p.loc[~p["has_engagement"].fillna(False), "E_sum"] = np.nan
    p["virality_ratio"] = [
        virality_ratio(pl, lk, cm, sh, vw)
        for pl, lk, cm, sh, vw in zip(p["platform"], p["likes"], p["comments"],
                                      p["shares"], p["views"])
    ]
    # F9 — age-conditional empirical percentile
    pct, refn = S.age_conditional_percentile(
        p["virality_ratio"].to_numpy(dtype=float),
        p["scrape_lag_days"].to_numpy(dtype=float),
        p["platform"].to_numpy(dtype=object),
        bands=P["age_bands"],
        min_band_n=P["virality_min_band_n"],
    )
    p["virality_pct_post"] = pct
    p["virality_ref_n"] = refn

    # F10 — reach normalised by channel median views
    vw = p["views"].where(p["views"] > 0)
    chan = (
        p.assign(_v=vw).dropna(subset=["_v"])
        .groupby(["platform", "author_uid"])["_v"]
        .agg(["median", "count"])
        .reset_index()
    )
    chan = chan[chan["count"] >= P["channel_median_min_posts"]]
    chan = chan.rename(columns={"median": "chan_median_views"})[
        ["platform", "author_uid", "chan_median_views"]
    ]
    p = p.merge(chan, on=["platform", "author_uid"], how="left").reset_index(drop=True)
    vw2 = p["views"].where(p["views"] > 0)
    p["reach_audience_norm_post"] = np.where(
        p["chan_median_views"].notna() & vw2.notna(),
        p["views"] / p["chan_median_views"].replace(0, np.nan),
        np.nan,
    )
    # scores: valid-only, readability winsorised
    lo, hi = P["readability_clip"]
    p["tone_v"] = p["tone_score"].where(p["tone_valid"].fillna(False))
    p["hate_v"] = p["hate_score"].where(p["hate_valid"].fillna(False))
    p["read_v"] = p["readability_score"].where(p["readability_valid"].fillna(False)).clip(lo, hi)
    p["read_ok"] = p["readability_valid"].fillna(False).astype(float)
    p["text_norm"] = [norm_text(t) for t in p["text"]]
    return p


def mine_tags(posts: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """post_uid x tag long table from the union of the hashtags field + inline #tags."""
    rows_uid, rows_tag, rows_src = [], [], []
    n_field = n_mined = n_any = 0
    per_plat: dict[str, dict] = {}
    for uid, plat, hfield, text in zip(posts["post_uid"], posts["platform"],
                                       posts["hashtags"], posts["text"]):
        f = [normalise_tag(x) for x in parse_hashtag_field(hfield)]
        m = [normalise_tag(x) for x in (HASHTAG_RE.findall(text) if isinstance(text, str) else [])]
        f = {x for x in f if x}
        m = {x for x in m if x}
        st = per_plat.setdefault(plat, {"n": 0, "field": 0, "mined": 0, "union": 0})
        st["n"] += 1
        if f:
            st["field"] += 1
            n_field += 1
        if m:
            st["mined"] += 1
            n_mined += 1
        u = f | m
        if u:
            st["union"] += 1
            n_any += 1
        for t in u:
            rows_uid.append(uid)
            rows_tag.append(t)
            rows_src.append("both" if (t in f and t in m) else ("field" if t in f else "mined"))
    long = pd.DataFrame({"post_uid": rows_uid, "tag": rows_tag, "tag_source": rows_src})
    cov = {
        "n_posts": int(len(posts)),
        "n_posts_with_any_tag": int(n_any),
        "coverage_pct": round(100.0 * n_any / max(len(posts), 1), 2),
        "n_posts_with_field_tag": int(n_field),
        "n_posts_with_mined_tag": int(n_mined),
        "n_distinct_tags": int(long["tag"].nunique()) if len(long) else 0,
        "per_platform": {
            k: {**v, "coverage_pct": round(100.0 * v["union"] / max(v["n"], 1), 2)}
            for k, v in sorted(per_plat.items(), key=lambda kv: -kv[1]["n"])
        },
    }
    return long, cov


# ==========================================================================
# cell aggregation
# ==========================================================================
def aggregate_cells(posts: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    g = posts.groupby(keys, dropna=False)

    def _sq(col):
        return (posts[col] ** 2)

    base = g.agg(
        V=("post_uid", "count"),
        tone_sum=("tone_v", "sum"), tone_n=("tone_v", "count"),
        hate_sum=("hate_v", "sum"), hate_n=("hate_v", "count"),
        hate_hits=("hate_v", lambda s: float((s.dropna() >= 0.5).sum())),
        read_sum=("read_v", "sum"), read_n=("read_v", "count"),
        read_cov=("read_ok", "mean"),
        vir_sum=("virality_pct_post", "sum"), vir_n=("virality_pct_post", "count"),
        views_sum=("views", "sum"),
        E_sum=("E_sum", "sum"),
        reach_norm_sum=("reach_audience_norm_post", "sum"),
        reach_norm_n=("reach_audience_norm_post", "count"),
        n_authors=("author_uid", "nunique"),
        mean_confidence=("confidence", "mean"),
    ).reset_index()

    # sums of squares for the EB within-variance component
    ss = posts.assign(
        _t2=posts["tone_v"] ** 2, _h2=posts["hate_v"] ** 2, _v2=posts["virality_pct_post"] ** 2
    ).groupby(keys, dropna=False).agg(
        tone_ss=("_t2", "sum"), hate_ss=("_h2", "sum"), vir_ss=("_v2", "sum")
    ).reset_index()
    base = base.merge(ss, on=keys, how="left")

    # author diversity + duplicate rate
    def _div(df: pd.DataFrame) -> pd.Series:
        vc = df["author_uid"].fillna("unknown").value_counts().to_numpy(dtype=float)
        tn = df["text_norm"]
        tn = tn[tn.str.len() > 20]
        dup = float(1.0 - tn.nunique() / len(tn)) if len(tn) else np.nan
        return pd.Series({
            "author_entropy": S.shannon_entropy(vc),
            "author_1_hhi": 1.0 - S.hhi(vc),
            "duplicate_rate": dup,
        })

    div = posts.groupby(keys, dropna=False)[["author_uid", "text_norm"]].apply(
        _div, include_groups=False
    ).reset_index()
    base = base.merge(div, on=keys, how="left")

    base["tone_mean"] = base["tone_sum"] / base["tone_n"].replace(0, np.nan)
    base["hate_mean"] = base["hate_sum"] / base["hate_n"].replace(0, np.nan)
    base["hate_prevalence"] = base["hate_hits"] / base["hate_n"].replace(0, np.nan)
    base["readability_mean"] = base["read_sum"] / base["read_n"].replace(0, np.nan)
    base["readability_coverage"] = base["read_cov"]
    base["virality_pct_raw"] = base["vir_sum"] / base["vir_n"].replace(0, np.nan)
    base["reach_audience_norm"] = base["reach_norm_sum"] / base["reach_norm_n"].replace(0, np.nan)
    base["reach_proxy"] = np.where(
        base["views_sum"].fillna(0) > 0, base["views_sum"], np.log1p(base["E_sum"].fillna(0))
    )
    return base


# ==========================================================================
# empirical Bayes (F5)
# ==========================================================================
def apply_eb(cells: pd.DataFrame, group_key: str, P: dict) -> dict:
    """Add tone_eb / hate_eb / virality_pct to `cells` in place. Returns diagnostics."""
    diag = {}
    for name, mcol, ncol, sscol, out in [
        ("tone", "tone_mean", "tone_n", "tone_ss", "tone_eb"),
        ("hate", "hate_mean", "hate_n", "hate_ss", "hate_eb"),
        ("virality", "virality_pct_raw", "vir_n", "vir_ss", "virality_pct"),
    ]:
        n = cells[ncol].astype(float)
        m = cells[mcol].astype(float)
        sumv = m * n
        # pooled within-cell variance
        num = float(np.nansum(cells[sscol].astype(float) - n * m**2))
        den = float(np.nansum(np.where(n > 1, n - 1, 0)))
        within = num / den if den > 0 else np.nan
        if not np.isfinite(within) or within <= 0:
            within = float(np.nanvar(m)) if np.isfinite(np.nanvar(m)) else 1.0
        ok = n > 0
        k, tau2 = S.estimate_eb_k(m[ok].to_numpy(), n[ok].to_numpy(), within)

        glob = float(np.nansum(sumv) / max(np.nansum(n), 1e-9)) if ok.any() else np.nan
        # parent means: group level, then platform, then global
        parents: dict[tuple, float] = {}
        gsum = cells.assign(_s=sumv, _n=n).groupby(group_key, dropna=False)[["_s", "_n"]].sum()
        for gid, row in gsum.iterrows():
            parents[("g", gid)] = row["_s"] / row["_n"] if row["_n"] >= P["eb_min_parent_n"] else np.nan
        if "platform" in cells.columns:
            psum = cells.assign(_s=sumv, _n=n).groupby("platform", dropna=False)[["_s", "_n"]].sum()
            for pid, row in psum.iterrows():
                parents[("p", pid)] = row["_s"] / row["_n"] if row["_n"] >= P["eb_min_parent_n"] else np.nan

        pm = []
        for gid, plat in zip(cells[group_key],
                             cells["platform"] if "platform" in cells.columns else [None] * len(cells)):
            v = parents.get(("g", gid), np.nan)
            if not np.isfinite(v):
                v = parents.get(("p", plat), np.nan)
            if not np.isfinite(v):
                v = glob
            pm.append(v)
        cells[out] = [S.eb_shrink(mm, nn, pp, k) for mm, nn, pp in zip(m, n, pm)]
        diag[name] = {"k": round(float(k), 4), "tau2": None if tau2 is None or not np.isfinite(tau2) else round(float(tau2), 6),
                      "within_var": round(float(within), 6), "global_mean": None if not np.isfinite(glob) else round(glob, 6)}
    cells["virality_n"] = cells["vir_n"]
    return diag


# ==========================================================================
# the single production time-series path (D5: harness must call THIS)
# ==========================================================================
def compute_panel(
    cells: pd.DataFrame,
    keys: list[str],
    effort: pd.DataFrame,
    P: dict,
    *,
    platform_days: dict,
    window_start: date,
    window_end: date,
    novelty_fn=None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Effort-normalised growth / acceleration / burst / surprise per series.

    `keys` is the series identity (everything except `day`). Returns
    (panel, episodes, diagnostics).
    """
    series_keys = [k for k in keys if k != "day"]
    has_plat = "platform" in series_keys
    out_rows: list[pd.DataFrame] = []
    episodes: list[dict] = []
    diag = {"burst_methods": {}, "n_series": 0, "n_series_ts": 0}

    eff_map = {(r.platform, r.day): r.effort for r in effort.itertuples()}
    eff_src = {(r.platform, r.day): r.effort_source for r in effort.itertuples()}
    # for platform-agnostic grains, exposure is the total across TS platforms
    tot_eff = effort.groupby("day")["effort"].sum().to_dict()

    in_win = cells["day"].between(window_start, window_end)
    hist = cells.loc[~in_win].copy()
    live = cells.loc[in_win].copy()

    for gid, g in live.groupby(series_keys, dropna=False):
        gid = gid if isinstance(gid, tuple) else (gid,)
        diag["n_series"] += 1
        plat = dict(zip(series_keys, gid)).get("platform") if has_plat else None
        # calendar: platform-active days inside the window, from first appearance
        if plat is not None:
            cal = sorted(d for d in platform_days.get(plat, set())
                         if window_start <= d <= window_end)
        else:
            cal = sorted(d for d in platform_days["__any__"]
                         if window_start <= d <= window_end)
        first = min(g["day"])
        cal = [d for d in cal if d >= first]
        if not cal:
            continue
        s = g.set_index("day").reindex(cal)
        for k, v in zip(series_keys, gid):
            s[k] = v
        s["V"] = s["V"].fillna(0.0)
        s = s.reset_index().rename(columns={"index": "day"})

        # ---- exposure ------------------------------------------------
        if plat is not None:
            s["effort"] = [eff_map.get((plat, d), np.nan) for d in s["day"]]
            s["effort_source"] = [eff_src.get((plat, d), "unavailable") for d in s["day"]]
        else:
            s["effort"] = [tot_eff.get(d, np.nan) for d in s["day"]]
            s["effort_source"] = "observed_active_channels_all_platforms"
        s["effort"] = s["effort"].astype(float).clip(lower=1.0)
        s["V_rate"] = s["V"] / s["effort"]

        a = P["alpha_count"]
        s["log_rate"] = np.log(s["V"] + a) - np.log(s["effort"])
        s["log_count"] = np.log(s["V"] + a)

        # ---- growth family (on the effort-normalised log rate) --------
        s["growth"] = s["log_rate"].diff()
        s["growth_count"] = s["log_count"].diff()
        L = P["growth_7d_lag"]
        v7 = s["V"].rolling(L, min_periods=1).sum()
        e7 = s["effort"].rolling(L, min_periods=1).sum()
        r7 = np.log(v7 + a) - np.log(e7)
        s["growth_7d"] = r7 - r7.shift(L)
        gz = s["growth"].fillna(0.0)
        s["momentum"] = ema(gz, P["ema_fast"]) - ema(gz, P["ema_slow"])

        # ---- acceleration family (spec section 3) --------------------
        s["accel"] = s["growth"].diff()
        deriv = (S.causal_savgol_deriv if P["savgol_mode"] == "causal"
                 else S.centered_savgol_deriv)
        s["accel_smooth"] = deriv(
            s["log_rate"].to_numpy(dtype=float),
            window=P["savgol_window"], order=P["savgol_order"], deriv=2,
        )

        # ---- NB baseline + surprise (F1, F2) -------------------------
        counts = s["V"].to_numpy(dtype=float)
        fits = S.trailing_baseline_fits(
            counts, window=P["baseline_window"],
            min_days=P["baseline_min_days"], min_mean=P["baseline_min_mean"],
        )
        mu_adj, theta, bdays, bmean, dist, p_ex, p_mid = ([] for _ in range(7))
        bmethod = []
        for t, f in enumerate(fits):
            if f is None or not f.ok:
                mu_adj.append(np.nan); theta.append(np.nan); bdays.append(0)
                bmean.append(np.nan); dist.append(None); bmethod.append(None)
                p_ex.append(np.nan); p_mid.append(np.nan)
                continue
            lo = max(0, t - P["baseline_window"])
            e_base = float(np.nanmean(s["effort"].to_numpy()[lo:t])) if t > lo else np.nan
            scale = 1.0
            if P["nb_use_exposure_offset"] and np.isfinite(e_base) and e_base > 0:
                scale = float(s["effort"].iloc[t]) / e_base
                # Cap the offset. `effort` is distinct authors ON THE DAY BEING
                # TESTED, so it is endogenous to the very signal the offset is
                # meant to normalise: a real event recruits authors, inflating
                # the baseline it is measured against. Uncapped this reached
                # 154x, which scored 2026-07-27 t002/reddit -- V=365, the largest
                # real Reddit day in the window -- at p=0.964, i.e. completely
                # unremarkable. The cap keeps genuine collection-effort changes
                # (typically <2x) corrected while refusing to explain away an
                # order-of-magnitude spike as "more people were posting".
                scale = float(np.clip(scale, 1.0 / P["exposure_scale_cap"],
                                      P["exposure_scale_cap"]))
            # F2b: project the baseline level forward along its own trend. The
            # trailing baseline is a window AVERAGE, so on a rising series it sits
            # below the day under test and ordinary growth reads as an anomaly.
            # This is what the harness's measured over-rejection actually was --
            # see S.drift_factor's docstring for the calibration numbers.
            if P["nb_use_drift_adjust"]:
                scale *= S.drift_factor(counts[lo:t])
            # theta must move with mu: NB2 variance is mu + mu^2/theta, so
            # rescaling mu alone silently changes the dispersion the tail assumes.
            theta_adj = f.theta * scale if np.isfinite(f.theta) else f.theta
            f_adj = S.NBFit(f.mu * scale, theta_adj, f.dist, f.method, f.n, f.var)
            mu_adj.append(f_adj.mu); theta.append(f_adj.theta); bdays.append(f.n)
            bmean.append(f.mu); dist.append(f.dist); bmethod.append(f.method)
            p_ex.append(S.exceedance_p(counts[t], f_adj, midp=False))
            p_mid.append(S.exceedance_p(counts[t], f_adj, midp=True))
        s["baseline_mu"] = mu_adj
        s["baseline_theta"] = theta
        s["baseline_days"] = bdays
        s["baseline_mean"] = bmean
        s["baseline_dist"] = dist
        # How theta was obtained (mle / mom / theta_floor_underdisp / ...). Previously
        # computed on NBFit and dropped, so the shipped data could not tell you
        # whether a baseline was a real MLE or a fallback.
        s["baseline_method"] = bmethod
        s["surprise_p"] = p_ex
        s["_p_fdr"] = p_mid if P["fdr_pvalue_kind"] == "midp" else p_ex
        s["surprise"] = S.surprise_from_p(s["surprise_p"].to_numpy(dtype=float))

        # ---- F3 support gate -----------------------------------------
        s["support_n"] = s["V"]
        s["support_ok"] = (
            (s["baseline_days"] >= P["baseline_min_days"])
            & (s["baseline_mean"] >= P["baseline_min_mean"])
            & (s["V"] >= P["support_min_v"])
        ).fillna(False)

        # ---- burst ----------------------------------------------------
        # Burst detection is a *series-level* estimator, so it carries a
        # series-level support gate rather than the per-cell F3 gate: a series
        # too short or too sparse to have a baseline gets NULL, not a guess.
        burst_support_ok = bool(
            len(s) >= P["baseline_min_days"]
            and float(np.nanmean(counts)) >= P["baseline_min_mean"]
        )
        s["burst_support_ok"] = burst_support_ok
        s["burst_episode_id"] = None
        if not burst_support_ok:
            s["burst_state"] = np.nan
            s["burst_intensity"] = np.nan
            s["burst_method"] = None
            s["onset_slope"] = np.nan
            eps = []
            diag["burst_methods"]["ungated_insufficient_support"] = (
                diag["burst_methods"].get("ungated_insufficient_support", 0) + 1)
        else:
            br = S.detect_bursts(
                counts,
                baseline_mu=s["baseline_mu"].to_numpy(dtype=float),
                min_days_kleinberg=P["burst_min_days_kleinberg"],
                min_total_kleinberg=P["burst_min_total_kleinberg"],
                min_mean_kleinberg=P["burst_min_mean_kleinberg"],
                s=P["burst_s"], gamma=P["burst_gamma"], cusum_h=P["burst_cusum_h"],
            )
            s["burst_state"] = br.state
            s["burst_intensity"] = br.intensity
            s["burst_method"] = br.method
            diag["burst_methods"][br.method] = diag["burst_methods"].get(br.method, 0) + 1
            eps = S.extract_episodes(br.state, counts, br.intensity)
        for ei, ep in enumerate(eps):
            sid = "_".join(str(x) for x in gid)
            epid = f"{sid}__{s['day'].iloc[ep['start_i']]}"
            s.loc[ep["start_i"]: ep["end_i"], "burst_episode_id"] = epid
            # rising limb includes the last pre-burst day so that a 1-day
            # episode still has a measurable onset (rise from baseline).
            rise_lo = max(0, ep["start_i"] - 1)
            rise = counts[rise_lo: ep["peak_i"] + 1]
            onset = S.fit_logistic_onset(rise)
            tail_end = min(len(counts), ep["end_i"] + 8)
            decay = S.fit_exp_decay(counts[ep["peak_i"]: tail_end])
            infl_i = rise_lo + onset.get("inflection_i", np.nan)
            infl_day = None
            if np.isfinite(infl_i):
                j = int(round(float(infl_i)))
                if 0 <= j < len(s):
                    infl_day = s["day"].iloc[j]
            rec = {"episode_id": epid,
                   "start_day": s["day"].iloc[ep["start_i"]],
                   "peak_day": s["day"].iloc[ep["peak_i"]],
                   "end_day": s["day"].iloc[ep["end_i"]],
                   "peak_V": ep["peak_V"], "total_V": ep["total_V"],
                   "n_days": ep["n_days"], "level": ep["level"],
                   "burst_method": br.method,
                   "onset_slope": onset["onset_slope"],
                   "onset_method": onset["method"],
                   "onset_r2": onset["r2"],
                   "inflection_day": infl_day,
                   "decay_lambda": decay["decay_lambda"],
                   "half_life_days": decay["half_life_days"],
                   "r2": decay["r2"], "decay_ok": decay["ok"]}
            for k, v in zip(series_keys, gid):
                rec[k] = v
            episodes.append(rec)
        # onset_slope per row: the slope of the episode the row belongs to
        os_map = {e["episode_id"]: e["onset_slope"] for e in episodes}
        s["onset_slope"] = [os_map.get(x, np.nan) for x in s["burst_episode_id"]]

        # ---- days_to_threshold (no look-ahead: NULL until crossed) ----
        k_thr = P["threshold_k"]
        crossed = np.flatnonzero(counts >= k_thr)
        d2t = np.full(len(s), np.nan)
        if crossed.size:
            first_cross = int(crossed[0])
            d2t[first_cross:] = float(first_cross)  # days since first appearance
        s["days_to_threshold"] = d2t

        # ---- novelty ---------------------------------------------------
        if novelty_fn:
            nv, nv_n, nv_bn = novelty_fn(s, gid, series_keys)
            s["novelty"], s["novelty_n"], s["novelty_base_n"] = nv, nv_n, nv_bn
        else:
            s["novelty"] = np.nan
            s["novelty_n"] = np.nan
            s["novelty_base_n"] = np.nan

        s["in_window"] = True
        out_rows.append(s)
        diag["n_series_ts"] += 1

    panel = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame()

    # ---- historical rows: cross-sectional only, TS columns NULL --------
    if len(hist):
        hist = hist.copy()
        hist["in_window"] = False
        if has_plat:
            hist["effort"] = [eff_map.get((pl, d), np.nan)
                              for pl, d in zip(hist["platform"], hist["day"])]
            hist["effort_source"] = "observed_active_channels"
        else:
            hist["effort"] = [tot_eff.get(d, np.nan) for d in hist["day"]]
            hist["effort_source"] = "observed_active_channels_all_platforms"
        hist["effort"] = hist["effort"].astype(float).clip(lower=1.0)
        hist["V_rate"] = hist["V"] / hist["effort"]
        for c in ["growth", "growth_count", "growth_7d", "momentum", "accel",
                  "accel_smooth", "surprise", "surprise_p", "_p_fdr",
                  "baseline_mu", "baseline_theta", "baseline_mean",
                  "burst_intensity", "onset_slope", "days_to_threshold", "novelty",
                  "novelty_n", "novelty_base_n", "log_rate", "log_count"]:
            hist[c] = np.nan
        hist["baseline_days"] = 0
        hist["baseline_dist"] = None
        hist["burst_state"] = np.nan
        hist["burst_method"] = None
        hist["burst_episode_id"] = None
        hist["burst_support_ok"] = False
        hist["support_n"] = hist["V"]
        hist["support_ok"] = False
        panel = pd.concat([panel, hist], ignore_index=True) if len(panel) else hist

    # ---- share of volume + growth_share (within grain, per day) --------
    if len(panel):
        tot = panel.groupby("day")["V"].transform("sum").replace(0, np.nan)
        panel["share_V"] = panel["V"] / tot
        panel = panel.sort_values(series_keys + ["day"]).reset_index(drop=True)
        gs = panel.groupby(series_keys, dropna=False)["share_V"].transform(
            lambda x: np.log(x.fillna(0) + 1e-6).diff()
        )
        panel["growth_share"] = np.where(panel["in_window"], gs, np.nan)

        # ---- FDR per day (F6) -------------------------------------------
        panel["surprise_q"] = np.nan
        gated = panel["support_ok"] & panel["_p_fdr"].notna()
        if P["fdr_scope"] == "day":
            for d, idx in panel.loc[gated].groupby("day").groups.items():
                idx = list(idx)
                panel.loc[idx, "surprise_q"] = S.bh_qvalues(
                    panel.loc[idx, "_p_fdr"].to_numpy(dtype=float), method=P["fdr_method"]
                )
        else:
            idx = list(panel.index[gated])
            panel.loc[idx, "surprise_q"] = S.bh_qvalues(
                panel.loc[idx, "_p_fdr"].to_numpy(dtype=float), method=P["fdr_method"]
            )
        gi = list(panel.index[gated])
        panel["surprise_q_global"] = np.nan
        if gi:
            panel.loc[gi, "surprise_q_global"] = S.bh_qvalues(
                panel.loc[gi, "_p_fdr"].to_numpy(dtype=float), method=P["fdr_method"]
            )
        panel["alert_flag"] = (panel["surprise_q"] < P["fdr_alpha"]).fillna(False)

        # ---- support gate blanks the trend/evidence family --------------
        # burst_* is intentionally NOT in this list: it carries its own
        # series-level gate (burst_support_ok) because it is a series estimator.
        for c in ["surprise", "surprise_p", "surprise_q", "surprise_q_global",
                  "accel", "accel_smooth", "momentum", "growth", "growth_share",
                  "growth_7d", "growth_count"]:
            panel.loc[~panel["support_ok"], c] = np.nan

        # ---- emergence composite ---------------------------------------
        w = P["emergence_weights"]
        comp = pd.DataFrame(index=panel.index)
        comp["surprise"] = S.rank_pct(panel["surprise"].to_numpy(dtype=float))
        comp["accel_smooth"] = S.rank_pct(panel["accel_smooth"].to_numpy(dtype=float))
        comp["novelty"] = S.rank_pct(panel["novelty"].to_numpy(dtype=float))
        comp["speed"] = S.rank_pct(-np.log1p(panel["days_to_threshold"].to_numpy(dtype=float)))
        wts = pd.Series({k: w[k] for k in comp.columns})
        mask = comp.notna()
        num = (comp.fillna(0) * wts).sum(axis=1)
        den = (mask * wts).sum(axis=1)
        n_comp = mask.sum(axis=1)
        panel["emergence_score"] = np.where(
            (n_comp >= P["emergence_min_components"]) & (den > 0) & panel["support_ok"],
            num / den.replace(0, np.nan), np.nan,
        )
        thr = panel["emergence_score"].quantile(P["emergence_alert_pct"])
        panel["emergence_flag"] = (
            (panel["emergence_score"] >= thr) & (panel["surprise_q"] < P["fdr_alpha"])
        ).fillna(False)
        diag["emergence_threshold"] = None if not np.isfinite(thr) else float(thr)
        panel = panel.drop(columns=["_p_fdr"])

    ep_df = pd.DataFrame(episodes)
    return panel, ep_df, diag


# ==========================================================================
# novelty (embedding centroid drift)
# ==========================================================================
def make_novelty_fn(posts: pd.DataFrame, emb: Optional[dict], P: dict):
    """Returns fn(series_df, gid, keys) -> (novelty, day_n, base_n)."""
    if emb is None:
        return lambda s, gid, keys: (np.full(len(s), np.nan),) * 3
    idx = emb["index"]
    mat = emb["mat"]
    sub = posts[posts["post_uid"].isin(idx.keys())][["post_uid", "topic_id", "day"]]
    rows = np.array([idx[u] for u in sub["post_uid"]])
    by_topic_day: dict[tuple, list[int]] = {}
    for r, tp, d in zip(rows, sub["topic_id"], sub["day"]):
        by_topic_day.setdefault((tp, d), []).append(int(r))

    def fn(s: pd.DataFrame, gid: tuple, keys: list[str]):
        kv = dict(zip(keys, gid))
        topic = kv.get("topic_id")
        n = len(s)
        out = np.full(n, np.nan)
        day_n = np.zeros(n)
        base_n = np.zeros(n)
        if topic is None:
            return out, np.full(n, np.nan), np.full(n, np.nan)
        days = list(s["day"])
        for t, d in enumerate(days):
            cur = by_topic_day.get((topic, d), [])
            base: list[int] = []
            for dd in days[max(0, t - P["novelty_window"]): t]:  # EXCLUDES d
                base.extend(by_topic_day.get((topic, dd), []))
            day_n[t] = len(cur)
            base_n[t] = len(base)
            if len(cur) < P["novelty_min_day_posts"] or len(base) < P["novelty_min_base_posts"]:
                continue
            c1 = mat[cur].mean(axis=0)
            c0 = mat[base].mean(axis=0)
            n1, n0 = np.linalg.norm(c1), np.linalg.norm(c0)
            if n1 <= 0 or n0 <= 0:
                continue
            out[t] = float(1.0 - np.dot(c1, c0) / (n1 * n0))
        return out, day_n, base_n

    return fn


# ==========================================================================
# main
# ==========================================================================
def main() -> int:
    np.random.seed(PARAMS["seed"])
    P = PARAMS
    t0 = datetime.now(timezone.utc)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    log("running stats_v2 self-checks ...")
    checks = S.run_self_checks()
    failed = [k for k, v in checks.items() if not v]
    assert not failed, f"stats_v2 self-checks FAILED: {failed}"
    log(f"  {len(checks)}/{len(checks)} self-checks passed")

    src = load_source()
    posts_all = src["posts"]
    window_start = pd.to_datetime(P["window_start"]).date()
    window_end = (pd.to_datetime(P["window_end"]).date() if P["window_end"]
                  else max(posts_all["day"]))
    log(f"window {window_start} .. {window_end}")

    ts_posts = posts_all[~posts_all["platform"].isin(TS_PLATFORMS_EXCLUDE)].copy()
    log(f"posts total={len(posts_all):,} in TS scope={len(ts_posts):,}")

    effort, effort_diag = build_effort(ts_posts, src["effort_ce"])
    log(f"effort: {effort_diag['n_platform_days']} platform-days, "
        f"reported available on {effort_diag['n_days_with_reported_effort']}, "
        f"corr={effort_diag.get('corr_observed_vs_reported')}")

    posts = enrich_posts(ts_posts, P)
    tag_long, tag_cov = mine_tags(posts)
    log(f"hashtag coverage: {tag_cov['n_posts_with_any_tag']:,}/{tag_cov['n_posts']:,} "
        f"= {tag_cov['coverage_pct']}%  distinct={tag_cov['n_distinct_tags']:,}")

    platform_days = {p: set(g["day"]) for p, g in posts.groupby("platform")}
    platform_days["__any__"] = set(posts["day"])

    novelty_fn = make_novelty_fn(posts, src["emb"], P)

    # ---------------- grain 1: topic x platform x day ------------------
    log("grain 1/3: topic x platform x day")
    cells_tp = aggregate_cells(posts, ["topic_id", "platform", "day"])
    eb_tp = apply_eb(cells_tp, "topic_id", P)
    panel_tp, episodes, diag_tp = compute_panel(
        cells_tp, ["topic_id", "platform", "day"], effort, P,
        platform_days=platform_days, window_start=window_start,
        window_end=window_end, novelty_fn=novelty_fn)
    log(f"  metrics_daily rows={len(panel_tp):,} episodes={len(episodes):,} "
        f"burst={diag_tp['burst_methods']}")

    # ---------------- grain 2: topic x day (platform rolled up) --------
    log("grain 2/3: topic x day")
    cells_t = aggregate_cells(posts, ["topic_id", "day"])
    eb_t = apply_eb(cells_t, "topic_id", P)
    panel_t, episodes_t, diag_t = compute_panel(
        cells_t, ["topic_id", "day"], effort, P,
        platform_days=platform_days, window_start=window_start,
        window_end=window_end, novelty_fn=novelty_fn)
    log(f"  metrics_topic_day rows={len(panel_t):,} burst={diag_t['burst_methods']}")

    # ---------------- grain 3: tag x platform x day --------------------
    log("grain 3/3: tag x platform x day")
    tp = posts.merge(tag_long, on="post_uid", how="inner")
    tag_counts = tp.groupby("tag")["post_uid"].nunique()
    tag_first = tp.groupby("tag")["day"].min()
    tag_last = tp.groupby("tag")["day"].max()
    tag_plats = tp.groupby("tag")["platform"].apply(lambda s: ",".join(sorted(set(s))))
    tag_dim = pd.DataFrame({
        "tag": tag_counts.index, "n_posts": tag_counts.to_numpy(),
        "first_seen": tag_first.reindex(tag_counts.index).to_numpy(),
        "last_seen": tag_last.reindex(tag_counts.index).to_numpy(),
        "platforms": tag_plats.reindex(tag_counts.index).to_numpy(),
    })
    tag_dim = tag_dim[tag_dim["n_posts"] >= P["tag_min_posts_dim"]].copy()
    tag_dim["tag_id"] = [tag_id_of(t) for t in tag_dim["tag"]]
    tag_dim["in_ts_scope"] = tag_dim["n_posts"] >= P["tag_min_posts_ts"]
    tag_dim = tag_dim[["tag_id", "tag", "n_posts", "platforms", "first_seen",
                       "last_seen", "in_ts_scope"]].sort_values("n_posts", ascending=False)

    ts_tags = set(tag_dim.loc[tag_dim["in_ts_scope"], "tag"])
    tp_ts = tp[tp["tag"].isin(ts_tags)].copy()
    tp_ts["tag_id"] = [tag_id_of(t) for t in tp_ts["tag"]]
    cells_tag = aggregate_cells(tp_ts, ["tag_id", "platform", "day"])
    eb_tag = apply_eb(cells_tag, "tag_id", P)
    panel_tag, _, diag_tag = compute_panel(
        cells_tag, ["tag_id", "platform", "day"], effort, P,
        platform_days=platform_days, window_start=window_start,
        window_end=window_end, novelty_fn=None)
    log(f"  tag_dim={len(tag_dim):,} ts_tags={len(ts_tags):,} "
        f"tag_metrics_daily rows={len(panel_tag):,} burst={diag_tag['burst_methods']}")

    # ---------------- coordination flag --------------------------------
    for pnl in (panel_tp, panel_t, panel_tag):
        if len(pnl):
            pnl["coordination_flag"] = (
                (pnl["V"] >= P["coord_min_v"])
                & (pnl["author_1_hhi"] < P["coord_max_diversity"])
                & (pnl["duplicate_rate"] > P["coord_min_dup_rate"])
            ).fillna(False)
            pnl["hate_calibrated"] = P["hate_calibrated"]

    # ---------------- persistence (F7) --------------------------------
    # SPEC DEVIATION (documented, flagged for review): F7 writes the AR(1) on the
    # *differenced* log-rate. Differencing a stationary series induces phi < 0,
    # for which ln(0.5)/ln(phi) is undefined -- it yields a half-life on only
    # 4/39 series, reproducing exactly the coverage weakness F7 set out to fix.
    # The interpretable persistence (time for a shock to decay by half) is the
    # AR(1) on the log-rate *level*. The level fit is therefore published in the
    # contract columns; the differenced fit is kept as diff_* for auditability.
    log("persistence: AR(1) on the log-rate level (primary) + differenced (diff_*)")
    pers_rows = []
    for (topic, plat), g in panel_tp[panel_tp["in_window"]].groupby(["topic_id", "platform"]):
        g = g.sort_values("day")
        lr = g["log_rate"].dropna().to_numpy(dtype=float)
        ret = g["log_rate"].diff().dropna().to_numpy(dtype=float)
        lvl = S.fit_ar1(lr, min_obs=P["ar1_min_obs"], lb_lags=P["ljung_box_lags"])
        dif = S.fit_ar1(ret, min_obs=P["ar1_min_obs"], lb_lags=P["ljung_box_lags"])
        pers_rows.append({
            "topic_id": topic, "platform": plat,
            "ar1_phi": lvl.phi, "phi_se": lvl.phi_se,
            "half_life_days": lvl.half_life_days,
            # volatility is the SD of log-returns, per F7
            "volatility": dif.volatility,
            "ljung_box_q": lvl.ljung_box_q, "ljung_box_p": lvl.ljung_box_p,
            "n_obs": lvl.n_obs, "fit_ok": lvl.ok,
            "ar1_series": "log_rate_level",
            "diff_ar1_phi": dif.phi, "diff_phi_se": dif.phi_se,
            "diff_half_life_days": dif.half_life_days,
            "diff_ljung_box_q": dif.ljung_box_q, "diff_ljung_box_p": dif.ljung_box_p,
            "diff_n_obs": dif.n_obs,
            "mean_reverting": bool(np.isfinite(lvl.phi) and 0.0 < lvl.phi < 1.0),
            "total_V": float(g["V"].sum()),
            "support_n": float(g["V"].sum()),
        })
    persistence = pd.DataFrame(pers_rows)
    log(f"  persistence rows={len(persistence):,} "
        f"level_half_life={int(persistence['half_life_days'].notna().sum()) if len(persistence) else 0} "
        f"diff_half_life={int(persistence['diff_half_life_days'].notna().sum()) if len(persistence) else 0}")

    # ---------------- null control + sensitivity (F8, D4, D5) ----------
    log("null control (F8) + sensitivity self-test (D5)")
    null_ctrl = []
    big = (panel_tp[panel_tp["in_window"]].groupby(["topic_id", "platform"])["V"]
           .sum().sort_values(ascending=False).head(8))
    for (topic, plat) in big.index:
        g = panel_tp[(panel_tp["in_window"]) & (panel_tp["topic_id"] == topic)
                     & (panel_tp["platform"] == plat)].sort_values("day")
        r = S.permutation_burst_test(
            g["V"].to_numpy(dtype=float), n_perm=P["permutation_n"], seed=P["seed"],
            min_days_kleinberg=P["burst_min_days_kleinberg"],
            min_total_kleinberg=P["burst_min_total_kleinberg"],
            min_mean_kleinberg=P["burst_min_mean_kleinberg"],
            s=P["burst_s"], gamma=P["burst_gamma"], cusum_h=P["burst_cusum_h"])
        lb_q, lb_p = S.ljung_box(g["V"].to_numpy(dtype=float), lags=P["ljung_box_lags"])
        acf = []
        v = g["V"].to_numpy(dtype=float)
        vc = v - v.mean()
        den = float(vc @ vc)
        n = v.size
        for h in range(1, 8):
            r_h = float(vc[h:] @ vc[:-h] / den) if den > 0 and n > h else np.nan
            acf.append({"lag": h, "acf": r_h,
                        "ci95": 1.96 / math.sqrt(n) if n else np.nan,
                        "significant": bool(np.isfinite(r_h) and abs(r_h) > 1.96 / math.sqrt(n))})
        null_ctrl.append({"topic_id": topic, "platform": plat, "n_days": int(n),
                          "total_V": float(v.sum()), **r,
                          "ljung_box_q": lb_q, "ljung_box_p": lb_p, "acf": acf})

    # D5 fix: the harness must reproduce corr(default, default) == 1.0 using the
    # *same* production function, before any other window comparison is trusted.
    sens = []
    ref_cols = ["momentum", "accel_smooth", "surprise"]

    def _variant(name: str, over: dict):
        Pv = {**P, **over}
        pv, _, _ = compute_panel(
            aggregate_cells(posts, ["topic_id", "platform", "day"]),
            ["topic_id", "platform", "day"], effort, Pv,
            platform_days=platform_days, window_start=window_start,
            window_end=window_end, novelty_fn=novelty_fn)
        merged = panel_tp.merge(pv, on=["topic_id", "platform", "day"],
                                suffixes=("_a", "_b"))
        row = {"variant": name, "params": json.dumps(over), "n_cells": int(len(merged))}
        for c in ref_cols:
            x, y = merged[f"{c}_a"], merged[f"{c}_b"]
            m = x.notna() & y.notna()
            row[f"corr_{c}"] = (float(np.corrcoef(x[m], y[m])[0, 1])
                                if m.sum() > 2 and x[m].std() > 0 and y[m].std() > 0 else np.nan)
            row[f"n_{c}"] = int(m.sum())
        sens.append(row)
        return row

    selftest = _variant("SELFTEST_default_vs_default", {})
    for c in ref_cols:
        v = selftest[f"corr_{c}"]
        assert np.isfinite(v) and abs(v - 1.0) < 1e-9, (
            f"D5 REGRESSION: corr(default,default) for {c} = {v}, must be exactly 1.0")
    log(f"  D5 self-test PASSED: corr(default,default)=1.0 for {ref_cols}")
    _variant("ema_5_14", {"ema_fast": 5, "ema_slow": 14})
    _variant("ema_10_28", {"ema_fast": 10, "ema_slow": 28})
    _variant("baseline_21", {"baseline_window": 21})
    _variant("baseline_35", {"baseline_window": 35})
    _variant("savgol_7", {"savgol_window": 7})
    _variant("fdr_by", {"fdr_method": "by"})

    # ---------------- alert candidates --------------------------------
    log("alert candidates")
    al = []
    for pnl, gk in ((panel_tp, "topic_platform"), (panel_tag, "tag_platform")):
        if not len(pnl):
            continue
        d = pnl[pnl["alert_flag"] | pnl["emergence_flag"]]
        for _, r in d.iterrows():
            for mk, val, why in (
                ("surprise", r.get("surprise"),
                 f"V={r['V']:.0f} vs {r['baseline_dist']} baseline mu="
                 f"{r['baseline_mu']:.2f} over {int(r['baseline_days'])}d"),
                ("emergence_score", r.get("emergence_score"),
                 f"accel_smooth={r.get('accel_smooth')}, novelty={r.get('novelty')}, "
                 f"days_to_threshold={r.get('days_to_threshold')}"),
            ):
                if not np.isfinite(val if val is not None else np.nan):
                    continue
                if mk == "surprise" and not r["alert_flag"]:
                    continue
                if mk == "emergence_score" and not r["emergence_flag"]:
                    continue
                al.append({
                    "day": r["day"],
                    "topic_id": r.get("topic_id"),
                    "platform": r.get("platform"),
                    "tag_id": r.get("tag_id"),
                    "metric_key": mk, "value": float(val),
                    "surprise_q": (float(r["surprise_q"]) if pd.notna(r["surprise_q"]) else None),
                    "severity": ("high" if pd.notna(r["surprise_q"]) and r["surprise_q"] < 0.01
                                 else "medium"),
                    "rationale": why, "grain": gk, "support_n": float(r["support_n"]),
                })
    alerts = pd.DataFrame(al)
    log(f"  alert_candidates={len(alerts):,}")

    # ---------------- dimensions --------------------------------------
    td = src["topic_dim"].copy()
    n_by_topic = posts_all.groupby("topic_id")["post_uid"].count()
    topic_dim = pd.DataFrame({
        "topic_id": td["topic_id"],
        "domain": td["domain"], "topic": td["topic"],
        "label": [f"{d} / {t}" for d, t in zip(td["domain"], td["topic"])],
        "n_posts": [int(n_by_topic.get(t, 0)) for t in td["topic_id"]],
        "description": [
            (s[:300] if isinstance(s, str) and s else "no top terms (residual cluster)")
            for s in td.get("top_terms", pd.Series([""] * len(td)))
        ],
        "cluster_size": td["size"],
    })
    plat_stats = posts_all.groupby("platform").agg(
        n_posts=("post_uid", "count"),
        has_eng=("has_engagement", "mean"),
        scrape_proxy_day_rate=("day_is_scrape_proxy", "mean"),
        first_day=("day", "min"), last_day=("day", "max")).reset_index()
    emb_uids = set(src["emb"]["index"]) if src["emb"] else set()
    emb_by_plat = (posts_all.assign(_e=posts_all["post_uid"].isin(emb_uids))
                   .groupby("platform")["_e"].mean())
    scrape_proxy_map = dict(zip(plat_stats["platform"], plat_stats["scrape_proxy_day_rate"]))

    def _platform_note(p: str) -> str:
        if p in TS_PLATFORMS_EXCLUDE:
            return "excluded from time series (coverage gap)"
        if p in NO_ENGAGEMENT_PLATFORMS:
            return "no engagement fields"
        if p == "youtube_video":
            cov = float(emb_by_plat.get(p, 0.0))
            return ("historical backfill to 2011; no embeddings" if cov < 0.5
                    else "historical backfill to 2011")
        if p == "telegram":
            return "dominant source; views+forwards only"
        proxy_rate = scrape_proxy_map.get(p, 0.0)
        if proxy_rate >= 0.99:
            return f"no posted_at ({proxy_rate:.0%} of rows); day axis is scraped_at proxy, support-gated"
        if proxy_rate > 0:
            return f"{proxy_rate:.0%} of rows use scraped_at proxy day"
        return ""

    platform_dim = pd.DataFrame({
        "platform": plat_stats["platform"],
        "has_engagement": [(p not in NO_ENGAGEMENT_PLATFORMS) and h > 0
                           for p, h in zip(plat_stats["platform"], plat_stats["has_eng"])],
        "in_ts_scope": [p not in TS_PLATFORMS_EXCLUDE for p in plat_stats["platform"]],
        "n_posts": plat_stats["n_posts"],
        "first_day": plat_stats["first_day"], "last_day": plat_stats["last_day"],
        "embedding_coverage": [float(emb_by_plat.get(p, 0.0)) for p in plat_stats["platform"]],
        "notes": [_platform_note(p) for p in plat_stats["platform"]],
    })

    # ---------------- D1 before/after evidence ------------------------
    v1 = src["v1"]
    d1 = {}
    if len(v1):
        v1z = v1["intensity_z"].abs()
        d1["v1"] = {
            "n_cells": int(len(v1)),
            "n_cells_V_le_2": int((v1["V"] <= 2).sum()),
            "pct_cells_V_le_2": round(100.0 * float((v1["V"] <= 2).mean()), 2),
            "max_abs_z": round(float(v1z.max()), 2),
            "max_abs_z_on_V_le_2": round(float(v1z[v1["V"] <= 2].max()), 2),
            "max_abs_z_on_V_eq_0": round(float(v1z[v1["V"] == 0].max()), 2),
            "n_V_le_2_with_abs_z_gt_5": int(((v1["V"] <= 2) & (v1z > 5)).sum()),
            "n_abs_z_gt_5": int((v1z > 5).sum()),
            "n_extreme_metric_on_zero_volume": int(((v1["V"] == 0) & v1z.notna()).sum()),
            "n_out_of_window_cells": int((pd.to_datetime(v1["posted_day"]).dt.date
                                          < window_start).sum()),
        }
    sp = panel_tp["surprise"]
    d1["v2"] = {
        "n_cells": int(len(panel_tp)),
        "n_cells_in_window": int(panel_tp["in_window"].sum()),
        "n_cells_V_le_2": int((panel_tp["V"] <= 2).sum()),
        "n_nonnull_surprise": int(sp.notna().sum()),
        "n_nonnull_surprise_on_V_le_2": int(sp[panel_tp["V"] <= 2].notna().sum()),
        "n_nonnull_surprise_on_V_eq_0": int(sp[panel_tp["V"] == 0].notna().sum()),
        "max_surprise": round(float(sp.max()), 3) if sp.notna().any() else None,
        "max_surprise_implied_p": (float(10 ** -sp.max()) if sp.notna().any() else None),
        "min_V_with_nonnull_surprise": (int(panel_tp.loc[sp.notna(), "V"].min())
                                        if sp.notna().any() else None),
        "support_gate": (f"baseline_days>={P['baseline_min_days']} & "
                         f"baseline_mean>={P['baseline_min_mean']} & "
                         f"V>={P['support_min_v']}"),
    }

    # ---------------- run_meta ----------------------------------------
    run_id = f"{t0.strftime('%Y%m%dT%H%M%SZ')}_metrics_v2"
    params_json = {
        "params": {k: v for k, v in P.items()},
        "burst_method_counts": {
            "topic_platform": diag_tp["burst_methods"],
            "topic": diag_t["burst_methods"],
            "tag_platform": diag_tag["burst_methods"],
        },
        "burst_method_rule": (
            f"Kleinberg 2-state HMM when n_days>={P['burst_min_days_kleinberg']} and "
            f"total_V>={P['burst_min_total_kleinberg']} and mean>={P['burst_min_mean_kleinberg']}; "
            "otherwise Poisson CUSUM (Lucas 1985) with trailing, current-day-excluded "
            f"baseline and h={P['burst_cusum_h']}. Per-row method in burst_method."
        ),
        "hashtag_coverage": tag_cov,
        "effort": effort_diag,
        "effort_definition": (
            "observed distinct active channels/authors per platform-day; "
            "collection_effort.active_channels retained only as a cross-check "
            "(scrape-day grain, 8/45 telegram coverage, zeros on 5 of 8 days)"
        ),
        "eb_diagnostics": {"topic_platform": eb_tp, "topic": eb_t, "tag_platform": eb_tag},
        "self_checks": checks,
        "sensitivity": sens,
        "null_control": null_ctrl,
        "d1_before_after": d1,
        "window": {"start": str(window_start), "end": str(window_end)},
        "fdr": {"method": P["fdr_method"], "alpha": P["fdr_alpha"],
                "scope": P["fdr_scope"], "pvalue_kind": P["fdr_pvalue_kind"]},
        "hate_calibrated": P["hate_calibrated"],
        "hate_caveat": ("uncalibrated: a war/terror corpus over-fires toxicity models; "
                        "reports/calibration_sample.csv is not yet hand-labelled"),
    }
    run_meta = pd.DataFrame([{
        "run_id": run_id,
        "created_at": t0,
        "source_extract_id": (str(src["extract"]["extract_id"].iloc[0])
                              if len(src["extract"]) else None),
        "window_start": window_start, "window_end": window_end,
        "params_json": json.dumps(params_json, default=str, sort_keys=True),
        "n_source_posts": int(len(posts_all)),
        "n_ts_posts": int(len(ts_posts)),
        "code_version": "metrics_v2",
    }])

    # ---------------- assertions before publish -----------------------
    log("post-build assertions")
    assert int(panel_tp.loc[panel_tp["in_window"], "V"].sum()) == int(
        posts[posts["day"].between(window_start, window_end)].shape[0]
    ), "in-window V does not reconcile with post count (double counting?)"
    dup = panel_tp.duplicated(subset=["topic_id", "platform", "day"]).sum()
    assert dup == 0, f"metrics_daily has {dup} duplicate grain keys"
    bad = panel_tp[(~panel_tp["support_ok"]) & panel_tp["surprise"].notna()]
    assert len(bad) == 0, f"{len(bad)} rows publish surprise without support"
    assert not panel_tp["hate_calibrated"].any(), "hate must stay uncalibrated"
    assert panel_tp.loc[panel_tp["V"] == 0, "surprise"].notna().sum() == 0, \
        "D1 regression: zero-volume cell carries a surprise value"
    log("  all assertions passed")

    # ---------------- rename to the schema contract -------------------
    # column order follows the section-5 contract; provenance extras trail it
    SPEC_ORDER = [
        "day", "topic_id", "tag_id", "platform", "in_window",
        "V", "V_rate", "effort",
        "growth", "growth_share", "growth_7d",
        "accel", "accel_smooth", "momentum",
        "surprise", "surprise_p", "surprise_q", "alert_flag",
        "burst_state", "burst_intensity", "burst_episode_id",
        "novelty", "days_to_threshold", "emergence_score",
        "tone_mean", "tone_eb", "tone_n",
        "hate_mean", "hate_eb", "hate_prevalence", "hate_calibrated", "hate_n",
        "virality_pct", "reach_proxy", "reach_audience_norm",
        "author_entropy", "author_1_hhi", "n_authors", "coordination_flag",
        "readability_mean", "readability_coverage",
        "support_n", "support_ok",
    ]

    def finalise(df: pd.DataFrame) -> pd.DataFrame:
        if not len(df):
            return df
        df = df.drop(columns=[c for c in ("tone_sum", "hate_sum", "hate_hits", "read_sum",
                                          "read_cov", "vir_sum", "vir_n", "tone_ss",
                                          "hate_ss", "vir_ss", "reach_norm_sum",
                                          "reach_norm_n") if c in df.columns])
        head = [c for c in SPEC_ORDER if c in df.columns]
        return df[head + [c for c in df.columns if c not in head]]

    outputs = {
        "metrics_daily": finalise(panel_tp),
        "metrics_topic_day": finalise(panel_t),
        "tag_metrics_daily": finalise(panel_tag),
        "burst_episodes": episodes,
        "persistence": persistence,
        "alert_candidates": alerts,
        "topic_dim": topic_dim,
        "tag_dim": tag_dim,
        "platform_dim": platform_dim,
        "run_meta": run_meta,
        "sensitivity_checks": pd.DataFrame(sens),
        "null_control": pd.DataFrame([{k: v for k, v in r.items() if k != "acf"}
                                      for r in null_ctrl]),
    }
    for name, df in outputs.items():
        path = STAGE_DIR / f"{name}.parquet"
        (df if len(df) else pd.DataFrame({"_empty": []})).to_parquet(path, index=False)
        log(f"  staged {name:22s} rows={len(df):>7,}  -> {path.name}")
    (STAGE_DIR / "run_params.json").write_text(json.dumps(params_json, default=str, indent=2))
    log(f"done in {(datetime.now(timezone.utc) - t0).total_seconds():.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
