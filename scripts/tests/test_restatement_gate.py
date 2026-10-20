#!/usr/bin/env python3
"""Tests for the anti-restatement gate.

A gate that only ever passes is indistinguishable from no gate. The load-bearing
assertion here is the *negative* one: plant a metric that is a pure function of
volume and confirm the gate catches it.

Also pins the Spearman-over-Pearson decision. Daily counts are heavy-tailed, and
on the first real run Pearson condemned `burst_intensity` (+0.808) and
`ar1_half_life_days` (+0.754) as volume when Spearman put them at +0.448 and
+0.494. A gate keyed on Pearson would have failed the build over a working burst
detector, so the choice of statistic is itself a thing to protect.

Run:  .venv/bin/python scripts/tests/test_restatement_gate.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_restatement import (  # noqa: E402
    IS_VOLUME, KNOWN_RESTATEMENTS, audit_panels, verdict,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


rng = np.random.default_rng(11)
N = 200
V = rng.integers(1, 400, N).astype(float)


def panel(**cols):
    return pd.DataFrame({"V": V, "support_ok": True, **cols})


# --- 1. the negative case: a planted restatement must be caught --------------
# `growth` is not declared in KNOWN_RESTATEMENTS, so making it a function of V
# is exactly the situation the gate exists for.
_rows, unexpected = audit_panels({"planted": panel(growth=V * 3.0 + 1.0)})
check("a pure function of volume is caught", len(unexpected) == 1,
      unexpected[0] if unexpected else "NOT CAUGHT -- the gate is inert")

# Monotone but non-linear: Spearman still sees it, Pearson might not.
_rows, unexpected = audit_panels({"planted": panel(growth=np.log1p(V) ** 3)})
check("a monotone non-linear restatement is still caught", len(unexpected) == 1,
      "Spearman is rank-based, so a curve does not hide it")

# --- 2. the positive case: genuine signal must pass --------------------------
_rows, unexpected = audit_panels({"clean": panel(growth=rng.normal(0, 1, N))})
check("an independent metric passes", not unexpected)

# Weakly related but well under threshold -- must NOT fail the build.
_rows, unexpected = audit_panels(
    {"weak": panel(growth=0.25 * V + rng.normal(0, 250, N))})
check("a weakly correlated metric passes (gate is not hair-trigger)", not unexpected)

# --- 3. declared restatements are exempt, undeclared are not -----------------
_rows, unexpected = audit_panels({"declared": panel(V_rate=V * 2.0)})
check("a DECLARED restatement does not fail the build", not unexpected,
      "V_rate is in KNOWN_RESTATEMENTS")
check("every declared restatement carries a written reason",
      all(isinstance(v, str) and len(v) > 20 for v in KNOWN_RESTATEMENTS.values()),
      f"{len(KNOWN_RESTATEMENTS)} declared")
check("share_V is declared as a known DEFECT, not as by-construction",
      "DEFECT" in KNOWN_RESTATEMENTS.get("share_V", ""),
      "it must not be filed alongside the definitional ones")

# --- 4. Spearman, not Pearson, decides ---------------------------------------
# Construct the real-world case: a heavy tail that inflates Pearson while the
# rank correlation stays moderate.
heavy = np.concatenate([rng.normal(0, 1, N - 5), [80, 90, 100, 110, 120]])
Vh = np.concatenate([rng.integers(1, 20, N - 5).astype(float), [900.0] * 5])
df = pd.DataFrame({"V": Vh, "support_ok": True, "growth": heavy})
rows, unexpected = audit_panels({"tail": df})
pear = [r[2] for r in rows if r[1] == "growth"][0]
spear = [r[3] for r in rows if r[1] == "growth"][0]
check("a heavy tail inflates Pearson above Spearman", pear > spear,
      f"Pearson {pear:+.3f} vs Spearman {spear:+.3f}")
check("the verdict follows Spearman",
      (len(unexpected) > 0) == (abs(spear) > IS_VOLUME),
      f"flagged={bool(unexpected)}, |Spearman|>{IS_VOLUME} = {abs(spear) > IS_VOLUME}")

# --- 5. degenerate input must not crash or silently pass ---------------------
check("a constant metric is not reported as a restatement",
      not audit_panels({"const": panel(growth=np.full(N, 7.0))})[1],
      "zero variance -> undefined correlation, must not flag")
check("a too-small panel is skipped, not flagged",
      not audit_panels({"tiny": pd.DataFrame(
          {"V": [1.0, 2, 3], "support_ok": True, "growth": [3.0, 6, 9]})})[1],
      "n < MIN_N")
check("an empty panel is handled", not audit_panels({"empty": pd.DataFrame()})[1])
check("a panel with no volume column is skipped",
      not audit_panels({"novol": pd.DataFrame({"growth": [1.0] * 50})})[1])

# --- 6. only support-gated rows are audited ----------------------------------
mixed = pd.DataFrame({"V": np.r_[V, V], "support_ok": [True] * N + [False] * N,
                      "growth": np.r_[rng.normal(0, 1, N), V * 5.0]})
check("rows failing the support gate are excluded from the audit",
      not audit_panels({"mixed": mixed})[1],
      "the planted restatement sits entirely in support_ok=False rows")

# --- 7. thresholds match the source they were borrowed from ------------------
check("thresholds match the GDELT project's (0.7 / 0.3)",
      IS_VOLUME == 0.7 and verdict(0.71) == "IS VOLUME"
      and verdict(0.5) == "contaminated" and verdict(0.1) == "clean")

print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
