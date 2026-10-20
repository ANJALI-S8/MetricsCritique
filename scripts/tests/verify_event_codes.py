#!/usr/bin/env python3
"""Post-run verification of the event_codes table written by 14_cameo_event_coding.py.

Checks the things that would otherwise be discovered by a user looking at a wrong
dashboard: that every class the taxonomy declares actually got scored, that the
threshold each class used is the one that was calibrated for it, and that classes
marked non-alertable are honestly reported rather than silently absent.

Read-only. Safe to run while other things hold the DB.

Run:  .venv/bin/python scripts/tests/verify_event_codes.py
"""
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import taxonomy_cameo  # noqa: E402

PASS, FAIL, WARN = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def note(name, detail=""):
    WARN.append(name)
    print(f"  NOTE  {name}" + (f"  {detail}" if detail else ""))


con = duckdb.connect(str(ROOT / "data" / "geo_ontology.duckdb"), read_only=True)

exists = con.execute(
    "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'event_codes'"
).fetchone()[0]
check("event_codes table exists", bool(exists))
if not exists:
    print("\n  0 checks passed -- run scripts/14_cameo_event_coding.py --level parent")
    sys.exit(1)

n_rows, n_posts, n_codes = con.execute(
    "SELECT count(*), count(DISTINCT post_uid), count(DISTINCT code) "
    "FROM event_codes WHERE axis = 'theme' AND level = 1"
).fetchone()
check("level-1 theme codes were written", n_rows > 0,
      f"{n_rows:,} rows, {n_posts:,} posts, {n_codes} distinct codes")

tax = taxonomy_cameo.build()
# codes is a flat list of 59 entries keyed by the collision-free `key`
# (code_key in the DB); numeric `code` repeats across axes/levels.
declared = [c["key"] for c in tax["codes"] if c["axis"] == "theme" and c["level"] == 1]
scored = {r[0] for r in con.execute(
    "SELECT DISTINCT code_key FROM event_codes WHERE axis='theme' AND level=1").fetchall()}

missing = [c for c in declared if c not in scored]
# A class with zero accepted posts is a real result, not a bug -- but it must be
# explainable, so surface it rather than let an empty panel speak for itself.
if missing:
    note(f"{len(missing)}/{len(declared)} declared classes accepted zero posts",
         ", ".join(missing))
else:
    check("every declared class accepted at least one post", True,
          f"{len(declared)}/{len(declared)}")

undeclared = sorted(scored - set(declared))
check("no code was written that the taxonomy does not declare",
      not undeclared, ", ".join(undeclared) or "clean")

# --- thresholds actually applied ------------------------------------------
thr_path = ROOT / "data" / "thresholds_cameo.json"
thresholds = json.loads(thr_path.read_text()) if thr_path.exists() else {}
# Per-class entries live under "classes"; the top level carries run metadata
# (fitted_at, global_fallback, ...), so iterating it directly finds nothing.
cal = {k for k, v in (thresholds.get("classes") or {}).items()
       if isinstance(v, dict) and v.get("status") == "calibrated"}

rows = con.execute(
    "SELECT code_key, threshold_source, count(*) AS n, "
    "       min(nli_threshold) AS t_min, max(nli_threshold) AS t_max "
    "FROM event_codes WHERE axis='theme' AND level=1 "
    "GROUP BY 1, 2 ORDER BY 1").fetchall()

print("\n  per-class results")
print(f"  {'code':<40} {'source':<16} {'n':>7}  threshold")
mismatched = []
for code, src, n, tmin, tmax in rows:
    band = f"{tmin:.3f}" if tmin == tmax else f"{tmin:.3f}-{tmax:.3f}"
    print(f"  {code:<40} {src or '?':<16} {n:>7,}  {band}")
    want = "fitted" if code in cal else "global_fallback"
    if src != want:
        mismatched.append(f"{code}: {src} (expected {want})")

check("each class used the threshold its calibration status implies",
      not mismatched, "; ".join(mismatched) or
      f"{len(cal)} fitted / {len(declared) - len(cal)} fallback")

check("every row carries a threshold source",
      con.execute("SELECT count(*) FROM event_codes "
                  "WHERE threshold_source IS NULL OR threshold_source = ''"
                  ).fetchone()[0] == 0)

check("no row scored below the threshold it was judged against",
      con.execute("SELECT count(*) FROM event_codes WHERE score < nli_threshold"
                  ).fetchone()[0] == 0)

# --- non-alertable classes must be honestly represented --------------------
non_alert = [c["key"] for c in tax["codes"] if c.get("alertable") is False]
if non_alert:
    present = [c for c in non_alert if c in scored]
    note("classes coded for the record but flagged non-alertable",
         ", ".join(present) or "none present in output")

# --- coverage sanity -------------------------------------------------------
total_posts = con.execute(
    "SELECT count(DISTINCT post_uid) FROM event_codes WHERE axis='theme' AND level=1"
).fetchone()[0]
multi = con.execute(
    "SELECT count(*) FROM (SELECT post_uid, count(*) c FROM event_codes "
    "WHERE axis='theme' AND level=1 GROUP BY 1 HAVING c >= 6)").fetchone()[0]
# The audit found 192 posts carrying >=6 of 9 risk categories -- the over-tagging
# that separating the axes was meant to reduce. Report it, do not assert a target.
note("posts carrying >=6 of 10 parent codes (audit baseline was 192 of 9)",
     f"{multi:,} of {total_posts:,}")

# --- cascade invariants (only meaningful once level 2 / actions exist) ------
n_sub = con.execute("SELECT count(*) FROM event_codes WHERE axis='theme' AND level=2").fetchone()[0]
n_act = con.execute("SELECT count(*) FROM event_codes WHERE axis='action'").fetchone()[0]

if n_sub:
    print(f"\n  cascade: {n_sub:,} sub-code rows, {n_act:,} action rows")

    # THE core guarantee of the cascade. If this fails, a sub-code was scored
    # against a post whose parent was never accepted -- the pruning map leaked,
    # and the hierarchy in the UI would show children with no parent.
    orphans = con.execute("""
        SELECT count(*) FROM event_codes s
        WHERE s.axis='theme' AND s.level=2
          AND NOT EXISTS (SELECT 1 FROM event_codes p
                          WHERE p.post_uid = s.post_uid AND p.axis='theme'
                            AND p.level=1 AND p.code = s.parent_code)
    """).fetchone()[0]
    check("every sub-code row sits under an accepted parent on the same post",
          orphans == 0, f"{orphans:,} orphaned sub-code rows")

    check("every sub-code declares a parent",
          con.execute("SELECT count(*) FROM event_codes WHERE axis='theme' "
                      "AND level=2 AND (parent_code IS NULL OR parent_code='')"
                      ).fetchone()[0] == 0)

    # A parent drill must return a superset of its children's posts. This is what
    # makes clicking a parent in the UI meaningful.
    bad = con.execute("""
        SELECT count(*) FROM (
          SELECT s.parent_code, s.post_uid FROM event_codes s
          WHERE s.axis='theme' AND s.level=2
          EXCEPT
          SELECT p.code, p.post_uid FROM event_codes p
          WHERE p.axis='theme' AND p.level=1)
    """).fetchone()[0]
    check("a parent drill returns a superset of its children's posts", bad == 0,
          f"{bad:,} child posts missing from the parent")

    sub_declared = {c["key"] for c in tax["codes"]
                    if c["axis"] == "theme" and c["level"] == 2}
    sub_scored = {r[0] for r in con.execute(
        "SELECT DISTINCT code_key FROM event_codes "
        "WHERE axis='theme' AND level=2").fetchall()}
    check("no sub-code was written that the taxonomy does not declare",
          not (sub_scored - sub_declared), ", ".join(sorted(sub_scored - sub_declared)) or "clean")
    empty = sorted(sub_declared - sub_scored)
    if empty:
        note(f"{len(empty)}/{len(sub_declared)} sub-codes accepted zero posts",
             ", ".join(empty[:6]) + (" ..." if len(empty) > 6 else ""))

if n_act:
    # Section B is conditional on Section A.
    stray = con.execute("""
        SELECT count(*) FROM event_codes a
        WHERE a.axis='action'
          AND NOT EXISTS (SELECT 1 FROM event_codes t
                          WHERE t.post_uid = a.post_uid AND t.axis='theme')
    """).fetchone()[0]
    check("no action code sits on a post with no thematic code", stray == 0,
          f"{stray:,} stray action rows")

    non_alert_act = [c["key"] for c in tax["codes"]
                     if c["axis"] == "action" and c.get("alertable") is False]
    if non_alert_act:
        note("action codes coded for the record but flagged non-alertable",
             ", ".join(non_alert_act))

# --- calibration coverage, stated honestly ---------------------------------
all_coded = {r[0] for r in con.execute(
    "SELECT DISTINCT code_key FROM event_codes").fetchall()}
uncal = [k for k in sorted(all_coded) if k not in cal]
if uncal:
    note(f"{len(uncal)} coded classes are UNCALIBRATED (global fallback threshold)",
         "precision unestablished for these; "
         + ", ".join(uncal[:5]) + (" ..." if len(uncal) > 5 else ""))

print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} checks passed"
      + (f", {len(WARN)} notes" if WARN else ""))
sys.exit(1 if FAIL else 0)
