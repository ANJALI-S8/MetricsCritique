#!/usr/bin/env python3
"""
Layer 1 — Metric-readiness diagnostics on the local extract.

Reads data/analytics.duckdb + parquet only (no Postgres).
Writes reports/METRIC_READINESS.md and reports/figures/layer1_*.png
Persists age-normalisation fit params into DuckDB table age_norm_params.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DUCKDB_PATH, FIGURES_DIR, REPORTS_DIR, TS_EXCLUDE_PLATFORMS
from lib.duck import connect, replace_table_from_df

L = dict(
    surface="#fcfcfb",
    ink="#0b0b0b",
    ink2="#52514e",
    muted="#898781",
    grid="#e1e0d9",
    baseline="#c3c2b7",
)
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.size": 11,
        "text.color": L["ink"],
        "axes.edgecolor": L["baseline"],
        "axes.labelcolor": L["ink2"],
        "xtick.color": L["muted"],
        "ytick.color": L["muted"],
        "axes.grid": True,
        "grid.color": L["grid"],
        "grid.linewidth": 0.8,
        "figure.facecolor": L["surface"],
        "axes.facecolor": L["surface"],
        "savefig.facecolor": L["surface"],
    }
)


def style_ax(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(length=0)


def save_fig(fig, name: str) -> Path:
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIGURES_DIR / f"{name}.png"
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def primary_engagement(row) -> float:
    """Best single engagement signal for age curves / heavy tails."""
    for col in ("views", "likes", "shares", "comments"):
        v = row.get(col)
        if pd.notna(v) and float(v) >= 0:
            return float(v)
    return np.nan


def engagement_sum(row) -> float:
    vals = []
    for col in ("likes", "comments", "shares", "views"):
        v = row.get(col)
        if pd.notna(v) and float(v) >= 0:
            vals.append(float(v))
    return float(sum(vals)) if vals else np.nan


def fit_age_curve(ages: np.ndarray, eng: np.ndarray) -> dict:
    """Fit log1p(E) ~ a + b * log1p(age_days). Returns params + R²."""
    mask = np.isfinite(ages) & np.isfinite(eng) & (ages >= 0) & (eng >= 0)
    if mask.sum() < 20:
        return {"n": int(mask.sum()), "ok": False, "r2": None, "a": None, "b": None}
    x = np.log1p(ages[mask])
    y = np.log1p(eng[mask])
    # linear regression
    slope, intercept, r, p, se = stats.linregress(x, y)
    return {
        "n": int(mask.sum()),
        "ok": True,
        "r2": float(r**2),
        "a": float(intercept),
        "b": float(slope),
        "p": float(p),
        "formula": "log1p(E) = a + b * log1p(age_days)",
    }


def main() -> int:
    if not DUCKDB_PATH.exists():
        print("ERROR: run 01_extract_base.py first")
        return 1

    con = connect(read_only=False)
    posts = con.execute("SELECT * FROM post_facts").df()
    effort = con.execute("SELECT * FROM collection_effort").df()
    extract_meta = con.execute("SELECT * FROM extract_meta").df()
    snap_n = con.execute(
        "SELECT COUNT(DISTINCT extract_id) AS n FROM engagement_snapshots"
    ).fetchone()[0]

    print(f"post_facts: {len(posts):,}  extract_ids in snapshots: {snap_n}")

    posts["posted_at"] = pd.to_datetime(posts["posted_at"], utc=True, errors="coerce")
    posts["scraped_at"] = pd.to_datetime(posts["scraped_at"], utc=True, errors="coerce")
    posts["E_primary"] = posts.apply(primary_engagement, axis=1)
    posts["E_sum"] = posts.apply(engagement_sum, axis=1)
    posts["age_days"] = posts["scrape_lag_days"]
    posts["text_len"] = posts["text"].fillna("").map(len)
    posts["word_count"] = posts["text"].fillna("").map(lambda t: len(str(t).split()))
    posts["posted_day"] = posts["posted_at"].dt.floor("D")
    posts["scraped_day"] = posts["scraped_at"].dt.floor("D")

    findings = []
    fig_paths = []

    # ---- 1. Platform volume after dedup ----
    vol = posts.groupby("platform").size().sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(8, 4))
    colors = [CAT[i % len(CAT)] for i in range(len(vol))]
    ax.barh(vol.index[::-1], vol.values[::-1], color=colors[::-1])
    ax.set_xlabel("deduped posts")
    ax.set_title("Layer 0 post_facts volume by platform")
    style_ax(ax)
    fig_paths.append(save_fig(fig, "layer1_volume_by_platform"))
    findings.append(
        ("Deduped volume", {k: int(v) for k, v in vol.items()}, "Telegram dominates after dedup.")
    )

    # ---- 2. Scrape lag distribution ----
    lag_stats = []
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for i, plat in enumerate(vol.index):
        if plat in TS_EXCLUDE_PLATFORMS:
            continue
        s = posts.loc[posts["platform"] == plat, "scrape_lag_days"].dropna()
        s = s[(s >= 0) & (s < 400)]
        if len(s) < 5:
            continue
        lag_stats.append(
            {
                "platform": plat,
                "n": len(s),
                "median": float(s.median()),
                "p90": float(s.quantile(0.9)),
                "mean": float(s.mean()),
                "neg_share": float((posts.loc[posts["platform"] == plat, "scrape_lag_days"] < 0).mean()),
            }
        )
        ax.hist(s, bins=40, alpha=0.45, label=plat, color=CAT[i % len(CAT)], density=True)
    ax.set_xlabel("scrape lag (days)")
    ax.set_ylabel("density")
    ax.set_title("Scrape lag distribution by platform")
    ax.legend(fontsize=8)
    style_ax(ax)
    fig_paths.append(save_fig(fig, "layer1_scrape_lag"))

    # ---- 3. Engagement vs age curves ----
    age_params = []
    platforms_with_eng = [
        p
        for p in vol.index
        if p not in TS_EXCLUDE_PLATFORMS
        and p != "reddit"
        and posts.loc[posts["platform"] == p, "has_engagement"].any()
    ]
    n_plat = len(platforms_with_eng)
    if n_plat:
        ncols = min(3, n_plat)
        nrows = int(np.ceil(n_plat / ncols))
        fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.4 * nrows), squeeze=False)
        for idx, plat in enumerate(platforms_with_eng):
            ax = axes[idx // ncols][idx % ncols]
            sub = posts[posts["platform"] == plat]
            ages = sub["age_days"].to_numpy(dtype=float)
            eng = sub["E_primary"].to_numpy(dtype=float)
            fit = fit_age_curve(ages, eng)
            fit["platform"] = plat
            age_params.append(fit)
            mask = np.isfinite(ages) & np.isfinite(eng) & (ages >= 0) & (eng >= 0)
            if mask.sum():
                # subsample for plot
                ix = np.where(mask)[0]
                if len(ix) > 2000:
                    ix = np.random.default_rng(0).choice(ix, 2000, replace=False)
                ax.scatter(
                    np.log1p(ages[ix]),
                    np.log1p(eng[ix]),
                    s=6,
                    alpha=0.25,
                    color=CAT[idx % len(CAT)],
                )
                if fit["ok"]:
                    xs = np.linspace(0, np.nanmax(np.log1p(ages[mask])), 50)
                    ax.plot(xs, fit["a"] + fit["b"] * xs, color=L["ink"], lw=1.5)
                    ax.set_title(f"{plat}  R²={fit['r2']:.2f}  n={fit['n']}")
                else:
                    ax.set_title(f"{plat}  (insufficient n)")
            ax.set_xlabel("log1p(age days)")
            ax.set_ylabel("log1p(E)")
            style_ax(ax)
        for j in range(idx + 1, nrows * ncols):
            axes[j // ncols][j % ncols].axis("off")
        fig_paths.append(save_fig(fig, "layer1_engagement_vs_age"))

    age_df = pd.DataFrame(age_params)
    if not age_df.empty:
        replace_table_from_df(con, "age_norm_params", age_df)

    # ---- 4. Collection effort vs volume ----
    if not effort.empty:
        effort["day"] = pd.to_datetime(effort["day"], utc=True, errors="coerce")
        vol_day = (
            posts.dropna(subset=["scraped_day"])
            .groupby(["platform", "scraped_day"])
            .size()
            .reset_index(name="volume")
        )
        # merge effort
        eff = effort.copy()
        eff["day"] = pd.to_datetime(eff["day"], utc=True).dt.floor("D")
        vol_day["scraped_day"] = pd.to_datetime(vol_day["scraped_day"], utc=True).dt.floor("D")
        merged = vol_day.merge(
            eff,
            left_on=["platform", "scraped_day"],
            right_on=["platform", "day"],
            how="inner",
        )
        if not merged.empty:
            fig, ax = plt.subplots(figsize=(8, 4.5))
            for i, plat in enumerate(merged["platform"].unique()):
                m = merged[merged["platform"] == plat]
                ax.scatter(
                    m["active_channels"].fillna(0),
                    m["volume"],
                    label=plat,
                    alpha=0.7,
                    color=CAT[i % len(CAT)],
                    s=30,
                )
            ax.set_xlabel("active channels / authors that day")
            ax.set_ylabel("scraped volume")
            ax.set_title("Collection effort vs volume (by platform-day)")
            ax.legend(fontsize=8, ncol=2)
            style_ax(ax)
            fig_paths.append(save_fig(fig, "layer1_effort_vs_volume"))

    # ---- 5. Panel density (platform × day) ----
    dens = (
        posts.dropna(subset=["posted_day"])
        .groupby(["platform", "posted_day"])
        .size()
        .reset_index(name="n")
    )
    if not dens.empty:
        pivot = dens.pivot(index="posted_day", columns="platform", values="n").fillna(0)
        # last ~60 days
        pivot = pivot.sort_index().tail(60)
        fig, ax = plt.subplots(figsize=(10, 5))
        im = ax.imshow(np.log1p(pivot.T.values), aspect="auto", cmap="Blues")
        ax.set_yticks(range(len(pivot.columns)))
        ax.set_yticklabels(pivot.columns)
        # sparse x labels
        xt = list(range(0, len(pivot), max(1, len(pivot) // 8)))
        ax.set_xticks(xt)
        ax.set_xticklabels([str(pivot.index[i].date()) for i in xt], rotation=45, ha="right")
        ax.set_title("Panel density: log1p(posts) by platform × posted day")
        fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
        fig_paths.append(save_fig(fig, "layer1_panel_density"))
        # trustable cells: platforms with median daily n >= 5 over last 21d
        recent = dens[dens["posted_day"] >= dens["posted_day"].max() - pd.Timedelta(days=21)]
        trust = (
            recent.groupby("platform")["n"].median().sort_values(ascending=False)
            if not recent.empty
            else pd.Series(dtype=float)
        )
    else:
        trust = pd.Series(dtype=float)

    # ---- 6. Language × platform ----
    lang_rows = []
    for plat, g in posts.groupby("platform"):
        vc = g["lang"].fillna("(null)").replace("", "(null)").value_counts().head(8)
        for lang, n in vc.items():
            lang_rows.append({"platform": plat, "lang": lang, "n": int(n)})
    lang_df = pd.DataFrame(lang_rows)

    # ---- 7. Text length / readability coverage ----
    READ_MIN_WORDS = 25
    cov_rows = []
    for plat, g in posts.groupby("platform"):
        n = len(g)
        scorable = int((g["word_count"] >= READ_MIN_WORDS).sum())
        cov_rows.append(
            {
                "platform": plat,
                "n": n,
                "median_chars": float(g["text_len"].median()),
                "median_words": float(g["word_count"].median()),
                "scorable_ge_25w": scorable,
                "scorable_pct": 100.0 * scorable / n if n else 0,
            }
        )
    cov_df = pd.DataFrame(cov_rows).sort_values("scorable_pct")

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.barh(cov_df["platform"], cov_df["scorable_pct"], color=CAT[0])
    ax.axvline(50, color=L["muted"], ls="--", lw=1)
    ax.set_xlabel("% posts with ≥25 words (readability-eligible)")
    ax.set_title("Readability coverage floor")
    style_ax(ax)
    fig_paths.append(save_fig(fig, "layer1_readability_coverage"))

    # ---- 8. Heavy-tail rank plots ----
    ht_plats = [p for p in platforms_with_eng if p in ("telegram", "youtube_video", "instagram", "facebook", "linkedin")]
    if ht_plats:
        fig, axes = plt.subplots(1, len(ht_plats), figsize=(3.5 * len(ht_plats), 3.5), squeeze=False)
        for i, plat in enumerate(ht_plats):
            ax = axes[0][i]
            e = posts.loc[posts["platform"] == plat, "E_primary"].dropna()
            e = e[e > 0].sort_values(ascending=False).to_numpy()
            if len(e) < 10:
                ax.set_title(plat)
                continue
            rank = np.arange(1, len(e) + 1)
            ax.loglog(rank, e, color=CAT[i % len(CAT)], lw=1.2)
            ax.set_xlabel("rank")
            ax.set_ylabel("engagement")
            ax.set_title(plat)
            style_ax(ax)
        fig_paths.append(save_fig(fig, "layer1_heavy_tails"))

    # ---- Per-metric feasibility table ----
    metrics_status = [
        {
            "metric": "Growth rate",
            "status": "ready",
            "notes": "Deduped daily volume + share-of-volume; normalise by collection_effort. Cap long windows at 21d.",
        },
        {
            "metric": "Momentum",
            "status": "ready",
            "notes": "EMA_7 − EMA_21 on growth; ~6wk history → 21d cap is the max sensible long window.",
        },
        {
            "metric": "Half-life / persistence",
            "status": "partial",
            "notes": f"Aggregate decay on topic×day volume ready after ontology. Per-post decay needs snapshot accrual (currently {snap_n} extract_id(s)).",
        },
        {
            "metric": "Intensity",
            "status": "ready_after_age_norm",
            "notes": "Use age-norm params from this layer; robust z vs 28d baseline of entity.",
        },
        {
            "metric": "Tone z-score",
            "status": "needs_layer3",
            "notes": "Requires multilingual sentiment scores.",
        },
        {
            "metric": "Virality",
            "status": "partial",
            "notes": "Per-platform formulas + age-norm. Reddit excluded (no engagement). Snapchat/Pinterest excluded from TS.",
        },
        {
            "metric": "Reach / author diversity",
            "status": "ready",
            "notes": "Reach is a PROXY (Σviews or Σlog1p(E)). Diversity = Shannon / 1−HHI / distinct authors.",
        },
        {
            "metric": "Readability",
            "status": "partial",
            "notes": "Length-gate ≥25 words; coverage low on short posts. zh/ja N/A for syllable scores.",
        },
        {
            "metric": "Ontology",
            "status": "needs_layer2",
            "notes": f"Embeddings present for {int(con.execute('SELECT COUNT(*) FROM post_embeddings_meta').fetchone()[0]):,} posts; youtube_video embeddings=0 → compute bge-m3 locally.",
        },
        {
            "metric": "Hate-speech score",
            "status": "needs_layer3_plus_calibration",
            "notes": "War/terror corpus → off-the-shelf toxicity will over-fire; calibrate before publishing.",
        },
    ]

    # ---- Write METRIC_READINESS.md ----
    lines = []
    lines.append("# Metric Readiness — Layer 1 Diagnostics")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if not extract_meta.empty:
        lines.append(f"Source extract_id: `{extract_meta.iloc[-1]['extract_id']}`")
        lines.append(f"Snapshot: `{extract_meta.iloc[-1]['snapshot_ts']}`")
    lines.append("")
    lines.append("## Deduped corpus")
    lines.append("")
    lines.append("| Platform | Posts |")
    lines.append("|---|---:|")
    for plat, n in vol.items():
        lines.append(f"| {plat} | {int(n):,} |")
    lines.append(f"| **total** | **{len(posts):,}** |")
    lines.append("")
    lines.append(
        f"Telegram raw was ~23.3k → **{int(vol.get('telegram', 0)):,}** after dedup "
        f"(~{100 * (1 - vol.get('telegram', 0) / 23325):.0f}% removed). Matches plan finding #2."
    )
    lines.append("")
    lines.append("## Scrape lag")
    lines.append("")
    if lag_stats:
        lines.append("| Platform | n | median days | p90 | mean | neg lag share |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for r in sorted(lag_stats, key=lambda x: -x["median"]):
            lines.append(
                f"| {r['platform']} | {r['n']:,} | {r['median']:.1f} | {r['p90']:.1f} | "
                f"{r['mean']:.1f} | {100*r['neg_share']:.1f}% |"
            )
    lines.append("")
    lines.append("Engagement is **age-confounded**. Age-normalisation curves are required before cross-age virality/intensity.")
    lines.append("")
    lines.append("## Age-normalisation fits (`log1p(E) = a + b·log1p(age)`)")
    lines.append("")
    if not age_df.empty:
        lines.append("| Platform | n | R² | a | b | usable |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for _, r in age_df.iterrows():
            if r.get("ok"):
                lines.append(
                    f"| {r['platform']} | {int(r['n']):,} | {r['r2']:.3f} | {r['a']:.3f} | {r['b']:.3f} | yes |"
                )
            else:
                lines.append(f"| {r['platform']} | {int(r.get('n') or 0):,} | — | — | — | no |")
        lines.append("")
        lines.append("Stored in DuckDB table `age_norm_params`.")
    lines.append("")
    lines.append("## Panel density (last 21d median posts/day)")
    lines.append("")
    if len(trust):
        lines.append("| Platform | median daily volume | trustworthy for daily metrics? |")
        lines.append("|---|---:|---|")
        for plat, med in trust.items():
            ok = "yes" if med >= 5 else "sparse"
            if plat in TS_EXCLUDE_PLATFORMS:
                ok = "exclude (coverage)"
            lines.append(f"| {plat} | {med:.1f} | {ok} |")
    lines.append("")
    lines.append("## Language mix (top codes)")
    lines.append("")
    if not lang_df.empty:
        for plat in vol.index:
            sub = lang_df[lang_df["platform"] == plat].head(5)
            if sub.empty:
                continue
            top = ", ".join(f"{r.lang}={r.n}" for r in sub.itertuples())
            lines.append(f"- **{plat}:** {top}")
    lines.append("")
    lines.append("## Readability coverage (≥25 words)")
    lines.append("")
    lines.append("| Platform | n | median chars | median words | scorable % |")
    lines.append("|---|---:|---:|---:|---:|")
    for _, r in cov_df.iterrows():
        lines.append(
            f"| {r['platform']} | {int(r['n']):,} | {r['median_chars']:.0f} | "
            f"{r['median_words']:.0f} | {r['scorable_pct']:.1f}% |"
        )
    lines.append("")
    lines.append("## Per-metric readiness")
    lines.append("")
    lines.append("| Metric | Status | Notes |")
    lines.append("|---|---|---|")
    for m in metrics_status:
        lines.append(f"| {m['metric']} | `{m['status']}` | {m['notes']} |")
    lines.append("")
    lines.append("## Figures")
    lines.append("")
    for p in fig_paths:
        lines.append(f"- `{p.relative_to(REPORTS_DIR.parent)}`")
    lines.append("")
    lines.append("## Next")
    lines.append("")
    lines.append("1. Layer 2 — ontology: embed youtube_videos with bge-m3, cluster, assign Telegram.")
    lines.append("2. Layer 3 — multilingual tone / hate / readability scores.")
    lines.append("3. Layer 4 — compute the 10 metrics at (topic × platform × day).")
    lines.append("")

    out = REPORTS_DIR / "METRIC_READINESS.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")

    # Persist a small JSON summary too
    summary = {
        "n_posts": len(posts),
        "by_platform": {k: int(v) for k, v in vol.items()},
        "age_params": age_params,
        "lag_stats": lag_stats,
        "readability_coverage": cov_rows,
        "metrics_status": metrics_status,
        "snapshot_extract_ids": int(snap_n),
    }
    (REPORTS_DIR / "metric_readiness_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    con.close()
    print("Layer 1 complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
