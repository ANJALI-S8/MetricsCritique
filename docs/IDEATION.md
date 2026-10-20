# How we ideated and formulated

A narrative record of *how* this analysis was reasoned into existence — the dead ends,
the findings that forced redesigns, and why each metric ended up defined the way it is.
For the *what*, see `METRICS_V2_SPEC.md`. For the *statistics*, see `STATISTICAL_REVIEW.md`.

---

## Stage 0 — Getting in the door

The brief was "do a detailed EDA on this data" with a host, port, database and
credentials. First attempts failed with `Lost connection to MySQL server during query`
on a port that accepted TCP connections but never spoke.

The diagnosis mattered more than the fix: TCP connected, but **no protocol handshake
ever arrived**. MySQL servers greet the client immediately; the silence meant whatever
was listening wasn't MySQL. Three retries and raw-socket probes at 15s and 30s all
timed out the same way.

It was **PostgreSQL** — where the *client* speaks first, so waiting for a server
greeting hangs forever. This produced the first durable lesson: **a hang and a refusal
are different diagnostics.** A refusal means nothing is listening; a hang with an open
socket means something is listening that you're speaking the wrong language to.

Once switched to `psycopg2`, the schema appeared instantly: PostgreSQL 18.4 / ShaktiDB,
617 MB, `vector` (pgvector 0.8.2) installed, 10 base tables + 2 union views.

## Stage 1 — The data is alive

Row counts taken minutes apart disagreed: `telegram_posts` went **14,281 → 23,325
(+63%)** while we were still reading the schema. Reddit, Instagram and LinkedIn moved too.

This reframed everything. A live-ingesting corpus means:
- Any two queries run separately produce **mutually contradictory numbers**.
- "Growth" in the data may be growth of the *collector*, not of the world.

Both consequences became structural commitments: every extract runs inside a single
`REPEATABLE READ` transaction (one consistent snapshot, `ROLLBACK` at the end), and
collection effort is tracked as its own series so it can be divided out later.

## Stage 2 — Profiling before planning

Rather than jump to the requested metrics, we profiled what would make them possible or
impossible. Ten findings changed the design; five changed it fundamentally.

**Finding 1 — no per-post time series.** `distinct(key, scraped_at)` equalled
`distinct(key)` on every table, and `youtube_videos` carries `updated_at` (327 distinct
values), meaning rows are updated **in place** and history is overwritten. Every post is
observed exactly once.

> This was the most consequential discovery. **Half-life and decay are not measurable
> from a single snapshot** — you cannot observe decay of a quantity you measured once.
> Rather than quietly compute something decay-shaped and mislabel it, we split the
> metric: aggregate-level decay now, and an append-only `engagement_snapshots` table
> seeded immediately so *true* per-post decay becomes available as the scraper runs.
> The honest version of the metric costs a table and some patience.

**Finding 2 — duplicates, not re-scrapes.** Telegram: 23,325 rows → 14,595 distinct
`post_url` (**37% exact duplicates**); Reddit 36%; Instagram 43% duplicate text;
Snapchat 94%; Pinterest 90%. Later diagnosis showed the same message ingested twice
through two pipeline paths, one leaving `channel_id` NULL. Dedup moved from hygiene to
**prerequisite** — every Telegram volume figure was inflated ~1.6×.

**Finding 3 — 68% of the corpus has no topic label.** `keyword` is NULL for
**all** 23,325 Telegram rows. Telegram is collected by *channel* (27 of them: rtnews,
rybar, intelslava, xhnews, BBC_Chinese_news, war_monitor…), not by keyword.

> This inverted the plan. "Ontology" had been listed as metric #9 of 10 — a nice-to-have
> classification layer. But every per-topic metric needs an answer to *"a signal about
> **what**?"*, and `keyword` could only answer that for 32% of the data. **Ontology was
> promoted from output to dependency**: it must be built before growth, momentum,
> acceleration, intensity, tone or hate can be computed over anything meaningful.

**Finding 4 — engagement is age-confounded.** Scrape lag ranges from 0.6 days
(Reddit/Facebook/Instagram) to 10.6 (Telegram), 81 (YouTube) and 150 (Snapchat). A post
measured 0.6 days after publication has had far less time to accumulate likes than one
measured 150 days later. Comparing their engagement directly measures **observation
delay, not virality**.

**Finding 5 — the collector ramp.** Telegram went 422 → 1,393 posts/day over 20 days
while active channels went 18 → 24. Naive growth on this series would report a dramatic
emerging narrative that is really just a scraper being switched on.

Supporting findings: the corpus is genuinely multilingual (Telegram 62% English, then
ru/zh-cn/bn/uk/hi, plus 2,626 `unknown`) so English-only NLP would silently mishandle
~38%; Reddit has **zero** engagement fields; Snapchat and Pinterest are effectively
empty (94% and 91% missing text, Pinterest has no `posted_at` at all); YouTube comments
have a median length of 59 characters with 36% under 40, which puts most of them below
the validity floor for any readability formula.

## Stage 3 — Four decisions that shaped the build

| Decision | Chosen | Reasoning |
|---|---|---|
| Metric entity | **Derived topic clusters** | Only option covering keyword-less Telegram (68% of corpus). Keyword-based metrics would have described a third of the data while appearing to describe all of it. |
| Half-life | **Aggregate now + snapshot history** | Honest about what a single snapshot can support, while starting the accrual that makes the real metric possible. |
| Tone / hate scoring | **Local multilingual transformers** | Handles the non-English 38% properly; nothing leaves the network — decisive for an OSINT corpus. An external API was rejected on data-egress grounds. |
| Analytics storage | **Local DuckDB + parquet, production read-only** | Credentials had superuser rights; we deliberately declined to use them. The scraper owns that database. |

That last decision is worth dwelling on: having write access is not a reason to write.
The analytics layer is a *consumer* of a live production system, and keeping it strictly
read-only means no analysis bug can ever corrupt collection.

## Stage 4 — Where v1 broke, and why

The first full pipeline ran end-to-end and produced 7,340 metric cells. Inspecting the
output rather than trusting it revealed that the numbers were not usable:

- `intensity_z` reached **143** — on cells containing **V ≤ 2 posts**. One cell scored
  **18.5 on a single post**.
- **91.3%** of cells had V ≤ 2. Only **447** cells had V ≥ 5 inside the real collection
  window. The panel averaged 2.8 posts per cell.
- **82%** of cells sat *outside* the collection window — the YouTube backfill reaches
  back to 2011, so 6,006 cells were historical videos polluting every trailing baseline.
- The null-control test reported `ok: true` while its own numbers contradicted its
  stated pass condition.
- The sensitivity harness reported `corr(7/21 vs default) = 0.791` — but 7/21 *was* the
  default, so that number must be exactly 1.0. A self-comparison that isn't 1.0 means
  the harness wasn't measuring what it claimed.

The common thread: **Gaussian statistics applied to sparse count data.** Robust-z uses
median/MAD; when a baseline is mostly zeros, MAD collapses toward zero and the z-score
explodes. The metric wasn't detecting intensity, it was detecting its own denominator
going to zero.

> The lesson we took: a metric that produces a spectacular number on one post is not a
> sensitive metric, it is a broken one. Sparsity has to be handled in the *model*, not
> patched by clipping outliers afterward.

## Stage 5 — The statistical reformulation

Each fix follows from treating the data as what it actually is:

- **Counts are counts.** Volume is an overdispersed count, so the baseline became a
  **Negative Binomial** and the anomaly score became an **exceedance probability**
  (`−log10 P(X ≥ V)`). This is bounded, reads as evidence, and is correctly unimpressed
  by V = 1 against a small baseline — dissolving the |z| = 143 class of artifact.
- **Exposure belongs in the model.** Dividing by collection effort (a log-exposure
  offset, as in epidemiology) means we measure posts *per active channel* — the collector
  ramp cancels instead of masquerading as signal.
- **No number without evidence.** Support gates emit `NULL` and `support_ok = false`
  rather than a confident-looking figure derived from two posts.
- **Sparse means get shrunk.** Tone and hate on 1–2 post cells are shrunk toward their
  parent topic/platform mean (empirical Bayes), so a single angry post can't define a day.
- **Many tests need multiplicity control.** Hundreds of daily cells produce false alarms
  by construction, so surprise p-values become **BH-FDR q-values**. This is what lets the
  notification system have a *calibrated* false-alarm rate instead of a guessed threshold.
- **Persistence from AR(1), not curve-fitting.** Post-peak exponential fits accepted only
  **6 of 39** cells. An AR(1) coefficient uses the whole series, yields a standard error,
  and converts cleanly to a half-life via `ln(0.5)/ln(φ)`.
- **Virality as a percentile, not a z.** Given the weak age-normalisation fit, an
  empirical percentile within (platform × age-band) is distribution-free and age-honest.

## Stage 6 — Formulating "acceleration"

The request was for *"the rate of popping up of a scenario"*. Interrogating that phrase
showed it isn't one quantity — it bundles three different questions, and a single number
would blur them:

1. *Is it speeding up?* → curvature. But raw 2nd differences double the noise, so the
   publishable form is **smoothed** (Savitzky–Golay).
2. *Is it bursting right now?* → a **state**, not a slope. Handled by burst detection
   (Kleinberg 2-state HMM, with a Poisson CUSUM / scan-statistic fallback if the series
   prove too short — an empirical question, not an assumption).
3. *Did it come from nothing?* → **emergence**: days from first appearance to crossing a
   volume threshold, and the steepness of the burst's rising limb.

A fourth sense emerged from the data rather than the phrase. Because bge-m3 embeddings
already exist, we can measure **semantic novelty** — the distance of today's posts from a
topic's trailing centroid. This catches a *new sub-narrative inside an existing topic*,
which volume alone cannot see: a topic can stay flat in count while completely changing
in content. For threat monitoring that is often the more important event.

These combine into a single FDR-gated `emergence_score` for alerting, while the
components stay individually inspectable — so an analyst can always ask *why* something
was flagged.

## Stage 7 — The hashtag problem

The notification system was to flag intensity **by hashtag**. Measuring coverage first:
the `hashtags` field is populated for LinkedIn (502/924) and Instagram (237/337) and is
**empty for Telegram, YouTube, Reddit and Facebook**. Mining inline `#tags` from text
adds only Telegram 1,883/14,595 (12.9%) and Reddit 66/2,107 (3.1%).

**Hashtags cover ~15% of the corpus and ~13% of Telegram — the dominant source.** A
hashtag-keyed alerting system would be blind to roughly 85% of what is collected.

Rather than build it as asked and let it fail quietly, we built `tag_metrics_daily`
anyway — hashtag alerting genuinely works where hashtags exist — but recommend keying
alerts primarily on **`topic_id`**, which covers 79% of posts with the remainder
explicitly `unassigned` rather than silently missing. Hashtag and keyword become
secondary facets, with NER entities a natural third.

---

## Principles that emerged

1. **Profile before planning.** Five findings from an hour of profiling invalidated
   large parts of a reasonable-looking plan. Metric definitions are downstream of data
   reality.
2. **A hang is not a refusal.** Diagnose the shape of a failure, not just its presence.
3. **Distinguish absent from zero.** Reddit has *no* engagement; Facebook has *zero*
   likes on 70% of posts. Collapsing those two into `0` would have fabricated a finding.
4. **Never let the collector become the signal.** In any scraped corpus, ask what part of
   the trend is your own instrument.
5. **Prefer an honest gap to a confident artifact.** `NULL` with `support_ok = false`
   is more useful than a z-score of 143.
6. **Read the output, don't trust the pipeline.** v1 ran cleanly end-to-end and reported
   `ok: true`. Every defect was found by inspecting results, not by watching for errors.
