#!/usr/bin/env python3
"""
Risk / theme multi-label classification — calibrated bi-encoder + NLI ensemble,
full corpus. REPLACES the prior UNSOUND flat-threshold version.

--------------------------------------------------------------------------------
Prior version's verified failure (docs/GEOPOLITICAL_ONTOLOGY_SPEC.md §2.3): a
single flat cosine-similarity threshold (0.45) across all 9 risk + 12 theme
categories, scored 29% strict / 45% lenient precision on a hand-judged n=42
sample, with confirmed recall gaps (a post explicitly about "EU sanctions don't
work, cost >EUR3T" scored zero on all 21 categories). Root cause measured then:
bge-m3 cosine similarity between a post embedding and a short category-description
embedding is compressed into a narrow band (~0.09-0.70, corpus mean ~0.377) that
does not move enough between "on-topic" and "off-topic" posts for one flat cut
(or even a per-category cut alone) to discriminate well.

THIS SESSION'S OWN CALIBRATION CONFIRMS THE SAME THING, HARDER THAN EXPECTED:
a fresh, hand-judged, stratified sample of 105 posts (5/category, spanning each
category's own p75/p90/p97 score bands — i.e. already restricted to each
category's *highest*-scoring candidates) found bi-encoder cosine score ALONE
tops out at only ~30-48% precision even when the threshold is pushed as high as
0.50 (see calibration numbers below) — confirming that better per-category
thresholds alone cannot fix this; the failure is in what cosine similarity to a
short description can discriminate at all, not just where the cut is placed.

FIX — two changes, justified by the evidence above:

1. Tightened category descriptions (CATEGORY_DESCRIPTIONS below) with explicit
   inclusion/exclusion language for the bleed patterns the prior verify pass
   named: `bri` now explicitly excludes domestic-China infrastructure ("outside
   China... not a domestic Chinese project"); `sanctions` explicitly excludes
   blockades/military embargoes; `insurgencies` explicitly excludes interstate
   war between regular state militaries; `maritime_security` (military/naval)
   vs `maritime_dimensions` (commercial/non-military) now split on the
   military/non-military axis explicitly; `project_delays` now requires a named
   concrete project, not general economic commentary.

2. A second-opinion cross-lingual NLI classifier — mDeBERTa-v3-base-mnli-xnli
   (multilingual, ~100 languages, matches the corpus's en/ru/zh-cn/bn/uk/hi mix)
   — makes the FINAL accept/reject decision per (post, category) pair. The
   bi-encoder is used only as a cheap recall net (BIENCODER_FLOOR, per category)
   to bound how many (post, category) pairs need the much more expensive NLI
   pass; it is not the precision decision anymore.

   Calibration (n=105 hand-judged sample, /tmp scratchpad calib_with_nli_v2.csv,
   this session): using a *short single-clause* NLI hypothesis per category
   (NLI_HYPOTHESES below — long, disjunctive description-style hypotheses
   measured markedly worse) and the full 3-way softmax P(entailment):
     - bi-encoder cosine alone, best point on the curve (t=0.50): 47.6% precision
       / 35.7% recall (lenient ground truth).
     - NLI P(entailment) >= 0.22 (this script's NLI_THRESHOLD): 72.2% precision
       / 46.4% recall (lenient); 61.1% precision / 64.7% recall (strict).
   NLI_THRESHOLD=0.22 was picked as the best-F1 point on this curve, not the
   single best-precision point, to avoid re-creating a recall-gap problem.
   BIENCODER_FLOOR=0.35 (roughly each category's own p50-p65) is a generous
   recall net — deliberately far below the old 0.45 cut — sized only to keep
   the NLI pass (the actual decision) computationally tractable on CPU
   (GPU is vLLM-held per docs/GEO_ONTOLOGY_CONTEXT.md; CPU throughout).

`score` (kept for backward compatibility with scripts/11_geo_timeline_metrics.py
and UI/build_dashboard.py, both of which only read post_uid/category/score/
gray_zone) is now the NLI P(entailment) — a meaningful, non-compressed [0,1]
confidence, not the old compressed cosine. The raw bi-encoder cosine is kept
alongside as `biencoder_score` for transparency/debugging. `gray_zone` is now
true when NLI P(entailment) sits within GRAY_ZONE_LO..GRAY_ZONE_HI of
NLI_THRESHOLD (still-uncertain band), not near the old 0.45 cosine cut.

Reads FULL corpus from data/analytics.duckdb (read-only, never written).
Writes risk_labels / theme_labels to data/geo_ontology.duckdb (sole writer here;
opened read_only=False only for the final write, after all compute is done).
"""
from __future__ import annotations

import sys
import time
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

EDA_ROOT = Path(__file__).resolve().parents[1]
SRC_DB = EDA_ROOT / "data" / "analytics.duckdb"
OUT_DB = EDA_ROOT / "data" / "geo_ontology.duckdb"
# Chunk-level checkpointing: the NLI pass is the expensive stage (hours on CPU) and this
# same class of job has previously been cut short by a session/time limit mid-run. Each
# completed chunk's scores are persisted here immediately so a re-run after an interruption
# resumes from the last completed chunk instead of recomputing from zero. Safe to resume
# because `cand` (and therefore chunking) is a deterministic function of the corpus +
# BIENCODER_FLOOR + category set, all fixed for a given run.
CHECKPOINT_DIR = EDA_ROOT / "data" / "_risk_theme_nli_checkpoint"

BIENCODER_MODEL = "BAAI/bge-m3"
NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
METHOD = "bge_m3_recall_floor+mdeberta_v3_nli_xnli_ensemble_v1"

BIENCODER_FLOOR = 0.35        # candidate/recall-net cut, per category (see docstring)
NLI_THRESHOLD = 0.22          # decisive precision gate, calibrated (see docstring)
GRAY_ZONE_LO, GRAY_ZONE_HI = 0.14, 0.30
NLI_MAX_CHARS = 800            # premise truncation before the tokenizer's own truncation
NLI_MAX_LEN = 256
NLI_BATCH = 64
N_WORKERS = 6                  # 6 x 8 threads = 48 cores (see nvidia-smi check: GPU vLLM-held)
THREADS_PER_WORKER = 8
PROGRESS_EVERY_CHUNKS = 10

# ---- bi-encoder side: tightened, exclusion-aware category descriptions ----
CATEGORY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "risk": {
        "domestic_instability": "Political unrest, mass protests, riots, government collapse, coups, or internal power struggles within a single country's domestic politics, threatening that country's internal stability. Not a war between two states.",
        "policy_shifts": "A national government, ministry, or regulator formally announces, reverses, or debates a specific law, trade policy, tariff, foreign-policy stance, or regulatory rule.",
        "project_delays": "A specific named infrastructure, energy, or investment project such as a port, railway, dam, or plant is stalled, postponed, over budget, defaulted on, or cancelled due to funding, contractor, or approval problems. Not a general economic or budget commentary unconnected to one concrete project.",
        "sanctions": "One country or bloc of countries formally imposes, discusses, evades, or lifts economic sanctions, export-control restrictions, or asset freezes against another state, entity, or individuals as a legal or diplomatic punitive measure. Not a naval blockade, a military embargo, or ordinary trade friction.",
        "border_conflicts": "Military standoffs, troop buildups, artillery exchanges, skirmishes, or territorial disputes between the armed forces of two or more states along a shared land border or line of control.",
        "insurgencies": "Non-state armed groups such as separatist militants, rebels, or insurgents fighting, attacking, or controlling territory inside a state. Distinct from a war between two national governments or their regular armed forces.",
        "shipping_disruptions": "A commercial shipping lane, port, or vessel is physically blocked, congested, attacked, seized, or delayed by piracy, vessel strikes, canal or strait closures, or port shutdowns, disrupting the physical flow of maritime trade.",
        "pipeline_vulnerabilities": "A specific named oil, gas, or energy pipeline or its supporting infrastructure is threatened, sabotaged, attacked, leaking, or suffers a technical failure.",
        "international_pressure": "Multiple foreign governments, the United Nations, or an international coalition jointly criticize, condemn, or issue a formal statement or resolution pressuring a state over its actions. Distinct from one country's own unilateral sanctions or policy.",
    },
    "theme": {
        "political_relations": "Diplomatic relations, state visits, bilateral meetings, agreements, or political alignment specifically between China and another country's government.",
        "economic_relations": "Trade flows, investment deals, loans, or economic cooperation and dependency specifically between China and a partner country, not a country's domestic economy alone.",
        "bri": "A Belt and Road Initiative project: a port, railway, road, or infrastructure financed, built, or operated by China in a foreign country outside China. Explicitly not a domestic Chinese infrastructure project built inside China's own borders.",
        "energy_security": "A country's oil or gas supply routes, energy imports, power-grid projects, or dependency on external energy sources, and the strategic risk this creates.",
        "maritime_security": "Military naval activity at sea: warship patrols, freedom-of-navigation operations, naval exercises, or armed incidents and skirmishes involving military vessels.",
        "maritime_dimensions": "Non-military maritime and coastal economic activity: commercial port development, undersea cables, fisheries, or blue-economy projects, without an armed or military angle.",
        "security_military_relations": "Defence cooperation, arms sales, joint military exercises, training, or formal security pacts between two or more states' militaries.",
        "border_issues": "Territorial claims, boundary demarcation disputes, or land-border infrastructure and security issues between neighbouring states, not open combat.",
        "internal_conflict_china_role": "China is explicitly named as playing a role in, supporting a side of, or influencing another country's internal political crisis or armed conflict.",
        "tech_digital_influence": "Chinese-made technology, telecom equipment, 5G networks, surveillance systems, AI platforms, or digital-influence and propaganda operations exported to or used in another country.",
        "humanitarian_dimension": "Refugee or displaced-persons flows, disaster relief, humanitarian aid delivery, or civilian suffering caused by conflict or a natural disaster.",
        "international_relations": "Multilateral diplomacy, international organisations such as the UN, G7, or ASEAN, alliances, or global power competition among major powers, not specific to a China-bilateral relationship.",
    },
}

# ---- NLI side: short, single-clause hypotheses (measured to work far better
# than reusing the long disjunctive bi-encoder descriptions as NLI hypotheses
# -- see docstring calibration numbers) ----
NLI_HYPOTHESES: dict[str, str] = {
    "domestic_instability": "This text describes political unrest, protests, or instability within one country.",
    "policy_shifts": "This text describes a government announcing or changing a policy.",
    "project_delays": "This text describes a specific infrastructure or investment project being delayed, over budget, or cancelled.",
    "sanctions": "This text describes economic sanctions or trade restrictions imposed by one country on another.",
    "border_conflicts": "This text describes a military conflict between the armed forces of two countries at their shared border.",
    "insurgencies": "This text describes non-state armed rebels or insurgents fighting within a country.",
    "shipping_disruptions": "This text describes a shipping lane, port, or vessel being disrupted, blocked, or attacked.",
    "pipeline_vulnerabilities": "This text describes an oil or gas pipeline being attacked, sabotaged, or damaged.",
    "international_pressure": "This text describes multiple countries or an international body jointly condemning or pressuring a state.",
    "political_relations": "This text describes diplomatic relations between China and another country.",
    "economic_relations": "This text describes trade or investment relations between China and another country.",
    "bri": "This text describes a Belt and Road Initiative infrastructure project that China is building in another country.",
    "energy_security": "This text describes a country's energy supply routes or energy dependency risk.",
    "maritime_security": "This text describes military naval activity at sea.",
    "maritime_dimensions": "This text describes commercial shipping, ports, or ocean economic activity without military conflict.",
    "security_military_relations": "This text describes defence cooperation or arms sales between two countries' militaries.",
    "border_issues": "This text describes a territorial or land border dispute between two countries.",
    "internal_conflict_china_role": "This text describes China's role in another country's internal conflict.",
    "tech_digital_influence": "This text describes Chinese technology or digital influence being used in another country.",
    "humanitarian_dimension": "This text describes refugees, disaster relief, or humanitarian suffering.",
    "international_relations": "This text describes multilateral diplomacy or global power competition.",
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================================================================
# Stage 1: bi-encoder candidate generation (recall net)
# ============================================================================

def compute_biencoder_sims() -> tuple[pd.DataFrame, np.ndarray, list[str], list[str]]:
    """Returns (posts_df[post_uid,text,lang,platform], sims[n_posts,n_cats], groups, keys)."""
    con = duckdb.connect(str(SRC_DB), read_only=True)
    posts = con.execute(
        """
        SELECT e.post_uid, e.embedding, p.text, p.lang, p.platform
        FROM post_embeddings e
        JOIN post_facts p USING (post_uid)
        WHERE e.embedding IS NOT NULL
        """
    ).df()
    con.close()
    log(f"posts with embeddings: {len(posts):,}")

    X = np.vstack(posts["embedding"].apply(np.array).values).astype(np.float32)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    groups, keys, descs = [], [], []
    for grp in ("risk", "theme"):
        for k, v in CATEGORY_DESCRIPTIONS[grp].items():
            groups.append(grp)
            keys.append(k)
            descs.append(v)

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(BIENCODER_MODEL, device="cpu")
    D = model.encode(descs, normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)
    sims = X @ D.T
    log(f"bi-encoder sims: shape={sims.shape} min={sims.min():.3f} max={sims.max():.3f} "
        f"mean={sims.mean():.3f}")
    return posts[["post_uid", "text", "lang", "platform"]], sims, groups, keys


def build_candidates(posts: pd.DataFrame, sims: np.ndarray, groups: list[str],
                      keys: list[str]) -> pd.DataFrame:
    rows = []
    for gi, (g, k) in enumerate(zip(groups, keys)):
        col = sims[:, gi]
        idx = np.where(col >= BIENCODER_FLOOR)[0]
        for i in idx:
            rows.append((posts.iloc[i]["post_uid"], g, k, float(col[i]), i))
    cand = pd.DataFrame(rows, columns=["post_uid", "group", "category", "biencoder_score", "post_idx"])
    log(f"candidates after floor={BIENCODER_FLOOR}: {len(cand):,} pairs "
        f"({cand['post_uid'].nunique():,} distinct posts, avg "
        f"{len(cand)/max(1,cand['category'].nunique()):.0f}/category)")
    return cand


# ============================================================================
# Stage 2: NLI second-opinion pass (decisive precision filter)
# ============================================================================

_WORKER_MODEL = None
_WORKER_TOK = None


def _nli_worker_init() -> None:
    global _WORKER_MODEL, _WORKER_TOK
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    torch.set_num_threads(THREADS_PER_WORKER)
    _WORKER_TOK = AutoTokenizer.from_pretrained(NLI_MODEL)
    _WORKER_MODEL = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL)
    _WORKER_MODEL.eval()


def _nli_score_chunk(chunk: list[tuple[str, str]]) -> np.ndarray:
    """chunk: list of (premise, hypothesis). Returns P(entailment), 3-way softmax."""
    import torch
    out = np.empty(len(chunk), dtype=np.float32)
    for start in range(0, len(chunk), NLI_BATCH):
        batch = chunk[start:start + NLI_BATCH]
        premises = [p for p, _ in batch]
        hyps = [h for _, h in batch]
        enc = _WORKER_TOK(premises, hyps, return_tensors="pt", truncation="only_first",
                           max_length=NLI_MAX_LEN, padding=True)
        with torch.no_grad():
            logits = _WORKER_MODEL(**enc).logits
        probs = torch.softmax(logits, dim=-1).numpy()
        out[start:start + len(batch)] = probs[:, 0]  # id2label[0] == 'entailment'
    return out


def run_nli(cand: pd.DataFrame, posts: pd.DataFrame) -> np.ndarray:
    text_by_idx = posts["text"].values
    pairs: list[tuple[str, str]] = []
    for row in cand.itertuples(index=False):
        premise = text_by_idx[row.post_idx]
        premise = (premise if isinstance(premise, str) else "")[:NLI_MAX_CHARS]
        pairs.append((premise, NLI_HYPOTHESES[row.category]))

    n = len(pairs)
    chunk_size = max(1, -(-n // (N_WORKERS * 20)))  # ~20 chunks per worker, for progress visibility
    chunks = [pairs[i:i + chunk_size] for i in range(0, n, chunk_size)]
    log(f"NLI pass: {n:,} pairs, {len(chunks)} chunks of ~{chunk_size}, "
        f"{N_WORKERS} workers x {THREADS_PER_WORKER} threads")

    # Resume support: a chunk's identity is (n, chunk_size, chunk_index) -- stable across
    # re-runs as long as candidate generation is unchanged, so a stale checkpoint dir from a
    # different corpus/floor/category set is auto-detected (n or chunk_size won't match the
    # filenames' embedded values) and ignored rather than silently reused.
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    ckpt_tag = f"n{n}_cs{chunk_size}"

    def ckpt_path(i: int) -> Path:
        return CHECKPOINT_DIR / f"{ckpt_tag}_chunk{i:05d}.npy"

    results: list[np.ndarray | None] = [None] * len(chunks)
    todo: list[int] = []
    for i in range(len(chunks)):
        p = ckpt_path(i)
        if p.exists():
            try:
                arr = np.load(p)
                if len(arr) == len(chunks[i]):
                    results[i] = arr
                    continue
            except Exception:
                pass  # corrupt/partial checkpoint file -- recompute this chunk
        todo.append(i)
    n_done_already = len(chunks) - len(todo)
    if n_done_already:
        log(f"resuming: {n_done_already}/{len(chunks)} chunks already checkpointed in "
            f"{CHECKPOINT_DIR}, {len(todo)} left to compute")

    t0 = time.time()
    done = n_done_already
    if todo:
        # IMPORTANT: use 'spawn', not the platform-default 'fork'. compute_biencoder_sims()
        # (called earlier in this same process) loads PyTorch via SentenceTransformer, which
        # initializes native (OpenMP/MKL) worker thread pools. Forking after that copies only
        # the calling thread into each child -- the inherited thread-pool mutexes are left
        # permanently locked with no owner to release them, so any child that then tries to
        # thread (torch.set_num_threads, a forward pass) hangs forever. Verified by direct
        # repro during this build: identical child_init/work under 'fork' hung indefinitely
        # (all workers stuck at ~0% CPU, state 'S'); switching to 'spawn' (fresh interpreter,
        # no inherited thread state) completed the same workload in <10s. 'spawn' costs one
        # extra model load per worker (~1-2s) but that's the only difference.
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=N_WORKERS, initializer=_nli_worker_init,
                                  mp_context=ctx) as ex:
            futs = {ex.submit(_nli_score_chunk, chunks[i]): i for i in todo}
            from concurrent.futures import as_completed
            for fut in as_completed(futs):
                i = futs[fut]
                arr = fut.result()
                results[i] = arr
                np.save(ckpt_path(i), arr)
                done += 1
                if done % PROGRESS_EVERY_CHUNKS == 0 or done == len(chunks):
                    elapsed = time.time() - t0
                    rate = (done - n_done_already) / elapsed if elapsed > 0 else float("nan")
                    remaining = len(chunks) - done
                    eta = remaining / rate if rate > 0 else float("nan")
                    log(f"  ... {done}/{len(chunks)} chunks, {elapsed:.0f}s elapsed, "
                        f"ETA {eta/60:.1f} min")
    return np.concatenate(results)


# ============================================================================
# Stage 3: write to geo_ontology.duckdb
# ============================================================================

def write_tables(cand: pd.DataFrame) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cand = cand.copy()
    cand["gray_zone"] = cand["nli_score"].between(GRAY_ZONE_LO, GRAY_ZONE_HI)
    cand["method"] = METHOD
    cand["biencoder_floor"] = BIENCODER_FLOOR
    cand["nli_threshold"] = NLI_THRESHOLD
    cand["scored_at"] = now
    # `score` kept for backward compatibility (scripts/11_geo_timeline_metrics.py,
    # UI/build_dashboard.py both read post_uid/category/score/gray_zone only) --
    # now the NLI P(entailment), a meaningful confidence, not the old compressed cosine.
    cand = cand.rename(columns={"nli_score": "score"})
    out_cols = ["post_uid", "category", "score", "biencoder_score", "biencoder_floor",
                "nli_threshold", "method", "gray_zone", "scored_at"]

    risk = cand.loc[cand["group"] == "risk", out_cols].reset_index(drop=True)
    theme = cand.loc[cand["group"] == "theme", out_cols].reset_index(drop=True)

    con = duckdb.connect(str(OUT_DB), read_only=False)
    con.register("_risk_labels", risk)
    con.execute("CREATE OR REPLACE TABLE risk_labels AS SELECT * FROM _risk_labels")
    con.unregister("_risk_labels")
    con.register("_theme_labels", theme)
    con.execute("CREATE OR REPLACE TABLE theme_labels AS SELECT * FROM _theme_labels")
    con.unregister("_theme_labels")

    log("\n--- risk_labels counts per category (post-NLI) ---")
    for r in con.execute(
        "SELECT category, COUNT(*) n, ROUND(AVG(score),3) avg_score, "
        "SUM(gray_zone::INT) n_gray FROM risk_labels GROUP BY category ORDER BY n DESC"
    ).fetchall():
        log(f"  {r}")
    log("\n--- theme_labels counts per category (post-NLI) ---")
    for r in con.execute(
        "SELECT category, COUNT(*) n, ROUND(AVG(score),3) avg_score, "
        "SUM(gray_zone::INT) n_gray FROM theme_labels GROUP BY category ORDER BY n DESC"
    ).fetchall():
        log(f"  {r}")

    n_posts_risk = con.execute("SELECT COUNT(DISTINCT post_uid) FROM risk_labels").fetchone()[0]
    n_posts_theme = con.execute("SELECT COUNT(DISTINCT post_uid) FROM theme_labels").fetchone()[0]
    n_posts_either = con.execute(
        "SELECT COUNT(*) FROM (SELECT post_uid FROM risk_labels "
        "UNION SELECT post_uid FROM theme_labels)"
    ).fetchone()[0]
    log(f"\ndistinct posts with >=1 risk label:  {n_posts_risk:,}")
    log(f"distinct posts with >=1 theme label: {n_posts_theme:,}")
    log(f"distinct posts with >=1 risk OR theme label: {n_posts_either:,}")
    con.close()
    log(f"\nwrote {OUT_DB}: risk_labels={len(risk):,} rows, theme_labels={len(theme):,} rows")


def main() -> int:
    if not SRC_DB.exists():
        print(f"ERROR: source db missing: {SRC_DB}")
        return 1

    t_start = time.time()
    posts, sims, groups, keys = compute_biencoder_sims()
    cand = build_candidates(posts, sims, groups, keys)

    nli_scores = run_nli(cand, posts)
    cand["nli_score"] = nli_scores
    log(f"NLI pass done in {(time.time()-t_start)/60:.1f} min total elapsed")

    kept = cand[cand["nli_score"] >= NLI_THRESHOLD].copy()
    log(f"pairs passing NLI_THRESHOLD={NLI_THRESHOLD}: {len(kept):,}/{len(cand):,} "
        f"({100*len(kept)/max(1,len(cand)):.1f}%)")

    write_tables(kept)

    import shutil
    if CHECKPOINT_DIR.exists():
        shutil.rmtree(CHECKPOINT_DIR, ignore_errors=True)
        log(f"cleared NLI checkpoint dir {CHECKPOINT_DIR} (run completed successfully)")

    log(f"\ntotal wall time: {(time.time()-t_start)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
