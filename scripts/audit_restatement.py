#!/usr/bin/env python3
"""Anti-restatement audit: which of our metrics are just volume wearing a hat?

Borrowed from the sibling GDELT project
(`EDA_GDELT/MetricsCritique/Gdelt/docs/metrics_v13_and_v2.md`), which used it to
kill most of its own proposed metrics before shipping them. The test is blunt and
that is the point: correlate every published metric against the raw count it was
supposed to add information beyond. A metric that tracks volume at rho > 0.7 is
not a second opinion about the world, it is the same opinion in different units,
and ranking on it tells an analyst only that a category is busy.

Thresholds, theirs:  |rho| > 0.7  -> it IS volume, must not ship as a rank
                     0.3 - 0.7    -> contaminated, context column at best
                     < 0.3        -> clean

Reports BOTH Pearson and Spearman, and leads with Spearman. That is not
ceremony: daily counts here are heavy-tailed, and Pearson is inflated by the
tail. On the first run of this audit Pearson condemned `burst_intensity`
(+0.808) and `ar1_half_life_days` (+0.754) as pure volume; Spearman put them at
+0.448 and +0.494 -- contaminated, but not restatements. A single-statistic
version of this audit would have thrown out a working burst detector.

Read-only. Run:  .venv/bin/python scripts/audit_restatement.py
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "geo_ontology.duckdb"

# The count each metric is supposed to add information beyond.
VOLUME_COL = "V"

# Panels and their key column.
PANELS = [
    ("theme_metrics_daily", "theme_category"),
    ("risk_metrics_daily", "risk_category"),
    ("entity_metrics_daily", "entity_uid"),
]

# Candidate metrics. Anything published as a number a user can sort or read.
CANDIDATES = [
    "V_rate", "share_V", "growth", "growth_7d", "momentum", "accel", "accel_smooth",
    "surprise", "burst_intensity", "ar1_phi", "ar1_half_life_days",
    "avg_score_mean", "avg_score_eb", "gray_zone_share", "log_rate",
    # source concentration (added 2026-08-06, ports 3 & 4 of docs/metrics-critique.md).
    # `n_authors` is deliberately NOT here: it is a count, and auditing a count
    # against a count answers nothing. It is a denominator and a support column,
    # the same class as `support_n` and `score_n`. Measured +0.801 on theme when
    # it was briefly listed -- which is the expected answer, not a finding.
    "posts_per_author", "pie_author",
]

MIN_N = 30
IS_VOLUME, CONTAMINATED = 0.7, 0.3

# Restatements we have already looked at and accepted, with the reason. Anything
# NOT on this list that crosses the threshold fails the build -- that is the
# point of the gate: it catches the *next* one, not the ones we already know
# about. Removing an entry here is how you re-open the question.
KNOWN_RESTATEMENTS = {
    "V_rate": "by construction: V / effort, a rescaling of V",
    "log_rate": "by construction: log(V / effort), monotone in V_rate; model input, not a rank",
    "share_V": "KNOWN DEFECT, see docs/metrics-critique.md §4 finding 1 -- demoted to a "
               "context column in the UI, not yet replaced",
    "posts_per_author": "data-quality column, NOT a rank. The syndication ratio is "
                        "partly volume by construction (V / n_authors, and n_authors "
                        "grows with V). Measured +0.724 on risk, +0.5 elsewhere. The "
                        "GDELT project measured the same thing (+0.40 to +0.61, 'over "
                        "half volume') and shipped it on the same terms: it flags "
                        "one-account amplification, it does not rank importance.",
}


def verdict(rho: float) -> str:
    if not np.isfinite(rho):
        return "n/a"
    a = abs(rho)
    return "IS VOLUME" if a > IS_VOLUME else ("contaminated" if a > CONTAMINATED else "clean")


def audit_panels(panels: dict, volume_col: str = VOLUME_COL) -> tuple:
    """Correlate every candidate metric against volume, for already-built panels.

    Returns (rows, unexpected) where `rows` is a list of
    (panel, metric, pearson, spearman, n) and `unexpected` names any metric that
    crosses the restatement threshold WITHOUT being in KNOWN_RESTATEMENTS.

    Shared by the CLI below and by 11_geo_timeline_metrics.py, so the number in
    the build log and the number in the audit are the same number.
    """
    rows, unexpected = [], []
    for name, df in panels.items():
        if df is None or not len(df) or volume_col not in df.columns:
            continue
        sub = df[df["support_ok"]] if "support_ok" in df.columns else df
        for m in CANDIDATES:
            if m not in sub.columns:
                continue
            s = sub[[volume_col, m]].dropna()
            if len(s) < MIN_N or s[m].nunique() < 2:
                rows.append((name, m, np.nan, np.nan, len(s)))
                continue
            pear = float(np.corrcoef(s[volume_col], s[m])[0, 1])
            spear = float(spearmanr(s[volume_col], s[m]).statistic)
            rows.append((name, m, pear, spear, len(s)))
            # Spearman decides: daily counts are heavy-tailed and Pearson is
            # inflated by the tail (measured: burst_intensity +0.808 Pearson vs
            # +0.448 Spearman -- Pearson alone would condemn a working detector).
            if abs(spear) > IS_VOLUME and m not in KNOWN_RESTATEMENTS:
                unexpected.append(f"{name}.{m} (Spearman {spear:+.3f}, n={len(s)})")
    return rows, unexpected


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    panels = {}
    for table, _key in PANELS:
        try:
            panels[table] = con.execute(f"SELECT * FROM {table} WHERE support_ok").fetchdf()
        except duckdb.CatalogException:
            print(f"  (skipping {table}: not built)")

    rows, unexpected = audit_panels(panels)
    if not rows:
        print("no panels available -- run scripts/11_geo_timeline_metrics.py first")
        return 1
    res = pd.DataFrame(rows, columns=["table", "metric", "pearson", "spearman", "n"])

    for table in panels:
        sub = res[res["table"] == table]
        if not len(sub):
            continue
        sub = sub.reindex(sub["spearman"].abs().sort_values(ascending=False).index)
        print(f"\n=== {table} ===")
        print(f"  {'metric':<20} {'Pearson':>8} {'Spearman':>9} {'n':>7}  verdict")
        for r in sub.itertuples():
            v = verdict(r.spearman)
            tag = "  [known]" if r.metric in KNOWN_RESTATEMENTS else ""
            ps = "    n/a" if not np.isfinite(r.pearson) else f"{r.pearson:+8.3f}"
            ss = "     n/a" if not np.isfinite(r.spearman) else f"{r.spearman:+9.3f}"
            print(f"  {r.metric:<20} {ps} {ss} {r.n:>7,}  {v}{tag}")
            if np.isfinite(r.pearson) and verdict(r.pearson) != v:
                print(f"  {'':<20} {'':>8} {'':>9} {'':>7}  ^ Pearson says "
                      f"'{verdict(r.pearson)}' -- heavy tail; trust Spearman")

    print("\n" + "=" * 70)
    if unexpected:
        print(f"FAIL: {len(unexpected)} UNDECLARED volume restatement(s):")
        for u in unexpected:
            print(f"  - {u}")
        print("\nEither fix the metric or add it to KNOWN_RESTATEMENTS with a reason.")
        return 1
    print("no undeclared volume restatements")
    for m, why in KNOWN_RESTATEMENTS.items():
        print(f"  [known] {m}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
