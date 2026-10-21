#!/usr/bin/env python3
"""Stage 22 - build/refresh the publisher registry from the highest-volume domains (read-only).

  python scripts/22_news_publisher_registry.py --top 600 --since 2026-08-17

Writes data/publisher_registry.csv (domain, country_iso2, status, basis, n_docs, reviewed_by, verified_by).
`verified_by` is left EMPTY for you to fill in: only rows with a value are treated as verified.
Rows with a non-empty `verified_by` are preserved verbatim on refresh; all other rows are re-derived. Publisher location is NEVER
used for event location or trend metrics.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import news_publishers as PB  # noqa: E402
from lib.news_sources import ReadOnlyOpenSearch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=600)
    ap.add_argument("--since", default="2026-08-17")
    a = ap.parse_args()
    os_ = ReadOnlyOpenSearch()
    r = os_.search({"size": 0, "track_total_hits": True,
                    "query": {"range": {"published_at": {"gte": f"{a.since}T00:00:00Z"}}},
                    "aggs": {"d": {"terms": {"field": "source_domain", "size": a.top}}}})
    total = r["hits"]["total"]["value"]
    existing = PB.load_registry()
    merged: dict[str, dict] = {}
    for b in r["aggregations"]["d"]["buckets"]:
        d = PB.normalise_domain(b["key"])
        row = merged.get(d)
        n = b["doc_count"] + (int(row["n_docs"]) if row else 0)     # 'x.com:443' folds into 'x.com'
        c, status, basis = PB.classify(d)
        old = existing.get(d, {})
        if old.get("verified_by"):          # a human verified this row: never overwrite it
            merged[d] = {**old, "n_docs": n}
            continue
        merged[d] = {"domain": d, "country_iso2": c or "", "status": status, "basis": basis, "n_docs": n,
                     "reviewed_by": "claude (from outlet knowledge)" if status == "reviewed" else "", "verified_by": ""}
    rows = sorted(merged.values(), key=lambda x: -x["n_docs"])
    PB.REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PB.REGISTRY_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["domain", "country_iso2", "status", "basis", "n_docs", "reviewed_by", "verified_by"])
        w.writeheader()
        w.writerows(rows)
    by = Counter()
    for x in rows:
        by[x["status"] if x["country_iso2"] else "unknown"] += x["n_docs"]
    print(f"{len(rows)} domains written to {PB.REGISTRY_FILE}; corpus docs since {a.since}: {total:,}")
    for k, v in by.most_common():
        print(f"  {k:13s} {v:>10,} docs  ({100 * v / total:.1f}% of corpus)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
