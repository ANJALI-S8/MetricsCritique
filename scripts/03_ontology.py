#!/usr/bin/env python3
"""
Layer 2 — Ontology: embed missing youtube_videos (bge-m3), cluster posts,
build domain→topic taxonomy, assign keyword-less posts with confidence.

All outputs local (parquet + DuckDB). Never writes to Postgres.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import MiniBatchKMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import normalize

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DUCKDB_PATH, EMBEDDING_DIM, EMBEDDING_MODEL, PARQUET_DIR, REPORTS_DIR
from lib.duck import connect, replace_table_from_df, write_parquet


# Keyword → coarse domain hints (first-pass label seeds)
KEYWORD_DOMAIN_HINTS = [
    (r"iran|israel|gaza|hezbollah|houthi|hamas|middle.?east", "Conflict", "Middle-East"),
    (r"ukraine|russia|rybar|donbas|putin|zelensk", "Conflict", "Russia-Ukraine"),
    (r"drone|missile|army|navy|defence|defense|drdo|pla|military|border|doklam|arunachal", "India", "Defence"),
    (r"terror|attack|isi|lashkar|jaish", "Security", "Terrorism"),
    (r"cyber|ransomware|malware|phishing|fraud", "Cyber", "Threats"),
    (r"flood|disaster|earthquake|cyclone", "Civil", "Disaster"),
    (r"election|politic|protest|corruption|neet", "Civil", "Politics"),
    (r"stock|nifty|market|startup", "Markets", "Finance"),
    (r"ai\b|tech|chip|semiconductor", "Tech", "AI"),
    (r"china|ccp|string of pearls|chittagong|djibouti", "Geopolitics", "China"),
    (r"bangladesh|myanmar|pak", "Geopolitics", "Neighbourhood"),
]


def load_embeddings_parquet() -> pd.DataFrame:
    path = PARQUET_DIR / "post_embeddings" / "part-0.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["post_uid", "platform", "embedding", "embedding_model", "embedding_dim"])
    return pd.read_parquet(path)


def embed_texts_bge(texts: list[str], batch_size: int = 8) -> np.ndarray:
    """Encode with BAAI/bge-m3 (must match production embedding space)."""
    import torch
    from sentence_transformers import SentenceTransformer

    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        print(f"  GPU free {free/1e9:.1f} / {total/1e9:.1f} GB")
        # If another process holds most of the card, fall back to CPU
        device = "cuda" if free > 4e9 else "cpu"
        if device == "cpu":
            print("  WARN: low GPU free memory — embedding on CPU")
        else:
            torch.cuda.empty_cache()
    else:
        device = "cpu"
    print(f"  loading {EMBEDDING_MODEL} on {device} ...")
    model = SentenceTransformer(EMBEDDING_MODEL, device=device)
    # shorter sequences reduce peak VRAM (transcripts truncated by caller)
    emb = model.encode(
        texts,
        batch_size=batch_size if device == "cuda" else 4,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    return np.asarray(emb, dtype=np.float32)


def keyword_hint(keyword: str | None) -> tuple[str, str] | None:
    if not keyword:
        return None
    k = str(keyword).lower()
    for pat, domain, topic in KEYWORD_DOMAIN_HINTS:
        if re.search(pat, k, re.I):
            return domain, topic
    return None


def label_clusters(texts: list[str], labels: np.ndarray, top_n: int = 8) -> dict[int, dict]:
    """TF-IDF discriminative terms + exemplars per cluster."""
    cards = {}
    # filter noise label -1
    uniq = sorted(set(labels) - {-1})
    clean_texts = [t if isinstance(t, str) else "" for t in texts]
    for lab in uniq:
        idx = np.where(labels == lab)[0]
        cluster_docs = [clean_texts[i] for i in idx if clean_texts[i].strip()]
        if not cluster_docs:
            cards[lab] = {"terms": [], "exemplars": [], "size": int(len(idx))}
            continue
        # TF-IDF within cluster vs rest is expensive; use in-cluster top terms
        try:
            # Aggressive stop/max_df so labels are content words, not "the/and/https"
            vec = TfidfVectorizer(
                max_features=8000,
                ngram_range=(1, 2),
                min_df=3,
                max_df=0.4,
                stop_words="english",
                token_pattern=r"(?u)\b[^\W\d_]{3,}\b",
            )
            X = vec.fit_transform(cluster_docs)
            scores = np.asarray(X.mean(axis=0)).ravel()
            terms = np.array(vec.get_feature_names_out())
            # drop pure URL fragments
            ranked = terms[scores.argsort()[::-1]]
            top = [t for t in ranked if "http" not in t and "www" not in t][:top_n]
        except ValueError:
            # too few docs
            top = []
        # exemplars: shortest non-empty snippets
        exemplars = []
        for t in sorted(cluster_docs, key=len)[:3]:
            exemplars.append(t.replace("\n", " ")[:160])
        cards[lab] = {"terms": top, "exemplars": exemplars, "size": int(len(idx))}
    return cards


def assign_domain_topic(card: dict, keyword_votes: Counter) -> tuple[str, str]:
    """Heuristic domain/topic from TF-IDF terms + keyword votes."""
    blob = " ".join(card.get("terms", [])).lower()
    for pat, domain, topic in KEYWORD_DOMAIN_HINTS:
        if re.search(pat, blob, re.I):
            return domain, topic
    if keyword_votes:
        # map top keyword vote
        top_kw, _ = keyword_votes.most_common(1)[0]
        hint = keyword_hint(top_kw)
        if hint:
            return hint
    # fallback from terms
    if card.get("terms"):
        return "Misc", card["terms"][0][:40]
    return "Misc", "unlabelled"


def main() -> int:
    if not DUCKDB_PATH.exists():
        print("ERROR: run 01_extract_base.py first")
        return 1

    con = connect(read_only=False)
    posts = con.execute(
        "SELECT post_uid, platform, native_id, text, keyword, author_name, posted_at FROM post_facts"
    ).df()
    print(f"posts: {len(posts):,}")

    emb_df = load_embeddings_parquet()
    print(f"existing embeddings: {len(emb_df):,}")

    # ---- Embed missing youtube_videos ----
    yt = posts[posts["platform"] == "youtube_video"].copy()
    have = set(emb_df["post_uid"]) if not emb_df.empty else set()
    missing_yt = yt[~yt["post_uid"].isin(have)]
    print(f"youtube_video missing embeddings: {len(missing_yt):,}")

    new_emb_rows = []
    if len(missing_yt) > 0:
        texts = missing_yt["text"].fillna("").astype(str).tolist()
        # truncate very long transcripts for encoding speed
        texts = [t[:2000] for t in texts]
        try:
            vecs = embed_texts_bge(texts, batch_size=4)
            for uid, vec in zip(missing_yt["post_uid"].tolist(), vecs):
                new_emb_rows.append(
                    {
                        "post_uid": uid,
                        "platform": "youtube_video",
                        "embedding": vec.tolist(),
                        "embedding_model": EMBEDDING_MODEL,
                        "embedding_dim": int(len(vec)),
                    }
                )
            new_emb_df = pd.DataFrame(new_emb_rows)
            emb_df = pd.concat([emb_df, new_emb_df], ignore_index=True)
            write_parquet(emb_df, "post_embeddings")
            emb_meta = emb_df.drop(columns=["embedding"])
            replace_table_from_df(con, "post_embeddings_meta", emb_meta)
            print(f"  wrote {len(new_emb_rows):,} youtube embeddings")
        except Exception as e:
            print(f"  WARN: could not embed youtube videos: {e}")
            print("  continuing with existing embeddings only")

    # ---- Build matrix for posts that have embeddings ----
    emb_df = emb_df.drop_duplicates(subset=["post_uid"], keep="last")
    emb_map = {r.post_uid: r.embedding for r in emb_df.itertuples()}
    posts_e = posts[posts["post_uid"].isin(emb_map)].copy()
    print(f"posts with embeddings for clustering: {len(posts_e):,}")

    if len(posts_e) < 50:
        print("ERROR: too few embeddings to cluster")
        return 1

    X = np.vstack([np.asarray(emb_map[u], dtype=np.float32) for u in posts_e["post_uid"]])
    # Ensure L2-normalised
    X = normalize(X, norm="l2")
    print(f"matrix: {X.shape}")

    # ---- Clustering: try GPU (cuML) UMAP+HDBSCAN, then CPU UMAP+HDBSCAN, then k-means ----
    # NOTE on min_cluster_size: it scales with corpus size (len(posts_e)//80), which is
    # why cluster count drops as the corpus grows (e.g. 8 clusters at ~16.6K embeddings
    # vs 3 at ~21.2K here) — smaller topical clusters fall below the rising threshold and
    # get absorbed rather than kept distinct. Worth decoupling from N if 3 proves too
    # coarse for briefing use; a fixed absolute floor (or fixing min_cluster_size to a
    # target k via search) is the fix, not just retuning this one run.
    labels = None
    method = "kmeans"
    umap_coords = None
    min_cluster_size = max(25, len(posts_e) // 80)

    def _to_numpy(arr):
        return arr.get() if hasattr(arr, "get") else np.asarray(arr)

    def _try_cuml():
        import cuml  # noqa: F401
        from cuml.cluster import HDBSCAN as cuHDBSCAN
        from cuml.manifold import UMAP as cuUMAP

        print("  UMAP → HDBSCAN (cuML/GPU) ...")
        reducer = cuUMAP(
            n_components=15, n_neighbors=15, min_dist=0.0,
            metric="cosine", random_state=42,
        )
        Xu = _to_numpy(reducer.fit_transform(X))
        clusterer = cuHDBSCAN(
            min_cluster_size=min_cluster_size, min_samples=5,
            metric="euclidean", cluster_selection_method="eom",
        )
        lab = _to_numpy(clusterer.fit_predict(Xu))
        return Xu, lab, "umap_hdbscan_gpu"

    def _try_cpu_umap_hdbscan():
        import umap
        import hdbscan

        print("  UMAP → HDBSCAN (CPU) ...")
        reducer = umap.UMAP(
            n_components=15,
            n_neighbors=15,
            min_dist=0.0,
            metric="cosine",
            random_state=42,
            verbose=False,
        )
        Xu = reducer.fit_transform(X)
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=5,
            metric="euclidean",
            cluster_selection_method="eom",
        )
        lab = clusterer.fit_predict(Xu)
        return Xu, lab, "umap_hdbscan"

    try:
        try:
            umap_coords, labels, method = _try_cuml()
        except Exception as e_gpu:
            print(f"  cuML path unavailable/failed ({e_gpu}); falling back to CPU UMAP/HDBSCAN")
            umap_coords, labels, method = _try_cpu_umap_hdbscan()
        n_clusters = len(set(labels) - {-1})
        noise = float((labels == -1).mean())
        print(f"  HDBSCAN clusters={n_clusters} noise={100*noise:.1f}% (method={method})")
        if n_clusters < 3 or noise > 0.7:
            raise RuntimeError(f"unstable HDBSCAN (k={n_clusters}, noise={noise:.2f})")
    except Exception as e:
        print(f"  HDBSCAN path failed ({e}); using MiniBatchKMeans")
        # choose k by silhouette on sample
        best_k, best_score, best_lab = 12, -1, None
        rng = np.random.default_rng(42)
        sample_n = min(4000, len(X))
        sample_ix = rng.choice(len(X), sample_n, replace=False)
        Xs = X[sample_ix]
        for k in (8, 12, 16, 20, 24):
            km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=1024, n_init=3)
            lab_s = km.fit_predict(Xs)
            try:
                sc = silhouette_score(Xs, lab_s, metric="cosine", sample_size=min(2000, sample_n))
            except Exception:
                sc = -1
            print(f"    k={k} silhouette={sc:.3f}")
            if sc > best_score:
                best_k, best_score, best_lab = k, sc, lab_s
        km = MiniBatchKMeans(n_clusters=best_k, random_state=42, batch_size=1024, n_init=5)
        labels = km.fit_predict(X)
        method = f"kmeans_k{best_k}"
        print(f"  chose k={best_k} silhouette={best_score:.3f}")

    labels = np.asarray(labels)
    posts_e = posts_e.reset_index(drop=True)
    posts_e["cluster_id"] = labels

    # Stability: re-run kmeans seed variance if kmeans
    stability = None
    if method.startswith("kmeans"):
        k = len(set(labels))
        from sklearn.metrics import adjusted_rand_score

        labs = []
        for seed in (0, 1, 2):
            km = MiniBatchKMeans(n_clusters=k, random_state=seed, batch_size=1024, n_init=3)
            labs.append(km.fit_predict(X))
        aris = [
            adjusted_rand_score(labs[i], labs[j])
            for i in range(len(labs))
            for j in range(i + 1, len(labs))
        ]
        stability = float(np.mean(aris))
        print(f"  cluster stability (mean ARI across seeds): {stability:.3f}")

    # ---- Label cards ----
    texts = posts_e["text"].fillna("").tolist()
    cards = label_clusters(texts, labels)

    # keyword votes per cluster
    kw_votes: dict[int, Counter] = defaultdict(Counter)
    for lab, kw in zip(labels, posts_e["keyword"].tolist()):
        if lab == -1:
            continue
        if kw and str(kw).strip():
            kw_votes[int(lab)][str(kw).strip()] += 1

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
            }
        )
    topic_dim = pd.DataFrame(topic_rows)

    # ---- Assign every post with embedding; unassigned for noise / no emb ----
    CONF_HIGH = 0.55  # cosine to centroid
    # centroids in original embedding space
    centroids = {}
    for lab in set(labels):
        if lab == -1:
            continue
        centroids[lab] = X[labels == lab].mean(axis=0)
        centroids[lab] = centroids[lab] / (np.linalg.norm(centroids[lab]) + 1e-9)

    assign_rows = []
    emb_index = {u: i for i, u in enumerate(posts_e["post_uid"])}
    for _, row in posts.iterrows():
        uid = row["post_uid"]
        if uid not in emb_index:
            assign_rows.append(
                {
                    "post_uid": uid,
                    "topic_id": "unassigned",
                    "cluster_id": -1,
                    "confidence": 0.0,
                    "assign_method": "no_embedding",
                }
            )
            continue
        i = emb_index[uid]
        lab = int(labels[i])
        if lab == -1:
            # nearest centroid for soft assign
            sims = {c: float(X[i] @ v) for c, v in centroids.items()}
            if sims:
                best_c, conf = max(sims.items(), key=lambda x: x[1])
                if conf >= CONF_HIGH:
                    assign_rows.append(
                        {
                            "post_uid": uid,
                            "topic_id": cluster_to_topic[best_c],
                            "cluster_id": int(best_c),
                            "confidence": conf,
                            "assign_method": "nearest_centroid",
                        }
                    )
                else:
                    assign_rows.append(
                        {
                            "post_uid": uid,
                            "topic_id": "unassigned",
                            "cluster_id": -1,
                            "confidence": conf,
                            "assign_method": "noise_low_conf",
                        }
                    )
            else:
                assign_rows.append(
                    {
                        "post_uid": uid,
                        "topic_id": "unassigned",
                        "cluster_id": -1,
                        "confidence": 0.0,
                        "assign_method": "noise",
                    }
                )
        else:
            conf = float(X[i] @ centroids[lab]) if lab in centroids else 1.0
            assign_rows.append(
                {
                    "post_uid": uid,
                    "topic_id": cluster_to_topic[lab],
                    "cluster_id": lab,
                    "confidence": conf,
                    "assign_method": method,
                }
            )

    # Keyword override: if keyword has domain hint, boost/tag
    post_topic = pd.DataFrame(assign_rows)
    # Add unassigned topic to topic_dim
    if "unassigned" not in set(topic_dim["topic_id"]):
        topic_dim = pd.concat(
            [
                topic_dim,
                pd.DataFrame(
                    [
                        {
                            "topic_id": "unassigned",
                            "cluster_id": -1,
                            "domain": "Unassigned",
                            "topic": "unassigned",
                            "size": int((post_topic["topic_id"] == "unassigned").sum()),
                            "top_terms": "",
                            "exemplar_1": "",
                            "exemplar_2": "",
                            "method": method,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )

    # Keyword-seeded mapping table
    kw_map_rows = []
    for kw, n in posts["keyword"].dropna().value_counts().items():
        if not str(kw).strip():
            continue
        hint = keyword_hint(str(kw))
        kw_map_rows.append(
            {
                "keyword": kw,
                "n": int(n),
                "domain": hint[0] if hint else None,
                "topic": hint[1] if hint else None,
            }
        )
    keyword_map = pd.DataFrame(kw_map_rows)

    # ---- Persist ----
    write_parquet(topic_dim, "topic_dim")
    write_parquet(post_topic, "post_topic")
    write_parquet(keyword_map, "keyword_map")
    replace_table_from_df(con, "topic_dim", topic_dim)
    replace_table_from_df(con, "post_topic", post_topic)
    replace_table_from_df(con, "keyword_map", keyword_map)

    # ---- ONTOLOGY.md ----
    n_un = int((post_topic["topic_id"] == "unassigned").sum())
    n_as = len(post_topic) - n_un
    tg = posts[posts["platform"] == "telegram"]
    tg_topics = post_topic[post_topic["post_uid"].isin(set(tg["post_uid"]))]
    tg_un = int((tg_topics["topic_id"] == "unassigned").sum()) if len(tg_topics) else 0

    lines = []
    lines.append("# Ontology — Layer 2")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Clustering method: `{method}`")
    if stability is not None:
        lines.append(f"Cluster stability (mean ARI): **{stability:.3f}**")
    lines.append(f"Embedding model: `{EMBEDDING_MODEL}` (dim={EMBEDDING_DIM})")
    lines.append(f"New youtube_video embeddings computed: **{len(new_emb_rows):,}**")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- Posts assigned: **{n_as:,}** / {len(post_topic):,}")
    lines.append(f"- Unassigned: **{n_un:,}** ({100*n_un/len(post_topic):.1f}%)")
    lines.append(f"- Telegram unassigned: **{tg_un:,}** / {len(tg):,}")
    lines.append("")
    lines.append("## Taxonomy (domain → topic)")
    lines.append("")
    lines.append("| topic_id | domain | topic | size | top terms |")
    lines.append("|---|---|---|---:|---|")
    for _, r in topic_dim.sort_values("size", ascending=False).iterrows():
        if r["topic_id"] == "unassigned":
            continue
        lines.append(
            f"| `{r['topic_id']}` | {r['domain']} | {r['topic']} | {int(r['size']):,} | {r['top_terms'][:80]} |"
        )
    lines.append("")
    lines.append("## Cluster cards (top 20 by size)")
    lines.append("")
    for _, r in topic_dim.sort_values("size", ascending=False).head(20).iterrows():
        if r["topic_id"] == "unassigned":
            continue
        lines.append(f"### `{r['topic_id']}` — {r['domain']} / {r['topic']} (n={int(r['size']):,})")
        lines.append(f"- Terms: {r['top_terms']}")
        if r["exemplar_1"]:
            lines.append(f"- Exemplar: {r['exemplar_1'][:200]}")
        lines.append("")
    lines.append("## Keyword domain hints (sample)")
    lines.append("")
    if not keyword_map.empty:
        hinted = keyword_map[keyword_map["domain"].notna()].head(25)
        lines.append("| keyword | n | domain | topic |")
        lines.append("|---|---:|---|---|")
        for _, r in hinted.iterrows():
            lines.append(f"| {r['keyword']} | {int(r['n'])} | {r['domain']} | {r['topic']} |")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Telegram posts have no scraper keywords; assignment is embedding-nearest-centroid with confidence.")
    lines.append("- `unassigned` is intentional — never force-fit low-confidence posts.")
    lines.append("- Multilingual NER deferred (optional follow-up).")
    lines.append("")

    out = REPORTS_DIR / "ONTOLOGY.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")

    meta = {
        "method": method,
        "stability_ari": stability,
        "n_assigned": n_as,
        "n_unassigned": n_un,
        "n_topics": int((topic_dim["topic_id"] != "unassigned").sum()),
        "new_yt_embeddings": len(new_emb_rows),
    }
    (REPORTS_DIR / "ontology_summary.json").write_text(json.dumps(meta, indent=2))
    con.close()
    print("Layer 2 complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
