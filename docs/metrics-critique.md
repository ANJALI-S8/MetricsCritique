# Metrics critique — our timeline metrics against the GDELT project

**Written 2026-08-06.** A comparison of `scripts/11_geo_timeline_metrics.py` against the sibling
project at `EDA_GDELT/MetricsCritique/Gdelt`, and an adversarial audit of our own metrics using
that project's central test.

Two systems, two corpora. Theirs codes a GDELT event stream at **location × hour** (503,593 events
over 5 days, 2,096 adm1 units). Ours codes scraped social posts at **entity/theme × day**
(23,356 posts over 46 days). The formulas are not interchangeable — but the *measurement
discipline* is, and that is what this document is about.

**The headline: we are ahead on statistical hygiene and behind on measurement theory.** They have
no multiple-testing correction anywhere in their codebase; we have BH/FDR, support gates, and 52
self-checks. They have an empirical test for whether a metric says anything beyond "this is busy";
we had none, and applying theirs found real problems in ours.

---

## 1. What they built

Eight location-grain metrics, frozen as a contract in `docs/metrices_formual.md` (v1.3):

| # | metric | form |
|---|---|---|
| 1 | Event velocity | `N(t)/Δt`, events per hour |
| 2 | Event acceleration | first difference of velocity, trailing-only 3-bucket MA |
| 3 | Severity / harm | constructed 0–100 index, two tiers |
| 4 | Events started | `count(DISTINCT event)`, plus a deduplicated "story" count |
| 5 | Tone z-score | rolling z against the location's own trailing baseline |
| 6 | corr(tone, severity) | Pearson across events at a location |
| 7 | Half-life | OLS fit of `ln(1+N) ~ −λt`, `t½ = ln2/λ` |
| 8 | Quad score | `(n₁+n₂−n₃−n₄)/N` over CAMEO QuadClass |

Plus a v2 family (`metrics_v2.geo_situation`) of four deliberately volume-orthogonal columns:
growth slope, doubling time, sub-location diversity, and casualty/violence shares.

### The practices worth stealing

- **The spec is a contract, not documentation.** "`build_metrics.py` implements exactly what is
  written here, and any change to a formula is a version bump, not an edit." Their changelog
  records nine repairs with before/after measurements on the same window.
- **NULL, never 0, below minimum sample.** Every row carries `n_events`/`n_mentions` so a NULL is
  explainable.
- **Explicit censoring.** Every row carries `observation_hours` and `is_censored_24h`; windowed
  metrics are NULL where the window exceeds what was observed.
- **Tier separation enforced by schema.** After v1.3, casualty-backed severity (`harm_score`) and
  code-derived severity (`type_severity_score`) are *different columns*, because the rule "never
  rank across tiers" was one their own dashboard forgot.
- **Adversarial self-critique.** `docs/metrics_critique.md` concludes that six of their own eight
  metrics are re-encodings of the two variables the project set out to move beyond.
- **They then killed most of their own replacements with data.** This is the rarest part.

---

## 2. What we built

`*_metrics_daily` — 37 columns per (key, day), across entity / risk / theme:

| group | columns |
|---|---|
| volume | `V`, `V_rate`, `share_V`, `effort`, `log_rate`, `log_count` |
| trend | `growth`, `growth_7d`, `momentum`, `accel`, `accel_smooth` |
| anomaly | `surprise`, `surprise_p`, `surprise_q`, `alert_flag` |
| baseline | `baseline_mu`, `baseline_theta`, `baseline_dist`, `baseline_days`, `baseline_mean` |
| burst | `burst_state`, `burst_intensity`, `burst_method`, `burst_support_ok` |
| persistence | `ar1_phi`, `ar1_half_life_days`, `ar1_ljung_box_p`, `ar1_n_obs`, `ar1_fit_ok` |
| classifier | `avg_score_mean`, `avg_score_eb`, `avg_score_n`, `gray_zone_share` |
| gating | `support_n`, `support_ok` |

The machinery behind them: negative-binomial baselines with a Poisson fallback recorded in
`baseline_dist`; burst detection routed by series length (Kleinberg for long, Poisson CUSUM for
short) with the method recorded per row; AR(1) persistence **with a Ljung–Box whiteness test** so a
half-life from an unfitted series is marked rather than published; empirical-Bayes shrinkage;
Benjamini–Hochberg FDR gating; and 52 statistical self-checks in `scripts/lib/stats_v2.py` covering
leakage, calibration, and estimator recovery.

---

## 3. Side by side

### What we have that they don't

| capability | us | them |
|---|---|---|
| **Multiple-testing correction** | BH/FDR, one family per day pooled across all dimensions; `surprise_q` published per row | **none** — `grep -riE "benjamini\|hochberg\|fdr\|bonferroni\|q.?value"` over their entire repo returns nothing |
| Support gates | `support_ok` per row, blanks the trend family | minimum-sample rules per metric (good), but no gate column |
| Self-check suite | 52 checks: leakage, null calibration, estimator recovery | none |
| Overdispersion handling | NB with Poisson fallback, recorded per row | none — z-score assumes normality on counts |
| Fit quality gating | Ljung–Box on AR(1) residuals; `ar1_fit_ok` | stores `fit_r2` (good) but does not gate on it |
| Shrinkage | empirical Bayes toward a parent mean | none |
| Exposure normalisation | `effort` per day | partial (`velocity_mean` denominator, v1.3) |

**Their anomaly rule is a bare `|z| ≥ 3` with no multiplicity control.** Their own gating
(`n_mentions ≥ 10` plus 24 full trailing buckets) cuts the number of live tests substantially, so
the naive arithmetic — 2,096 adm1 keys × 24 hourly buckets × 0.27% ≈ 136 false anomalies/day — is
an upper bound, not a measured rate. But the direction is not in doubt, and it is exactly the
failure our D12 finding documented on our own side before we corrected it.

### What they have that we don't

| capability | why it matters |
|---|---|
| **Anti-restatement check** | An empirical test for "is this metric just volume?" We had none. See §4 |
| **Hurlbert's PIE** | The size-unbiased diversity estimator. They measured that naive HHI and top-share carry an n-bias in the *opposite* direction (−0.41, −0.45) — inverted volume proxies. Only PIE came out clean (+0.055) |
| **Episode as the unit** | "Phase" is not a property of a day-bucket. An episode has an onset, peak and tail |
| **Orthogonal indicator families** | ACLED's architecture: several independent dimensions published separately, percentile-ranked, rather than one composite built from collinear inputs |
| **Tier separation by schema** | Not by a convention callers can forget |
| **Frozen spec / version bump** | Formula changes are versioned events with before/after measurements |
| **Censoring columns** | `observation_hours`, `is_censored_24h` |

### The thing they did that neither of us usually does

They proposed a replacement metric set, then **measured it and threw most of it away**:

| their proposal | measured | verdict |
|---|---|---|
| `n_dyads` (fragmentation) | ρ +0.961 vs `n_events` | is volume |
| `n_stories` | ρ +0.980 | is volume |
| dyad Shannon entropy | ρ +0.831 | is volume |
| dyad HHI | ρ −0.407 | **n-bias artefact** |
| maximum escalation rung | 396 of 406 keys saturate | dead |
| `pie_dyad` (unbiased) | mean 0.951, min 0.815 | saturated, dead at 5 days |
| sub-location PIE | **ρ +0.055** | **clean — shipped** |

Their own Part 2 recommendations were "wrong twice over, and it is measurable." Of roughly a dozen
proposals, four shipped.

---

## 4. The audit: applying their test to us

Reproducible: **`.venv/bin/python scripts/audit_restatement.py`** (read-only).

Correlate every published metric against `V`, the raw count it is supposed to add information
beyond. Their thresholds: **|ρ| > 0.7 = it is volume, do not ship as a rank**; 0.3–0.7 =
contaminated; < 0.3 = clean. Support-gated rows only.

### Report both statistics — this is not ceremony

Daily counts here are heavy-tailed, and Pearson is inflated by the tail. Where the two disagree,
Spearman is the one to trust:

| metric | Pearson | Spearman | what Pearson alone would have said |
|---|---:|---:|---|
| `burst_intensity` (theme) | +0.808 | **+0.448** | "IS VOLUME" — would have condemned a working burst detector |
| `ar1_half_life_days` (theme) | +0.754 | **+0.494** | "IS VOLUME" |
| `avg_score_eb` (theme) | +0.334 | **+0.157** | "contaminated" |

A single-statistic version of this audit reaches the wrong answer on three metrics. The script
prints a warning wherever the two verdicts differ.

### Results (Spearman)

| metric | theme | risk | entity | verdict |
|---|---:|---:|---:|---|
| `share_V` | **+0.843** | +0.690 | **+0.865** | **IS VOLUME** |
| `V_rate` | +0.890 | +0.905 | +0.814 | by construction (`V/effort`) |
| `log_rate` | +0.864 | +0.899 | +0.771 | by construction |
| `burst_intensity` | +0.448 | +0.680 | +0.372 | contaminated |
| `ar1_half_life_days` | +0.494 | +0.555 | +0.420 | contaminated |
| `ar1_phi` | +0.494 | +0.555 | +0.424 | contaminated |
| `surprise` | +0.468 | +0.462 | +0.176 | contaminated → clean |
| `growth_7d` | +0.191 | +0.359 | +0.048 | clean → contaminated |
| `avg_score_mean` | +0.218 | +0.292 | — | clean |
| `growth` | +0.251 | +0.042 | −0.065 | **clean** |
| `momentum` | +0.182 | −0.009 | −0.057 | **clean** |
| `accel` | +0.177 | +0.079 | −0.056 | **clean** |
| `accel_smooth` | +0.152 | −0.140 | −0.060 | **clean** |
| `gray_zone_share` | −0.116 | −0.332 | — | clean → contaminated |

`n` = 297 (theme), 261 (risk), 1,784 (entity).

### Findings

**1. `share_V` is a volume restatement and it is our most prominent number.** Spearman +0.843
(theme) and +0.865 (entity). It is rendered as a headline percentage in the dashboard. A "share"
that tracks the raw count this closely is telling the analyst that a busy category is busy. The
intent — "how much of today's attention went here" — is legitimate, but on this corpus the day
totals do not vary enough relative to per-category counts for the normalisation to add anything.

**2. `V_rate` and `log_rate` are volume by construction, and that is fine — provided they are not
ranked.** `V_rate = V/effort` is a rescaling, not a second statistic. They belong as model inputs
and context columns. The audit script excludes them from findings by name rather than by threshold,
so the exclusion is a stated decision rather than a number that happens to land under a cut.

**3. The trend family is genuinely clean.** `growth`, `momentum`, `accel`, `accel_smooth` all sit
at |ρ| ≤ 0.25 across all three panels. These are the metrics doing real work.

**4. `surprise` is clean on entity (+0.176) and contaminated on theme/risk (+0.46).** With 9 risk
and 12 theme categories, the NB baseline is fitted on few keys and tracks the level more than it
should. Worth watching, not condemning.

**5. Persistence is contaminated on every panel.** `ar1_phi` and `ar1_half_life_days` at +0.42 to
+0.56. Persistence *should* be volume-free — how long attention lasts is not how much there is.
Some coupling is expected (longer series fit better), which is what `ar1_fit_ok` and the Ljung–Box
gate exist for, but this is the metric most worth re-examining.

### Where their load-bearing defect does *not* apply to us

Their central finding is that the unit of analysis is the document, not the event: GDELT's
extractor emits 7.07 rows per source document (max 150), so every count measures NLP verbosity.
Twelve of their verification spikes had a **median of one source document**.

**We largely do not have this problem.** Exact-duplicate post text is **600 posts, 2.6%** of 23,356,
and the top offenders are the already-documented LinkedIn artifact pages (`x31`, `x24`) and a Reddit
boilerplate string. Our `post_uid` is a real post.

**But we have an analogue they would recognise: author concentration.**

| theme | posts | authors | posts/author |
|---|---:|---:|---:|
| `maritime_security` | 2,957 | 406 | **7.28** |
| `border_issues` | 1,210 | 274 | 4.42 |
| `security_military_relations` | 607 | 187 | 3.25 |

The three most prolific accounts in the labelled corpus: `@rtnews` (678 posts), `intelslava` (510),
`@war_monitor` (456). A burst in `maritime_security` can be one account posting forty times. We do
not currently measure or correct for this anywhere.

---

## 5. What to port

Ranked by value against cost. **S** ≈ under a day, **M** ≈ a few days.

| # | Port | Rationale | Cost |
|---|---|---|---|
| 1 | **Anti-restatement gate** in the metrics build — ✅ **DONE**, see §5b | Turns this one-off audit into a standing invariant, in the same spirit as `stats_v2.run_self_checks()` | S |
| 2 | **Demote `share_V`** from headline percentage to context column | It is volume, and it is the first number a user sees | S |
| 3 | **Hurlbert's PIE** over distinct authors — ✅ **DONE**, clean at entity grain only, see §5b | We had no diversity metric at all. PIE specifically — they measured HHI and Shannon as *inverted* volume proxies | S |
| 4 | **Author-syndication ratio** = posts / distinct authors — ✅ **DONE**, shipped as data-quality, not a rank, see §5b | Directly addresses the 7.28 finding | S |
| 5 | **Escalation ladder: first-passage time** to a high CAMEO root, plus shares | We now have the codes (`event_codes`, 21,952 rows). Ship first-passage and shares — they measured the *maximum* rung saturates at 396/406 keys | M |
| 6 | **Episode grain** — contiguous runs above baseline, with onset/peak/tail | We already detect burst episodes via Kleinberg; this exposes them as rows so "phase" becomes expressible | M |
| 7 | **Novelty / actor entry** — entities not seen in the prior 30 days | One window function over `entity_mentions`; cannot be derived from volume | M |
| 8 | **Tier separation by schema** | Our 52-uncalibrated-of-59 classes are exactly this shape: calibrated and uncalibrated codes share one column and will rank against each other | M |
| 9 | **Frozen-spec discipline** — formula changes become version bumps with before/after numbers | `docs/GEOPOLITICAL_ONTOLOGY_SPEC.md` already exists; add the convention | S |

### Not porting, and why

- **Hawkes branching ratio.** The best idea in their document conceptually — it separates "a lot
  happened once" from "this is feeding itself". But it needs per-key MLE on a point process, and our
  grain is daily over 46 days. Not enough resolution. Revisit if we move to hourly.
- **Fragmentation / dyad measures.** They measured it dead: `pie_dyad` mean 0.951, min 0.815 —
  saturated, because actor codes at short windows are mostly single-occurrence role strings. Our
  entity dyads would be worse, since cross-lingual entity fragmentation is a known open defect.
- **Stretched-exponential decay.** Blocked on history length for them and for us.
- **Casualty/harm index.** No casualty data in our corpus.

---

## 5b. Stage 1 implemented — and what the gate caught

Ports 1, 3 and 4 are **built** (2026-08-06). What happened on the first real run
is more instructive than the recommendation was.

**Port 1, the gate**, is now wired into `scripts/11_geo_timeline_metrics.py` and runs on
every build: 42 metric-panel pairs checked, and a `SystemExit` if any *undeclared* metric
crosses |Spearman| 0.7. Restatements we have already examined are declared in
`audit_restatement.KNOWN_RESTATEMENTS` **with a written reason**, so the gate catches the
*next* one rather than re-reporting the ones we know about. Behaviour is pinned by
`scripts/tests/test_restatement_gate.py` (15 checks), whose load-bearing assertion is the
negative one: plant a pure function of volume and confirm it is caught. A gate that only
ever passes is indistinguishable from no gate.

**The gate immediately failed the build on two metrics I had just added.**

```
UNDECLARED VOLUME RESTATEMENT: theme.n_authors        (Spearman +0.801)
UNDECLARED VOLUME RESTATEMENT: risk.posts_per_author  (Spearman +0.724)
```

This is the same lesson the GDELT project recorded — *every count form is volume wearing a
hat* (`n_dyads` +0.961, `n_stories` +0.980). I added a count and it was a count. Resolved:

- **`n_authors`** — removed from the audited set entirely. It is a denominator and a support
  column, the same class as `support_n` and `score_n`, which were never candidates. Auditing
  a count against a count answers nothing. Not a demotion, a category error on my part.
- **`posts_per_author`** — declared as a **data-quality column, not a rank**, which is
  precisely how the GDELT project ships its own syndication ratio after measuring it at
  +0.40 to +0.61 ("over half volume"). It flags one-account amplification; it does not rank
  importance.

**Port 3, `pie_author`** (Hurlbert's PIE over distinct authors) — the one that survives,
partially:

| panel | Spearman vs V | median | verdict |
|---|---:|---:|---|
| entity (n=1,784) | **−0.115** | 0.667 | **clean** |
| theme (n=297) | −0.387 | 0.860 | contaminated |
| risk (n=261) | −0.412 | 0.729 | contaminated |

Clean where there are 1,784 keys, contaminated where there are only 9–12 categories. With
that few cells a day's volume and its author diversity necessarily co-move, so the residual
is at least partly structural rather than a defect in the estimator. **Do not rank on
`pie_author` at theme or risk grain.** At entity grain it is the first genuinely
volume-orthogonal column this project has had.

Scoreboard for Stage 1: of three new columns, **one clean (on one grain), one demoted to
data quality, one withdrawn as a category error.** That ratio is not a disappointment — it
is the same ratio the GDELT project got (four of ~twelve proposals shipped), and it is what
the gate is for. Without it, all three would have shipped as metrics.

## 6. Getting to "current + that"

The two systems are complementary, and the merge is additive — nothing we have needs to be
discarded to adopt what they have.

**Stage 1 (S).** ✅ Ports 1, 3, 4 done — see §5b. Port 2 (demote `share_V` in the UI) is the
remaining piece. Outcome so far: the build now refuses to ship an undeclared volume
restatement, and `pie_author` gives us one genuinely volume-orthogonal column at entity grain.

**Stage 2 (M, after the CAMEO axes are wired).** Ports 5 and 8. The escalation ladder is the
payoff from Phases 2–3 — an ordinal severity signal that is not derived from volume or from the
classifier's confidence. Tier separation matters *before* sub-code alerts ship, not after.

**Stage 3 (M).** Ports 6 and 7. Episode grain and actor novelty. These change the shape of the
output rather than adding a column, so they want the earlier stages settled first.

**Stage 4 (S, continuous).** Port 9. Freeze the metric spec with a changelog, and require
before/after measurements for formula changes.

### The honest summary

Their measurement theory is better than ours and their statistical practice is worse. They caught
that six of their own eight metrics were one variable measured twice; we never asked the question,
and when I asked it here it found that our most prominent published number is a volume restatement.
Conversely, they run per-location per-hour anomaly tests with no multiplicity correction of any
kind, which we would not now ship.

The single most valuable thing to take is not any one metric. It is the **habit of testing whether
a metric says anything beyond the count** — and being willing to delete it when it does not. They
threw away most of their own proposed replacements. That is the standard.

---

## Reproducing every number here

| claim | how |
|---|---|
| correlation tables | `.venv/bin/python scripts/audit_restatement.py` |
| duplicate-text share | `GROUP BY text HAVING count(*)>1` over `analytics.post_facts` |
| posts/author by theme | join `geo.theme_labels` to `post_facts`, `count(*) / count(DISTINCT author_uid)` |
| `event_codes` shape | `SELECT axis, level, count(*) ... GROUP BY 1,2` |
| their absence of FDR | `grep -riE "benjamini\|hochberg\|fdr\|bonferroni\|q.?value" <their repo>` |

**Caveat on their figures.** Their `metrics_critique.md` quotes an 8,532,459-row database that
their own provenance warning says is unreachable; the primary has never held more than 503,593
rows. They state that every *structural* defect reproduces on the smaller window but the
*magnitudes* do not. Numbers quoted here are taken from `docs/metrics_v13_and_v2.md`, which is the
re-measured document.
