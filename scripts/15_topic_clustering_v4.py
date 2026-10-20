#!/usr/bin/env python3
"""Hierarchical topic clustering on GPU -- parent topics and sub-topics.

WHY THIS FILE EXISTS
--------------------
`topic_dim_v3` / `post_topic_v3` are what the product serves, and until now
nothing in `scripts/` could rebuild them -- they existed only as ad-hoc
scratchpad pickles. That is a reproducibility defect of mine, recorded in
`docs/GEOPOLITICAL_ONTOLOGY_SPEC.md`. This script is the fix: it rebuilds the
topic layer end-to-end from `post_embeddings`, and it writes **v4** rather than
overwriting v3, so the serving tables are never left in a half-built state and
the two can be compared.

WHAT CHANGED FROM v3, AND WHY (all four are measured, not asserted)
-------------------------------------------------------------------
1. **`MIN_CLUSTER_SIZE` is one explicit constant.** `09_ontology_v2_cluster_fix.py`
   declared it "a FIXED ABSOLUTE CONSTANT ... does not scale with N" and then
   overrode it from a hardcoded ladder; `topic_dim_v2` shows the value actually
   used was 292, 19x the declared 15. The N-coupled coarsening failure mode was
   laundered through the ladder rather than removed. Here the value is chosen by
   a measured stability sweep (see below) and stated once.

2. **Silhouette is computed in the ORIGINAL 1024-d cosine space**, never in UMAP
   space. UMAP is explicitly optimised to exaggerate separation, so scoring there
   inflates. Measured: v3 stored 0.395; the same clustering scored in the real
   embedding space lands at 0.04-0.09. The honest number is the small one.

3. **Stability is measured, not left null.** `reports/ontology_summary.json` has
   `"stability_ari": null` for the umap_hdbscan path. This runs N_SEEDS UMAP+HDBSCAN
   fits and reports pairwise adjusted Rand index. This is what selected
   MIN_CLUSTER_SIZE:

       mcs   k across 5 seeds            mean ARI   min ARI
        15   [280,290,291,276,287]          0.552     0.534
        20   [215,234,212,214,223]          0.578     0.563
        25   [166,169,158,157,170]          0.589     0.557
        30   [136,133,126,131,141]          0.595     0.556   <- chosen
        40   [109,107, 94,102,115]          0.575     0.525
        60   [ 73, 71,  8, 70,  8]          0.288     0.017   <- CLIFF

   mcs=60 is not "coarser", it is *broken*: two of five seeds collapse to 8
   clusters and pairwise ARI reaches 0.017, which is chance agreement. The v3
   production clustering (k=33) sits in that coarse region. Anything above ~40
   here is a landmine, and that is exactly the kind of thing a hardcoded ladder
   walks into silently.

4. **Hierarchy is built by agglomerating cluster centroids, not by a second
   HDBSCAN pass.** A coarse HDBSCAN run would land on the cliff above. Ward
   linkage over the fine clusters' centroids is deterministic given the clusters,
   so the parent level adds no new instability.

GPU
---
cuML (RAPIDS) UMAP + HDBSCAN. Measured on an L40S: UMAP 21,228x1024 -> 10d in
1.3 s, HDBSCAN in 0.1 s. The whole multi-seed stability sweep costs seconds, which
is why it is run every time rather than being a thing we promise to do later.
Falls back to CPU umap-learn / hdbscan if cuML is unavailable.

Run:
    .venv/bin/python scripts/15_topic_clustering_v4.py            # full build
    .venv/bin/python scripts/15_topic_clustering_v4.py --sweep    # sweep only, no write
    .venv/bin/python scripts/15_topic_clustering_v4.py --seeds 5  # stability seeds
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC_DB = ROOT / "data" / "analytics.duckdb"
OUT_DB = ROOT / "data" / "geo_ontology.duckdb"
REPORT = ROOT / "reports" / "ontology_v4_summary.json"

# ---- parameters. Every one of these is a stated choice, not a ladder. -------
UMAP_COMPONENTS = 10
UMAP_NEIGHBORS = 15
UMAP_MIN_DIST = 0.0
UMAP_METRIC = "cosine"

# Selected by the stability sweep in the module docstring: highest mean pairwise
# ARI (0.595) with the cliff at 60 comfortably avoided. NOT scaled by N, and not
# overridden anywhere below -- if you want a different granularity, change this
# number and re-run --sweep to see what it costs.
MIN_CLUSTER_SIZE = 30
MIN_SAMPLES = 5

N_SEEDS = 5           # stability seeds; ARI is reported across all pairs
PRIMARY_SEED = 42

# Parent level: agglomerate fine-cluster centroids. Chosen so parents stay
# human-scannable while each holds a handful of sub-topics.
N_PARENTS = 24

# A cluster is flagged INCOHERENT if it is not a real grouping: one near-identical
# text repeated, or too small to judge.
#
# MAX_AUTHOR_SHARE is 0.90, not the 0.60 first tried. Measured across the 130
# clusters, top-author share has median 0.565 and p90 0.776 -- this corpus is
# author-concentrated by nature (the metrics critique measured 7.28 posts/author
# in maritime_security, with three accounts dominating). A 0.60 cut therefore
# flagged 59 of 130 clusters, including h039 "geran/нпз/дронов" -- 975 posts of
# real Russian drone/refinery-strike reporting whose only sin is that one outlet
# files most of it. Encoding the corpus norm as an anomaly is not a quality gate.
# 0.90 flags genuine single-source clusters against a median of 0.565.
MAX_AUTHOR_SHARE = 0.90
MAX_DUP_TEXT_SHARE = 0.35
MIN_COHERENT_SIZE = 20

# Separate and deliberately NOT folded into `coherent`: a cluster can be a
# perfectly real grouping that our geopolitical taxonomy simply does not cover.
# Measured: five ransomware-victim clusters (~470 posts) and a Reddit
# moderation-boilerplate cluster receive a CAMEO theme code on ~0% of their
# posts, against a corpus median of 0.287. That is an objective disqualification
# for the artifact clusters v3 had to annotate by hand -- but calling them
# "incoherent" would be wrong. They cohere; they are off-topic.
MIN_CODED_SHARE = 0.05

LABEL_TERMS = 6
SIL_SAMPLE = 5000

# Columns whose SQL type must be declared, never inferred. `CREATE TABLE AS
# SELECT *` types each column from what the frame happens to hold, and an
# all-NULL column comes out INTEGER. That already bit us once: `event_codes`
# ended up with a VARCHAR `code` and an INTEGER `parent_code`, so '05' became 5
# and the zero-pad was lost (spec §9.8).
#
# Here the same inference typed `topic_parent_v4.parent_topic_id` INTEGER -- it
# is NULL for every row, because a parent has no parent -- while the identical
# column is VARCHAR in the other two tables. Nothing was corrupted, but the
# failure mode is worse than a crash: DuckDB resolves
#   topic_dim_v4 d JOIN topic_parent_v4 p USING (parent_topic_id)
# to a common type, matches nothing, and returns **0 rows silently**. A join
# that looks right and answers wrong is exactly the trap the explicit-DDL rule
# exists to prevent, so ids are pinned by name whether or not they are populated.
ID_TYPES = {
    "topic_id": "VARCHAR", "parent_topic_id": "VARCHAR", "post_uid": "VARCHAR",
    "cluster_id": "BIGINT", "parent_cluster_id": "BIGINT",
    "dominant_code": "VARCHAR", "medoid_post_uid": "VARCHAR",
}


def _projection(payload: pd.DataFrame) -> str:
    """SELECT list that casts identifier columns to their declared type."""
    return ", ".join(
        f'CAST("{c}" AS {ID_TYPES[c]}) AS "{c}"' if c in ID_TYPES else f'"{c}"'
        for c in payload.columns)


def log(msg: str) -> None:
    print(f"[15] {msg}", flush=True)


# ============================================================================
# GPU / CPU backend
# ============================================================================
def get_backend() -> tuple:
    """(umap_fn, hdbscan_fn, name). cuML if importable, else CPU."""
    try:
        from cuml.cluster import HDBSCAN as cuHDBSCAN
        from cuml.manifold import UMAP as cuUMAP

        def umap_fn(X, seed):
            E = cuUMAP(n_components=UMAP_COMPONENTS, n_neighbors=UMAP_NEIGHBORS,
                       min_dist=UMAP_MIN_DIST, metric=UMAP_METRIC,
                       random_state=seed).fit_transform(X)
            return np.asarray(E.get() if hasattr(E, "get") else E, dtype=np.float32)

        def hdbscan_fn(E, mcs, ms):
            lab = cuHDBSCAN(min_cluster_size=mcs, min_samples=ms).fit_predict(E)
            return np.asarray(lab.get() if hasattr(lab, "get") else lab)

        return umap_fn, hdbscan_fn, "cuml_gpu"
    except ImportError:
        import hdbscan as cpu_hdbscan
        import umap as cpu_umap

        def umap_fn(X, seed):
            return cpu_umap.UMAP(n_components=UMAP_COMPONENTS, n_neighbors=UMAP_NEIGHBORS,
                                 min_dist=UMAP_MIN_DIST, metric=UMAP_METRIC,
                                 random_state=seed).fit_transform(X).astype(np.float32)

        def hdbscan_fn(E, mcs, ms):
            return cpu_hdbscan.HDBSCAN(min_cluster_size=mcs, min_samples=ms).fit_predict(E)

        return umap_fn, hdbscan_fn, "cpu_umap_hdbscan"


# ============================================================================
# Data
# ============================================================================
def load_posts() -> pd.DataFrame:
    con = duckdb.connect(str(SRC_DB), read_only=True)
    df = con.execute("""
        SELECT e.post_uid, e.embedding, p.text, p.lang, p.author_uid
        FROM post_embeddings e
        JOIN post_facts p ON p.post_uid = e.post_uid
    """).fetchdf()
    con.close()
    log(f"posts with embeddings: {len(df):,}")
    return df


def unit_norm(df: pd.DataFrame) -> np.ndarray:
    X = np.vstack(df["embedding"].apply(np.array).values).astype(np.float32)
    n = np.linalg.norm(X, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return X / n


# ============================================================================
# Quality: measured in the real space, across real seeds
# ============================================================================
def cosine_silhouette(X: np.ndarray, labels: np.ndarray) -> float:
    """Silhouette in the ORIGINAL 1024-d cosine space.

    Deliberately NOT in UMAP space: UMAP optimises for visual separation, so a
    silhouette computed there measures the projection, not the clustering. v3
    stored 0.395 from UMAP space; the honest figure for the same data is an order
    of magnitude smaller.
    """
    from sklearn.metrics import silhouette_score
    m = labels >= 0
    k = len(set(labels[m]))
    if k < 2 or m.sum() < 10:
        return float("nan")
    return float(silhouette_score(X[m], labels[m], metric="cosine",
                                  sample_size=min(SIL_SAMPLE, int(m.sum())),
                                  random_state=0))


def stability(X, umap_fn, hdbscan_fn, mcs, ms, seeds) -> dict:
    from sklearn.metrics import adjusted_rand_score
    labs, ks = [], []
    for s in seeds:
        lab = hdbscan_fn(umap_fn(X, s), mcs, ms)
        labs.append(lab)
        ks.append(int(len(set(lab)) - (1 if -1 in lab else 0)))
    aris = [adjusted_rand_score(labs[i], labs[j])
            for i in range(len(labs)) for j in range(i + 1, len(labs))]
    return {"k_per_seed": ks, "mean_ari": float(np.mean(aris)),
            "min_ari": float(np.min(aris)), "n_seeds": len(seeds)}


# ============================================================================
# Labelling -- multilingual by construction
# ============================================================================
_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
# Deliberately NOT sklearn's stop_words="english". The corpus spans 6 scripts;
# an English stoplist plus English regexes is why a v2 cluster ended up named
# "бпла". Instead: drop terms that are common ACROSS clusters, which is
# language-agnostic and adapts to whatever the corpus actually contains.
_URL = re.compile(r"https?://\S+|www\.\S+")


def tokenise(text: str) -> list[str]:
    return _TOKEN.findall(_URL.sub(" ", str(text).lower()))


def label_clusters(df: pd.DataFrame, labels: np.ndarray, X: np.ndarray) -> dict:
    """c-TF-IDF style terms per cluster, plus dominant language and a medoid.

    Distinctiveness is `share_in_cluster / share_across_corpus`, so a term that
    is frequent everywhere cannot win regardless of which language it is in.
    """
    out = {}
    doc_freq: Counter = Counter()
    per_cluster: dict[int, Counter] = {}
    for cid in sorted(set(labels)):
        if cid < 0:
            continue
        idx = np.where(labels == cid)[0]
        c: Counter = Counter()
        for i in idx:
            c.update(set(tokenise(df["text"].iloc[i])))
        per_cluster[cid] = c
        doc_freq.update(c.keys())

    n_clusters = max(1, len(per_cluster))
    for cid, c in per_cluster.items():
        idx = np.where(labels == cid)[0]
        n = len(idx)
        scored = []
        for term, cnt in c.items():
            if cnt < max(2, 0.05 * n):
                continue
            spread = doc_freq[term] / n_clusters      # in how many clusters it appears
            scored.append(((cnt / n) / (spread + 1e-9), term, cnt / n))
        scored.sort(reverse=True)
        terms = [t for _s, t, _f in scored[:LABEL_TERMS]]

        langs = Counter(df["lang"].iloc[idx].fillna("und"))
        # medoid = post closest to the cluster centroid; a real example beats a
        # bag of keywords when a human has to judge whether the cluster is real.
        cen = X[idx].mean(axis=0)
        cen /= (np.linalg.norm(cen) or 1.0)
        medoid = idx[int(np.argmax(X[idx] @ cen))]

        out[cid] = {
            "terms": terms,
            "name": " / ".join(terms[:3]) if terms else f"cluster {cid}",
            "langs": langs.most_common(3),
            "dominant_lang": langs.most_common(1)[0][0] if langs else "und",
            "medoid_post_uid": df["post_uid"].iloc[medoid],
            "medoid_text": str(df["text"].iloc[medoid])[:240].replace("\n", " "),
        }
    return out


def coherence_flags(df: pd.DataFrame, labels: np.ndarray,
                    coded: set | None = None) -> dict:
    """Flag artifact clusters objectively instead of annotating them by hand.

    Two INDEPENDENT flags, because they mean different things:
      coherent            -- is this a real grouping at all?
      off_topic_geopolitical -- is it a real grouping we simply do not code?
    """
    flags = {}
    for cid in sorted(set(labels)):
        if cid < 0:
            continue
        idx = np.where(labels == cid)[0]
        n = len(idx)
        auth = Counter(df["author_uid"].iloc[idx].fillna("?"))
        top_auth = auth.most_common(1)[0][1] / n if auth else 0.0
        txt = Counter(str(t)[:120] for t in df["text"].iloc[idx])
        top_dup = txt.most_common(1)[0][1] / n if txt else 0.0
        reasons = []
        if n < MIN_COHERENT_SIZE:
            reasons.append(f"below MIN_COHERENT_SIZE ({n} < {MIN_COHERENT_SIZE})")
        if top_auth > MAX_AUTHOR_SHARE:
            reasons.append(f"one author is {top_auth:.0%} of the cluster "
                           f"(threshold {MAX_AUTHOR_SHARE:.0%}; corpus median ~0.57)")
        if top_dup > MAX_DUP_TEXT_SHARE:
            reasons.append(f"one near-identical text is {top_dup:.0%}")

        coded_share = (len({u for u in df["post_uid"].iloc[idx]} & coded) / n
                       if coded else float("nan"))
        off_topic = bool(coded == coded and coded and coded_share < MIN_CODED_SHARE)

        flags[cid] = {"coherent": not reasons, "reason": "; ".join(reasons) or None,
                      "top_author_share": round(top_auth, 3),
                      "top_dup_share": round(top_dup, 3),
                      "coded_share": None if coded_share != coded_share else round(coded_share, 3),
                      "off_topic_geopolitical": off_topic}
    return flags


# ============================================================================
# Hierarchy
# ============================================================================
def build_parents(X: np.ndarray, labels: np.ndarray, n_parents: int) -> dict:
    """Agglomerate fine-cluster centroids into parent topics.

    Ward over L2-normalised centroids. Deterministic given the clusters, so the
    parent level introduces no instability of its own -- which a second, coarser
    HDBSCAN pass certainly would (measured: mcs=60 gives min pairwise ARI 0.017).
    """
    from sklearn.cluster import AgglomerativeClustering
    cids = sorted(c for c in set(labels) if c >= 0)
    if len(cids) < 2:
        return {c: 0 for c in cids}
    cen = np.vstack([X[labels == c].mean(axis=0) for c in cids])
    cen /= np.linalg.norm(cen, axis=1, keepdims=True).clip(min=1e-9)
    k = min(n_parents, len(cids))
    par = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(cen)
    return dict(zip(cids, (int(p) for p in par)))


def crosswalk(con, post_topic: pd.DataFrame) -> pd.DataFrame:
    """topic x CAMEO parent code, with concentration and entropy.

    Validates both directions: a topic spanning many codes is incoherent, and a
    code with no topic is under-served by the corpus.
    """
    if not con.execute("SELECT count(*) FROM duckdb_tables() "
                       "WHERE table_name='event_codes'").fetchone()[0]:
        return pd.DataFrame()
    ec = con.execute("SELECT post_uid, code_key FROM event_codes "
                     "WHERE axis='theme' AND level=1").fetchdf()
    if not len(ec):
        return pd.DataFrame()
    j = post_topic.merge(ec, on="post_uid", how="inner")
    rows = []
    for tid, g in j.groupby("topic_id"):
        cnt = g["code_key"].value_counts()
        p = (cnt / cnt.sum()).to_numpy()
        ent = float(-(p * np.log(p + 1e-12)).sum())
        rows.append({
            "topic_id": tid, "n_posts_coded": int(cnt.sum()),
            "dominant_code": cnt.index[0], "dominant_share": float(p[0]),
            "n_codes": int(len(cnt)),
            "entropy": round(ent, 4),
            "entropy_norm": round(ent / np.log(len(cnt)), 4) if len(cnt) > 1 else 0.0,
        })
    return pd.DataFrame(rows)


# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true",
                    help="run the stability sweep and exit without writing")
    ap.add_argument("--seeds", type=int, default=N_SEEDS)
    ap.add_argument("--min-cluster-size", type=int, default=MIN_CLUSTER_SIZE)
    ap.add_argument("--parents", type=int, default=N_PARENTS)
    args = ap.parse_args()

    t0 = time.time()
    umap_fn, hdbscan_fn, backend = get_backend()
    log(f"backend: {backend}")

    df = load_posts()
    X = unit_norm(df)
    log(f"embeddings: {X.shape}")

    seeds = list(range(args.seeds))
    if args.sweep:
        log("stability sweep (no write)")
        print(f"\n{'mcs':>5} {'k per seed':<32} {'mean ARI':>9} {'min ARI':>8} {'sil_1024d':>10}")
        for mcs in [15, 20, 25, 30, 40, 60]:
            st = stability(X, umap_fn, hdbscan_fn, mcs, MIN_SAMPLES, seeds)
            lab = hdbscan_fn(umap_fn(X, PRIMARY_SEED), mcs, MIN_SAMPLES)
            print(f"{mcs:>5} {str(st['k_per_seed']):<32} {st['mean_ari']:>9.3f} "
                  f"{st['min_ari']:>8.3f} {cosine_silhouette(X, lab):>10.3f}")
        return 0

    mcs = args.min_cluster_size
    log(f"stability across {len(seeds)} seeds at min_cluster_size={mcs} ...")
    st = stability(X, umap_fn, hdbscan_fn, mcs, MIN_SAMPLES, seeds)
    log(f"  k per seed {st['k_per_seed']}  mean ARI {st['mean_ari']:.3f}  "
        f"min ARI {st['min_ari']:.3f}")
    if st["min_ari"] < 0.3:
        log("  WARNING: min pairwise ARI < 0.3 -- this clustering is not reproducible "
            "across seeds; treat every topic below as provisional")

    E = umap_fn(X, PRIMARY_SEED)
    labels = hdbscan_fn(E, mcs, MIN_SAMPLES)
    k = int(len(set(labels)) - (1 if -1 in labels else 0))
    noise = int((labels == -1).sum())
    sil = cosine_silhouette(X, labels)
    log(f"clusters: {k}  noise: {noise:,} ({100*noise/len(labels):.1f}%)  "
        f"silhouette(cosine, 1024d): {sil:.3f}")

    parents = build_parents(X, labels, args.parents)
    n_par = len(set(parents.values()))
    log(f"parent topics: {n_par} (agglomerated from {k} sub-topics)")

    labs = label_clusters(df, labels, X)
    _c = duckdb.connect(str(OUT_DB), read_only=True)
    coded = set()
    if _c.execute("SELECT count(*) FROM duckdb_tables() "
                  "WHERE table_name='event_codes'").fetchone()[0]:
        coded = {r[0] for r in _c.execute(
            "SELECT DISTINCT post_uid FROM event_codes WHERE axis='theme'").fetchall()}
    _c.close()
    flags = coherence_flags(df, labels, coded)
    n_coh = sum(1 for f in flags.values() if f["coherent"])
    n_off = sum(1 for f in flags.values() if f["off_topic_geopolitical"])
    log(f"coherent: {n_coh}/{k}   off-topic (no geopolitical code): {n_off}/{k}")

    run_at = datetime.now(timezone.utc).isoformat()
    dim_rows, post_rows = [], []
    for cid in sorted(c for c in set(labels) if c >= 0):
        idx = np.where(labels == cid)[0]
        L, F = labs[cid], flags[cid]
        pid = parents[cid]
        dim_rows.append({
            "topic_id": f"h{cid:03d}", "cluster_id": int(cid),
            "parent_topic_id": f"p{pid:02d}", "parent_cluster_id": int(pid),
            "level": 2,
            "name": L["name"], "terms": ", ".join(L["terms"]),
            "dominant_lang": L["dominant_lang"],
            "langs": json.dumps(L["langs"], ensure_ascii=False),
            "medoid_post_uid": L["medoid_post_uid"], "medoid_text": L["medoid_text"],
            "coherent": F["coherent"], "incoherent_reason": F["reason"],
            "off_topic_geopolitical": F["off_topic_geopolitical"],
            "coded_share": F["coded_share"],
            "top_author_share": F["top_author_share"], "top_dup_share": F["top_dup_share"],
            "size": int(len(idx)),
            "method": f"{backend}|umap{UMAP_COMPONENTS}|mcs{mcs}|ms{MIN_SAMPLES}",
            "n_clusters_total": k, "silhouette_cosine_1024d": round(sil, 4),
            "stability_mean_ari": round(st["mean_ari"], 4),
            "stability_min_ari": round(st["min_ari"], 4),
            "run_at": run_at,
        })
        for i in idx:
            sim = float(X[i] @ (X[idx].mean(axis=0) /
                                (np.linalg.norm(X[idx].mean(axis=0)) or 1.0)))
            post_rows.append({"post_uid": df["post_uid"].iloc[i],
                              "topic_id": f"h{cid:03d}", "cluster_id": int(cid),
                              "parent_topic_id": f"p{pid:02d}",
                              "confidence": round(sim, 4), "assign_method": "hdbscan"})
    for i in np.where(labels == -1)[0]:
        post_rows.append({"post_uid": df["post_uid"].iloc[i], "topic_id": None,
                          "cluster_id": -1, "parent_topic_id": None,
                          "confidence": None, "assign_method": "noise"})

    topic_dim = pd.DataFrame(dim_rows)
    post_topic = pd.DataFrame(post_rows)

    # parent dimension: named from its children's most distinctive terms
    par_rows = []
    for pid in sorted(set(parents.values())):
        kids = topic_dim[topic_dim["parent_cluster_id"] == pid]
        terms = Counter()
        for t in kids["terms"]:
            terms.update(t.split(", "))
        par_rows.append({
            "topic_id": f"p{pid:02d}", "cluster_id": int(pid), "parent_topic_id": None,
            "parent_cluster_id": None, "level": 1,
            "name": " / ".join([t for t, _ in terms.most_common(3)]),
            "terms": ", ".join(t for t, _ in terms.most_common(LABEL_TERMS)),
            "n_subtopics": int(len(kids)), "size": int(kids["size"].sum()),
            "coherent": bool(kids["coherent"].any()),
            "run_at": run_at,
        })
    parent_dim = pd.DataFrame(par_rows)

    con = duckdb.connect(str(OUT_DB), read_only=False)
    xw = crosswalk(con, post_topic[post_topic["topic_id"].notna()])
    for name, payload in [("topic_dim_v4", topic_dim), ("topic_parent_v4", parent_dim),
                          ("post_topic_v4", post_topic), ("topic_code_crosswalk", xw)]:
        if payload is None or not len(payload):
            log(f"  (skipping {name}: empty)")
            continue
        con.register("_p", payload)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT {_projection(payload)} FROM _p")
        con.unregister("_p")
        log(f"  published {name:<22} rows={len(payload):>7,}")
    con.close()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "run_at": run_at, "backend": backend,
        "n_posts": int(len(df)), "n_subtopics": k, "n_parents": n_par,
        "noise": noise, "noise_share": round(noise / len(labels), 4),
        "coherent": n_coh, "off_topic_geopolitical": n_off,
        "silhouette_cosine_1024d": round(sil, 4),
        "silhouette_space": "original 1024-d embedding, cosine -- NOT UMAP space",
        "stability": st,
        "params": {"min_cluster_size": mcs, "min_samples": MIN_SAMPLES,
                   "umap_components": UMAP_COMPONENTS, "umap_neighbors": UMAP_NEIGHBORS,
                   "n_parents": args.parents, "primary_seed": PRIMARY_SEED},
    }, indent=2))
    log(f"wrote {REPORT}")
    log(f"done in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
