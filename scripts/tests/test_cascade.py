#!/usr/bin/env python3
"""Unit tests for the cascade's candidate pruning (stages 2 and 3).

A bug here is expensive in a specific way: it does not crash, it just scores the
wrong grid for two hours and writes plausible-looking rows. The two failure modes
that matter are opposites --

  - pruning too little: a sub-code gets tested on posts whose parent was never
    accepted, which is both wasteful and wrong (a maritime sub-code has no
    business being evaluated on a non-maritime post);
  - pruning too much: a sub-code silently gets zero candidates, which looks
    exactly like "this class is rare" in the output.

Run:  .venv/bin/python scripts/tests/test_cascade.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "cameo", ROOT / "scripts" / "14_cameo_event_coding.py")
cameo = importlib.util.module_from_spec(spec)
# Importing runs module-level constants only; main() is behind __main__.
spec.loader.exec_module(cameo)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def code(key, axis, level, num, parent=None):
    return {"key": key, "axis": axis, "level": level, "code": num,
            "parent_code": parent, "description": key, "hypothesis": key}


# Two parents, two sub-codes each, two actions.
SUBS = [code("a05_1_naval", "theme", 2, "05.1", "05"),
        code("a05_2_disputes", "theme", 2, "05.2", "05"),
        code("a07_1_borders", "theme", 2, "07.1", "07"),
        code("a07_2_incidents", "theme", 2, "07.2", "07")]
ACTS = [code("b18_assault", "action", 1, "18"),
        code("b14_protest", "action", 1, "14")]

# p1 and p2 accepted parent 05; p3 accepted 07. p4 accepted nothing.
KEPT = pd.DataFrame({"post_uid": ["p1", "p2", "p3"], "code": ["05", "05", "07"]})

# --- stage 2 routing --------------------------------------------------------
allowed = cameo.allowed_for_subcodes(KEPT, SUBS)
check("a sub-code is offered exactly its parent's accepted posts",
      allowed["a05_1_naval"] == {"p1", "p2"} and allowed["a07_1_borders"] == {"p3"},
      f"05.1 -> {sorted(allowed['a05_1_naval'])}, 07.1 -> {sorted(allowed['a07_1_borders'])}")
check("siblings under one parent share that parent's post set",
      allowed["a05_1_naval"] == allowed["a05_2_disputes"])
check("a post that accepted no parent is offered to no sub-code",
      not any("p4" in v for v in allowed.values()))

orphan = cameo.allowed_for_subcodes(KEPT, [code("a09_1_x", "theme", 2, "09.1", "09")])
check("a sub-code whose parent was never accepted gets an empty set, not everything",
      orphan["a09_1_x"] == set(), f"got {orphan['a09_1_x']}")

# --- stage 3 routing --------------------------------------------------------
theme_kept = pd.DataFrame({"post_uid": ["p1", "p3"], "code": ["05", "07"]})
act_allowed = cameo.allowed_for_actions(theme_kept, ACTS)
check("actions are offered only posts carrying >=1 thematic code",
      act_allowed["b18_assault"] == {"p1", "p3"}
      and act_allowed["b14_protest"] == {"p1", "p3"})
check("Section B is conditional: a post with no Section A code gets no action",
      "p2" not in act_allowed["b18_assault"])

# --- build_candidates honours the map ---------------------------------------
posts = pd.DataFrame({"post_uid": ["p1", "p2", "p3", "p4"]})
# Every sim above the floor, so any pruning observed is the map's doing alone.
sims = np.full((4, len(SUBS)), 0.90, dtype=np.float32)

cand = cameo.build_candidates(posts, sims, SUBS, allowed=allowed, label="test")
got = {(r.post_uid, r.code_key) for r in cand.itertuples()}
check("pruned grid contains only parent-accepted pairs",
      got == {("p1", "a05_1_naval"), ("p2", "a05_1_naval"),
              ("p1", "a05_2_disputes"), ("p2", "a05_2_disputes"),
              ("p3", "a07_1_borders"), ("p3", "a07_2_incidents")},
      f"{len(got)} pairs")
check("pruning is what shrank it, not the floor",
      len(cand) == 6 and len(posts) * len(SUBS) == 16,
      f"{len(cand)} of {len(posts) * len(SUBS)} possible")

unpruned = cameo.build_candidates(posts, sims, SUBS, allowed=None, label="test")
check("allowed=None means no pruning (stage 1 behaviour is unchanged)",
      len(unpruned) == 16, f"{len(unpruned)} pairs")

# The floor must still apply *inside* the allowed set.
low = np.full((4, len(SUBS)), 0.10, dtype=np.float32)
check("the bi-encoder floor still applies within the allowed set",
      len(cameo.build_candidates(posts, low, SUBS, allowed=allowed, label="test")) == 0)

# --- emitted columns are what write_event_codes needs -----------------------
check("candidates carry the parent link and level needed by the hierarchy",
      list(cand.columns) == cameo.CAND_COLS
      and set(cand["level"]) == {2}
      and cand[cand.code_key == "a05_1_naval"]["parent_code"].iloc[0] == "05",
      ", ".join(cand.columns))

# --- checkpoint tags must not collide across stages -------------------------
import inspect  # noqa: E402
src = inspect.getsource(cameo.run_nli)
check("run_nli builds its checkpoint tag from the stage as well as the count",
      'f"{stage}_n{n}_cs{chunk_size}"' in src,
      "two stages with equal pair counts cannot share a checkpoint")

print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
