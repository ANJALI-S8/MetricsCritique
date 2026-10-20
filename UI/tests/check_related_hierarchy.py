#!/usr/bin/env python3
"""Related-list hierarchy and slot-budget checks.

Two failure modes here are silent -- they produce a well-formed related list that
looks reasonable and is useless:

  1. A parent code co-occurs with its own child in 100% of the child's posts, so
     its lift is maximal by construction. Exact-key self-exclusion does not catch
     it, and the parent then tops the related list for every one of its sub-codes.
  2. Sub-codes are the narrowest categories and carry the highest lift, so an
     unbudgeted top-N over the axis union hands every slot to one axis.

Neither raises. Both need a direct assertion, which is what this file is.

Run:  .venv/bin/python UI/tests/check_related_hierarchy.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import api_server as api  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


# --- 1. exclusion tuple shape and hierarchy scoping -------------------------
def excl(view, **qs):
    return api._self_exclusions(view, {k: [v] for k, v in qs.items()})


e = excl("theme_subcode", code="05.2")
check("sub-code view excludes on the axis, not the dim_type",
      e[4] == ("theme", "05.2"), f"got {e[4]}")

e = excl("alert", dimension_type="theme_code", dimension_key="05")
check("alert drill on a CAMEO axis populates the 5th slot",
      e[4] == ("theme", "05"), f"got {e[4]}")

e = excl("risk", category="sanctions")
check("legacy views leave the CAMEO slot empty",
      e[4] == ("", "") and e[1:3] == ("risk", "sanctions"), f"got {e[1:3]} {e[4]}")

check("every routable CAMEO dimension has a related-axis mapping",
      all(d in api.CAMEO_RELATED_AXES
          for d in api.ALERT_ROUTES if d not in ("entity", "risk", "theme")),
      f"routes={sorted(api.ALERT_ROUTES)}")

# --- 2. the SQL predicate itself --------------------------------------------
# Exercise the three-way ancestor/self/descendant match directly, since this is
# the clause that decides whether a tautological pair reaches the UI.
con = api._main_con


def excluded(viewed: str, candidate: str, axis="theme", cand_axis="theme") -> bool:
    return not con.execute(
        "SELECT NOT (? = ? AND (? = ? OR ? LIKE ? || '.%' OR ? LIKE ? || '.%'))",
        [cand_axis, axis, candidate, viewed, viewed, candidate, candidate, viewed],
    ).fetchone()[0]


check("a code excludes itself", excluded("05.2", "05.2"))
check("a sub-code excludes its parent (the 100%-lift tautology)",
      excluded("05.2", "05"))
check("a parent excludes its descendants", excluded("05", "05.2"))
check("a grandparent chain is covered", excluded("05.2.1", "05"))
check("siblings are NOT excluded (they are real associations)",
      not excluded("05.2", "05.3"))
check("a code is not excluded by a lexical prefix twin",
      not excluded("05.2", "052"), "05.2 vs 052 must not match")
check("the other axis is untouched",
      not excluded("05.2", "05", axis="theme", cand_axis="action"))
check("empty sentinel excludes nothing",
      not excluded("", "05", axis=""), "unset view must not filter the list")

# --- 3. slot budget ---------------------------------------------------------
check("pre-CAMEO slot budget is unchanged (2 axes -> 3 each -> 6 total)",
      api._per_axis_cap(2) * 2 == api.RELATED_LIMIT, f"cap={api._per_axis_cap(2)}")
check("a full 5-axis union guarantees >= 3 axes are represented",
      api.RELATED_LIMIT / api._per_axis_cap(5) >= 3, f"cap={api._per_axis_cap(5)}")
check("no axis can ever monopolise the list",
      all(api._per_axis_cap(n) < api.RELATED_LIMIT for n in range(2, 8)))

# --- 4. the real query still runs on live data ------------------------------
for view, qs in [("risk", {"category": ["sanctions"]}),
                 ("theme", {"category": ["border_issues"]}),
                 ("entity", {"entity_uid": ["unresolved_white_house"]})]:
    try:
        cur = con.cursor()
        rows = api.compute_related(cur, view, qs, None)["categories"]
        dims = [r["dim_type"] for r in rows]
        n_axes = 2 + (len(api.CAMEO_RELATED_AXES) if api._has_table("event_codes") else 0)
        cap = api._per_axis_cap(n_axes)
        over = [d for d in set(dims) if dims.count(d) > cap]
        check(f"{view}: related query runs and respects the budget", not over,
              f"{len(rows)} rows, cap={cap}, dims={sorted(set(dims))}")
        check(f"{view}: no row is the viewed key itself",
              not any(r["dim_type"] == view and r["category"] == qs.get("category", [""])[0]
                      for r in rows),
              "self-exclusion held")
    except Exception as ex:  # noqa: BLE001
        check(f"{view}: related query runs", False, f"{type(ex).__name__}: {ex}")

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
