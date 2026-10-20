#!/usr/bin/env python3
"""Identifier columns must have a declared type, never an inferred one.

This is a standing invariant, not a one-off check, because the same defect has
now appeared twice from the same cause: `CREATE TABLE ... AS SELECT *` types
each column from whatever the frame happened to hold, and a column that is NULL
for every row comes out INTEGER.

  1. `event_codes` (spec §9.8) got a VARCHAR `code` and an INTEGER
     `parent_code`. '05' became 5 and the zero-pad was lost. My own orphan check
     passed anyway, because DuckDB obligingly cast '01' -> 1 on comparison.

  2. `topic_parent_v4.parent_topic_id` came out INTEGER -- it is NULL for every
     row, since a parent has no parent -- while the identically-named column is
     VARCHAR in `topic_dim_v4` and `post_topic_v4`. Nothing was corrupted, but

         topic_dim_v4 d JOIN topic_parent_v4 p USING (parent_topic_id)

     resolved to a common type, matched nothing, and returned 0 rows *silently*.

Both times the failure mode was a wrong answer rather than a crash, which is why
a test is worth more here than care. An id is a name, not a number: nothing is
ever ordered by it or arithmetic done on it, so VARCHAR is correct even when the
values look numeric -- and especially when there are no values at all to look at.

Read-only. Run:  .venv/bin/python scripts/tests/test_id_column_types.py

Verifying this test (it must fail on a defective DB, or it is decoration):

    cp data/geo_ontology.duckdb /tmp/bad.duckdb
    .venv/bin/python -c "import duckdb; c=duckdb.connect('/tmp/bad.duckdb'); \\
        c.execute('ALTER TABLE topic_parent_v4 ALTER parent_topic_id TYPE INTEGER')"
    .venv/bin/python scripts/tests/test_id_column_types.py /tmp/bad.duckdb   # must exit 1

Confirmed doing exactly that on 2026-08-07: 2 failures, exit 1.
"""
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
# argv[1] lets this run against a copy -- used to confirm the check actually
# fails when the defect is present, since a check that only ever passes proves
# nothing. See the `verifying this test` note at the bottom.
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "geo_ontology.duckdb"

# column -> required SQL type, for every published table that carries it.
REQUIRED = {
    "post_uid": "VARCHAR",
    "topic_id": "VARCHAR",
    "parent_topic_id": "VARCHAR",
    "medoid_post_uid": "VARCHAR",
    "code": "VARCHAR",
    "code_key": "VARCHAR",
    "parent_code": "VARCHAR",
    "dominant_code": "VARCHAR",
    "entity_uid": "VARCHAR",
    "cluster_id": "BIGINT",
    "parent_cluster_id": "BIGINT",
}

TABLES = ["event_codes", "topic_dim_v4", "topic_parent_v4", "post_topic_v4",
          "topic_code_crosswalk"]

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


con = duckdb.connect(str(DB), read_only=True)
present = {r[0] for r in con.execute("SHOW TABLES").fetchall()}

seen_cols = {}
for t in TABLES:
    if t not in present:
        check(f"{t}: exists", False, "table missing -- run its build script")
        continue
    cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE {t}").fetchall()}
    bad = {c: ty for c, ty in cols.items()
           if c in REQUIRED and ty != REQUIRED[c]}
    check(f"{t}: identifier columns carry their declared type",
          not bad,
          "; ".join(f"{c} is {ty}, must be {REQUIRED[c]}" for c, ty in bad.items())
          or f"{sum(c in REQUIRED for c in cols)} id column(s) checked")
    for c, ty in cols.items():
        if c in REQUIRED:
            seen_cols.setdefault(c, {})[t] = ty

# The specific trap: one name, two types across tables. Even if both types were
# individually defensible, a join on the shared name would silently return zero.
for c, by_table in seen_cols.items():
    if len(by_table) < 2:
        continue
    check(f"`{c}` has one type everywhere it appears",
          len(set(by_table.values())) == 1,
          ", ".join(f"{t}={ty}" for t, ty in by_table.items()))

# Regression pin for defect 1: the zero-pad that the INTEGER type destroyed.
if "event_codes" in present:
    malformed = con.execute(
        "SELECT count(*) FROM event_codes WHERE parent_code IS NOT NULL "
        "AND length(parent_code) < 2").fetchone()[0]
    check("event_codes.parent_code keeps its zero-pad", malformed == 0,
          f"{malformed} rows shorter than 2 chars")
    orphans = con.execute(
        "SELECT count(*) FROM event_codes e WHERE e.parent_code IS NOT NULL AND NOT "
        "EXISTS (SELECT 1 FROM event_codes p WHERE p.code = e.parent_code "
        "AND p.axis = e.axis)").fetchone()[0]
    check("every parent_code resolves to a real parent row", orphans == 0,
          f"{orphans} orphans")

# Regression pin for defect 2: the hierarchy join must return the children.
if {"topic_dim_v4", "topic_parent_v4"} <= present:
    n = con.execute("SELECT count(*) FROM topic_dim_v4 d JOIN topic_parent_v4 p "
                    "ON d.parent_topic_id = p.topic_id").fetchone()[0]
    k = con.execute("SELECT count(*) FROM topic_dim_v4").fetchone()[0]
    check("every v4 sub-topic joins to its parent row", n == k and k > 0,
          f"{n} of {k} sub-topics matched")
    bad = con.execute("""SELECT count(*) FROM
        (SELECT parent_topic_id pid, sum(size) s, count(*) c FROM topic_dim_v4 GROUP BY 1) x
        JOIN topic_parent_v4 p ON p.topic_id = x.pid
        WHERE x.s <> p.size OR x.c <> p.n_subtopics""").fetchone()[0]
    check("each parent's size equals the sum of its children", bad == 0,
          f"{bad} parents disagree")

print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
