#!/usr/bin/env python3
"""Tests for the FDR family boundary (PARAMS["fdr_scope"]).

Two things need pinning:

  1. `fdr_scope` must actually be read. It sat in the params block for the whole
     life of the project without a single reader -- the family boundary was
     instead *structural*, equal to however many times build_dimension_panel
     happened to be called. A params entry that documents a choice the code does
     not make is worse than no entry.

  2. Pooling is NOT uniformly conservative. I initially wrote the opposite into
     the docstring as justification, and the real run disproved it (entity
     260 -> 266 while risk 95 -> 58). BH's q depends on rank within the family,
     so pooling can move a q either way. This file constructs the counterexample
     directly so the claim cannot quietly revert.

Run:  .venv/bin/python scripts/tests/test_fdr_family.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "metrics", ROOT / "scripts" / "11_geo_timeline_metrics.py")
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
S = metrics.S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def panel(pvals, day="2026-07-01"):
    return pd.DataFrame({
        "day": [day] * len(pvals),
        "_p_fdr": pvals,
        "support_ok": [True] * len(pvals),
        "surprise_q": [np.nan] * len(pvals),
    })


P = dict(metrics.PARAMS)

# --- 1. fdr_scope is read ---------------------------------------------------
check("fdr_scope is no longer dead config",
      "fdr_scope" in metrics.PARAMS
      and 'P.get("fdr_scope")' in Path(
          ROOT / "scripts" / "11_geo_timeline_metrics.py").read_text(),
      f"default={metrics.PARAMS['fdr_scope']!r}")
check("the shipped default pools across dimensions",
      metrics.PARAMS["fdr_scope"] == "day_global")

# --- 2. pooling actually merges the families --------------------------------
a, b = panel([0.001, 0.002]), panel([0.02, 0.90])
info = metrics.apply_fdr_family({"a": a, "b": b}, P)
check("all dimensions land in one family per day",
      info["families"] == 1 and info["n_tests"] == 4,
      f"{info['n_tests']} tests in {info['families']} family")
check("_p_fdr is consumed, not left on the panel",
      "_p_fdr" not in a.columns and "_p_fdr" not in b.columns)
check("every gated row gets a q back on its own panel",
      a["surprise_q"].notna().all() and b["surprise_q"].notna().all())

# --- 3. two days stay two families ------------------------------------------
two = pd.concat([panel([0.01], "2026-07-01"), panel([0.01], "2026-07-02")],
                ignore_index=True)
info2 = metrics.apply_fdr_family({"x": two}, P)
check("days are never pooled with each other", info2["families"] == 2,
      f"{info2['families']} families")

# --- 4. the counterexample: pooling can LOWER a q ---------------------------
# Alone: [0.04, 0.90] -> q(0.04) = 0.04 * 2/1 = 0.08, no alert at 0.05.
# Pooled with three strongly-significant tests, 0.04 drops to rank 4 of 5:
#   q = 0.04 * 5/4 = 0.05 -- lower than 0.08. Pooling helped it.
alone = panel([0.04, 0.90])
metrics.apply_fdr_family({"solo": alone}, P)
q_alone = float(alone.loc[0, "surprise_q"])

marginal, strong = panel([0.04, 0.90]), panel([0.001, 0.002, 0.003])
metrics.apply_fdr_family({"m": marginal, "s": strong}, P)
q_pooled = float(marginal.loc[0, "surprise_q"])

check("pooling can LOWER a marginal test's q (not uniformly conservative)",
      q_pooled < q_alone,
      f"q alone={q_alone:.4f} -> pooled={q_pooled:.4f}")

# And the opposite direction, so neither is assumed.
lonely = panel([0.04])
metrics.apply_fdr_family({"solo": lonely}, P)
q_lonely = float(lonely.loc[0, "surprise_q"])
big, noise = panel([0.04]), panel([0.5, 0.6, 0.7, 0.8])
metrics.apply_fdr_family({"b": big, "n": noise}, P)
check("pooling can also RAISE a q (the direction that suppresses alerts)",
      float(big.loc[0, "surprise_q"]) > q_lonely,
      f"q alone={q_lonely:.4f} -> pooled={float(big.loc[0, 'surprise_q']):.4f}")

# --- 5. the legacy scope still works ----------------------------------------
legacy = dict(P, fdr_scope="day")
c = panel([0.001, 0.002])
info3 = metrics.apply_fdr_family({"c": c}, legacy)
check("scope='day' still pools whatever it is handed (caller decides grouping)",
      info3["families"] == 1, "apply_fdr_family is the pooled path by construction")

# --- 6. rows failing the support gate are excluded from the family ----------
gated = panel([0.001, 0.002, 0.003])
gated.loc[1, "support_ok"] = False
info4 = metrics.apply_fdr_family({"g": gated}, P)
check("support-gate failures are not counted as tests", info4["n_tests"] == 2,
      f"{info4['n_tests']} of 3 rows entered the family")
check("a support-gate failure gets no q", pd.isna(gated.loc[1, "surprise_q"]))

print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
