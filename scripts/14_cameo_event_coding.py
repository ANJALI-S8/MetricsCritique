"""14 -- CAMEO/GDELT-compatible two-axis event coding.

Implements the Global Geopolitical Event & Topic Coding Framework v1.0:
  * Section A (axis `theme`)  -- WHAT the document is about. Always at least one.
  * Section B (axis `action`) -- WHAT KIND of action occurred. Conditional.

The taxonomy itself is data (`scripts/lib/taxonomy_cameo.py` ->
`data/taxonomy_cameo.json`); this script is only the scorer.


WHY THIS EXISTS -- THE DEFECT IT FIXES
======================================
The predecessor (`12_risk_theme_classification.py`) has 9 "risk" categories
that conflate *subject matter* with *action type* -- exactly the confusion the
A/B split exists to remove:

    sanctions            = subject 02.4 + action 16 REDUCE RELATIONS
    border_conflicts     = subject 07.4 + action 18/19 ASSAULT/FIGHT
    shipping_disruptions = subject 05.3 + action 17/19 (blockade)
    international_pressure = almost purely action 13 THREATEN

Because those are two independent questions crammed into one label, a post can
plausibly satisfy several at once; the audit measured **192 posts carrying >=6
of the 9 risk categories**. Splitting the axes is a modelling change, not a
renaming: each axis becomes separately answerable, and a post carrying 6 of 9
subject codes is then a legible defect rather than the expected behaviour.

`risk_labels` / `theme_labels` are deliberately LEFT IN PLACE and untouched.
Nothing downstream breaks mid-migration, and old-vs-new agreement stays
measurable rather than asserted.


CASCADE -- WHY THE FULL 59-CODE SCHEME COSTS ABOUT WHAT 21 FLAT CODES DID
=========================================================================
Measured baseline from logs/12_risk_theme_classification_run2.log: 21 codes ->
288,993 candidate pairs -> 5.0 h wall (16.15 pairs/s, 6 workers x 8 threads,
CPU -- the GPU is held by a vLLM process).

    approach                        codes    pairs    NLI wall
    today (flat)                       21     289k       5.0 h  (measured)
    flat, all sub-codes + actions      ~59     812k        14 h  (projected)
    cascade (this script)              ~59     260k       ~4.5 h (projected)

The cascade works because BIENCODER_FLOOR admits a roughly constant fraction of
posts per category (bge-m3 cosine is compressed: mean 0.373 on this corpus), so
cost scales with *categories scored*, not with taxonomy size. Scoring 10
parents, then sub-codes only inside ACCEPTED parents, then actions only on
posts that got >=1 thematic code, collapses the multiplication.

`--level parent` runs stage 1 only. That is the reviewable checkpoint: it is
the cheapest run that produces something to look at, and sub-codes are
meaningless if the parents are wrong.


CALIBRATION -- WHAT WAS WRONG BEFORE, AND WHAT IS DIFFERENT HERE
================================================================
Script 12's NLI_THRESHOLD = 0.22 came from a hand-judged sample stored in a
`/tmp` file that no longer exists. That is unreproducible, and the audit found
three further problems with it:

  1. **The sample only covered the top score quartile.** 61% of judged pairs
     sat outside the band the threshold actually operates on, so the estimate
     was fitted where it mattered least.
  2. **One global threshold for 21 classes.** Classes differ enormously in how
     well a short NLI hypothesis discriminates them; a single cut is a strong
     and unexamined assumption.
  3. **18 positives total** (Wilson CI ~0.49-0.88) and **zero Bengali**, on a
     corpus where the NLI model's own training set (XNLI) covers neither
     Bengali nor Ukrainian.

So `--calibrate` here:
  * draws **stratified by class x language x score-band**, from the actual
    production candidate distribution -- not from the top of the ranking;
  * writes **`data/calibration_cameo.csv`, committed in-repo**, so the
    threshold is reproducible from a file anyone can re-open and re-judge;
  * fits a **per-class** threshold with a **Wilson interval**, and
  * refuses to fit where the evidence is too thin: a class with too few judged
    positives is written as `uncalibrated` and falls back to the global
    threshold **with `threshold_source` recorded per row in the DB**, rather
    than silently borrowing a number and looking calibrated.

Each class also gets its own gray band (nearest scores either side of its
threshold) instead of the fixed 0.14-0.30, which straddled 0.22 and therefore
collapsed `gray_zone` to "0.22-0.30" -- it flagged only near-miss *accepts* and
lost the near-miss *rejections*, which are the more interesting half.


OUTPUT
======
`event_codes` in geo_ontology.duckdb, one row per (post, code):

    post_uid, axis, code, code_key, parent_code, level, score,
    biencoder_score, nli_threshold, threshold_source, gray_zone,
    method, scored_at

One table with an `axis` column rather than three tables: hierarchy queries
("give me everything under 05") need a single scan, and the metrics layer keys
panels off (axis, level) anyway.


USAGE
=====
    # 1. draw the stratified calibration sample (fast -- scores the sample only)
    python scripts/14_cameo_event_coding.py --calibrate 300

    # 2. judge data/calibration_cameo.csv (fill the `judgment` column: 1/0)
    # 3. fit per-class thresholds from the judged file
    python scripts/14_cameo_event_coding.py --fit-thresholds

    # 4. full parent pass
    python scripts/14_cameo_event_coding.py --level parent

    # 5. cascade to sub-codes and actions (phase 3)
    python scripts/14_cameo_event_coding.py --level all
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# ---- lab-env torch/transformers shim (see docs/GEO_ONTOLOGY_CONTEXT.md).
# Script 12 lacks this and only works because it happens to be launched from an
# env that already has torch. ----
try:
    import torch  # noqa: F401
except ImportError:                                              # pragma: no cover
    _LAB_ENV = ("/home/hariramanan/NTRO/Entity_extraction/.venv/lib/python3.12/"
                "site-packages")
    if _LAB_ENV not in sys.path:
        sys.path.insert(0, _LAB_ENV)

EDA_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDA_ROOT / "scripts" / "lib"))
import taxonomy_cameo  # noqa: E402

SRC_DB = EDA_ROOT / "data" / "analytics.duckdb"
OUT_DB = EDA_ROOT / "data" / "geo_ontology.duckdb"
CALIB_PATH = EDA_ROOT / "data" / "calibration_cameo.csv"
THRESH_PATH = EDA_ROOT / "data" / "thresholds_cameo.json"
# Chunk-level checkpointing: this pass is hours on CPU and this class of job has
# been cut short mid-run before. The tag embeds the pair count, so a taxonomy or
# floor change auto-invalidates a stale checkpoint dir rather than silently
# reusing scores computed for a different candidate set.
CHECKPOINT_DIR = EDA_ROOT / "data" / "_cameo_nli_checkpoint"

BIENCODER_MODEL = "BAAI/bge-m3"
NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
METHOD = "cameo_v1_bge_m3_floor+mdeberta_v3_nli_xnli_cascade"

BIENCODER_FLOOR = 0.35     # recall net, per class (bge-m3 cosine is compressed)
MIN_JUDGED_POSITIVES = 8   # below this a class is 'uncalibrated', never guessed

# ---- decision score: MEASURED, not inherited ----------------------------
# The predecessor treats the NLI entailment probability as the *decisive*
# gate and discards the bi-encoder cosine after candidate generation. On the
# 636-pair hand-judged sample in data/calibration_cameo.csv that is backwards.
# Sampling-weighted (Horvitz-Thompson, correcting for the score-band strata)
# per-class AUC:
#
#     class                            NLI     bi-encoder
#     a01 political/diplomatic        0.551      0.754
#     a02 economic relations          0.725      0.884
#     a03 infrastructure              0.870      0.988
#     a04 energy security             0.532      0.740
#     a05 maritime                    0.759      0.839
#     a06 security/military           0.416      0.590   <- NLI below chance
#     a07 border issues               0.485      0.981
#     a08 internal conflict           0.711      0.730
#     a09 technology/digital          0.500      0.866
#     a10 humanitarian                0.983      0.997
#     ------------------------------------------------
#     mean                            0.653      0.837
#
# The bi-encoder ranks better on 10 of 10 classes (sign test p ~ 0.001), and
# NLI is at or below chance on four. The likely cause is the corpus: a large
# share of premises are 5-to-10-word multilingual alert fragments
# ("Загроза балістики зі Сходу"), and mDeBERTa-v3-mnli-xnli degenerates toward
# entailment on those -- one such Ukrainian air-alert line scored 0.95 for
# "maritime", 0.73 for "technology" and 0.70 for "internal conflict". bge-m3 is
# trained for exactly this multilingual-retrieval setting and does not.
#
# So NLI is no longer a veto. It is one weighted term in the decision score.
# The weight was chosen on the calibration set by mean per-class F1
# (w=0.0 -> 0.497, 0.1 -> 0.532, **0.2 -> 0.538**, 0.3 -> 0.496, 0.5 -> 0.473)
# and is deliberately a SINGLE GLOBAL constant rather than per-class: with 2-14
# judged positives per class, a per-class weight would be fitting noise.
# One tuned degree of freedom on this sample is already a caveat worth stating.
NLI_BLEND_W = 0.2
GLOBAL_THRESHOLD = 0.42    # fallback on the blended scale (see thresholds file)


def blended_score(biencoder: np.ndarray, nli: np.ndarray) -> np.ndarray:
    """The decision score. Bi-encoder cosine rescaled from its live range
    [BIENCODER_FLOOR, 0.70] onto [0,1], plus a small NLI term.

    Rescaling matters: raw bge-m3 cosine on this corpus spans roughly
    0.35-0.70 after the floor, so leaving it unscaled would give the NLI term
    (a full 0-1 probability) several times the intended influence.
    """
    bi = (np.asarray(biencoder, dtype=float) - BIENCODER_FLOOR) / (0.70 - BIENCODER_FLOOR)
    bi = np.clip(bi, 0.0, 1.0)
    return (1.0 - NLI_BLEND_W) * bi + NLI_BLEND_W * np.asarray(nli, dtype=float)
NLI_MAX_CHARS = 800
NLI_MAX_LEN = 256
NLI_BATCH = 64
N_WORKERS = 6              # 6 x 8 threads = 48 cores (GPU is vLLM-held)
THREADS_PER_WORKER = 8
PROGRESS_EVERY_CHUNKS = 10

# Score bands for stratified calibration sampling. The old sample sat entirely
# in the top band, which is why its threshold was fitted where it mattered
# least -- the decision boundary lives near the bottom of the candidate set.
SCORE_BANDS = [(0.35, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 1.01)]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================================================================
# taxonomy access
# ============================================================================

def load_taxonomy() -> tuple[dict, list[dict]]:
    tax = taxonomy_cameo.build()
    errs = taxonomy_cameo.validate(tax)
    if errs:
        for e in errs:
            log(f"TAXONOMY INVALID: {e}")
        raise SystemExit("refusing to code against an invalid taxonomy")
    return tax, tax["codes"]


def select_codes(codes: list[dict], level: str) -> list[dict]:
    """Which classes stage 1 scores. Stages 2/3 select dynamically per post."""
    if level in ("parent", "all"):
        return [c for c in codes if c["axis"] == "theme" and c["level"] == 1]
    raise ValueError(f"unknown level {level!r}")


def subcodes_of(codes: list[dict]) -> list[dict]:
    return [c for c in codes if c["axis"] == "theme" and c["level"] == 2]


def action_codes(codes: list[dict]) -> list[dict]:
    return [c for c in codes if c["axis"] == "action"]


def allowed_for_subcodes(kept_parents: pd.DataFrame,
                         subs: list[dict]) -> dict[str, set]:
    """code_key -> posts eligible for it: a sub-code is only scored against posts
    whose *parent* code was accepted. This is the whole reason the cascade is
    affordable -- scoring 41 sub-codes flat against every post would cost more
    than the parents did, and would also be wrong: a maritime sub-code has no
    business being tested on a post that is not maritime."""
    by_parent: dict[str, set] = {}
    for code, uid in zip(kept_parents["code"], kept_parents["post_uid"]):
        by_parent.setdefault(code, set()).add(uid)
    return {s["key"]: by_parent.get(s["parent_code"], set()) for s in subs}


def allowed_for_actions(kept_theme: pd.DataFrame,
                        actions: list[dict]) -> dict[str, set]:
    """Section B is conditional on Section A: an action code is only scored
    against posts that received at least one thematic code. A post with no
    subject matter has no geopolitical action to code."""
    posts_with_theme = set(kept_theme["post_uid"])
    return {a["key"]: posts_with_theme for a in actions}


# ============================================================================
# Stage 1: bi-encoder candidate generation (recall net)
# ============================================================================

def load_posts() -> pd.DataFrame:
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
    return posts


def biencoder_sims(posts: pd.DataFrame, codes: list[dict]) -> np.ndarray:
    X = np.vstack(posts["embedding"].apply(np.array).values).astype(np.float32)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(BIENCODER_MODEL, device="cpu")
    D = model.encode([c["description"] for c in codes],
                     normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)
    sims = X @ D.T
    log(f"bi-encoder sims: shape={sims.shape} min={sims.min():.3f} "
        f"max={sims.max():.3f} mean={sims.mean():.3f}")
    return sims


CAND_COLS = ["post_uid", "axis", "code_key", "code", "parent_code", "level",
             "biencoder_score", "post_idx"]


def build_candidates(posts: pd.DataFrame, sims: np.ndarray, codes: list[dict],
                     allowed: dict[str, set] | None = None,
                     label: str = "candidates") -> pd.DataFrame:
    """(post, code) pairs above the bi-encoder floor.

    `allowed` prunes the grid before the floor is applied and is what makes the
    cascade affordable: stage 2 only scores a sub-code against posts whose
    *parent* was accepted, stage 3 only scores actions against posts that got at
    least one thematic code. Keyed by code_key -> set of post_uid.

    `allowed=None` disables pruning entirely -- that is stage 1, where every code
    is scored against every post. When a map IS given, a code missing from it is
    scored against *nothing*: fail closed, so a taxonomy entry the cascade forgot
    to route produces zero rows rather than a silent full-corpus scan."""
    rows = []
    uids = posts["post_uid"].values
    for ci, c in enumerate(codes):
        col = sims[:, ci]
        idx = np.where(col >= BIENCODER_FLOOR)[0]
        allow = None if allowed is None else allowed.get(c["key"], set())
        for i in idx:
            uid = uids[i]
            if allow is not None and uid not in allow:
                continue
            rows.append((uid, c["axis"], c["key"], c["code"],
                         c["parent_code"], c["level"], float(col[i]), int(i)))
    cand = pd.DataFrame(rows, columns=CAND_COLS)
    n_posts = cand["post_uid"].nunique() if len(cand) else 0
    log(f"{label} after floor={BIENCODER_FLOOR}: {len(cand):,} pairs "
        f"({n_posts:,} distinct posts, {len(cand)/max(1, len(codes)):.0f}/class avg)")
    return cand


def apply_thresholds(cand: pd.DataFrame, codes: list[dict]) -> pd.DataFrame:
    """Blend, look up each class's own threshold, and cut.

    Re-reads the thresholds file rather than reusing a copy loaded before the NLI
    pass: that pass is hours long, thresholds are cheap to refit and may
    legitimately have improved while it ran, and scores do not depend on them --
    only the final cut does."""
    cand = cand.copy()
    cand["score"] = blended_score(cand["biencoder_score"].values,
                                  cand["nli_score"].values)
    cls = load_thresholds(codes).get("classes", {})
    cand["nli_threshold"] = [cls.get(k, {}).get("threshold", GLOBAL_THRESHOLD)
                             for k in cand["code_key"]]
    cand["threshold_source"] = [cls.get(k, {}).get("source", "global_fallback")
                                for k in cand["code_key"]]
    gray_lo = np.array([cls.get(k, {}).get("gray_lo", GLOBAL_THRESHOLD - 0.08)
                        for k in cand["code_key"]])
    gray_hi = np.array([cls.get(k, {}).get("gray_hi", GLOBAL_THRESHOLD + 0.08)
                        for k in cand["code_key"]])
    cand["gray_zone"] = (cand["score"].values >= gray_lo) & (cand["score"].values <= gray_hi)
    kept = cand[cand["score"] >= cand["nli_threshold"]].copy()
    log(f"pairs passing their class threshold: {len(kept):,}/{len(cand):,} "
        f"({100*len(kept)/max(1,len(cand)):.1f}%)")
    return kept


# ============================================================================
# Stage 2: NLI second-opinion pass
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
    import torch
    out = np.empty(len(chunk), dtype=np.float32)
    for start in range(0, len(chunk), NLI_BATCH):
        batch = chunk[start:start + NLI_BATCH]
        enc = _WORKER_TOK([p for p, _ in batch], [h for _, h in batch],
                          return_tensors="pt", truncation="only_first",
                          max_length=NLI_MAX_LEN, padding=True)
        with torch.no_grad():
            logits = _WORKER_MODEL(**enc).logits
        out[start:start + len(batch)] = torch.softmax(logits, dim=-1).numpy()[:, 0]
    return out


def run_nli(pairs: list[tuple[str, str]], *, use_checkpoint: bool = True,
            stage: str = "s1") -> np.ndarray:
    n = len(pairs)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    chunk_size = max(1, -(-n // (N_WORKERS * 20)))
    chunks = [pairs[i:i + chunk_size] for i in range(0, n, chunk_size)]
    log(f"NLI pass: {n:,} pairs, {len(chunks)} chunks of ~{chunk_size}, "
        f"{N_WORKERS} workers x {THREADS_PER_WORKER} threads")

    # Stage-scoped: the cascade runs three NLI passes in one process and two
    # of them could coincidentally have the same pair count, which would make
    # stage 3 silently resume from stage 2's scores.
    ckpt_tag = f"{stage}_n{n}_cs{chunk_size}"

    def ckpt_path(i: int) -> Path:
        return CHECKPOINT_DIR / f"{ckpt_tag}_chunk{i:05d}.npy"

    results: list[np.ndarray | None] = [None] * len(chunks)
    todo = list(range(len(chunks)))
    if use_checkpoint:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        todo = []
        for i in range(len(chunks)):
            p = ckpt_path(i)
            if p.exists():
                try:
                    arr = np.load(p)
                    if len(arr) == len(chunks[i]):
                        results[i] = arr
                        continue
                except Exception:
                    pass  # corrupt/partial checkpoint -- recompute this chunk
            todo.append(i)
        if len(chunks) - len(todo):
            log(f"resuming: {len(chunks)-len(todo)}/{len(chunks)} chunks already "
                f"checkpointed in {CHECKPOINT_DIR}")

    t0 = time.time()
    done = len(chunks) - len(todo)
    n_done_already = done
    if todo:
        # 'spawn', not the default 'fork'. The bi-encoder stage in this same
        # process loads PyTorch, which initialises native OpenMP/MKL thread
        # pools. Forking after that copies only the calling thread, leaving the
        # inherited pool mutexes permanently locked with no owner to release
        # them -- any child that then tries to thread hangs forever. Verified by
        # direct repro on this machine: identical work under 'fork' hung with all
        # workers at ~0% CPU in state 'S'; 'spawn' finished in <10s. The cost is
        # one extra model load per worker (~1-2s).
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=N_WORKERS, initializer=_nli_worker_init,
                                 mp_context=ctx) as ex:
            futs = {ex.submit(_nli_score_chunk, chunks[i]): i for i in todo}
            for fut in as_completed(futs):
                i = futs[fut]
                results[i] = fut.result()
                if use_checkpoint:
                    np.save(ckpt_path(i), results[i])
                done += 1
                if done % PROGRESS_EVERY_CHUNKS == 0 or done == len(chunks):
                    elapsed = time.time() - t0
                    rate = (done - n_done_already) / elapsed if elapsed > 0 else float("nan")
                    eta = (len(chunks) - done) / rate if rate > 0 else float("nan")
                    log(f"  ... {done}/{len(chunks)} chunks, {elapsed:.0f}s elapsed, "
                        f"ETA {eta/60:.1f} min")
    return np.concatenate(results)


def nli_pairs(cand: pd.DataFrame, posts: pd.DataFrame,
              hyp_by_key: dict[str, str]) -> list[tuple[str, str]]:
    texts = posts["text"].values
    pairs = []
    for row in cand.itertuples(index=False):
        premise = texts[row.post_idx]
        premise = (premise if isinstance(premise, str) else "")[:NLI_MAX_CHARS]
        pairs.append((premise, hyp_by_key[row.code_key]))
    return pairs


# ============================================================================
# Calibration
# ============================================================================

# Exact two-sided 95% normal quantile. Using the rounded 1.96 shifts the
# interval by ~1e-5, which is immaterial for the decision but makes the output
# fail an exact comparison against reference implementations (statsmodels,
# scipy) -- and these numbers get published, so they should reconcile.
Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval. Used instead of the normal approximation because
    per-class positive counts here are small and often near 0 or 1, where the
    normal interval runs outside [0,1] and understates uncertainty."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def draw_calibration_sample(cand: pd.DataFrame, posts: pd.DataFrame, n_target: int,
                            seed: int, *, only_classes: list[str] | None = None,
                            band_alloc: list[float] | None = None) -> pd.DataFrame:
    """Stratify by class x language x bi-encoder score band.

    The old sample was drawn from the top of the ranking, so 61% of judged
    pairs sat above the band the threshold actually operates in. Sampling
    across bands puts judgements where the decision boundary is. Language is a
    stratum because the NLI model (XNLI) never saw Bengali or Ukrainian, and
    the old sample contained zero Bengali -- so any per-language weakness was
    structurally invisible.
    """
    rng = np.random.RandomState(seed)
    lang = posts["lang"].values
    cand = cand.copy()
    cand["lang"] = [lang[i] for i in cand["post_idx"]]
    cand["lang"] = cand["lang"].fillna("unk").replace({"nan": "unk", "": "unk"})
    cand["band"] = pd.cut(cand["biencoder_score"],
                          bins=[b[0] for b in SCORE_BANDS] + [SCORE_BANDS[-1][1]],
                          labels=[f"{lo:.2f}-{hi:.2f}" for lo, hi in SCORE_BANDS],
                          include_lowest=True, right=False)

    if only_classes:
        cand = cand[cand["code_key"].isin(only_classes)]

    strata = list(cand.groupby(["code_key", "band"], observed=True))
    if not strata:
        return cand.head(0)

    # Allocation across score bands. Equal allocation is the right default, but
    # for a RARE class it wastes almost the whole budget on the bottom band
    # where nothing is positive -- round 1 returned only 2 judged positives for
    # a10_humanitarian out of 69 pairs, far short of what any threshold fit
    # needs. `band_alloc` shifts the budget toward the bands where positives
    # actually live.
    #
    # This does NOT bias the result. The inclusion probability of every stratum
    # is known exactly and carried out in the `weight` column, so a weighted fit
    # still estimates the population quantity. It is textbook unequal-
    # probability sampling for a rare trait, not cherry-picking: the estimator
    # is corrected, the sample is merely allocated where it is informative.
    band_names = [f"{lo:.2f}-{hi:.2f}" for lo, hi in SCORE_BANDS]
    alloc = dict(zip(band_names, band_alloc or [1.0] * len(band_names)))
    tot_alloc = sum(alloc.get(str(band), 1.0) for (_ck, band), _g in strata)
    parts = []
    for (_ck, _band), grp in strata:
        per = max(1, int(round(n_target * alloc.get(str(_band), 1.0) / max(1e-9, tot_alloc))))
        # Within a stratum, spread across languages before taking volume, so a
        # dominant language cannot monopolise the cell.
        by_lang = list(grp.groupby("lang", observed=True))
        per_lang = max(1, per // max(1, len(by_lang)))
        for _, lg in by_lang:
            parts.append(lg.sample(n=min(len(lg), per_lang), random_state=rng))
    out = pd.concat(parts).drop_duplicates(subset=["post_uid", "code_key"]).reset_index(drop=True)

    # Horvitz-Thompson inclusion weights. The sample is deliberately NOT
    # proportional to the population -- low score bands are far more numerous
    # than high ones, and drawing proportionally would put almost every
    # judgement where the decision boundary is not. That makes every unweighted
    # statistic computed from this file an estimate of the wrong distribution.
    # Carrying the weight IN THE FILE means anyone re-fitting later gets the
    # population-level answer without having to rediscover the design.
    pop = cand.groupby(["code_key", "band"], observed=True).size().rename("pop_n")
    smp = out.groupby(["code_key", "band"], observed=True).size().rename("smp_n")
    wt = pd.concat([pop, smp], axis=1).dropna()
    wt["weight"] = wt.pop_n / wt.smp_n
    out = out.merge(wt["weight"].reset_index(), on=["code_key", "band"], how="left")
    out["weight"] = out["weight"].fillna(1.0)

    log(f"calibration sample: {len(out):,} pairs over {len(strata)} class x band strata, "
        f"{out['lang'].nunique()} languages, {out['code_key'].nunique()} classes; "
        f"inclusion weights {out.weight.min():.1f}-{out.weight.max():.1f}")
    return out


def fit_thresholds(judged: pd.DataFrame) -> dict:
    """Per-class threshold from judged pairs; `uncalibrated` where too thin.

    The criterion is the score that maximises F1 on the judged pairs for that
    class, subject to at least MIN_JUDGED_POSITIVES positives. Classes below
    that bar are explicitly recorded as `uncalibrated` -- they still get coded,
    but at the global fallback, and every row they produce carries
    threshold_source='global_fallback' in the database so nothing downstream
    can mistake them for calibrated.
    """
    out = {"fitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "global_fallback": GLOBAL_THRESHOLD,
           "min_judged_positives": MIN_JUDGED_POSITIVES,
           "judged_total": int(len(judged)),
           "classes": {}}
    for key, grp in judged.groupby("code_key"):
        y = grp["judgment"].astype(int).values
        s = blended_score(grp["biencoder_score"].values, grp["nli_score"].values)
        # Sampling weights, if the file carries them. Without them the fit
        # optimises F1 on the *stratified sample*, which over-represents the
        # high score bands -- i.e. it would tune the threshold for a
        # distribution that does not exist in production.
        wt = (grp["weight"].astype(float).values if "weight" in grp
              else np.ones(len(grp), dtype=float))
        n_pos = int(y.sum())
        rec: dict = {"n_judged": int(len(grp)), "n_positive": n_pos,
                     "pop_positive_rate": round(float((wt * y).sum() / wt.sum()), 4),
                     "weighted": "weight" in grp,
                     "languages": sorted(grp["lang"].dropna().unique().tolist())}
        if n_pos < MIN_JUDGED_POSITIVES:
            rec.update({"threshold": GLOBAL_THRESHOLD, "source": "global_fallback",
                        "status": "uncalibrated",
                        "reason": f"only {n_pos} judged positives "
                                  f"(< {MIN_JUDGED_POSITIVES}); a fitted threshold "
                                  f"would not be distinguishable from noise"})
            out["classes"][key] = rec
            continue
        # Candidate thresholds are the observed scores. Ties in F1 are broken
        # toward the HIGHER threshold (`>=` while iterating ascending), which is
        # the precision-favouring choice: at equal F1 it keeps fewer, better
        # supported labels. That direction is deliberate -- the audit's central
        # finding about the predecessor was over-tagging (192 posts carrying >=6
        # of 9 risk categories), so where the judged data cannot distinguish two
        # cuts, the product should take the stricter one.
        def counts(t: float) -> tuple[float, float, float]:
            pred = s >= t
            return ((wt * (pred & (y == 1))).sum(),
                    (wt * (pred & (y == 0))).sum(),
                    (wt * (~pred & (y == 1))).sum())

        best = (-1.0, GLOBAL_THRESHOLD)
        for t in np.unique(np.round(s, 3)):
            tp_w, fp_w, fn_w = counts(float(t))
            f1 = 2 * tp_w / max(1e-9, 2 * tp_w + fp_w + fn_w)
            if f1 >= best[0]:
                best = (f1, float(t))
        f1, t = best
        tp_w, fp_w, fn_w = counts(t)
        # Wilson intervals need integer counts, so they are computed on the
        # RAW judged pairs: the weights change the point estimate (which
        # population the rate refers to) but cannot manufacture sample size,
        # and an interval widened or narrowed by weights would misstate how
        # much was actually judged.
        pred = s >= t
        tp = int((pred & (y == 1)).sum())
        fp = int((pred & (y == 0)).sum())
        fn = int((~pred & (y == 1)).sum())
        p_lo, p_hi = wilson(tp, max(1, tp + fp))
        r_lo, r_hi = wilson(tp, max(1, tp + fn))
        # Per-class gray band: the judged scores immediately either side of the
        # threshold. The old fixed 0.14-0.30 straddled the global 0.22, so
        # gray_zone collapsed to "0.22-0.30" -- near-miss accepts only, with the
        # near-miss rejections (the more informative half) discarded.
        below = s[s < t]
        above = s[s >= t]
        prec = tp_w / max(1e-9, tp_w + fp_w)
        base = (wt * y).sum() / wt.sum()
        out["classes"][key] = {**rec,
                               "threshold": t, "source": "fitted", "status": "calibrated",
                               "f1": round(f1, 3),
                               "precision": round(prec, 3),
                               "precision_ci95": [round(p_lo, 3), round(p_hi, 3)],
                               # Precision alone is unreadable without the base
                               # rate: 0.40 against a 2% prior is a 20x lift and
                               # a genuinely useful filter; against a 40% prior
                               # it is worthless. Publish both.
                               "lift_over_base": round(prec / max(1e-9, base), 1),
                               "recall": round(tp_w / max(1e-9, tp_w + fn_w), 3),
                               "recall_ci95": [round(r_lo, 3), round(r_hi, 3)],
                               "tp": tp, "fp": fp, "fn": fn,
                               "gray_lo": round(float(np.percentile(below, 90)), 3)
                                          if len(below) else round(t - 0.05, 3),
                               "gray_hi": round(float(np.percentile(above, 10)), 3)
                                          if len(above) else round(t + 0.05, 3)}
    return out


def load_thresholds(codes: list[dict]) -> dict:
    if not THRESH_PATH.exists():
        log(f"WARNING: {THRESH_PATH.name} absent -- every class falls back to the "
            f"global {GLOBAL_THRESHOLD} and is recorded as threshold_source="
            f"'global_fallback'. Run --calibrate then --fit-thresholds.")
        return {"classes": {}}
    th = json.loads(THRESH_PATH.read_text())
    known = {c["key"] for c in codes}
    stale = sorted(set(th.get("classes", {})) - known)
    if stale:
        log(f"WARNING: thresholds file has {len(stale)} class(es) not in the current "
            f"taxonomy (ignored): {stale[:5]}")
    n_cal = sum(1 for v in th.get("classes", {}).values() if v.get("status") == "calibrated")
    log(f"thresholds: {n_cal}/{len(known)} classes calibrated, rest at the global fallback")
    return th


# ============================================================================
# Write
# ============================================================================

def load_kept_parents() -> pd.DataFrame | None:
    """Level-1 rows already in event_codes, or None if there are none.

    Stage 1 is deterministic given the same posts and taxonomy, so re-scoring
    146k pairs for two hours to rediscover rows that are already in the table is
    pure waste -- and the checkpoints that would have made it cheap are cleared
    on completion by design. Only the columns the cascade actually routes on are
    read; scores are not reused for anything but the routing decision."""
    con = duckdb.connect(str(OUT_DB), read_only=True)
    try:
        if not con.execute("SELECT count(*) FROM duckdb_tables() "
                           "WHERE table_name = 'event_codes'").fetchone()[0]:
            return None
        df = con.execute("SELECT post_uid, code, code_key FROM event_codes "
                         "WHERE axis = 'theme' AND level = 1").fetchdf()
        return df if len(df) else None
    finally:
        con.close()


def _cleanup_checkpoints() -> None:
    """Only on a completed run -- the checkpoints are what make an interrupted
    multi-hour pass resumable, so they must survive any early exit."""
    import shutil
    if CHECKPOINT_DIR.exists():
        shutil.rmtree(CHECKPOINT_DIR, ignore_errors=True)
        log(f"cleared checkpoint dir {CHECKPOINT_DIR} (run completed)")


def open_writer_with_retry(db_path: Path, attempts: int = 45, delay_s: float = 20.0):
    """DuckDB is single-writer and the API server / another script may hold it.
    Retry rather than crashing or clobbering.

    The budget is 15 minutes, not the 2 it used to be. This is the last step of a
    multi-hour run and it is the one step that can fail for a reason wholly
    outside the run: DuckDB permits one writer OR many readers, so a dev
    `UI/api_server.py` left running holds this lock *indefinitely*. Two minutes of
    retry meant a 110-minute pass could burn all of its compute and then throw
    away the result because somebody had a dashboard open. Scores are
    checkpointed so it is recoverable, but the failure lands at the very end.
    A wait long enough for an operator to notice the message and stop the server
    costs nothing when the lock is free."""
    last = None
    for i in range(attempts):
        try:
            return duckdb.connect(str(db_path), read_only=False)
        except duckdb.IOException as e:
            last = e
            if i == 0:
                log(f"writer lock on {db_path.name} is held -- if UI/api_server.py "
                    f"is running, stop it now; this will retry for "
                    f"{attempts * delay_s / 60:.0f} min")
            log(f"writer lock busy (attempt {i+1}/{attempts}): {e}; retrying in {delay_s:.0f}s")
            time.sleep(delay_s)
    raise RuntimeError(f"could not acquire writer lock on {db_path}") from last


def write_event_codes(kept: pd.DataFrame, level: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    out = kept.copy()
    out["method"] = METHOD
    out["nli_weight"] = NLI_BLEND_W
    out["scored_at"] = now
    cols = ["post_uid", "axis", "code", "code_key", "parent_code", "level", "score",
            "biencoder_score", "nli_score", "nli_weight", "nli_threshold",
            "threshold_source", "gray_zone", "method", "scored_at"]
    out = out[cols].reset_index(drop=True)

    # Codes are STRINGS, not numbers. '05' is a CAMEO code, not the integer five,
    # and the zero-pad is load-bearing: it is what makes '05' sort next to '05.2'
    # and what every join and LIKE-prefix in the API assumes. Left to inference,
    # a level-1-only frame has parent_code all-None, DuckDB types that column
    # INTEGER, and the first cascade insert silently coerces '05' -> 5 -- after
    # which parent_code = code never matches again. So state the schema.
    for col in ("post_uid", "axis", "code", "code_key", "parent_code",
                "threshold_source", "method", "scored_at"):
        out[col] = out[col].map(lambda v: None if v is None or v != v else str(v))

    con = open_writer_with_retry(OUT_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS event_codes (
            post_uid VARCHAR, axis VARCHAR, code VARCHAR, code_key VARCHAR,
            parent_code VARCHAR, level BIGINT, score DOUBLE,
            biencoder_score DOUBLE, nli_score DOUBLE, nli_weight DOUBLE,
            nli_threshold DOUBLE, threshold_source VARCHAR, gray_zone BOOLEAN,
            method VARCHAR, scored_at VARCHAR)
    """)
    con.register("_ec", out)
    if level == "parent":
        # Replace only the grain this run produced, so a parent-only re-run does
        # not silently delete sub-codes or actions from a previous full run.
        con.execute("DELETE FROM event_codes WHERE axis='theme' AND level=1")
        con.execute("INSERT INTO event_codes SELECT * FROM _ec")
    elif level == "cascade":
        # --reuse-parents: the level-1 rows were not re-scored by this run, so
        # replacing the whole table would delete them.
        con.execute("DELETE FROM event_codes WHERE (axis='theme' AND level>1) OR axis='action'")
        con.execute("INSERT INTO event_codes SELECT * FROM _ec")
    else:
        con.execute("CREATE OR REPLACE TABLE event_codes AS SELECT * FROM _ec")
    con.unregister("_ec")

    log("\n--- event_codes per class ---")
    for r in con.execute(
        "SELECT code, code_key, COUNT(*) n, ROUND(AVG(score),3) avg_score, "
        "SUM(gray_zone::INT) n_gray, ANY_VALUE(threshold_source) src "
        "FROM event_codes GROUP BY code, code_key ORDER BY code"
    ).fetchall():
        log(f"  {r[0]:>5}  {r[1]:<42} n={r[2]:>6,}  avg={r[3]}  gray={r[4]:>5,}  {r[5]}")
    n_posts = con.execute("SELECT COUNT(DISTINCT post_uid) FROM event_codes").fetchone()[0]
    n_rows = con.execute("SELECT COUNT(*) FROM event_codes").fetchone()[0]
    log(f"\nevent_codes: {n_rows:,} rows over {n_posts:,} distinct posts "
        f"({n_rows/max(1,n_posts):.2f} codes/post)")
    con.close()


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-parents", action="store_true",
                    help="with --level all: route the cascade off the level-1 rows "
                         "already in event_codes instead of re-running stage 1. "
                         "Stage 1 is deterministic, so this is equivalent and "
                         "saves the full parent pass.")
    ap.add_argument("--level", choices=["parent", "all"], default="parent",
                    help="'parent' = Section A level 1 only (the reviewable "
                         "checkpoint); 'all' = cascade to sub-codes and actions")
    ap.add_argument("--calibrate", type=int, default=0,
                    help="draw a stratified sample of this many pairs, score them, "
                         "and write data/calibration_cameo.csv for judging. "
                         "Does not touch the database.")
    ap.add_argument("--fit-thresholds", action="store_true",
                    help="fit per-class thresholds from the judged calibration CSV")
    ap.add_argument("--calibrate-classes", default="",
                    help="comma-separated code_keys to restrict the draw to "
                         "(top up classes that came back under-powered)")
    ap.add_argument("--oversample-top", action="store_true",
                    help="allocate the calibration budget 1:2:3:4 across score "
                         "bands instead of equally. For rare classes, equal "
                         "allocation spends the budget where nothing is "
                         "positive. Inclusion weights keep the estimate "
                         "population-correct.")
    ap.add_argument("--append", action="store_true",
                    help="append to data/calibration_cameo.csv, preserving "
                         "existing judgements, instead of overwriting")
    ap.add_argument("--seed", type=int, default=14)
    args = ap.parse_args()

    if args.fit_thresholds:
        if not CALIB_PATH.exists():
            print(f"ERROR: {CALIB_PATH} missing -- run --calibrate first")
            return 1
        judged = pd.read_csv(CALIB_PATH)
        unjudged = judged["judgment"].isna().sum()
        if unjudged:
            log(f"WARNING: {unjudged:,}/{len(judged):,} rows still unjudged -- "
                f"fitting on the {len(judged)-unjudged:,} judged rows only")
            judged = judged[judged["judgment"].notna()]
        if judged.empty:
            print("ERROR: no judged rows")
            return 1
        th = fit_thresholds(judged)
        THRESH_PATH.write_text(json.dumps(th, indent=1))
        cal = [(k, v) for k, v in th["classes"].items() if v["status"] == "calibrated"]
        unc = [(k, v) for k, v in th["classes"].items() if v["status"] != "calibrated"]
        log(f"\n--- fitted thresholds ({len(cal)} calibrated, {len(unc)} uncalibrated) ---")
        for k, v in sorted(cal, key=lambda x: -x[1]["f1"]):
            log(f"  {k:<42} t={v['threshold']:.3f} F1={v['f1']:.2f} "
                f"P={v['precision']:.2f} [{v['precision_ci95'][0]:.2f},{v['precision_ci95'][1]:.2f}] "
                f"R={v['recall']:.2f} n={v['n_judged']} (+{v['n_positive']})")
        for k, v in unc:
            log(f"  {k:<42} UNCALIBRATED -- {v['reason']}")
        log(f"\nwrote {THRESH_PATH}")
        return 0

    if not SRC_DB.exists():
        print(f"ERROR: source db missing: {SRC_DB}")
        return 1

    t_start = time.time()
    tax, codes = load_taxonomy()
    scored = select_codes(codes, args.level)
    log(f"taxonomy v{tax['framework_version']}: scoring {len(scored)} class(es) "
        f"at level={args.level}")
    hyp_by_key = {c["key"]: c["hypothesis"] for c in codes}

    posts = load_posts()
    sims = biencoder_sims(posts, scored)
    cand = build_candidates(posts, sims, scored)

    if args.calibrate:
        only = [c.strip() for c in args.calibrate_classes.split(",") if c.strip()]
        sample = draw_calibration_sample(
            cand, posts, args.calibrate, args.seed,
            only_classes=only or None,
            band_alloc=[1.0, 2.0, 3.0, 4.0] if args.oversample_top else None)
        pairs = nli_pairs(sample, posts, hyp_by_key)
        sample = sample.copy()
        # No checkpointing for the sample: it is cheap, and a stale sample
        # checkpoint colliding with a full run's tag would be a silent
        # correctness bug.
        sample["nli_score"] = run_nli(pairs, use_checkpoint=False)
        texts = posts["text"].values
        sample["text"] = [str(texts[i])[:400].replace("\n", " ") for i in sample["post_idx"]]
        sample["hypothesis"] = [hyp_by_key[k] for k in sample["code_key"]]
        sample["judgment"] = ""          # 1 = the hypothesis is true of the text, 0 = not
        sample["judged_by"] = ""
        sample["note"] = ""
        out_cols = ["post_uid", "code", "code_key", "lang", "band", "biencoder_score",
                    "nli_score", "hypothesis", "text", "judgment", "judged_by", "note"]
        out_cols = out_cols[:6] + ["weight"] + out_cols[6:]
        sample = sample.sort_values(["code_key", "nli_score"], ascending=[True, False])[out_cols]
        if args.append and CALIB_PATH.exists():
            prev = pd.read_csv(CALIB_PATH)
            n_prev = len(prev)
            sample = pd.concat([prev, sample], ignore_index=True)
            # A pair judged in an earlier round keeps that judgement -- re-drawing
            # it must not silently blank the work.
            sample = sample.drop_duplicates(subset=["post_uid", "code_key"], keep="first")
            log(f"--append: {n_prev:,} existing rows + new draw -> {len(sample):,} "
                f"after de-duplicating on (post_uid, code_key)")
        sample.to_csv(CALIB_PATH, index=False)
        log(f"\nwrote {CALIB_PATH} -- {len(sample):,} pairs awaiting judgement")
        log("Fill the `judgment` column with 1/0 and `judged_by` with who judged it, "
            "then run --fit-thresholds.")
        log(f"\nelapsed {(time.time()-t_start)/60:.1f} min")
        return 0

    reused = load_kept_parents() if (args.level == "all" and args.reuse_parents) else None
    if reused is not None:
        # Routing only needs (post_uid, code); the level-1 rows stay in the table
        # untouched and are NOT rewritten by this run.
        log(f"--reuse-parents: routing off {len(reused):,} level-1 rows already in "
            f"event_codes ({reused['post_uid'].nunique():,} posts); skipping stage 1")
        kept = reused
    else:
        pairs = nli_pairs(cand, posts, hyp_by_key)
        cand["nli_score"] = run_nli(pairs, stage="s1")
        log(f"stage 1 NLI done, {(time.time()-t_start)/60:.1f} min elapsed")
        # The published `score` is the blended decision score, not the raw NLI
        # probability. Both inputs are kept in the row so any downstream consumer
        # can recompute or re-weight without re-running the model.
        kept = apply_thresholds(cand, codes)

    if args.level == "parent":
        write_event_codes(kept, "parent")
        _cleanup_checkpoints()
        log(f"\ntotal wall time: {(time.time()-t_start)/60:.1f} min")
        return 0

    # ---- stage 2: sub-codes, only inside accepted parents -------------------
    all_kept = [] if reused is not None else [kept]
    subs = subcodes_of(codes)
    log(f"\n--- stage 2: {len(subs)} sub-codes inside {len(kept):,} accepted parents ---")
    sub_cand = build_candidates(posts, biencoder_sims(posts, subs), subs,
                                allowed=allowed_for_subcodes(kept, subs),
                                label="sub-code candidates")
    if len(sub_cand):
        sub_cand["nli_score"] = run_nli(nli_pairs(sub_cand, posts, hyp_by_key), stage="s2")
        log(f"stage 2 NLI done, {(time.time()-t_start)/60:.1f} min elapsed")
        all_kept.append(apply_thresholds(sub_cand, codes))
    else:
        log("no sub-code candidates cleared the floor inside accepted parents")

    # ---- stage 3: actions, only on posts that got a thematic code -----------
    acts = action_codes(codes)
    theme_kept = pd.concat([kept] + all_kept, ignore_index=True) \
        if reused is not None else pd.concat(all_kept, ignore_index=True)
    log(f"\n--- stage 3: {len(acts)} action codes on "
        f"{theme_kept['post_uid'].nunique():,} posts with >=1 thematic code ---")
    act_cand = build_candidates(posts, biencoder_sims(posts, acts), acts,
                                allowed=allowed_for_actions(theme_kept, acts),
                                label="action candidates")
    if len(act_cand):
        act_cand["nli_score"] = run_nli(nli_pairs(act_cand, posts, hyp_by_key), stage="s3")
        log(f"stage 3 NLI done, {(time.time()-t_start)/60:.1f} min elapsed")
        all_kept.append(apply_thresholds(act_cand, codes))
    else:
        log("no action candidates cleared the floor")

    if not all_kept:
        log("nothing new to write")
        _cleanup_checkpoints()
        return 0
    write_event_codes(pd.concat(all_kept, ignore_index=True),
                      "cascade" if reused is not None else "all")

    _cleanup_checkpoints()
    log(f"\ntotal wall time: {(time.time()-t_start)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
