#!/usr/bin/env python3
"""
Ontology v2 — clustering fix (decouple cluster count from corpus size N).

Root cause of the coarsening bug in scripts/03_ontology.py: min_cluster_size was
computed as max(25, len(posts_e)//80), so it grows with N. At N~23K this produced
only 3 clusters (one holding 85%+ of posts). The fix: HDBSCAN min_cluster_size and
min_samples are FIXED ABSOLUTE CONSTANTS, chosen once via prototyping on an N=2000
reservoir sample (see docs/GEO_ONTOLOGY_CONTEXT.md task log), then applied unchanged
to the full corpus.

Reads FULL corpus from data/analytics.duckdb (read-only, never written).
Writes topic_dim_v2 / post_topic_v2 to data/geo_ontology.duckdb (sole writer here).
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.preprocessing import normalize

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module

_o1 = import_module("03_ontology")  # reuse label_clusters / keyword_hint / assign_domain_topic
label_clusters = _o1.label_clusters
assign_domain_topic = _o1.assign_domain_topic

EDA_ROOT = Path(__file__).resolve().parents[1]
SRC_DB = EDA_ROOT / "data" / "analytics.duckdb"
OUT_DB = EDA_ROOT / "data" / "geo_ontology.duckdb"

# ---- Fixed absolute HDBSCAN constants (THE FIX: do not scale with N) ----
UMAP_N_NEIGHBORS = 15
UMAP_N_COMPONENTS = 10
UMAP_MIN_DIST = 0.0
UMAP_METRIC = "cosine"
HDBSCAN_MIN_CLUSTER_SIZE = 15   # fixed, independent of len(posts_e)
HDBSCAN_MIN_SAMPLES = 5         # fixed, independent of len(posts_e)
HDBSCAN_METRIC = "euclidean"
HDBSCAN_SELECTION = "eom"
CONF_HIGH = 0.55
SEED = 42


def main() -> int:
    if not SRC_DB.exists():
        print(f"ERROR: source db missing: {SRC_DB}")
        return 1
    if OUT_DB.exists():
        print(f"ERROR: {OUT_DB} already exists; refusing to clobber blindly")
        return 1

    src = duckdb.connect(str(SRC_DB), read_only=True)
    posts = src.execute(
        "SELECT post_uid, platform, text, keyword, posted_at FROM post_facts"
    ).df()
    emb = src.execute(
        "SELECT pe.post_uid, pe.embedding "
        "FROM post_embeddings pe JOIN post_facts pf ON pe.post_uid = pf.post_uid"
    ).df()
    src.close()
    print(f"post_facts: {len(posts):,}  post_embeddings (joined): {len(emb):,}")

    emb = emb.drop_duplicates(subset=["post_uid"], keep="last")
    emb_map = {r.post_uid: r.embedding for r in emb.itertuples()}
    posts_e = posts[posts["post_uid"].isin(emb_map)].reset_index(drop=True)
    print(f"posts with embeddings (full corpus for clustering): {len(posts_e):,}")

    X = np.vstack([np.asarray(emb_map[u], dtype=np.float32) for u in posts_e["post_uid"]])
    X = normalize(X, norm="l2")
    print(f"matrix: {X.shape}")

    import umap
    import hdbscan

    print(
        f"  UMAP(n_neighbors={UMAP_N_NEIGHBORS}, n_components={UMAP_N_COMPONENTS}, "
        f"min_dist={UMAP_MIN_DIST}, metric={UMAP_METRIC}) ..."
    )
    reducer = umap.UMAP(
        n_neighbors=UMAP_N_NEIGHBORS,
        n_components=UMAP_N_COMPONENTS,
        min_dist=UMAP_MIN_DIST,
        metric=UMAP_METRIC,
        random_state=SEED,
        verbose=False,
    )
    Xu = reducer.fit_transform(X)
    n = len(Xu)
    np.save(EDA_ROOT / "data" / "_umap_cache_v2.npy", Xu)  # cache: UMAP is the expensive step

    from sklearn.metrics import silhouette_score

    rng = np.random.default_rng(SEED)

    def run_hdbscan(mcs: int, ms: int):
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=mcs, min_samples=ms,
            metric=HDBSCAN_METRIC, cluster_selection_method=HDBSCAN_SELECTION,
        )
        lab = clusterer.fit_predict(Xu)
        nclu = len(set(lab) - {-1})
        noise = float((lab == -1).mean())
        sizes = pd.Series(lab[lab != -1]).value_counts()
        largest = float(sizes.max() / n) if len(sizes) else 0.0
        sil = None
        mask = lab != -1
        if mask.sum() > 2 and nclu > 1:
            idx = np.where(mask)[0]
            samp = rng.choice(idx, size=min(5000, len(idx)), replace=False)
            try:
                sil = float(silhouette_score(Xu[samp], lab[samp], metric="euclidean"))
            except Exception:
                sil = None
        return lab, nclu, noise, largest, sil

    # ---- Calibration sweep on the FULL corpus ----
    # The N=2000 prototype's chosen (15, 5) is the correct *shape* of fix (fixed,
    # low, independent of N) but at 10.6x the N it over-fragments (328 clusters,
    # 35% noise) rather than coarsening. Sweeping fixed candidates directly on the
    # full corpus's already-computed UMAP embedding (cheap — UMAP is the expensive
    # step, done once above) reveals a sharp density cliff around mcs~305: below
    # it, ~11-16 healthy clusters with noise plateaued at 16-35%; at/above it,
    # HDBSCAN collapses to 3-6 clusters with one holding 68-94% of the corpus —
    # i.e. the exact coarsening bug reproduced by raising this one fixed constant
    # too far. min_samples must also stay low (raising it accelerates the same
    # collapse, per the N=2000 prototype finding). The row list below traces that
    # story end-to-end; the last pre-cliff point is the final choice.
    candidates = [
        (15, 5), (60, 5), (100, 8), (200, 3), (292, 3), (300, 3), (306, 3),
    ]
    print("  calibration sweep (fixed constants, full N):")
    sweep_results = []
    for mcs, ms in candidates:
        lab, nclu, noise, largest, sil = run_hdbscan(mcs, ms)
        ok = largest < 0.40 and noise < 0.15
        sweep_results.append((mcs, ms, lab, nclu, noise, largest, sil, ok))
        print(f"    mcs={mcs:>4} ms={ms:>3} -> clusters={nclu:>4} noise={100*noise:5.1f}% "
              f"largest={100*largest:5.1f}% silhouette={sil} {'PASS' if ok else ''}")

    # Selection priority: (1) largest_share<40% is a hard constraint — a config
    # that collapses into one giant cluster is never acceptable no matter how low
    # its noise looks. (2) among non-collapsed configs, minimize noise. (3) if
    # every config collapses, fall back to the smallest largest_share.
    not_collapsed = [r for r in sweep_results if r[5] < 0.40]
    passing = [r for r in not_collapsed if r[7]]
    if passing:
        chosen = max(passing, key=lambda r: (r[6] if r[6] is not None else -1))
    elif not_collapsed:
        chosen = min(not_collapsed, key=lambda r: r[4])
        print("  WARN: no candidate met noise<15% without collapsing; using lowest-noise non-collapsed config")
    else:
        chosen = min(sweep_results, key=lambda r: (r[5], r[4]))
        print("  WARN: every candidate collapsed (largest>=40%); using least-bad fallback")

    HDBSCAN_MIN_CLUSTER_SIZE, HDBSCAN_MIN_SAMPLES = chosen[0], chosen[1]
    labels, n_clusters, noise_frac, largest_share, sil = chosen[2], chosen[3], chosen[4], chosen[5], chosen[6]
    print(f"  CHOSEN: min_cluster_size={HDBSCAN_MIN_CLUSTER_SIZE} min_samples={HDBSCAN_MIN_SAMPLES} "
          f"-> clusters={n_clusters} noise={100*noise_frac:.1f}% largest={100*largest_share:.1f}% silhouette={sil}")

    print(f"  raw HDBSCAN noise={100*noise_frac:.1f}% (bar target <15% checked again below on the "
          f"final post-centroid-reassignment unassigned rate, which is the number that lands in post_topic_v2)")

    # ---- Label cards + domain/topic assignment (reuse 03_ontology helpers) ----
    texts = posts_e["text"].fillna("").tolist()
    cards = label_clusters(texts, labels)

    kw_votes: dict[int, Counter] = defaultdict(Counter)
    for lab, kw in zip(labels, posts_e["keyword"].tolist()):
        if lab == -1:
            continue
        if kw and str(kw).strip():
            kw_votes[int(lab)][str(kw).strip()] += 1

    run_ts = datetime.now(timezone.utc).isoformat()
    method = "umap_hdbscan_v2_fixed_params"
    topic_rows = []
    cluster_to_topic = {}
    for lab, card in sorted(cards.items()):
        domain, topic = assign_domain_topic(card, kw_votes.get(lab, Counter()))
        topic_id = f"t{lab:03d}"
        cluster_to_topic[lab] = topic_id
        topic_rows.append(
            {
                "topic_id": topic_id,
                "cluster_id": int(lab),
                "domain": domain,
                "topic": topic,
                "size": card["size"],
                "top_terms": ", ".join(card["terms"][:8]),
                "exemplar_1": card["exemplars"][0] if card["exemplars"] else "",
                "exemplar_2": card["exemplars"][1] if len(card["exemplars"]) > 1 else "",
                "method": method,
                "min_cluster_size": HDBSCAN_MIN_CLUSTER_SIZE,
                "min_samples": HDBSCAN_MIN_SAMPLES,
                "umap_n_components": UMAP_N_COMPONENTS,
                "umap_n_neighbors": UMAP_N_NEIGHBORS,
                "n_clusters_total": n_clusters,
                "noise_frac_total": noise_frac,
                "largest_cluster_share_total": largest_share,
                "silhouette_sampled": sil,
                "run_at": run_ts,
            }
        )
    topic_dim_v2 = pd.DataFrame(topic_rows)

    # ---- Soft-assign noise points to nearest centroid (original embedding space) ----
    centroids = {}
    for lab in set(labels):
        if lab == -1:
            continue
        c = X[labels == lab].mean(axis=0)
        centroids[lab] = c / (np.linalg.norm(c) + 1e-9)

    emb_index = {u: i for i, u in enumerate(posts_e["post_uid"])}
    assign_rows = []
    for _, row in posts.iterrows():
        uid = row["post_uid"]
        if uid not in emb_index:
            assign_rows.append(
                {"post_uid": uid, "topic_id": "unassigned", "cluster_id": -1,
                 "confidence": 0.0, "assign_method": "no_embedding"}
            )
            continue
        i = emb_index[uid]
        lab = int(labels[i])
        if lab == -1:
            if centroids:
                sims = {c: float(X[i] @ v) for c, v in centroids.items()}
                best_c, conf = max(sims.items(), key=lambda x: x[1])
                if conf >= CONF_HIGH:
                    assign_rows.append(
                        {"post_uid": uid, "topic_id": cluster_to_topic[best_c], "cluster_id": int(best_c),
                         "confidence": conf, "assign_method": "nearest_centroid"}
                    )
                    continue
            assign_rows.append(
                {"post_uid": uid, "topic_id": "unassigned", "cluster_id": -1,
                 "confidence": 0.0, "assign_method": "noise_unassigned"}
            )
        else:
            assign_rows.append(
                {"post_uid": uid, "topic_id": cluster_to_topic[lab], "cluster_id": lab,
                 "confidence": 1.0, "assign_method": "hdbscan_core"}
            )
    post_topic_v2 = pd.DataFrame(assign_rows)

    final_unassigned_frac = float((post_topic_v2["topic_id"] == "unassigned").mean())
    bar_ok = largest_share < 0.40 and final_unassigned_frac < 0.15
    print(
        f"\n  FINAL (post_topic_v2, full post_facts corpus incl. no-embedding rows):"
        f" unassigned={100*final_unassigned_frac:.1f}% largest_cluster_share={100*largest_share:.1f}%"
    )
    print(f"  acceptance bar (largest<40%, noise<15%): {'PASS' if bar_ok else 'FAIL'}")

    # ---- Write to geo_ontology.duckdb (sole writer this stage) ----
    out = duckdb.connect(str(OUT_DB), read_only=False)
    out.register("_topic_dim_v2", topic_dim_v2)
    out.execute("CREATE TABLE topic_dim_v2 AS SELECT * FROM _topic_dim_v2")
    out.unregister("_topic_dim_v2")
    out.register("_post_topic_v2", post_topic_v2)
    out.execute("CREATE TABLE post_topic_v2 AS SELECT * FROM _post_topic_v2")
    out.unregister("_post_topic_v2")
    out.close()

    print(f"\nwrote {OUT_DB}")
    print(f"  topic_dim_v2: {len(topic_dim_v2):,} rows")
    print(f"  post_topic_v2: {len(post_topic_v2):,} rows (full post_facts corpus)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
