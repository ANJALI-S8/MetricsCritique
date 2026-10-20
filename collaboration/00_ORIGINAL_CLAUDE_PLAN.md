# Plan: Metric-Readiness EDA for ShaktiDB Social (NTRO OSINT corpus)

## Context

`shaktidb_social` (Postgres 18.4 / ShaktiDB, 617 MB) holds a **live-ingesting** OSINT
social-media corpus: 10 base tables + 2 union views, ~105K rows, spanning Telegram,
YouTube (videos + comments), Reddit, LinkedIn, Instagram, Facebook, Pinterest,
Snapchat (WhatsApp empty). Keywords are geopolitical/defence-flavoured
("iran israel war", "drone attack india", "drdo", "terror attack") and Telegram
collection is channel-based over conflict/state-media channels (rtnews, rybar,
intelslava, xhnews, BBC_Chinese_news, war_monitor…).

The goal is not a descriptive EDA for its own sake — it is to arrive at 10 derived
signal metrics: **growth rate, momentum, half-life (volatility / persistence),
intensity, tone z-score, virality, reach / author diversity, readability, ontology,
hate-speech score.**

Profiling the live DB surfaced structural facts that dictate how — and whether — each
metric can be computed. This plan is therefore an EDA **designed to establish the
inputs, baselines and normalisation constants those 10 metrics require**, then compute them.

A descriptive EDA pass is already running in the background and will land
`reports/EDA_Report.html`, `reports/KEY_FINDINGS.md`, `scripts/run_eda.py`. That becomes
the Layer-1 baseline; this plan extends it.

### Decisions locked
1. **Entity = derived topic clusters** (ontology built first; only option covering keyword-less Telegram).
2. **Half-life = aggregate now + append-only snapshot history** seeded today.
3. **Tone/hate = local multilingual transformers** on the L40S.
4. **Analytics layer = local parquet / DuckDB. Production Postgres is READ-ONLY —
   the pipeline issues no DDL/DML against `shaktidb_social`.** (Superuser rights exist
   but are deliberately unused.)

---

## Blocking findings from profiling (these drive every design choice)

| # | Finding | Consequence |
|---|---|---|
| 1 | **No per-post time series.** `distinct(key, scraped_at)` == `distinct(key)` on every table; `youtube_videos` updates in place (327 distinct `updated_at`, history overwritten). Each post observed exactly once. | True per-post decay is not measurable from this snapshot → aggregate decay now, local snapshot history to unlock it prospectively. |
| 2 | **Duplicate rows, not re-scrapes.** telegram 23,325 rows → 14,595 distinct `post_url` (**37% exact dupes**); reddit 2,107 → 1,348 (**36%**); instagram 43% dup text; snapchat 94%; pinterest 90%. | Dedup is a **prerequisite**. Every Telegram volume figure is inflated ~1.6× until fixed. |
| 3 | **Telegram has NO keyword** — 23,325/23,325 NULL, i.e. **68% of the corpus is unlabelled**. | Ontology is a **hard dependency**, not the 9th metric. |
| 4 | **Scrape lag varies 0.6 → 150 days** (reddit/fb/ig 0.6d; telegram 10.6d; youtube 81d; snapchat 150d). | Engagement is **age-confounded** → need an age-normalisation curve before any cross-platform comparison. |
| 5 | **Collector ramp-up confounds growth.** Telegram 422→1,393 posts/day over 20 days while active channels went 18→24. | Naive growth measures **collection effort, not narrative growth** → normalise by effort / use share-of-volume. |
| 6 | **Multilingual**: telegram en 62%, then ru, zh-cn, bn, uk, hi + 2,626 `unknown`. | English-only NLP invalid for ~38% → multilingual models required. |
| 7 | **Engagement coverage is platform-specific, sometimes absent.** Reddit has **zero** engagement (all 4 fields 100% NULL). Telegram likes/comments NULL but views/forwards present (median 4,185 views). Facebook 70% zero-likes. `importance_score` 86% NULL. | Virality needs a **per-platform formula**; Reddit excluded from engagement metrics. |
| 8 | **Snapchat & Pinterest effectively dead**: snapchat 94% NULL text/posted_at/engagement (1 usable day); pinterest 100% NULL `posted_at`, 91% empty text. | Exclude from time-series metrics; report as coverage gaps. |
| 9 | **Text is short where it matters**: yt_comments median 59 chars, 36% under 40 chars. | Readability invalid for most comments → length-gate and report coverage honestly. |
| 10 | **Embedding gaps**: bge-m3 1024-d multilingual — yt_comments 100%, telegram 88.5%, reddit/linkedin/facebook ~100%, but **youtube_videos 100% NULL**. | Compute the 2,305 missing video embeddings **locally** (cannot write back under read-only). |

**Infrastructure confirmed:** NVIDIA **L40S 48 GB**, 48 CPUs, 251 GB RAM, HuggingFace +
PyPI reachable, `torch`/`transformers`/`sklearn` installed. Local multilingual inference
is viable — no external API, no data leaves the network.

**Panel density (daily grain):** telegram 45 days @ ~488/day · yt_comments 43 @ ~1,389/day ·
youtube_videos 43 @ ~45/day · reddit 31 @ ~61/day · linkedin 31 @ ~30/day · facebook 27 @
~8.5/day · instagram 19 @ ~17.7/day. Snapchat/pinterest unusable.
**Effective history ≈ 6 weeks → long-window parameters capped at ~21 days.**

---

## Design: a 5-layer pipeline (all outputs local)

Local store: **`data/analytics.duckdb`** with large tables mirrored as parquet under
`data/parquet/`. DuckDB gives SQL over the derived layer without touching production.

### Layer 0 — Read-only extract → clean local base
`scripts/01_extract_base.py`

Single `REPEATABLE READ` transaction against Postgres (the DB is live — Telegram grew
63% during profiling), so one internally-consistent snapshot; `ROLLBACK` at the end.
Record the snapshot timestamp as `extract_id`.

- **`post_facts`** — one row per unique post. Dedup key `(platform, native_id)`, falling
  back to `(platform, md5(text), author)` where `native_id` is unreliable.
  Columns: `post_uid, platform, native_id, url, author_uid, author_name, is_verified,
  text, lang, posted_at, scraped_at, scrape_lag_days, keyword, hashtags, likes, comments,
  shares, views, has_engagement`.
  Per-platform native columns are normalised into the common slots (telegram
  `views`/`forwards`, youtube `video_view_count`/`like_count`, …). **`has_engagement`
  keeps "absent" distinct from "zero"** — essential for Reddit and for Facebook's 70% zero-likes.
  Reuses the existing `social_posts_unified` view definition as the column-mapping
  reference, extended to include YouTube and the new flag.
- **`comment_facts`** — youtube_comments text + metadata (~76K rows, ~20 MB).
- **`post_embeddings`** — pgvector → parquet for the ~24.5K posts that have them (~100 MB).
  **Comment embeddings (313 MB) are deliberately NOT pulled**: comments inherit their
  parent video's topic by default. Pull them only if comment-level sub-topic clustering
  is needed later.
- **`engagement_snapshots`** — **append-only, local**, `(post_uid, extract_id, observed_at,
  likes, comments, shares, views)`. Seeded from this extract; **every subsequent run
  appends a new observation**, which is what makes true per-post decay measurable over time.
  ⚠️ Because history now accumulates locally rather than in the DB, this only builds up if
  the extract runs on a schedule — recommend a **daily cron**; extract cadence sets the
  resolution of any future per-post half-life.
- **`collection_effort`** — per platform-day: active channels, active keywords, rows
  ingested. **The control series for finding #5.**

### Layer 1 — Metric-readiness diagnostics (the EDA proper)
`scripts/02_eda_diagnostics.py` → extends the running descriptive report

- **Dedup impact**: before/after volume per platform-day (quantifies finding #2).
- **Scrape-lag distribution** + **engagement-vs-age curve**: fit `E ~ f(age)` per platform
  on `log1p(engagement)`. **This fitted curve is the age-normaliser** for virality/intensity.
- **Collection-effort vs volume** overlay — separates collector growth from signal growth.
- **Panel density map**: which (topic × platform × day) cells are populated → defines where
  metrics are trustworthy vs interpolated.
- **Language × platform matrix**; detected-vs-declared language agreement.
- **Text-length vs readability validity floor** → scorable coverage per platform.
- **Heavy-tail characterisation**: log-log rank plots per engagement metric → justifies
  `log1p` + robust (median/MAD) z-scores rather than mean/σ.

### Layer 2 — Ontology (hard dependency for 6 of the 10 metrics)
`scripts/03_ontology.py`

- Compute the 2,305 missing `youtube_videos` embeddings **locally with bge-m3** — must match
  the existing model; never mix embedding spaces.
- Cluster L2-normalised embeddings: UMAP → HDBSCAN, k-means as stable fallback; choose k by
  silhouette + cluster stability across seeds.
- Label clusters from class-discriminative TF-IDF terms + representative posts.
- Build a **2-level taxonomy**: `domain → topic` (e.g. Conflict/Middle-East,
  Conflict/Russia-Ukraine, India/Defence, Cyber/Ransomware, Markets, Tech/AI).
- Map the 418 existing keywords into it; assign keyword-less Telegram posts by nearest
  centroid **with a confidence score and an explicit `unassigned` bucket** — never force-fit.
- **Multilingual NER** for people/orgs/locations/weapon-systems — the OSINT entity layer
  alongside topics.
- Output: `topic_dim`, `post_topic(post_uid, topic_id, confidence)`.

### Layer 3 — NLP scoring
`scripts/04_nlp_scores.py` → `post_scores`

Multilingual, batched on the L40S:
- **Language detection** to fill gaps and verify the declared `language` column.
- **Tone/sentiment** → continuous score per post.
- **Hate speech / toxicity** → probability per post.
- **Readability** → only where `lang` is known **and** length ≥ ~25 words; Flesch/FK for
  English, LIX (more language-portable) elsewhere; **zh/ja get no syllable-based score** —
  recorded as not-applicable rather than a wrong number.
- Every score carries `model_name`, `model_version`, `scored_at`, `valid`.

> **Domain caveat:** this is a war/terror-reporting corpus. Off-the-shelf toxicity models
> fire on *quoted* and *reported* violence. Hate-speech output is **not usable raw** —
> Layer 5 calibrates against a hand-labelled sample before any hate figure is published.

### Layer 4 — The 10 metrics
`scripts/05_metrics.py` → `metrics_daily`

Grain **(topic × platform × day)**, rolled up to (topic × day) and (channel × day).
`α = 1` smoothing; heavy tails via `log1p` + robust z (median/MAD).
`V` = deduped volume, `E` = age-normalised engagement, `s` = share-of-volume.

| # | Metric | Definition | Status |
|---|---|---|---|
| 1 | **Growth rate** | `g = ln((V_t+α)/(V_{t-1}+α))`, plus 7d-vs-prior-7d. Reported on **share `s`** as well as raw, to strip collector ramp-up. | ✅ |
| 2 | **Momentum** | `M = EMA_7(g) − EMA_21(g)` (MACD-style) + sign-persistence run length. | ✅ (21d cap — ~6wk history) |
| 3 | **Half-life / persistence / volatility** | (a) **Decay**: fit `V = V_peak·e^{−λ(t−t_peak)}` post-peak → `t½ = ln2/λ`, **reported with R²**, rejected below a fit threshold. (b) **Persistence**: lag-1..k ACF + Hurst. (c) **Volatility**: σ of log-returns `g`. | ⚠️ aggregate only; `engagement_snapshots` unlocks per-post prospectively |
| 4 | **Intensity** | `I = z_robust(w₁·log1p(V) + w₂·log1p(E))` vs trailing 28d baseline of the same entity. | ✅ |
| 5 | **Tone z-score** | Post tone → entity-day mean `τ̄` (optionally engagement-weighted) → `z = (τ̄ − μ_base)/σ_base`, baseline = trailing 28d of that entity, falling back to platform-global when sparse. | ⟶ Layer 3 |
| 6 | **Virality** | Per-platform amplification ratio → age-normalised by the Layer-1 curve → z-scored **within** platform (cross-platform comparison only after standardisation): telegram `forwards/views`; ig/fb `shares/(likes+comments+shares)`; youtube `(likes+comments)/views`; linkedin `comments/reactions`. **Reddit excluded** (no engagement) → structural proxy (cross-post / duplicate-URL count). | ⚠️ per-platform; Reddit excluded |
| 7 | **Reach / author diversity** | **Reach is a proxy — no follower/audience field exists anywhere**: `R = Σviews` where available, else `Σlog1p(engagement)`, always labelled as proxy. **Diversity** = Shannon entropy of author share, `1−HHI`, distinct-author count. *High volume + low diversity + high duplicate rate → coordinated-amplification flag* (ties findings #2/#5). | ✅ (reach = proxy) |
| 8 | **Readability** | Language-appropriate, length-gated, with explicit scorable-coverage reporting. | ⚠️ low coverage on yt_comments |
| 9 | **Ontology** | Layer 2 taxonomy + NER entity layer. | ⟶ Layer 2 |
| 10 | **Hate-speech score** | Per-post probability → entity-day **prevalence** (share above calibrated threshold) + mean, z-scored vs baseline. | ⚠️ requires calibration |

### Layer 5 — Validation
`scripts/06_validate.py`

- Recompute on a held-out window; assert stability.
- **Sensitivity analysis** on window lengths (7/14/21d) and `α` → report which metrics are
  parameter-fragile given only ~6 weeks of history.
- **Hand-label ~200 stratified posts** for tone + hate → precision/recall, threshold
  calibration, per-language breakdown (the war-reporting false-positive check).
- Spot-check top-N by each metric for face validity.
- **Null control**: shuffle timestamps → growth/momentum must collapse to noise.

---

## Deliverables

```
/home/hariramanan/NTRO/EDA/
  data/analytics.duckdb                 # derived layer (local, read-only vs prod)
  data/parquet/                         # post_facts, embeddings, snapshots
  scripts/01_extract_base.py  02_eda_diagnostics.py  03_ontology.py
          04_nlp_scores.py    05_metrics.py          06_validate.py
  reports/EDA_Report.html               # descriptive (running now)
          KEY_FINDINGS.md               # descriptive (running now)
          METRIC_READINESS.md           # Layer 1: per-metric feasibility + blockers
          ONTOLOGY.md                   # taxonomy + cluster cards
          METRICS_SPEC.md               # exact formulas, params, caveats
          VALIDATION.md                 # calibration + sensitivity results
```

Charts follow the `dataviz` skill: validated default palette, fixed categorical hue order,
sequential blue for magnitude/heatmaps, diverging blue↔red for correlations, light+dark via
CSS custom properties, self-contained offline HTML.

---

## Verification

1. **Read-only guarantee**: pipeline issues only `SELECT`; grep the scripts for
   `INSERT|UPDATE|CREATE|DROP|ALTER` against the Postgres connection → must be zero.
   Connection opened with `default_transaction_read_only` where supported.
2. **Layer 0**: `post_facts` row counts equal distinct natural keys per platform
   (telegram ≈ 14.6K, **not** 23.3K); no duplicate `post_uid`; `has_engagement` false for
   100% of Reddit; `scrape_lag_days ≥ 0` for all rows (negatives = clock bug, flag them).
3. **Layer 1**: age-normalisation curve fits with reported R²; dedup before/after deltas
   match finding #2 (~37% telegram, ~36% reddit).
4. **Layer 2**: cluster stability ≥ threshold across seeds; every Telegram post either
   assigned with confidence or explicitly `unassigned`; manually inspect 20 cluster cards.
5. **Layer 3**: scores present for all non-empty text; `valid=false` wherever language
   unknown or below length floor; **calibration sample scored before any hate figure is reported.**
6. **Layer 4**: deterministic re-run against the same `extract_id`; shuffled-timestamp
   control collapses growth/momentum to ~0.
7. **End-to-end**: `python3 scripts/01_extract_base.py && … && 06_validate.py` from clean,
   then open `reports/METRICS_SPEC.md` and the HTML report and eyeball for label
   collisions/overflow.
8. **Snapshot accrual**: run the extract twice and confirm `engagement_snapshots` gains a
   second `extract_id` for existing `post_uid`s (proves the half-life path works).
