#!/usr/bin/env python3
"""
Statistical review harness for the Layer-4 metrics panel.

Reproduces every number quoted in:
  docs/STATISTICAL_REVIEW.md
  docs/STATISTICAL_APPENDIX.md

READ-ONLY against data/analytics.duckdb. Writes nothing except an optional
JSON dump of results (--json PATH). Deterministic: all RNG seeded.

Usage
-----
    source .venv/bin/activate
    python scripts/review/stat_review.py                 # all sections
    python scripts/review/stat_review.py --only D1 D5 B  # subset
    python scripts/review/stat_review.py --json out.json

Sections
--------
  D1  sparse robust-z blowup + the silent z=0 masking bug
  D2  panel sparsity, effective sample size
  D3  window pollution / zero-fill baseline contamination
  D4  corrected null control: Ljung-Box, ACF+CI, burst permutation test
  D5  sensitivity-harness bug: root cause + corrected numbers
  B   distributional assumptions: Poisson vs NB, ZI, transforms, EB shrinkage
  C   acceleration: noise amplification, smoother selection, detector bake-off
  D   multiple testing: effective tests, FDR method, severity bands
  E   defect hunt not covered by METRICS_V2_SPEC.md
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats
from scipy.signal import savgol_filter
from scipy.stats import nbinom
from sklearn.metrics import roc_auc_score
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.stattools import acf

import statsmodels.api as sm

warnings.filterwarnings("ignore")

# ----------------------------------------------------------------------------
# Constants — every tunable used anywhere in the review lives here.
# ----------------------------------------------------------------------------
EDA_ROOT = Path(__file__).resolve().parents[2]
DB = EDA_ROOT / "data" / "analytics.duckdb"

SEED = 20260729                 # master seed; every RNG derives from it
WINDOW_START = "2026-06-14"     # start of the live collection window (F4)
# 2026-07-29: dropped the blanket snapchat/pinterest/whatsapp exclusion to match
# the same fix applied to lib/config.py's TS_EXCLUDE_PLATFORMS -- no code here
# should decide in advance which platforms exist either. Sparse platforms flow
# through this review like any other and their own support gates NaN out
# whatever they can't support.

V1_ALPHA = 1.0                  # 05_metrics.py ALPHA
V1_EMA_FAST, V1_EMA_SLOW = 7, 21
V1_BASELINE_DAYS = 28

# support gates used when re-deriving metrics the way the review recommends
MIN_SERIES_LEN = 20             # in-window days required to model a cell
MIN_SERIES_VOL = 20             # in-window posts required to model a cell
BASELINE_WIN = 21               # recommended trailing baseline (days)
SCAN_L = 3                      # recommended scan-statistic window (days)

N_SHUFFLE_NULL = 200            # null-control shuffles
N_PERM_BURST = 2000             # burst permutation replicates
N_INJECT = 400                  # synthetic-burst injection replicates

pd.set_option("display.width", 260)
pd.set_option("display.max_columns", 80)

RESULTS: dict = {}


def rec(section: str, key: str, value):
    """Record a result for the JSON dump and echo it."""
    RESULTS.setdefault(section, {})[key] = value
    return value


def hdr(t: str):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def sub(t: str):
    print("\n--- " + t + " ---")


def jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return str(o)
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, pd.DataFrame):
        return jsonable(o.to_dict(orient="records"))
    if isinstance(o, pd.Series):
        return jsonable(o.to_dict())
    return o


# ============================================================================
# Data loading
# ============================================================================
def load():
    if not DB.exists():
        sys.exit(f"ERROR: {DB} not found")
    con = duckdb.connect(str(DB), read_only=True)

    md = con.execute(
        "SELECT * FROM metrics_daily ORDER BY topic_id, platform, posted_day"
    ).df()
    md["posted_day"] = pd.to_datetime(md["posted_day"])
    w0 = pd.Timestamp(WINDOW_START, tz="UTC")
    md["in_window"] = md["posted_day"] >= w0

    posts = con.execute(
        f"""
        SELECT f.post_uid, f.platform, f.posted_at, f.scraped_at, f.scrape_lag_days,
               f.author_uid, f.likes, f.comments, f.shares, f.views, f.has_engagement,
               t.topic_id, s.tone_score, s.hate_score, s.readability_score
        FROM post_facts f
        LEFT JOIN post_topic  t USING(post_uid)
        LEFT JOIN post_scores s USING(post_uid)
        """
    ).df()
    posts["posted_at"] = pd.to_datetime(posts["posted_at"], utc=True)
    posts["scraped_at"] = pd.to_datetime(posts["scraped_at"], utc=True)
    # Same fallback as 07_metrics_v2.py: some platforms (pinterest) have no
    # posted_at at all -- fall back to scraped_at rather than silently vanishing.
    day_ts = posts["posted_at"].fillna(posts["scraped_at"])
    posts["day"] = day_ts.dt.floor("D")
    posts["topic_id"] = posts["topic_id"].fillna("unassigned")
    posts = posts.dropna(subset=["day"])

    return con, md, posts, w0


def cell_series(md: pd.DataFrame) -> dict:
    """In-window daily V per (topic, platform), gated to modellable cells."""
    iw = md[md.in_window]
    out = {}
    for key, g in iw.groupby(["topic_id", "platform"]):
        g = g.sort_values("posted_day")
        if len(g) >= MIN_SERIES_LEN and g.V.sum() >= MIN_SERIES_VOL:
            out[key] = g.V.fillna(0).to_numpy(float)
    return out


def wide_panel(md: pd.DataFrame):
    """(day x cell) volume matrix restricted to modellable cells."""
    iw = md[md.in_window]
    piv = iw.pivot_table(
        index="posted_day", columns=["topic_id", "platform"], values="V", aggfunc="sum"
    ).fillna(0)
    keep = [
        c for c in piv.columns
        if piv[c].sum() >= MIN_SERIES_VOL and (piv[c] > 0).sum() >= 10
    ]
    return piv, keep


# ============================================================================
# v1 primitives, copied verbatim from scripts/05_metrics.py so the review
# reproduces production exactly rather than approximating it.
# ============================================================================
def v1_robust_z(series: pd.Series, baseline: pd.Series) -> float:
    med = baseline.median()
    mad = (baseline - med).abs().median()
    if pd.isna(mad) or mad == 0:
        mad = baseline.std()
        if pd.isna(mad) or mad == 0:
            return 0.0                      # <-- the silent masking fallback
        mad = mad * 0.6745
    x = series.iloc[-1] if len(series) else np.nan
    if pd.isna(x) or pd.isna(med):
        return np.nan
    return float((x - med) / (1.4826 * mad + 1e-9))


def fixed_robust_z(x: float, baseline: pd.Series, min_n: int = 10) -> float:
    """Support-gated, NaN-on-degenerate replacement for v1_robust_z."""
    b = baseline.dropna()
    if len(b) < min_n or pd.isna(x):
        return np.nan
    med = b.median()
    mad = (b - med).abs().median()
    if mad <= 0:
        return np.nan                       # refuse rather than fabricate
    return float((x - med) / (1.4826 * mad))


def mom_theta(h: np.ndarray) -> float:
    """Method-of-moments NB2 theta on a baseline window (Poisson limit = 1e6)."""
    mu = float(np.mean(h))
    v = float(np.var(h, ddof=1)) if len(h) > 1 else 0.0
    return mu * mu / max(v - mu, 1e-9) if v > mu * 1.0001 else 1e6


def nb_surprise_p(hist: np.ndarray, obs: float, theta_floor=0.0, midp=True) -> float:
    mu = float(np.mean(hist))
    if mu <= 0:
        return np.nan
    th = max(min(mom_theta(hist), 1e6), theta_floor) if theta_floor else min(
        mom_theta(hist), 1e6
    )
    p = th / (th + mu)
    up = nbinom.sf(obs - 1, th, p)
    if midp:
        up = up - 0.5 * nbinom.pmf(obs, th, p)
    return float(max(min(up, 1.0), 1e-300))


def nb_scan_p(y, off, i, win=BASELINE_WIN, L=SCAN_L, midp=True) -> float:
    """
    L-day NB scan statistic with an exposure offset.
    Baseline = the `win` days strictly BEFORE the scan window (no self-inclusion).
    """
    lo = i - L + 1 - win
    if lo < 0:
        return np.nan
    h = np.asarray(y[lo: i - L + 1], float)
    ho = np.asarray(off[lo: i - L + 1], float)
    if h.sum() <= 0 or ho.sum() <= 0:
        return np.nan
    rate = h.sum() / ho.sum()
    mu = rate * np.asarray(off[i - L + 1: i + 1], float).sum()
    if mu <= 0:
        return np.nan
    th = max(mom_theta(h), 1e-9)
    thL = L * th
    obs = float(np.asarray(y[i - L + 1: i + 1]).sum())
    p = nbinom.sf(obs - 1, thL, thL / (thL + mu))
    if midp:
        p -= 0.5 * nbinom.pmf(obs, thL, thL / (thL + mu))
    return float(max(min(p, 1.0), 1e-300))


# ============================================================================
# D1 — robust-z blowup and the silent z = 0 masking bug
# ============================================================================
def section_D1(md):
    hdr("D1 — robust-z on sparse cells")

    md = md.copy()
    md["absz"] = md.intensity_z.abs()
    bins = pd.cut(md.V, [-1, 0, 2, 4, 9, 10 ** 9], labels=["0", "1-2", "3-4", "5-9", "10+"])
    tab = md.groupby(bins, observed=True).agg(
        n=("V", "size"),
        z_nonnull=("intensity_z", "count"),
        max_abs_z=("absz", "max"),
        p99_abs_z=("absz", lambda s: s.quantile(0.99)),
        frac_abs_gt5=("absz", lambda s: float((s > 5).mean())),
        exact_zero=("intensity_z", lambda s: int((s == 0).sum())),
    )
    print(tab.round(4).to_string())
    rec("D1", "by_V_bucket", tab.reset_index().rename(columns={"V": "V_bucket"}))

    ext = {c: float(np.nanmax(np.abs(md[c]))) for c in
           ["intensity_z", "tone_z", "hate_z", "virality_z"]}
    print("\nmax |z| per metric:", {k: f"{v:.4g}" for k, v in ext.items()})
    rec("D1", "max_abs_z", ext)

    nn = int(md.intensity_z.notna().sum())
    z0 = int((md.intensity_z == 0).sum())
    print(f"\nintensity_z exactly 0.0: {z0} of {nn} non-null ({100*z0/nn:.1f}%)")
    print("  -> robust_z() returns 0.0 when baseline MAD==0 AND baseline std==0.")
    print("     A cell going 0,0,0,0,0 -> 50 posts is reported as z = 0.0.")
    rec("D1", "intensity_z_exact_zero", {"n_zero": z0, "n_nonnull": nn,
                                         "frac": z0 / nn})

    for k in (50, 100):
        top = md.dropna(subset=["intensity_z"]).nlargest(k, "intensity_z")
        d = {"n": k, "V_le_2": int((top.V <= 2).sum()), "V_lt_5": int((top.V < 5).sum()),
             "out_of_window": int((~top.in_window).sum())}
        print(f"top-{k} by intensity_z: V<=2 in {d['V_le_2']}, V<5 in {d['V_lt_5']}, "
              f"outside collection window in {d['out_of_window']}")
        rec("D1", f"top{k}_artifact_share", d)

    sub("what actually drives intensity_z (component variance)")
    iw = md[md.in_window]
    a = 0.5 * np.log1p(iw.V)
    b = 0.5 * np.log1p(iw.E_sum.fillna(0))
    share_e = float(b.var() / (a.var() + b.var() + 2 * a.cov(b)))
    print(f"0.5*log1p(V): sd={a.std():.3f}   0.5*log1p(E): sd={b.std():.3f}"
          f"   -> E term supplies {100*share_e:.0f}% of intensity variance")
    print(iw.groupby("platform").agg(intensity_raw=("intensity_raw", "mean"),
                                     E_sum=("E_sum", "mean"), V=("V", "mean")).round(2).to_string())
    rec("D1", "intensity_E_variance_share", share_e)

    sub("fixed robust-z (support gate n>=10, NaN on degenerate MAD)")
    n_v1 = n_fix = 0
    max_fix = 0.0
    for _, g in md.groupby(["topic_id", "platform"]):
        g = g.sort_values("posted_day").reset_index(drop=True)
        ir = g.intensity_raw
        for i in range(len(g)):
            base = ir.iloc[max(0, i - V1_BASELINE_DAYS): i]
            if len(base) >= 5:
                n_v1 += 1
            z = fixed_robust_z(ir.iloc[i], base, min_n=10)
            if np.isfinite(z):
                n_fix += 1
                max_fix = max(max_fix, abs(z))
    print(f"v1 emits {n_v1} intensity_z values (max |z| = {ext['intensity_z']:.1f});"
          f" gated version emits {n_fix} (max |z| = {max_fix:.2f})")
    rec("D1", "gated_z", {"n_v1": n_v1, "n_fixed": n_fix, "max_abs_fixed": max_fix})


# ============================================================================
# D2 — sparsity and effective sample size
# ============================================================================
def section_D2(md):
    hdr("D2 — panel sparsity")

    n = len(md)
    d = {
        "n_cells": n,
        "V_le_2": int((md.V <= 2).sum()),
        "V_le_2_pct": float(100 * (md.V <= 2).mean()),
        "V_eq_0": int((md.V == 0).sum()),
        "V_ge_5": int((md.V >= 5).sum()),
        "V_ge_5_in_window": int((md.in_window & (md.V >= 5)).sum()),
        "n_in_window": int(md.in_window.sum()),
        "n_pre_2026_06": int((md.posted_day < pd.Timestamp("2026-06-01", tz="UTC")).sum()),
        "distinct_days": int(md.posted_day.nunique()),
        "V_total": float(md.V.sum()),
        "posts_per_cell": float(md.V.sum() / n),
    }
    w = md.V.to_numpy(float)
    d["kish_n_eff"] = float(w.sum() ** 2 / (w ** 2).sum())
    d["n_modellable_cells"] = len(cell_series(md))
    for k, v in d.items():
        print(f"  {k:26s} {v}")
    print(f"\n  Kish effective sample size on volume weights: {d['kish_n_eff']:.1f}")
    print(f"  -> {n} rows carry the information of ~{d['kish_n_eff']:.0f} equally-weighted cells")
    rec("D2", "panel", d)

    by = md.groupby("platform").agg(
        cells=("V", "size"), totV=("V", "sum"), in_window=("in_window", "sum"),
        first=("posted_day", "min"), last=("posted_day", "max"))
    print("\n" + by.to_string())
    rec("D2", "by_platform", by.reset_index())

    split = md.groupby("in_window").V.agg(["size", "sum"])
    print("\ncells and posts by window:\n" + split.to_string())
    outp = split.loc[False]
    print(f"\n  {outp['size']}/{n} cells ({100*outp['size']/n:.1f}%) hold "
          f"{outp['sum']:.0f}/{md.V.sum():.0f} posts ({100*outp['sum']/md.V.sum():.1f}%)")
    rec("D2", "window_split", split.reset_index())


# ============================================================================
# D3 — window pollution / baseline contamination
# ============================================================================
def section_D3(md):
    hdr("D3 — window pollution and baseline contamination")

    pre = md[md.posted_day < pd.Timestamp("2026-06-01", tz="UTC")]
    t = pre.groupby("platform").V.agg(["size", "sum"])
    print("pre-2026-06 cells and the posts they contain:\n" + t.to_string())
    print(f"\ntotal pre-2026-06 cells: {len(pre)}  posts: {pre.V.sum():.0f}")
    rec("D3", "pre_window_by_platform", t.reset_index())

    sub("trailing-28d baseline composition for in-window cells")
    recs = []
    for (tp, pl), g in md.groupby(["topic_id", "platform"]):
        g = g.sort_values("posted_day").reset_index(drop=True)
        ir = g.intensity_raw
        for i in range(len(g)):
            if not g.in_window.iloc[i]:
                continue
            s = max(0, i - V1_BASELINE_DAYS)
            b_all = ir.iloc[s:i]
            mask = g.in_window.iloc[s:i]
            b_win = b_all[mask]
            recs.append(dict(
                topic=tp, plat=pl, day=g.posted_day.iloc[i], V=g.V.iloc[i],
                n_base=len(b_all),
                frac_pre=float((~mask).mean()) if len(b_all) else np.nan,
                frac_zero=float((b_all == 0).mean()) if len(b_all) else np.nan,
                base_med_all=b_all.median(), base_med_win=b_win.median(),
                z_all=v1_robust_z(ir.iloc[s:i + 1], b_all) if len(b_all) >= 5 else np.nan,
                z_win=v1_robust_z(ir.iloc[s:i + 1], b_win) if len(b_win) >= 5 else np.nan,
                z_v1=g.intensity_z.iloc[i]))
    D = pd.DataFrame(recs)
    ok = bool(np.allclose(D.z_all.fillna(-999), D.z_v1.fillna(-999)))
    print(f"self-test: recomputed z reproduces metrics_daily.intensity_z exactly -> {ok}")
    sb = D.dropna(subset=["z_all", "z_win"])
    d = dict(
        n_in_window=len(D),
        mean_frac_baseline_pre_window=float(D.frac_pre.mean()),
        cells_ge50pct_pre=int((D.frac_pre >= 0.5).sum()),
        cells_100pct_pre=int((D.frac_pre >= 1.0).sum()),
        mean_frac_baseline_zero_filled=float(D.frac_zero.mean()),
        median_baseline_median_all=float(sb.base_med_all.median()),
        median_baseline_median_window_only=float(sb.base_med_win.median()),
        corr_z_all_vs_z_window_only=float(sb.z_all.corr(sb.z_win)),
        n_comparable=len(sb),
        z_gt3_lost_when_window_restricted=int(((sb.z_all > 3) & (sb.z_win < 3)).sum()),
        z_gt3_gained_when_window_restricted=int(((sb.z_all < 3) & (sb.z_win > 3)).sum()),
        sign_flips_absz_gt1=int(((np.sign(sb.z_all) != np.sign(sb.z_win)) & (sb.z_all.abs() > 1)).sum()),
        recompute_matches_v1=ok)
    for k, v in d.items():
        print(f"  {k:42s} {v}")
    print("\n  Mechanism: reindexing to a continuous daily range zero-fills gaps, so")
    print("  the median trailing baseline median is 0.0 (in-window-only: "
          f"{d['median_baseline_median_window_only']:.4f}). MAD -> 0 -> D1.")
    rec("D3", "baseline_contamination", d)


# ============================================================================
# D4 — corrected null control
# ============================================================================
def section_D4(md):
    hdr("D4 — null control, done properly")
    rng = np.random.default_rng(SEED)
    S = cell_series(md)
    print(f"modellable in-window cells: {len(S)}")

    def mabs(v):
        v = np.asarray(v, float)
        return np.nanmean(np.abs(np.log((v[1:] + 1) / (v[:-1] + 1)))) if len(v) > 1 else np.nan

    sub("v1's comparison is not apples-to-apples")
    g_all = md.growth.replace([np.inf, -np.inf], np.nan)
    iw = md[md.in_window]
    v1 = dict(
        v1_reported_real=float(g_all.abs().mean()),
        v1_reported_shuffled=0.7679872972180027,       # from reports/VALIDATION.md
        pooled_all_rows=float(g_all.abs().mean()),
        pooled_in_window=float(iw.growth.replace([np.inf, -np.inf], np.nan).abs().mean()),
        pooled_out_of_window=float(md[~md.in_window].growth.replace(
            [np.inf, -np.inf], np.nan).abs().mean()),
        frac_growth_exactly_zero=float((g_all == 0).mean()),
        frac_growth_exactly_zero_out_of_window=float((g_all[~md.in_window] == 0).mean()),
        n_groups_in_shuffled_average=int(md.groupby(["topic_id", "platform"]).ngroups))
    for k, v in v1.items():
        print(f"  {k:42s} {v}")
    print("\n  v1 numerator  = volume-weighted pooled mean over ALL 7,340 rows")
    print("  v1 denominator= UNWEIGHTED mean of per-group means (append is inside the")
    print("                  group loop in 06_validate.py:39) over 5 shuffles.")
    print("  Two mismatches (window + weighting) on top of the inverted criterion.")
    rec("D4", "v1_comparison_defects", v1)

    sub(f"apples-to-apples: unweighted per-group means, in-window, {N_SHUFFLE_NULL} shuffles")
    real = np.array([mabs(v) for v in S.values()])
    shuf = np.zeros((N_SHUFFLE_NULL, len(S)))
    for i in range(N_SHUFFLE_NULL):
        for j, v in enumerate(S.values()):
            shuf[i, j] = mabs(rng.permutation(v))
    per_shuffle = shuf.mean(1)
    d = dict(
        real_mean_abs_dlog=float(real.mean()),
        shuffled_mean_abs_dlog=float(per_shuffle.mean()),
        shuffled_sd=float(per_shuffle.std()),
        shuffled_ci95=[float(x) for x in np.percentile(per_shuffle, [2.5, 97.5])],
        ratio_shuffled_over_real=float(per_shuffle.mean() / real.mean()),
        permutation_p_shuffled_le_real=float((per_shuffle <= real.mean()).mean()),
        series_with_shuffled_gt_real=int((shuf.mean(0) > real).sum()),
        n_series=len(S))
    for k, v in d.items():
        print(f"  {k:34s} {v}")
    print("\n  VERDICT: shuffling RAISES |Delta log V|. That is the correct signature of")
    print("  positive autocorrelation. The v1 pass criterion was inverted.")
    rec("D4", "corrected_null_control", d)

    sub("Ljung-Box + lag 1..7 ACF with +-1.96/sqrt(n) bands, log1p(V)")
    rows = []
    for (tp, pl), v in S.items():
        x = np.log1p(v)
        if np.std(x) == 0:
            continue
        lb = acorr_ljungbox(x, lags=[1, 3, 7], return_df=True)
        a = acf(x, nlags=7, fft=False)
        se = 1 / np.sqrt(len(x))
        rows.append(dict(topic=tp, plat=pl, n=len(x), totV=int(v.sum()),
                         **{f"acf{l}": float(a[l]) for l in range(1, 8)},
                         ci95=float(1.96 * se),
                         LB1=float(lb.loc[1, "lb_stat"]), p1=float(lb.loc[1, "lb_pvalue"]),
                         LB3=float(lb.loc[3, "lb_stat"]), p3=float(lb.loc[3, "lb_pvalue"]),
                         LB7=float(lb.loc[7, "lb_stat"]), p7=float(lb.loc[7, "lb_pvalue"]),
                         acf1_significant=bool(abs(a[1]) > 1.96 * se)))
    LB = pd.DataFrame(rows).sort_values("totV", ascending=False)
    print(LB.round(4).to_string())
    chi = float(-2 * np.log(LB.p7.clip(1e-300)).sum())
    d = dict(
        n_series=len(LB),
        LB7_p_lt_05=int((LB.p7 < 0.05).sum()), LB1_p_lt_05=int((LB.p1 < 0.05).sum()),
        acf1_outside_band=int(LB.acf1_significant.sum()),
        mean_acf={f"lag{l}": float(LB[f"acf{l}"].mean()) for l in range(1, 8)},
        median_acf={f"lag{l}": float(LB[f"acf{l}"].median()) for l in range(1, 8)},
        fisher_chi2=chi, fisher_df=2 * len(LB),
        fisher_p=float(stats.chi2.sf(chi, 2 * len(LB))))
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("D4", "ljung_box_acf", d)

    sub("is the autocorrelation real, or just the collector ramp?")
    tr = []
    for (tp, pl), v in S.items():
        x = np.log1p(v)
        t = np.arange(len(x))
        lr = stats.linregress(t, x)
        res = x - (lr.intercept + lr.slope * t)
        tr.append(dict(topic=tp, plat=pl, slope=lr.slope, p_slope=lr.pvalue,
                       trend_r2=lr.rvalue ** 2,
                       acf1_raw=float(acf(x, nlags=1, fft=False)[1]),
                       acf1_detrended=float(acf(res, nlags=1, fft=False)[1]),
                       acf1_diff=float(acf(np.diff(x), nlags=1, fft=False)[1])))
    TR = pd.DataFrame(tr).sort_values("acf1_raw", ascending=False)
    print(TR.round(4).to_string())
    d = dict(series_with_significant_trend=int((TR.p_slope < 0.05).sum()), n=len(TR),
             mean_acf1_raw=float(TR.acf1_raw.mean()),
             mean_acf1_detrended=float(TR.acf1_detrended.mean()),
             mean_acf1_first_difference=float(TR.acf1_diff.mean()),
             max_trend_r2=float(TR.trend_r2.max()))
    print("\n" + json.dumps(jsonable(d), indent=2))
    print("  -> genuine autocorrelation survives detrending, but ~30% of raw acf1 is trend.")
    rec("D4", "trend_vs_autocorrelation", d)
    rec("D4", "trend_table", TR)

    sub(f"permutation test on a burst statistic (max 7d-sum / median 7d-sum), {N_PERM_BURST} perms")
    rng2 = np.random.default_rng(SEED + 1)

    def bstat(v):
        s = pd.Series(v).rolling(7, min_periods=7).sum().dropna().to_numpy()
        return np.nan if len(s) == 0 or np.median(s) <= 0 else s.max() / np.median(s)

    out = []
    for (tp, pl), v in S.items():
        obs = bstat(v)
        if not np.isfinite(obs):
            continue
        nul = np.array([bstat(rng2.permutation(v)) for _ in range(N_PERM_BURST)])
        nul = nul[np.isfinite(nul)]
        out.append(dict(topic=tp, plat=pl, obs=float(obs),
                        null_median=float(np.median(nul)),
                        null_p95=float(np.percentile(nul, 95)),
                        p=float(((nul >= obs).sum() + 1) / (len(nul) + 1)),
                        exceeds_null_p95=bool(obs > np.percentile(nul, 95))))
    B = pd.DataFrame(out).sort_values("p")
    print(B.round(4).to_string())
    chi = float(-2 * np.log(B.p.clip(1e-300)).sum())
    d = dict(n_series=len(B), exceeds_null_p95=int(B.exceeds_null_p95.sum()),
             p_lt_05=int((B.p < 0.05).sum()), fisher_chi2=chi, fisher_df=2 * len(B),
             fisher_p=float(stats.chi2.sf(chi, 2 * len(B))))
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("D4", "burst_permutation", d)
    rec("D4", "burst_permutation_table", B)


# ============================================================================
# D5 — sensitivity-harness bug
# ============================================================================
def v1_sensitivity(metrics: pd.DataFrame) -> dict:
    """Verbatim copy of scripts/06_validate.py::sensitivity."""
    out = {}
    for sf, ss in ((5, 14), (V1_EMA_FAST, V1_EMA_SLOW), (7, 14)):
        corrs = []
        for _, g in metrics.groupby(["topic_id", "platform"]):
            g = g.sort_values("posted_day")
            v = g["V"].fillna(0).to_numpy(dtype=float)
            if len(v) < ss + 2:
                continue
            growth = np.log((v[1:] + 1) / (v[:-1] + 1))     # <-- BUG: drops t=0
            s = pd.Series(growth)
            m = s.ewm(span=sf, adjust=False).mean() - s.ewm(span=ss, adjust=False).mean()
            base = g["momentum"].iloc[1:].reset_index(drop=True)
            m = m.reset_index(drop=True)
            n = min(len(base), len(m))
            if n > 10 and base.iloc[:n].std() > 0:
                corrs.append(float(base.iloc[:n].corr(m.iloc[:n])))
        out[f"ema_{sf}_{ss}_corr_vs_default"] = float(np.nanmean(corrs)) if corrs else None
    return out


def fixed_sensitivity(metrics: pd.DataFrame, spans=((5, 14), (7, 21), (7, 14), (10, 30))) -> dict:
    """
    Corrected harness. Rebuilds the growth series EXACTLY as 05_metrics.py does
    (leading NaN -> 0.0, so the EMA recursion is seeded at 0 for t=0) and
    compares against the full, unshifted momentum column.

    corr(default, default) == 1.0 is asserted before anything else is reported.
    """
    out = {}
    for sf, ss in spans:
        corrs, ns = [], []
        for _, g in metrics.groupby(["topic_id", "platform"]):
            g = g.sort_values("posted_day")
            v = g["V"].fillna(0).to_numpy(dtype=float)
            if len(v) < ss + 2:
                continue
            growth = np.concatenate([[0.0], np.log((v[1:] + V1_ALPHA) / (v[:-1] + V1_ALPHA))])
            s = pd.Series(growth)
            m = (s.ewm(span=sf, adjust=False, min_periods=1).mean()
                 - s.ewm(span=ss, adjust=False, min_periods=1).mean()).reset_index(drop=True)
            base = g["momentum"].reset_index(drop=True)
            n = min(len(base), len(m))
            if n > 10 and base.iloc[:n].std() > 0:
                corrs.append(float(base.iloc[:n].corr(m.iloc[:n])))
                ns.append(n)
        out[f"ema_{sf}_{ss}"] = dict(
            mean_corr=float(np.nanmean(corrs)) if corrs else None,
            min_corr=float(np.nanmin(corrs)) if corrs else None,
            n_series=len(corrs))
    # ---- mandatory self-test ----
    self_corr = out[f"ema_{V1_EMA_FAST}_{V1_EMA_SLOW}"]["mean_corr"]
    assert abs(self_corr - 1.0) < 1e-9, (
        f"SELF-TEST FAILED: corr(default,default) = {self_corr}, must be 1.0")
    out["self_test_corr_default_vs_default"] = self_corr
    out["self_test_passed"] = True
    return out


def section_D5(md):
    hdr("D5 — sensitivity harness bug: root cause and corrected numbers")

    sub("reproduce v1 verbatim")
    v1 = v1_sensitivity(md)
    print(json.dumps(v1, indent=2))
    print(f"\n  ema_7_21_corr_vs_default = {v1['ema_7_21_corr_vs_default']:.4f}"
          "  <-- 7/21 IS the default; must be 1.0")
    rec("D5", "v1_reported", v1)

    sub("root cause: isolate by changing exactly one line")
    for drop_lead in (True, False):
        res = {}
        for sf, ss in ((5, 14), (7, 21), (7, 14)):
            corrs = []
            for _, g in md.groupby(["topic_id", "platform"]):
                g = g.sort_values("posted_day")
                v = g["V"].fillna(0).to_numpy(float)
                if len(v) < ss + 2:
                    continue
                if drop_lead:
                    growth = np.log((v[1:] + 1) / (v[:-1] + 1))
                    base = g["momentum"].iloc[1:].reset_index(drop=True)
                else:
                    growth = np.concatenate([[0.0], np.log((v[1:] + 1) / (v[:-1] + 1))])
                    base = g["momentum"].reset_index(drop=True)
                s = pd.Series(growth)
                m = (s.ewm(span=sf, adjust=False).mean()
                     - s.ewm(span=ss, adjust=False).mean()).reset_index(drop=True)
                n = min(len(base), len(m))
                if n > 10 and base.iloc[:n].std() > 0:
                    corrs.append(float(base.iloc[:n].corr(m.iloc[:n])))
            res[f"{sf}/{ss}"] = round(float(np.nanmean(corrs)), 10)
        tag = "v1 (leading element dropped)" if drop_lead else "fixed (leading 0.0 kept)"
        print(f"  {tag:32s} {res}")
        rec("D5", "isolation_drop_lead" if drop_lead else "isolation_keep_lead", res)

    print("\n  ROOT CAUSE  06_validate.py:65")
    print("    growth = np.log((v[1:] + 1) / (v[:-1] + 1))        # length N-1")
    print("  05_metrics.py:293,300")
    print("    g['growth'] = np.log((V+a)/(V.shift(1)+a))         # length N, g[0]=NaN")
    print("    momentum    = ema(g['growth'].fillna(0), 7) - ema(..., 21)")
    print("  With adjust=False the EMA recursion is seeded at its FIRST input, so")
    print("  production seeds at growth[0]=0.0 while the harness seeds at growth[1].")
    print("  The two recursions have different initial conditions and never coincide;")
    print("  the discrepancy decays only as (1-2/(span+1))^t, i.e. a ~23-day memory on")
    print("  the slow leg, against series of ~45 usable days where 81% of growth values")
    print("  are exactly 0. Shifting momentum by iloc[1:] hides the off-by-one instead")
    print("  of fixing it. Restoring the leading 0.0 gives corr(default,default)=1.0.")

    sub("corrected harness, with the self-test that would have caught this")
    fx = fixed_sensitivity(md)
    print(json.dumps(jsonable(fx), indent=2))
    rec("D5", "corrected", fx)

    sub("stability of what actually matters: the recommended alerting statistic")
    piv, keep = wide_panel(md)
    days = piv.index
    ref = None
    stab = {}
    for win, L in ((21, 3), (14, 3), (28, 3), (21, 2), (21, 5)):
        sc = {}
        for c in keep:
            y = piv[c].to_numpy(float)
            off = np.ones(len(y))
            for i in range(len(y)):
                p = nb_scan_p(y, off, i, win=win, L=L)
                if np.isfinite(p):
                    sc[(days[i], c)] = -np.log10(p)
        s = pd.Series(sc)
        if ref is None:
            ref = s
            stab[f"win{win}_L{L}"] = 1.0
        else:
            j = ref.index.intersection(s.index)
            stab[f"win{win}_L{L}"] = float(stats.spearmanr(ref[j], s[j]).statistic)
    print("Spearman rank correlation of scan surprise vs the recommended default (21/3):")
    print(json.dumps({k: round(v, 4) for k, v in stab.items()}, indent=2))
    rec("D5", "scan_parameter_stability_spearman", stab)


# ============================================================================
# B — distributional assumptions
# ============================================================================
def section_B(md, posts):
    hdr("B — distributional assumptions")

    sub("Poisson vs Negative Binomial per (topic x platform), in-window")
    rows = []
    for (tp, pl), y in cell_series(md).items():
        y = y.astype(int)
        X = np.ones((len(y), 1))
        try:
            po = sm.GLM(y, X, family=sm.families.Poisson()).fit()
            nb = sm.NegativeBinomial(y, X, loglike_method="nb2").fit(disp=0, maxiter=500)
        except Exception:
            continue
        alpha = float(nb.params[-1])
        theta = 1 / alpha if alpha > 0 else np.inf
        lr = float(2 * (nb.llf - po.llf))
        mu = po.fittedvalues
        z = ((y - mu) ** 2 - y) / (mu * np.sqrt(2))
        ct = sm.OLS(z, (mu / np.sqrt(2))).fit()
        bic_p = float(-2 * po.llf + np.log(len(y)) * 1)
        rows.append(dict(
            topic=tp, plat=pl, n=len(y), totV=int(y.sum()), mean=float(y.mean()),
            var=float(y.var(ddof=1)), vmr=float(y.var(ddof=1) / y.mean()),
            pearson_dispersion=float(((y - mu) ** 2 / mu).sum() / (len(y) - 1)),
            nb_alpha=alpha, nb_theta=float(theta),
            LR_stat=lr, LR_p=float(0.5 * stats.chi2.sf(lr, 1)) if lr > 0 else 1.0,
            cameron_trivedi_t=float(ct.tvalues[0]), cameron_trivedi_p=float(ct.pvalues[0]),
            aic_poisson=float(po.aic), aic_nb=float(nb.aic),
            bic_poisson=bic_p, bic_nb=float(nb.bic),
            zeros_obs=int((y == 0).sum()),
            zeros_exp_poisson=float(np.exp(-mu).sum()),
            zeros_exp_nb=float(((theta / (theta + mu)) ** theta).sum()) if np.isfinite(theta) else np.nan))
    R = pd.DataFrame(rows)
    R["nb_wins_aic"] = R.aic_nb < R.aic_poisson
    R["nb_wins_bic"] = R.bic_nb < R.bic_poisson
    print(R.round(4).to_string())
    d = dict(
        n_series=len(R),
        nb_wins_aic=int(R.nb_wins_aic.sum()), nb_wins_bic=int(R.nb_wins_bic.sum()),
        LR_p_lt_05=int((R.LR_p < 0.05).sum()),
        cameron_trivedi_p_lt_05=int((R.cameron_trivedi_p < 0.05).sum()),
        theta_median=float(R.nb_theta.median()),
        theta_q25=float(R.nb_theta.quantile(0.25)), theta_q75=float(R.nb_theta.quantile(0.75)),
        theta_min=float(R.nb_theta.min()), theta_max=float(R.nb_theta.max()),
        pearson_dispersion_median=float(R.pearson_dispersion.median()),
        pearson_dispersion_min=float(R.pearson_dispersion.min()),
        pearson_dispersion_max=float(R.pearson_dispersion.max()),
        vmr_median=float(R.vmr.median()), vmr_max=float(R.vmr.max()))
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("B", "poisson_vs_nb", d)
    rec("B", "poisson_vs_nb_table", R)

    sub("zero-inflation: does plain NB already explain the zeros?")
    zi = dict(zeros_observed=int(R.zeros_obs.sum()),
              zeros_expected_poisson=float(R.zeros_exp_poisson.sum()),
              zeros_expected_nb=float(R.zeros_exp_nb.sum()))
    zi["nb_ratio_obs_over_exp"] = zi["zeros_observed"] / zi["zeros_expected_nb"]
    zi["poisson_ratio_obs_over_exp"] = zi["zeros_observed"] / zi["zeros_expected_poisson"]
    print(json.dumps(jsonable(zi), indent=2))
    print("  -> NB accounts for the zeros to within "
          f"{100*abs(zi['nb_ratio_obs_over_exp']-1):.1f}%. ZINB is NOT needed.")
    rec("B", "zero_inflation", zi)

    sub("pooled theta per platform (topic fixed effects) — the stable estimate")
    iw = md[md.in_window]
    pooled = {}
    for pl, g in iw.groupby("platform"):
        keepT = g.groupby("topic_id").V.sum()
        g = g[g.topic_id.isin(keepT[keepT >= MIN_SERIES_VOL].index)]
        if len(g) < 30:
            continue
        y = g.V.astype(int).to_numpy()
        X = sm.add_constant(pd.get_dummies(g.topic_id, drop_first=True).astype(float)).to_numpy()
        try:
            nb = sm.NegativeBinomial(y, X, loglike_method="nb2").fit(disp=0, maxiter=500)
            pooled[pl] = dict(n=len(g), alpha=float(nb.params[-1]),
                              theta=float(1 / nb.params[-1]))
        except Exception:
            pass
    print(json.dumps(jsonable(pooled), indent=2))
    rec("B", "pooled_theta_per_platform", pooled)

    sub("power of NB exceedance as a function of theta (why theta ~ 0.5 hurts)")
    pw = {}
    for th in (0.3, 0.5, 1.0, 2.0, 5.0, 10.0):
        pw[f"theta={th}"] = {
            f"mu={mu},V={obs}": dict(
                p=float(nbinom.sf(obs - 1, th, th / (th + mu))),
                surprise=float(-np.log10(max(nbinom.sf(obs - 1, th, th / (th + mu)), 1e-300))))
            for mu, obs in ((10, 30), (10, 50), (10, 100), (50, 150), (50, 300))}
    for k, v in pw.items():
        print(f"  {k:12s} " + "  ".join(f"{kk}: p={vv['p']:.3g}" for kk, vv in v.items()))
    print("  -> at the empirical median theta=0.55 a 3x jump gives p=0.088 (never survives FDR).")
    rec("B", "nb_power_vs_theta", pw)

    sub("variance-stabilising transforms on engagement (log1p vs Anscombe vs Freeman-Tukey)")
    eng = posts[posts.has_engagement.fillna(False)]
    tr = []
    for col in ("likes", "comments", "shares", "views"):
        x = eng[col].dropna()
        x = x[x >= 0].to_numpy(float)
        if len(x) < 50:
            continue
        tr.append(dict(field=col, n=len(x), mean=float(x.mean()), max=float(x.max()),
                       raw_skew=float(stats.skew(x)),
                       log1p_skew=float(stats.skew(np.log1p(x))),
                       log1p_kurtosis=float(stats.kurtosis(np.log1p(x))),
                       anscombe_skew=float(stats.skew(2 * np.sqrt(x + 3 / 8))),
                       freeman_tukey_skew=float(stats.skew(np.sqrt(x) + np.sqrt(x + 1))),
                       pct_zero=float(100 * (x == 0).mean())))
    T = pd.DataFrame(tr)
    print(T.round(3).to_string())
    rec("B", "engagement_transforms", T)

    sub("variance stabilisation on the count V itself")
    g = md[md.in_window].groupby(["topic_id", "platform"]).V
    tab = pd.DataFrame({
        "mean": g.mean(), "var": g.var(),
        "log1p_var": g.apply(lambda s: np.log1p(s).var()),
        "anscombe_var": g.apply(lambda s: (2 * np.sqrt(s + 3 / 8)).var()),
        "ft_var": g.apply(lambda s: (np.sqrt(s) + np.sqrt(s + 1)).var())}).dropna()
    tab = tab[tab["mean"] > 0.4]
    d = dict(
        spearman_mean_vs_var_raw=float(stats.spearmanr(tab["mean"], tab["var"]).statistic),
        spearman_mean_vs_var_log1p=float(stats.spearmanr(tab["mean"], tab.log1p_var).statistic),
        spearman_mean_vs_var_anscombe=float(stats.spearmanr(tab["mean"], tab.anscombe_var).statistic),
        spearman_mean_vs_var_ft=float(stats.spearmanr(tab["mean"], tab.ft_var).statistic),
        mean_transformed_var_log1p=float(tab.log1p_var.mean()),
        mean_transformed_var_anscombe=float(tab.anscombe_var.mean()),
        mean_transformed_var_ft=float(tab.ft_var.mean()),
        var_of_transformed_var_log1p=float(tab.log1p_var.var()),
        var_of_transformed_var_anscombe=float(tab.anscombe_var.var()),
        var_of_transformed_var_ft=float(tab.ft_var.var()))
    print(json.dumps(jsonable(d), indent=2))
    print("  -> Anscombe/Freeman-Tukey target Poisson (theta=inf). At theta~0.5 they leave")
    print("     variance ~18x the nominal 1.0. log1p lands at 0.999. log1p wins outright.")
    rec("B", "count_transforms", d)

    sub("empirical-Bayes shrinkage constant k from variance components")
    iwp = posts[posts.day >= pd.Timestamp(WINDOW_START, tz="UTC")].copy()

    def virality(r):
        lk = 0.0 if pd.isna(r.likes) else r.likes
        cm = 0.0 if pd.isna(r.comments) else r.comments
        sh = 0.0 if pd.isna(r.shares) else r.shares
        vw = 0.0 if pd.isna(r.views) else r.views
        if r.platform == "telegram":
            return sh / (vw + 1) if (vw or sh) else np.nan
        if r.platform in ("instagram", "facebook"):
            return sh / (lk + cm + sh + 1)
        if r.platform == "youtube_video":
            return (lk + cm) / (vw + 1) if (vw or lk or cm) else np.nan
        if r.platform == "linkedin":
            return cm / (lk + 1) if (lk or cm) else np.nan
        return np.nan

    iwp["virality"] = iwp.apply(virality, axis=1)
    iwp["log_engagement"] = np.log1p(
        iwp[["likes", "comments", "shares", "views"]].fillna(0).sum(axis=1)
        .where(iwp.has_engagement))

    def vcomp(df, col, keys):
        d = df.dropna(subset=[col])
        gg = d.groupby(keys)[col]
        n = gg.size()
        m = gg.mean()
        K, N = len(n), int(n.sum())
        ssw = gg.apply(lambda s: ((s - s.mean()) ** 2).sum()).sum()
        sigma2 = ssw / (N - K) if N > K else np.nan
        grand = d[col].mean()
        msb = (n * (m - grand) ** 2).sum() / (K - 1)
        n0 = (N - (n ** 2).sum() / N) / (K - 1)
        tau2 = max((msb - sigma2) / n0, 0.0)
        return dict(metric=col, grain="x".join(keys), K_cells=K, N_posts=N,
                    sigma2_within=float(sigma2), tau2_between=float(tau2),
                    n0=float(n0), k=float(sigma2 / tau2) if tau2 > 0 else np.inf,
                    icc=float(tau2 / (tau2 + sigma2)), grand_mean=float(grand),
                    median_cell_n=float(n.median()),
                    frac_cells_n_le_2=float((n <= 2).mean()))

    cols = ["tone_score", "hate_score", "readability_score", "virality", "log_engagement"]
    VC = pd.DataFrame([vcomp(iwp, c, ["topic_id", "platform", "day"]) for c in cols])
    print(VC.round(5).to_string())
    rec("B", "variance_components_topic_platform_day", VC)

    VC2 = pd.DataFrame([vcomp(iwp, c, ["topic_id", "day"])
                        for c in ("tone_score", "hate_score", "virality")])
    print("\nsame at (topic x day) — platforms pooled:")
    print(VC2.round(5).to_string())
    print("  -> virality ICC collapses 0.554 -> 0.030 when platforms are pooled:")
    print("     virality MUST NOT be averaged across platforms.")
    rec("B", "variance_components_topic_day", VC2)

    sub("how far a 1-2 post cell mean sits from its shrunken estimate")
    shrink = []
    for col in ("tone_score", "hate_score", "virality"):
        r = VC[VC.metric == col].iloc[0]
        k = r["k"]
        d = iwp.dropna(subset=[col])
        gg = d.groupby(["topic_id", "platform", "day"])[col]
        n, m = gg.size(), gg.mean()
        parent = d.groupby("platform")[col].mean()
        mu = pd.Series([i[1] for i in m.index], index=m.index).map(parent)
        eb = (n * m + k * mu) / (n + k)
        disp = (m - eb).abs()
        for lo, hi, lab in ((1, 2, "n<=2"), (3, 4, "n=3-4"), (5, 9, "n=5-9"), (10, 10 ** 9, "n>=10")):
            msk = (n >= lo) & (n <= hi)
            if msk.sum() == 0:
                continue
            shrink.append(dict(metric=col, k=round(float(k), 3), bucket=lab,
                               n_cells=int(msk.sum()),
                               mean_abs_raw_minus_eb=float(disp[msk].mean()),
                               pct_of_between_cell_sd=float(
                                   100 * disp[msk].mean() / np.sqrt(r.tau2_between)),
                               mean_shrink_weight_k_over_n_plus_k=float(
                                   (k / (n[msk] + k)).mean())))
    SH = pd.DataFrame(shrink)
    print(SH.round(4).to_string())
    rec("B", "eb_shrinkage_displacement", SH)


# ============================================================================
# C — acceleration family
# ============================================================================
def det_raw_accel(y):
    x = np.log1p(y)
    s = np.full(len(y), np.nan)
    for i in range(2, len(y)):
        s[i] = (x[i] - x[i - 1]) - (x[i - 1] - x[i - 2])
    return s


def det_savgol(y, w, o):
    """Causal SavGol 2nd derivative: fit on the trailing w points, read the endpoint."""
    x = np.log1p(y)
    s = np.full(len(y), np.nan)
    for i in range(w - 1, len(y)):
        s[i] = savgol_filter(x[i - w + 1: i + 1], w, o, deriv=2, mode="interp")[-1]
    return s


def det_nb(y, win=BASELINE_WIN):
    s = np.full(len(y), np.nan)
    for i in range(win, len(y)):
        p = nb_surprise_p(y[i - win:i], y[i])
        if np.isfinite(p):
            s[i] = -np.log10(p)
    return s


def det_cusum(y, win=BASELINE_WIN, k=1.5):
    s = np.full(len(y), np.nan)
    C = 0.0
    for i in range(win, len(y)):
        h = np.asarray(y[i - win:i], float)
        mu = h.mean()
        if mu <= 0:
            s[i] = C
            continue
        th = mom_theta(h)
        var = mu + mu * mu / th
        C = max(0.0, C + (y[i] - mu) / np.sqrt(max(var, 1e-9)) - k)
        s[i] = C
    return s


def det_scan(y, win=BASELINE_WIN, L=SCAN_L):
    off = np.ones(len(y))
    s = np.full(len(y), np.nan)
    for i in range(len(y)):
        p = nb_scan_p(y, off, i, win=win, L=L)
        if np.isfinite(p):
            s[i] = -np.log10(p)
    return s


def section_C(md, posts, con):
    hdr("C — acceleration family")
    S = cell_series(md)

    sub("noise amplification of 1st and 2nd differences of log1p(V)")
    rows = []
    for (tp, pl), v in S.items():
        x = np.log1p(v)
        g = np.diff(x)
        a = np.diff(g)
        rows.append(dict(topic=tp, plat=pl, n=len(x),
                         sd_level=float(x.std(ddof=1)), sd_growth=float(g.std(ddof=1)),
                         sd_accel=float(a.std(ddof=1)),
                         amp_growth_over_level=float(g.std(ddof=1) / x.std(ddof=1)),
                         amp_accel_over_growth=float(a.std(ddof=1) / g.std(ddof=1)),
                         var_ratio_accel_over_growth=float(a.var(ddof=1) / g.var(ddof=1)),
                         acf1_growth=float(acf(g, nlags=1, fft=False)[1]),
                         acf1_accel=float(acf(a, nlags=1, fft=False)[1]) if a.std() > 0 else np.nan))
    A = pd.DataFrame(rows)
    d = dict(
        sd_accel_over_sd_growth_mean=float(A.amp_accel_over_growth.mean()),
        sd_accel_over_sd_growth_median=float(A.amp_accel_over_growth.median()),
        iid_theory_sqrt3=float(np.sqrt(3)),
        var_accel_over_var_growth_mean=float(A.var_ratio_accel_over_growth.mean()),
        sd_growth_over_sd_level_mean=float(A.amp_growth_over_level.mean()),
        iid_theory_sqrt2=float(np.sqrt(2)),
        acf1_accel_mean=float(A.acf1_accel.mean()),
        acf1_accel_iid_theory=-2 / 3,
        implied_noise_share_of_accel=float(A.acf1_accel.mean() / (-2 / 3)),
        acf1_growth_mean=float(A.acf1_growth.mean()))
    print(json.dumps(jsonable(d), indent=2))
    print("  -> the raw 2nd difference carries ~"
          f"{100*d['implied_noise_share_of_accel']:.0f}% white noise "
          "(acf1 -0.50 against the -0.667 of pure differenced noise).")
    rec("C", "accel_noise_amplification", d)

    sub("SavGol noise reduction vs raw 2nd difference")
    sg = {}
    for w, o in ((5, 2), (7, 2), (9, 2), (7, 3), (11, 2), (11, 3)):
        rs, ac = [], []
        for (tp, pl), v in S.items():
            x = np.log1p(v)
            if len(x) <= w:
                continue
            a = np.diff(np.diff(x))
            d2 = savgol_filter(x, w, o, deriv=2, delta=1.0)
            rs.append(d2.std(ddof=1) / a.std(ddof=1))
            if d2.std() > 0:
                ac.append(acf(d2, nlags=1, fft=False)[1])
        sg[f"win{w}_ord{o}"] = dict(sd_ratio_vs_raw_accel=float(np.mean(rs)),
                                    noise_reduction_pct=float(100 * (1 - np.mean(rs))),
                                    acf1_of_smoothed=float(np.nanmean(ac)))
    print(json.dumps(jsonable(sg), indent=2))
    rec("C", "savgol_noise_reduction", sg)

    sub(f"detector bake-off: synthetic bursts injected into shuffled real baselines "
        f"(N={N_INJECT})")
    rng = np.random.default_rng(SEED + 2)
    # use the padded wide panel so every donor baseline is the full 45-day window
    piv0, keep0 = wide_panel(md)
    base = [piv0[c].to_numpy(float) for c in keep0]
    DETS = {
        "raw_accel": det_raw_accel,
        "savgol_5_2": lambda y: det_savgol(y, 5, 2),
        "savgol_7_2": lambda y: det_savgol(y, 7, 2),
        "savgol_9_2": lambda y: det_savgol(y, 9, 2),
        "savgol_7_3": lambda y: det_savgol(y, 7, 3),
        "nb_surprise_21": det_nb,
        "nb_cusum_k1.5": det_cusum,
        "nb_scan_3d": det_scan,
    }
    acc = {k: {"y": [], "s": []} for k in DETS}
    for rep in range(N_INJECT):
        y = rng.permutation(base[rng.integers(len(base))]).astype(float)
        lab = np.zeros(len(y), int)
        if rep % 2 == 0:
            t0 = int(rng.integers(25, len(y) - 6))
            mult = float(rng.choice([2.0, 3.0, 5.0, 8.0]))
            mu = max(y.mean(), 0.5)
            ramp = np.array([0.35, 0.7, 1.0, 1.0, 0.7])[: len(y) - t0]
            add = rng.poisson(mu * (mult - 1) * ramp)
            y[t0: t0 + len(add)] += add
            lab[t0: t0 + len(add)] = 1
        for k, f in DETS.items():
            s = f(y)
            m = np.isfinite(s)
            acc[k]["y"].append(lab[m])
            acc[k]["s"].append(s[m])
    bake = []
    for k in DETS:
        yy = np.concatenate(acc[k]["y"])
        ss = np.concatenate(acc[k]["s"])
        thr5 = np.quantile(ss[yy == 0], 0.95)
        thr1 = np.quantile(ss[yy == 0], 0.99)
        bake.append(dict(detector=k, auc=float(roc_auc_score(yy, ss)),
                         tpr_at_fpr5=float((ss[yy == 1] > thr5).mean()),
                         tpr_at_fpr1=float((ss[yy == 1] > thr1).mean()),
                         null_thr95=float(thr5), null_thr99=float(thr1), n_obs=int(len(yy))))
    BK = pd.DataFrame(bake).sort_values("auc", ascending=False)
    print(BK.round(4).to_string())
    print("  -> the spec's publishable accel_smooth (SavGol win 5 ord 2) is at chance.")
    rec("C", "detector_bakeoff", BK)

    sub("Kleinberg 2-state HMM feasibility at this data volume")
    piv, keep = wide_panel(md)
    runs = []
    for c in keep:
        y = piv[c].to_numpy(float)
        mtr = pd.Series(y).rolling(BASELINE_WIN, min_periods=10).median().shift(1)
        hi = (y > 2 * mtr.fillna(np.inf)).astype(int)
        runs.append(dict(cell=f"{c[0]}|{c[1]}", n=len(y), elevated_days=int(hi.sum()),
                         episodes=int(((hi == 1) & (np.r_[0, hi[:-1]] == 0)).sum())))
    RU = pd.DataFrame(runs)
    d = dict(series_length_days=int(len(piv)), n_cells=len(keep),
             median_episodes_per_cell=float(RU.episodes.median()),
             total_episodes=int(RU.episodes.sum()),
             cells_with_zero_episodes=int((RU.episodes == 0).sum()),
             mean_elevated_days=float(RU.elevated_days.mean()),
             pct_days_elevated=float(100 * RU.elevated_days.mean() / len(piv)),
             params_to_estimate="2 emission + 2 transition")
    print(json.dumps(jsonable(d), indent=2))
    print("  -> 45 observations, ~5 episodes, 31% of days already 'elevated', and a")
    print("     non-stationary baseline. A 2-state HMM is not identifiable here.")
    rec("C", "kleinberg_feasibility", d)

    sub("novelty via embedding centroids — coverage")
    cov = con.execute(f"""
        SELECT COALESCE(t.topic_id,'unassigned') topic_id, f.platform,
               date_trunc('day', f.posted_at) d, COUNT(*) n,
               SUM(CASE WHEN e.post_uid IS NOT NULL THEN 1 ELSE 0 END) emb
        FROM post_facts f
        LEFT JOIN post_embeddings e USING(post_uid)
        LEFT JOIN post_topic t USING(post_uid)
        WHERE f.posted_at >= '{WINDOW_START}'
        GROUP BY 1,2,3""").df()
    plat = con.execute("""
        SELECT f.platform, COUNT(*) n,
               SUM(CASE WHEN e.post_uid IS NOT NULL THEN 1 ELSE 0 END) emb
        FROM post_facts f LEFT JOIN post_embeddings e USING(post_uid)
        GROUP BY 1 ORDER BY 2 DESC""").df()
    plat["pct"] = (100 * plat.emb / plat.n).round(1)
    print(plat.to_string())
    d = dict(
        in_window_cells=len(cov),
        cells_with_ge1_embedding=int((cov.emb >= 1).sum()),
        cells_with_ge5_embeddings=int((cov.emb >= 5).sum()),
        pct_cells_ge5=float(100 * (cov.emb >= 5).mean()),
        cells_fully_covered=int((cov.emb == cov.n).sum()),
        post_weighted_coverage_pct=float(100 * cov.emb.sum() / cov.n.sum()),
        youtube_video_embeddings=int(plat.loc[plat.platform == "youtube_video", "emb"].iloc[0]),
        youtube_video_posts=int(plat.loc[plat.platform == "youtube_video", "n"].iloc[0]))
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("C", "novelty_embedding_coverage", d)
    rec("C", "embedding_coverage_by_platform", plat)

    sub("persistence: AR(1) on the level vs AR(1) on the difference (F7 as written)")
    rows = []
    for (tp, pl), v in S.items():
        x = np.log1p(v)
        for lab, y in (("level", x), ("first_difference", np.diff(x))):
            if len(y) < 15 or np.std(y) == 0:
                continue
            m = sm.OLS(y[1:], sm.add_constant(y[:-1])).fit()
            phi = float(m.params[1])
            rows.append(dict(topic=tp, plat=pl, kind=lab, n=len(y), phi=phi,
                             phi_se=float(m.bse[1]), t=phi / float(m.bse[1]),
                             half_life_days=float(np.log(0.5) / np.log(phi)) if 0 < phi < 1 else np.nan,
                             volatility=float(np.std(np.diff(x), ddof=1))))
    AR = pd.DataFrame(rows)
    lv, df1 = AR[AR.kind == "level"], AR[AR.kind == "first_difference"]
    hl = con.execute("SELECT * FROM half_life").df()
    d = dict(
        n_series=len(lv),
        level_phi_in_0_1=int(((lv.phi > 0) & (lv.phi < 1)).sum()),
        level_phi_median=float(lv.phi.median()),
        level_half_life_median=float(lv.half_life_days.median()),
        level_half_life_defined=int(lv.half_life_days.notna().sum()),
        diff_phi_median=float(df1.phi.median()),
        diff_phi_negative=int((df1.phi < 0).sum()),
        diff_half_life_defined=int(df1.half_life_days.notna().sum()),
        v1_half_life_ok=int(hl.ok.sum()), v1_half_life_rows=len(hl),
        v1_half_life_r2_median=float(hl.r2.median()),
        v1_half_life_r2_negative=int((hl.r2 < 0).sum()),
        v1_half_life_r2_null=int(hl.r2.isna().sum()),
        v1_half_life_max_days=float(hl.half_life_days.max()))
    print(json.dumps(jsonable(d), indent=2))
    print("  -> F7's literal formula (AR(1) on the Delta log-rate) yields phi<0 for "
          f"{d['diff_phi_negative']}/{len(df1)} series, so ln(0.5)/ln(phi) is UNDEFINED.")
    print("     AR(1) on the LEVEL gives a usable half-life for "
          f"{d['level_half_life_defined']}/{len(lv)} vs v1's {d['v1_half_life_ok']}/39.")
    rec("C", "persistence_ar1", d)
    rec("C", "persistence_ar1_level_table", lv)


# ============================================================================
# D — multiple testing and alerting
# ============================================================================
def section_MT(md, con):
    hdr("D — multiple testing and alert calibration")
    piv, keep = wide_panel(md)
    days = piv.index

    sub("effective number of independent daily tests")
    X = np.log1p(piv[keep].to_numpy(float))
    X = X - X.mean(0)
    C = np.corrcoef(X.T)
    M = C.shape[0]
    ev = np.clip(np.linalg.eigvalsh(C)[::-1], 0, None)
    d = dict(
        M_cells_tested_per_day=M,
        mean_abs_offdiag_corr=float(np.abs(C[np.triu_indices(M, 1)]).mean()),
        mean_offdiag_corr=float(C[np.triu_indices(M, 1)].mean()),
        nyholt_cheverud=float(1 + (M - 1) * (1 - ev.var(ddof=1) / M)),
        participation_ratio=float(ev.sum() ** 2 / (ev ** 2).sum()),
        li_ji=float(sum(1 if e >= 1 else e - np.floor(e) for e in ev)),
        n_pcs_for_95pct_var=int(np.searchsorted(np.cumsum(ev) / ev.sum(), 0.95) + 1),
        first_pc_variance_share=float(ev[0] / ev.sum()),
        top5_eigenvalue_shares=[float(x) for x in (ev[:5] / ev.sum())])
    d["naive_M_over_participation_ratio"] = M / d["participation_ratio"]
    print(json.dumps(jsonable(d), indent=2))
    rec("MT", "effective_tests", d)

    by = {}
    for M_ in (int(d["participation_ratio"]), 10, M, 40, 100):
        cM = float(np.log(M_) + 0.5772156649 + 1 / (2 * M_))
        by[f"M={M_}"] = dict(
            by_inflation_sum_1_over_i=cM,
            by_q05_equivalent_bh_q=0.05 / cM,
            by_threshold_for_most_significant=0.05 / (M_ * cM),
            bonferroni_threshold=0.05 / M_,
            by_stricter_than_bonferroni_at_rank1=bool(cM > 1.0))
    print("\nBenjamini-Yekutieli cost:\n" + json.dumps(jsonable(by), indent=2))
    print("  -> at M=22, BY's threshold for the SINGLE most significant test is "
          "3.69x stricter than Bonferroni's. BY is the wrong tool at this panel width.")
    rec("MT", "by_inflation", by)

    sub("is the NB p-value uniform under the null? (baseline window x theta_floor x mid-p)")
    grid = []
    for win in (10, 14, 21, 28):
        for thfl in (0.0, 0.5, 1.0, 2.0):
            for midp in (False, True):
                # fixed seed per config -> every config sees the SAME shuffled panel,
                # so differences between configs are not shuffle noise
                rng = np.random.default_rng(SEED + 3)
                pn, pr = [], []
                for c in keep:
                    y = piv[c].to_numpy(float)
                    ysh = rng.permutation(y)
                    for i in range(win, len(y)):
                        a = nb_surprise_p(ysh[i - win:i], ysh[i], thfl, midp)
                        b = nb_surprise_p(y[i - win:i], y[i], thfl, midp)
                        if np.isfinite(a):
                            pn.append(a)
                        if np.isfinite(b):
                            pr.append(b)
                pn, pr = np.array(pn), np.array(pr)
                grid.append(dict(
                    baseline_window=win, theta_floor=thfl, mid_p=midp,
                    null_frac_lt_05=float((pn < 0.05).mean()),
                    null_frac_lt_01=float((pn < 0.01).mean()),
                    ks_uniform_p=float(stats.kstest(pn, "uniform").pvalue),
                    real_frac_lt_05=float((pr < 0.05).mean()),
                    enrichment=float((pr < 0.05).mean() / max((pn < 0.05).mean(), 1e-9)),
                    implied_fdr_at_p05=float((pn < 0.05).mean() / max((pr < 0.05).mean(), 1e-9))))
    G = pd.DataFrame(grid)
    G["calibration_error"] = (G.null_frac_lt_05 - 0.05).abs()
    print(G.round(4).to_string())
    print("\nbest-calibrated configurations:")
    print(G.sort_values(["calibration_error", "enrichment"], ascending=[True, False])
          .head(6).round(4).to_string())
    rec("MT", "null_calibration_grid", G)

    sub("exposure offset: which one removes the collector ramp?")
    iw = md[md.in_window]
    PT = iw.pivot_table(index="posted_day", columns="platform", values="V",
                        aggfunc="sum").reindex(days).fillna(0).clip(lower=1)
    ch = con.execute(f"""
        SELECT platform, date_trunc('day', posted_at)::DATE d, COUNT(DISTINCT author_uid) c
        FROM post_facts WHERE posted_at >= '{WINDOW_START}'
        GROUP BY 1,2""").df()
    ch["d"] = pd.to_datetime(ch.d).dt.tz_localize("UTC")
    CH = ch.pivot_table(index="d", columns="platform", values="c").reindex(days) \
        .ffill().fillna(1).clip(lower=1)

    offsets = {"none": None, "active_channels": CH, "platform_day_total": PT}
    off_res = {}
    p_by_offset = {}
    for name, OF in offsets.items():
        r2s, slopes = [], []
        P = pd.DataFrame(index=days, columns=pd.MultiIndex.from_tuples(keep), dtype=float)
        for c in keep:
            y = piv[c].to_numpy(float)
            off = np.ones(len(y)) if OF is None else OF[c[1]].to_numpy(float)
            lr = stats.linregress(np.arange(len(y)), np.log((y + 1) / off))
            r2s.append(lr.rvalue ** 2)
            slopes.append(abs(lr.slope))
            for i in range(len(y)):
                P.loc[days[i], c] = nb_scan_p(y, off, i)
        na = []
        for dd in days:
            row = P.loc[dd].dropna()
            if len(row) == 0:
                na.append(0)
                continue
            rej, _, _, _ = multipletests(row.values, alpha=0.05, method="fdr_bh")
            na.append(int(rej.sum()))
        na = pd.Series(na, index=days)
        mid = days[len(days) // 2]
        off_res[name] = dict(
            mean_trend_r2_of_log_rate=float(np.mean(r2s)),
            mean_abs_trend_slope=float(np.mean(slopes)),
            n_tests=int(P.notna().sum().sum()),
            raw_p_lt_05=int((P < 0.05).sum().sum()),
            bh_alerts=int(na.sum()),
            bh_alerts_first_half=int(na[na.index < mid].sum()),
            bh_alerts_second_half=int(na[na.index >= mid].sum()),
            alerts_per_day=float(na.sum() / max(int(P.notna().any(axis=1).sum()), 1)))
        p_by_offset[name] = P
    print(json.dumps(jsonable(off_res), indent=2))
    print("  -> the exposure offset removes ~90% of the alerts, i.e. ~90% of raw-count")
    print("     alerts were collector-ramp artifacts. But every surviving alert is still")
    print("     in the second half of the window: the offset does NOT make it stationary.")
    rec("MT", "offset_comparison", off_res)

    sub("FDR method comparison on the effort-normalised p-values")
    P = p_by_offset["active_channels"]
    meth = {}
    for m in ("fdr_bh", "fdr_by", "bonferroni", "holm"):
        tot = 0
        for dd in days:
            row = P.loc[dd].dropna()
            if len(row) == 0:
                continue
            rej, _, _, _ = multipletests(row.values, alpha=0.05, method=m)
            tot += int(rej.sum())
        meth[m] = tot
    meth["raw_p_lt_05"] = int((P < 0.05).sum().sum())
    meth["n_tests"] = int(P.notna().sum().sum())
    print(json.dumps(jsonable(meth), indent=2))
    print(f"  -> BY keeps {meth['fdr_by']}/{meth['fdr_bh']} of BH's detections "
          f"({100*meth['fdr_by']/max(meth['fdr_bh'],1):.0f}%). Dependence is POSITIVE "
          f"(mean rho = +{d['mean_offdiag_corr']:.2f}), which BH already handles under PRDS.")
    rec("MT", "fdr_methods", meth)

    sub("severity bands from the realised q distribution")
    qs = P.copy() * np.nan
    for dd in days:
        row = P.loc[dd].dropna()
        if len(row) == 0:
            continue
        _, q, _, _ = multipletests(row.values, alpha=0.05, method="fdr_bh")
        qs.loc[dd, row.index] = q
    qv = qs.stack(level=[0, 1], future_stack=True).dropna().to_numpy(float)
    bands = {}
    for lab, thr in (("INFO", 0.20), ("LOW", 0.10), ("MEDIUM", 0.05),
                     ("HIGH", 0.01), ("CRITICAL", 0.001)):
        bands[lab] = dict(q_threshold=thr, n=int((qv < thr).sum()),
                          pct_of_tests=float(100 * (qv < thr).mean()),
                          alerts_per_day=float((qv < thr).sum() / len(days)))
    print(json.dumps(jsonable(bands), indent=2))
    print("q quantiles:", {f"p{p}": round(float(np.quantile(qv, p / 100)), 4)
                           for p in (1, 5, 10, 25, 50)})
    rec("MT", "severity_bands", bands)

    st = qs.stack(level=[0, 1], future_stack=True).dropna().sort_values().head(12)
    top = pd.DataFrame([dict(day=str(k[0])[:10], topic=k[1], platform=k[2],
                             V_day=float(piv.loc[k[0], (k[1], k[2])]),
                             V_scan_3d=float(piv[(k[1], k[2])].loc[:k[0]].iloc[-SCAN_L:].sum()),
                             q=float(v)) for k, v in st.items()])
    print("\ntop 12 alerts:\n" + top.to_string())
    rec("MT", "top_alerts", top)


# ============================================================================
# E — defects the spec does not mention
# ============================================================================
def section_E(md, posts, con):
    hdr("E — defects not covered by METRICS_V2_SPEC.md")

    sub("E1 timezone / day-boundary")
    h = con.execute("""
        SELECT platform, COUNT(*) n,
          SUM(CASE WHEN EXTRACT(hour FROM posted_at)*60 + EXTRACT(minute FROM posted_at)
                   >= 18*60+30 THEN 1 ELSE 0 END) after_1830_utc,
          SUM(CASE WHEN EXTRACT(hour FROM posted_at)=0 AND EXTRACT(minute FROM posted_at)=0
                   THEN 1 ELSE 0 END) exactly_midnight_utc
        FROM post_facts WHERE posted_at IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""").df()
    h["pct_would_shift_under_IST"] = (100 * h.after_1830_utc / h.n).round(1)
    h["pct_midnight"] = (100 * h.exactly_midnight_utc / h.n).round(1)
    print(h.to_string())
    d = dict(overall_pct_shift_under_IST=float(100 * h.after_1830_utc.sum() / h.n.sum()),
             linkedin_midnight_share=float(
                 h.loc[h.platform == "linkedin", "pct_midnight"].iloc[0]))
    print("\n" + json.dumps(jsonable(d), indent=2))
    print("  -> 18% of posts change day under IST; LinkedIn posted_at is date-only "
          "(100% at 00:00:00Z) so it has no intra-day time at all.")
    rec("E", "E1_timezone", d)
    rec("E", "E1_table", h)

    sub("E2 collection_effort cannot be joined on posted_day (F2 as written is infeasible)")
    eff = con.execute("SELECT * FROM collection_effort").df()
    eff["day"] = pd.to_datetime(eff.day)
    pf = con.execute("""SELECT platform, date_trunc('day',posted_at) d, COUNT(*) n
                        FROM post_facts GROUP BY 1,2""").df()
    pf["d"] = pd.to_datetime(pf.d, utc=True).dt.tz_localize(None)
    rows = []
    for pl in ("telegram", "reddit", "youtube_video", "linkedin", "instagram", "facebook"):
        e = set(eff[eff.platform == pl].day)
        p = set(pf[(pf.platform == pl) & (pf.d >= pd.Timestamp(WINDOW_START))].d)
        rows.append(dict(platform=pl, effort_days=len(e), posted_days_in_window=len(p),
                         overlap=len(e & p),
                         pct_posted_days_with_effort=round(100 * len(e & p) / max(len(p), 1), 0)))
    EF = pd.DataFrame(rows)
    print(EF.to_string())
    d = dict(active_channels_zero_rows=int((eff.active_channels == 0).sum()),
             total_effort_rows=len(eff),
             pct_active_channels_zero=float(100 * (eff.active_channels == 0).mean()),
             telegram_effort_rows_ingested=int(eff[eff.platform == "telegram"].rows_ingested.sum()),
             telegram_post_facts=int(con.execute(
                 "SELECT COUNT(*) FROM post_facts WHERE platform='telegram'").fetchone()[0]),
             telegram_effort_days=int((eff.platform == "telegram").sum()))
    print("\n" + json.dumps(jsonable(d), indent=2))
    print("  -> effort.day is the SCRAPE day and pre-dedup (23,325 vs 14,595); "
          "active_channels==0 in 31% of rows so log(effort) is undefined.")
    rec("E", "E2_effort_join", d)
    rec("E", "E2_table", EF)

    sub("E3 the collector ramp is far larger than the spec claims")
    tg = con.execute("""
        SELECT date_trunc('day',posted_at)::DATE d, COUNT(*) n,
               COUNT(DISTINCT author_uid) channels
        FROM post_facts WHERE platform='telegram' AND posted_at >= '2026-06-01'
        GROUP BY 1 ORDER BY 1""").df()
    tg = tg.set_index(pd.to_datetime(tg.d))
    wk = tg.resample("W")[["n", "channels"]].mean()
    print(wk.round(1).to_string())
    d = dict(
        first_week_posts_per_day=float(wk.n.iloc[0]), peak_week_posts_per_day=float(wk.n.max()),
        volume_ramp_x=float(wk.n.max() / wk.n.iloc[0]),
        first_week_channels=float(wk.channels.iloc[0]), peak_week_channels=float(wk.channels.max()),
        channel_ramp_x=float(wk.channels.max() / wk.channels.iloc[0]),
        posts_per_channel_ramp_x=float((wk.n.max() / wk.channels.max())
                                       / (wk.n.iloc[0] / wk.channels.iloc[0])),
        spec_claim="422 -> 1,393 posts/day, 18 -> 24 channels",
        spec_claim_reproducible=False,
        note="No day has n=422 or n=1393; channels never step 18->24 as a regime change.")
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("E", "E3_collector_ramp", d)

    iw = md[md.in_window].copy()
    iw["lv"] = np.log1p(iw.V)
    iw["t"] = (iw.posted_day - iw.posted_day.min()).dt.days
    fe = sm.add_constant(pd.get_dummies(iw.topic_id + "|" + iw.platform,
                                        drop_first=True).astype(float))
    m0 = sm.OLS(iw.lv, fe).fit()
    m1 = sm.OLS(iw.lv, pd.concat(
        [fe, pd.get_dummies(iw.platform, prefix="ramp").astype(float).mul(iw.t, axis=0)],
        axis=1)).fit()
    d2 = dict(r2_cell_fixed_effects=float(m0.rsquared),
              r2_plus_platform_linear_trend=float(m1.rsquared),
              share_of_residual_variance_explained_by_ramp=float(
                  (m1.rsquared - m0.rsquared) / (1 - m0.rsquared)))
    print(json.dumps(jsonable(d2), indent=2))
    rec("E", "E3_ramp_variance_share", d2)

    sub("E4 virality_z self-inclusion and epsilon division")
    d = dict(max_abs_virality_z=float(np.nanmax(np.abs(md.virality_z))),
             n_abs_gt_1e6=int((md.virality_z.abs() > 1e6).sum()),
             n_nonnull=int(md.virality_z.notna().sum()),
             mechanism="rolling(28).median() INCLUDES day t, so the MAD-of-deviations "
                       "collapses to 0 and the +1e-9 epsilon becomes the divisor")
    print(json.dumps(jsonable(d), indent=2))
    max_fix, n_fix = 0.0, 0
    for _, g in md.groupby(["topic_id", "platform"]):
        s = g.sort_values("posted_day").virality_mean.reset_index(drop=True)
        for i in range(len(s)):
            z = fixed_robust_z(s.iloc[i], s.iloc[max(0, i - V1_BASELINE_DAYS):i], min_n=8)
            if np.isfinite(z):
                n_fix += 1
                max_fix = max(max_fix, abs(z))
    print(f"  trailing-only + gated: emits {n_fix} values, max |z| = {max_fix:.2f}")
    d.update(fixed_n=n_fix, fixed_max_abs=max_fix)
    rec("E", "E4_virality_z", d)

    sub("E5 growth_share uses epsilon=1e-6 while growth uses alpha=1.0")
    gs = md.growth_share.replace([np.inf, -np.inf], np.nan)
    d = dict(growth_sd=float(md.growth.std()), growth_share_sd=float(gs.std()),
             sd_ratio=float(gs.std() / md.growth.std()),
             growth_max_abs=float(md.growth.abs().max()),
             growth_share_max_abs=float(gs.abs().max()),
             rows_abs_growth_share_gt5=int((gs.abs() > 5).sum()),
             rows_abs_growth_gt5=int((md.growth.abs() > 5).sum()),
             corr_in_window=float(md[md.in_window].growth.corr(md[md.in_window].growth_share)),
             note="log(1e-6/share) saturates at +-13.8 = +-ln(1e6); it is a boundary "
                  "artifact, not a growth rate")
    print(json.dumps(jsonable(d), indent=2))
    rec("E", "E5_growth_share_epsilon", d)

    sub("E6 share_V denominator and the silently-dropped share_V_platform")
    dt = md.groupby("posted_day").V.sum()
    w0 = pd.Timestamp(WINDOW_START, tz="UTC")
    d = dict(day_total_median_pre_window=float(dt[dt.index < w0].median()),
             day_total_median_in_window=float(dt[dt.index >= w0].median()),
             share_V_eq_1_rows=int((md.share_V == 1.0).sum()),
             share_V_platform_null_rows=int(md.share_V_platform.isna().sum()),
             share_V_platform_null_pct=float(100 * md.share_V_platform.isna().mean()),
             note="share_V pools ALL platforms and ALL topics including the 2011-2026 "
                  "YouTube backfill; share_V_platform is computed then lost on the "
                  "reindexed gap days and never used")
    print(json.dumps(jsonable(d), indent=2))
    rec("E", "E6_share_denominator", d)

    sub("E7 reach_proxy mixes raw view counts with log1p(E) in one column")
    rp = md.reach_proxy.dropna()
    print(md.groupby("platform").reach_proxy.describe()[["count", "mean", "50%", "max"]]
          .round(1).to_string())
    d = dict(n=len(rp), lt_1=int((rp < 1).sum()), between_1_100=int(((rp >= 1) & (rp < 100)).sum()),
             gt_1000=int((rp > 1000).sum()), max=float(rp.max()),
             note="engagement platforms get Sum(views) ~ 1e4-1e7; reddit/facebook/linkedin "
                  "get log1p(E) ~ 0-15. The column has two incompatible units.")
    print(json.dumps(jsonable(d), indent=2))
    rec("E", "E7_reach_proxy_units", d)

    sub("E8 EMA initialisation / burn-in bias in momentum")
    n_burn = tot = 0
    for _, g in md.groupby(["topic_id", "platform"]):
        g = g.sort_values("posted_day")
        start = g.posted_day.min()
        r = g[g.in_window]
        tot += len(r)
        n_burn += int(((r.posted_day - start).dt.days < 3 * V1_EMA_SLOW).sum())
    d = dict(in_window_rows=tot, rows_within_3x_slow_span_of_series_start=n_burn,
             pct=float(100 * n_burn / tot), momentum_exactly_zero_rows=int((md.momentum == 0).sum()),
             note="ewm(adjust=False) seeds at the first value, so momentum[0]==0 by "
                  "construction and the first ~63 days are biased toward 0")
    print(json.dumps(jsonable(d), indent=2))
    rec("E", "E8_ema_burn_in", d)

    sub("E9 half-life: R2 on the wrong scale, zeros dropped, absurd values accepted")
    hl = con.execute("SELECT * FROM half_life").df()
    print(hl.sort_values("r2", ascending=False).round(4).head(14).to_string())
    d = dict(rows=len(hl), ok=int(hl.ok.sum()), r2_negative=int((hl.r2 < 0).sum()),
             r2_null=int(hl.r2.isna().sum()), r2_median=float(hl.r2.median()),
             max_half_life_days=float(hl.half_life_days.max()),
             note="polyfit is on ln(y) but R2 is computed on the raw y scale, so R2<0 is "
                  "possible (7 rows); the y>0 mask drops decayed-to-zero days, biasing "
                  "lambda downward; peak is argmax over the WHOLE series including the "
                  "2011 backfill; a 1.16e17-day half-life is stored")
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("E", "E9_half_life", d)

    sub("E10 metrics_topic_day averages z-scores and sums author counts")
    td = con.execute("SELECT * FROM metrics_topic_day").df()
    d = dict(intensity_z_sd=float(td.intensity_z.std()), intensity_z_max=float(td.intensity_z.max()),
             n_authors_max=float(td.n_authors.max()),
             true_distinct_authors=int(con.execute(
                 "SELECT COUNT(DISTINCT author_uid) FROM post_facts").fetchone()[0]),
             note="mean of per-platform robust-z is not a z (sd=3.6); n_authors is summed "
                  "across platforms so cross-platform authors are double counted")
    print(json.dumps(jsonable(d), indent=2))
    rec("E", "E10_rollup", d)

    sub("E11 age normalisation explains essentially none of the variance")
    ap = con.execute("SELECT * FROM age_norm_params").df()
    print(ap.round(6).to_string())
    d = dict(max_r2=float(ap.r2.max()), min_r2=float(ap.r2.min()),
             all_flagged_ok=bool(ap.ok.all()),
             facebook_p=float(ap.loc[ap.platform == "facebook", "p"].iloc[0]),
             note="ok=True is gated on the p-value, not R2. R2 spans 0.0003-0.0355, so "
                  "age_normalise() subtracts a curve explaining <4% of variance. "
                  "05_metrics.py also computes E_age_norm and never uses it: "
                  "intensity_raw uses the RAW E_sum despite METRICS_SPEC.md claiming "
                  "'Age-norm residuals used in E where fitted'.")
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("E", "E11_age_normalisation", d)

    sub("E12 engagement_snapshots carries no temporal information")
    sn = con.execute("""SELECT extract_id, COUNT(*) n, MIN(observed_at) mn, MAX(observed_at) mx
                        FROM engagement_snapshots GROUP BY 1 ORDER BY 3""").df()
    print(sn.to_string())
    ts = pd.to_datetime(sn.mn)
    d = dict(n_extract_ids=len(sn),
             separation_seconds=float((ts.max() - ts.min()).total_seconds()),
             note="06_validate.py asserts 'append path works, per-post half-life "
                  "measurable' from n_extract_ids >= 2 alone. The two extracts are 25 s "
                  "apart, so per-post engagement accrual is NOT measurable. The check is "
                  "vacuous: it must assert a minimum separation in TIME.")
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("E", "E12_snapshots", d)

    sub("E13 author diversity is confounded with the collector's channel list")
    au = con.execute("""SELECT platform, COUNT(*) n, COUNT(DISTINCT author_uid) distinct_authors,
                        SUM(CASE WHEN author_uid IS NULL THEN 1 ELSE 0 END) null_author
                        FROM post_facts GROUP BY 1 ORDER BY 2 DESC""").df()
    print(au.to_string())
    d = dict(telegram_posts=int(au.loc[au.platform == "telegram", "n"].iloc[0]),
             telegram_distinct_authors=int(
                 au.loc[au.platform == "telegram", "distinct_authors"].iloc[0]),
             max_possible_telegram_entropy=float(np.log(
                 au.loc[au.platform == "telegram", "distinct_authors"].iloc[0])),
             note="14,595 telegram posts come from 28 channels, which IS the collector's "
                  "subscription list. Low author entropy therefore measures collection "
                  "design, not coordination. coordination_flag in the v2 schema would "
                  "fire on the collector, not on an adversary.")
    print("\n" + json.dumps(jsonable(d), indent=2))
    rec("E", "E13_author_diversity", d)

    sub("E14 no weekly seasonality (so baselines need no day-of-week term)")
    iw = md[md.in_window].copy()
    iw["dow"] = iw.posted_day.dt.dayofweek
    kw = stats.kruskal(*[np.log1p(g.V.to_numpy()) for _, g in iw[iw.V > 0].groupby("dow")])
    a7 = []
    for v in cell_series(md).values():
        x = np.log1p(v)
        a = acf(x - x.mean(), nlags=7, fft=False)
        a7.append(a[7] - np.mean(a[[5, 6]]))
    d = dict(kruskal_H=float(kw.statistic), kruskal_p=float(kw.pvalue),
             mean_acf7_minus_acf56=float(np.mean(a7)),
             verdict="no detectable day-of-week effect")
    print(json.dumps(jsonable(d), indent=2))
    rec("E", "E14_seasonality", d)


# ============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="subset of D1 D2 D3 D4 D5 B C MT E")
    ap.add_argument("--json", default=None, help="dump all recorded results to PATH")
    args = ap.parse_args()

    np.random.seed(SEED)
    con, md, posts, _ = load()
    print(f"analytics.duckdb (read-only): metrics_daily={len(md):,} rows, "
          f"post_facts(ts-scope)={len(posts):,} rows")
    print(f"seed={SEED}  window_start={WINDOW_START}  baseline_window={BASELINE_WIN}d  "
          f"scan_L={SCAN_L}d")

    order = ["D1", "D2", "D3", "D4", "D5", "B", "C", "MT", "E"]
    run = order if not args.only else [s for s in order if s in args.only]
    fns = {"D1": lambda: section_D1(md), "D2": lambda: section_D2(md),
           "D3": lambda: section_D3(md), "D4": lambda: section_D4(md),
           "D5": lambda: section_D5(md), "B": lambda: section_B(md, posts),
           "C": lambda: section_C(md, posts, con), "MT": lambda: section_MT(md, con),
           "E": lambda: section_E(md, posts, con)}
    for s in run:
        fns[s]()

    con.close()
    if args.json:
        Path(args.json).write_text(json.dumps(jsonable(RESULTS), indent=2))
        print(f"\nwrote {args.json}")
    print("\nstat_review complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
