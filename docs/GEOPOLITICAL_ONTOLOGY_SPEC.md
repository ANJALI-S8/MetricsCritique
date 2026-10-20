# Geopolitical Ontology Layer — Spec & Verification Record

Status: **mixed, remediation in progress**. Original build: one subsystem SOUND, one
PARTIAL-but-real-improvement, one PARTIAL/unmeasured, one **UNSOUND**. A remediation pass
targeting the UNSOUND risk/theme labels and the sparse entity subtype coverage is
underway — see **§6 Remediation pass** at the end of this document for the current,
authoritative state; §1–5 below describe the original build and are otherwise unchanged.
See `docs/GEO_ONTOLOGY_CONTEXT.md` for the environment/taxonomy this was built against.

Corpus: 23,356 posts (`data/analytics.duckdb.post_facts`), India-perspective
OSINT feed on China's regional influence / Belt-and-Road activity, ~62%
English + ru/zh-cn/bn/uk/hi/other. Output DB: `data/geo_ontology.duckdb`
(DuckDB, single-writer; each Implement stage is the sole writer for its turn,
Verify stages open read-only after the writer has finished).

---

## 1. What was built

| Subsystem | Script | Writes | Rows |
|---|---|---|---|
| Topic clustering v2 | `scripts/09_ontology_v2_cluster_fix.py` | `topic_dim_v2`, `post_topic_v2` | 11 topics / 23,356 assignments |
| Entity extraction | `scripts/10_entity_extraction.py` | `entity_dim`, `entity_mentions` | 29,136 entities / 110,983 mentions |
| Risk / theme labels | *(scratchpad only — see §5 gap)* | `risk_labels`, `theme_labels` | 20,777 / 24,566 label rows |
| Timeline metrics + alerts | `scripts/11_geo_timeline_metrics.py` | `entity_metrics_daily`, `risk_metrics_daily`, `theme_metrics_daily`, `geo_alert_candidates` | 16,281 / 412 / 552 / 490 rows |
| Alert export | `scripts/11_geo_timeline_metrics.py` | `UI/data/geo_alerts.json` | 490 records (flat list, mirrors `geo_alert_candidates` exactly) |
| Dashboard sections | `UI/build_dashboard.py`, `UI/assets/dash.js`, `UI/assets/dash.css` | §16–19 of `UI/index.html` | entity directory, risk heatmap, theme prevalence, timeline & alerts |
| Reference notification poller | `scripts/notify_poller.py` | `reports/notify_poller_state.json`, `reports/notify_poller.log` | — |

### 1.1 Taxonomy (as implemented)

- **Entity types**: PERSON (`political_leader`/`military_leader`/`diplomat`/
  `business_executive`), ORG (`ministry`/`state_owned_enterprise`/
  `armed_group`/`international_org`/`business_org`), LOCATION (`port`/
  `border_town`/`economic_zone`/`pipeline`/`railway`). Every mention keeps
  `entity_uid`, `surface_form`, `entity_type`, `entity_subtype`,
  `subtype_confidence`, `subtype_source` (`gazetteer`|`unresolved` — the
  zero-shot subtyping pass named in the original taxonomy was not run this
  session), `post_uid`. Unresolved entities are kept, never dropped
  (provisional `entity_uid` = `unresolved_` + slugified surface form).
- **Risk categories** (9) / **thematic dimensions** (12): exactly the list in
  `docs/GEO_ONTOLOGY_CONTEXT.md` §Taxonomy, multi-label per post via
  `embedding_similarity_bge_m3_zeroshot` (cosine similarity of the post's
  bge-m3 embedding to a category description embedding, one flat threshold
  0.45 for all 21 categories, `gray_zone` flagged near the boundary).
- **Timeline metrics**: `scripts/lib/stats_v2.py` reused unchanged (NB-surprise
  baselines, Benjamini–Hochberg FDR, AR(1) persistence, Savitzky-Golay
  acceleration, empirical-Bayes shrinkage) — applied to `entity_uid × day`,
  `risk_category × day`, `theme_category × day` in addition to the existing
  `topic_id × platform × day` grain in `data/metrics.duckdb`.
- **Alerts**: `geo_alert_candidates` = FDR `q < 0.05`, `fdr_scope="day"`
  computed independently **per dimension family** (entity/risk/theme each get
  their own BH correction, not pooled) — same philosophy as
  `data/metrics.duckdb.alert_candidates`, extended to the three new grains.

---

## 2. Verification verdicts (independent pass, honest — including what is not sound)

Each subsystem below was checked by re-deriving its numbers from the
underlying rows/text rather than trusting the builder's own summary. Verdicts
use SOUND / PARTIAL / UNSOUND; PARTIAL covers two different situations
(a real, measurable improvement that still has a residual defect, and a
verification that could not be completed) — both are spelled out, not
conflated into one number.

### 2.1 Topic clustering v2 — **PARTIAL** (real, verified improvement; one residual defect)

The known bug (`docs/GEO_ONTOLOGY_CONTEXT.md`): `min_cluster_size =
max(25, N//80)` scaled with corpus size, collapsing to 3 clusters at N=23,356
with one cluster holding 85.3% of the corpus (94.3% of assigned posts).

**Fix**: HDBSCAN `min_cluster_size`/`min_samples` are now fixed absolute
constants (15 / 5), chosen once on a reservoir sample and applied unchanged to
the full corpus — decoupling cluster count from N by construction.

**Independently reproduced** from `topic_dim_v2`/`post_topic_v2`
(`run_at 2026-07-30T06:20:18Z`): 11 clusters (t000–t010). Data integrity is
clean — `post_topic_v2` has exactly 23,356 rows (= `post_facts` count),
23,356 distinct `post_uid`, one run. Two valid denominators for
noise/largest-cluster share, both far below the old 85%:

| Denominator | Noise | Largest cluster |
|---|---|---|
| Raw HDBSCAN diagnostic (~21,228 embedded posts, pre-fallback) | 16.45% | 23.04% (t005) |
| Final assignment (`post_topic_v2`, 23,356 posts, post-fallback) | 10.9% (2,548 rows, `cluster_id=-1`) | 25.7% of corpus / 28.9% of non-noise (cluster 10) |

`assign_method` breakdown ties out exactly: `hdbscan_core`=17,737,
`nearest_centroid`=3,071 (→ 20,808 non-noise), `no_embedding`=2,128,
`noise_unassigned`=420 (→ 2,548 noise). **3→11 clusters, largest-cluster share
85–94%→23–29%** under every denominator checked — a real, ~3x structural
change, not a rounding artifact or relabeled noise.

**Coherence, checked by reading source text directly (not just
`top_terms`/exemplar columns)**: most clusters are topically real —
t001 = Ukraine drone/UAV chatter, t002 = Russia-Ukraine war, t005 =
Iran/Middle-East/Trump, t003 = India NEET-exam politics, t008/t009 = India
defence & India-China border, t000 = a tight ransomware-gang-name cluster
(Qilin/DragonForce/INC Ransom) mislabeled "Misc."

**Residual defect**: the single largest final cluster (**t010**, n=6,010,
25.7% of the whole corpus) is **not coherent** under direct text sampling
despite a plausible-looking `top_terms` list ("china, hong kong, security,
india"). Its `hdbscan_core` sample mixed check-fraud statistics, a SpaceX
geothermal spinoff, Microsoft Xbox layoffs, Wang Yi diplomacy, a Vietnamese
malware complaint, and Russia-China naval drills; its `nearest_centroid`
members were even less related (a LinkedIn 404 page, a Nigerian
fire-service review, a Commonwealth Games travel update, an r/TrivandrumMeetup
housekeeping post, a Sri Lanka AI-governance conference). **This is the same
"undifferentiated miscellany" failure mode as the old bug, shrunk from 85% to
~26% of the corpus, not eliminated.**

Secondary caveat: some clusters are format/boilerplate-driven, not purely
semantic — t002's top terms are dominated by Reddit markup ("link comments,
link, comments, submitted"), t006/t007 by RSS/BBC markup — bge-m3 embeddings
are partly clustering on source-platform text patterns.

Minor footgun: `topic_dim_v2.noise_frac_total`/`largest_cluster_share_total`
use the ~21,228-embedded-posts denominator (pre-fallback); the final
`post_topic_v2` table uses 23,356 (post-fallback). Both numbers above are
correct for the question they answer — don't compare them to each other
without noting the denominator switch.

#### Cluster names (2026-07-30, assigned from direct text sampling)

`topic_dim_v2` now carries `cluster_name`, `cluster_description`, and a
`coherent` boolean — assigned by reading 12 real posts per cluster directly
(not just `top_terms`/2 exemplars, which is what produced the misleading
"Misc" label on t000 in the first place). 9 of 11 are genuinely coherent;
t006 and t010 are honestly flagged `coherent=false` rather than force-fit:

| topic_id | size | name | coherent |
|---|---:|---|:---:|
| t000 | 561 | Ransomware Leak-Site Victim Feed | ✅ |
| t001 | 517 | Ukraine Real-Time Air-Raid & Drone Alert Feed | ✅ |
| t002 | 3,293 | Russia-Ukraine War & Diplomacy | ✅ |
| t003 | 709 | Domestic Politics & Protest Movements (India/Bangladesh-heavy) | ✅ |
| t004 | 314 | FIFA World Cup 2026 Coverage | ✅ |
| t005 | 4,890 | Middle East Conflict — Iran/Israel/US Strikes | ✅ |
| t006 | 1,328 | General World News Wire (BBC/Reddit mix) | ❌ loose |
| t007 | 323 | Wildfires & Natural Disasters (mostly Europe) | ✅ |
| t008 | 757 | India Defence, Military & Border Security | ✅ |
| t009 | 457 | China-India-Neighbourhood Geopolitics & BRI-Adjacent Analysis | ✅ |
| t010 | 4,588 | Unsorted / Miscellaneous (residual) | ❌ incoherent |

t009 is the corpus's core BRI/String-of-Pearls strategic-analysis cluster —
directly on-theme for the project's stated purpose. t010 confirms the §2.1
residual-defect finding by name: on direct sampling it mixes LinkedIn bug
reports, dead pages, a pentesting explainer, and a stock-market explainer
with genuinely on-topic maritime-security content — still the leftover
bucket from the corpus-size-scaling clustering bug, not a real topic.

### 2.2 Entity extraction — **PARTIAL / precision unmeasured**

At the time of the independent verification pass, `entity_dim`/`entity_mentions`
did **not yet exist** — the Implement stage (`scripts/10_entity_extraction.py`)
was still mid-NER-pass (per `docs/GEO_ONTOLOGY_CONTEXT.md`'s own stated
protocol, "Verify stages open it read_only=True only, and only after the
relevant Implement stage has finished," so verification correctly did not run
against a half-written table). The extraction subsequently completed
(confirmed after the fact: 20,347 posts processed, 110,983 mentions, 29,136
entities, 0 errors, `entity_dim`/`entity_mentions` both present with row
counts matching the run log exactly) — **but no re-verification of precision
has been performed since**. Nothing below should be read as a precision
measurement; it is what the verification pass could establish from code
inspection alone, before the table existed to sample from.

Known risks (code-inspection only, not yet confirmed against real extracted
text):

1. **Gazetteer overreach.** Several gazetteer surface forms are bare
   city/country names also used generically: `Djibouti` → `djibouti_base`
   (`port`) even though most mentions of "Djibouti" in a geopolitical feed are
   about the country/base generally, not literally the port; `Colombo` →
   `colombo_port` (Colombo is Sri Lanka's capital, not exclusively the port);
   `Gwadar` → `gwadar_port` (also a town name). Any LOCATION mention of these
   bare names is force-labeled with `subtype_confidence=1.0` regardless of
   context — a likely systematic subtype false-positive source.
2. The script's own "~84% correct" figure is from a **216-post prototype
   sample** taken during design, not this 20,347-post production run. Known
   failure modes it names (bare acronyms like "AI" tagged ORG, single stray
   non-Latin characters tagged LOC) are explicitly left unfiltered.
3. `MAX_CHARS=1000` truncation means entities appearing later in long posts
   are silently missed (an undercounting/completeness issue, not a precision
   one).
4. `entity_dim`'s `entity_subtype` is a **mode** over each entity's mentions —
   correct for a rollup, but means a minority-subtype (or no-subtype) mention
   disagreeing with `entity_dim`'s value is expected, not a bug.

### 2.3 Risk categories & thematic dimensions — **UNSOUND**

Stratified sample: n=42 (1 confident + 1 gray-zone example per risk category
[9] and theme category [12], English-only, judged against real
`post_facts.text`, not just the score).

| | Strict (unambiguous match only) | Lenient (loosely-thematic counts as hit) |
|---|---:|---:|
| All rows (n=42) | 29% (12/42) | 45% (19/42) |
| non-`gray_zone` (n=21) | 43% (9/21) | 62% (13/21) |
| `gray_zone=true` (n=21) | 14% (3/21) | 24% (5/21) |

The `gray_zone` flag is doing real, if crude, work (its subset is markedly
worse) — but even the "clean" subset tops out at 62% lenient precision.

**Confirmed precision failures** (read against source text): generic or
topically-adjacent-but-wrong hits dominate — a Flexport shipping-map post →
`shipping_disruptions`; a UK government-borrowing explainer →
`project_delays`; a domestic-rail-tunnel-completion story → `bri` (no
overseas/BRI content at all); a typhoon-flight-cancellation story → `bri`.
Category confusion: interstate-war rhetoric (state actors) mis-tagged
`insurgencies` (a non-state-actor category); a Houthi blockade of Saudi oil
exports tagged `sanctions` (a blockade is not a sanction). Over-tagging: one
long post picked up 6 of 9 risk categories and 5 of 12 themes simultaneously.

**Confirmed recall failures** (full text read, not snippets): a Kirill
Dmitriev quote that EU sanctions "do not work, cost >€3T" — zero risk AND
zero theme tags. A von der Leyen Kiev visit dense with
humanitarian/political/security content — 3 risk tags, **zero** theme tags.
A Polish-minister post on Ukraine-EU accession / anti-Ukrainian sentiment /
refugees / corruption — zero risk and zero theme tags. A Yemeni-missile
strike on a named pipeline + ARAMCO facility — correct `pipeline_vulnerabilities`
risk tag, but zero theme tags (missing `energy_security` at minimum).
`theme_labels` has a visibly worse recall gap than `risk_labels`.

**Root cause**: one flat cosine-similarity threshold (0.45) across all 21
categories, with no per-category calibration. Corpus-wide, only 11,401/23,356
posts (48.8%) carry any risk or theme label at all; mean score sits at
~0.47–0.49 against that same 0.45 threshold for every category; `gray_zone`
is true on ~80–85% of all assigned labels.

**Consequence**: do not treat `risk_labels`/`theme_labels` as ground truth
without re-tuning (per-category thresholds, or a stronger classifier).
Consumers should at minimum filter to `gray_zone=false` — and even that
subset is only ~45–62% precise on lenient scoring. The dashboard (§17)
surfaces this verdict inline, not just in this document.

### 2.4 Timeline metrics (entity/risk/theme) — **SOUND**

`stats_v2.run_self_checks()`: 46/46 pass, independently re-run (including the
D1 zero-inflation regression tests). `entity_metrics_daily`=16,281 rows (443
entities, every one with in-window `sum(V) >= 20`, matching the
`entity_min_posts_ts` floor exactly); `risk_metrics_daily`=412;
`theme_metrics_daily`=552. Every non-null surprise cell across all three
tables has non-null `support_n` and `support_ok=True` (0 exceptions). The
support gate (`baseline_days>=14 & baseline_mean>=0.5 & V>=3`) was
independently re-derived from raw stored columns and matched the stored
`support_ok` flag with **0 mismatches** across all 16,281+412+552 rows.

Full dataset-wide independent recomputation of NB `baseline_mu` and
exceedance p-values (10,762 fitted cells total) produced **0 mismatches**.
Independent BH q-value recomputation (per-day, per-dimension, mid-p) across
2,923 gated rows: **0 mismatches**. `geo_alert_candidates`=490 rows (entity
337, risk 76, theme 77), `q_value` range [4.47e-14, 0.0499], all < 0.05,
severity bands internally consistent (`high` iff `q<0.01`, 0 violations), 0
duplicate `(day, dimension_type, dimension_key)` keys, alert counts match
`*_metrics_daily.alert_flag` counts exactly. Window
2026-06-14…2026-07-29 (46 days); first alert day 2026-06-28 =
`window_start + baseline_min_days(14)`, consistent with no-look-ahead design.
`UI/data/geo_alerts.json`: valid JSON, 490 records, 0 nulls, matches the DB
row count and content exactly.

No correctness defects found. One design observation, not a defect: alerts
cluster on ~24 of 46 window days because BH-FDR is corrected independently
**per dimension family per day** (`fdr_scope="day"`) rather than jointly, and
because real news days move many correlated series at once — expected given
the stated design, worth knowing that daily alert counts reflect co-occurring
real events, not independent tests. A few alerts sit right at the `q<0.05`
edge (e.g. `support_n=3`, exactly at the `support_min_v` floor) —
mathematically correct per the gate, but a reminder that `support_min_v=3` is
a low floor by construction (same default as Metrics v2).

---

## 3. Dashboard

Extends the existing `UI/index.html` (same z-theme, same conventions — plain-
English one-liners, table-view fallback, light+dark, no per-panel filter
state beyond the existing global filter bar) with four new sections, §16–19:

- **§16 Entity directory** — facts + type breakdown, top-10-per-type bar
  charts, full directory table (`entity_dim`, ≥2 distinct posts, 7,117 of
  29,136 rows shipped row-by-row; singletons counted in the totals but not
  rendered). Carries the §2.2 PARTIAL/unmeasured caveat inline.
- **§17 Risk categories & thematic dimensions** — opens with the §2.3 UNSOUND
  verdict as a warnbox (same visual pattern as the existing Hate/toxicity
  section's "uncalibrated, not publishable" box), then a risk-category × day
  surprise heatmap and a 12-panel theme-prevalence-over-time small-multiple.
- **§18 Timeline & alerts (entity/risk/theme)** — same FDR-gated alerting
  philosophy as the existing §2 Alerts, extended to the three new grains: a
  three-swimlane event timeline (all 9 risk categories, all 12 themes, top 20
  entities by alert-day count) plus a full sortable alert table.
- **§19 Geo-ontology — verification summary** — a compact, always-visible
  table of all four verdicts above (cluster PARTIAL, entities PARTIAL, risk/
  theme UNSOUND, timeline SOUND), so the caveats are load-bearing UI, not
  just prose in this document.

`UI/build_dashboard.py` reads `data/geo_ontology.duckdb` read-only as a
second, independent source alongside `data/metrics.duckdb` (own connection,
own snapshot-copy fallback if a writer holds the lock) and merges
`entity_dim`, `entity_metrics_daily`, `risk_metrics_daily`,
`theme_metrics_daily`, `geo_alert_candidates` into the same
`UI/data/metrics.json` snapshot under their own table names — additive, no
existing table renamed or touched. If `data/geo_ontology.duckdb` is absent,
§16–19 render an explicit "awaiting data" state per table, the same pattern
already used for optional Metrics-v2 tables — never a fixture stand-in.

Rebuild: `python UI/build_dashboard.py` (writes `UI/index.html` and
`UI/data/metrics.json`; add `--static-embed` for an offline `file://`-safe
build). Serve: `cd UI && python3 -m http.server 8877`.

**Rendering verified** (headless DOM check via jsdom against a
`--static-embed` build, both `data-theme="dark"` and `data-theme="light"`):
19 sections, 32 panels rendered, **0 blank, 0 render errors, 0 panels without
content** in either theme. Each of the six new panels independently confirmed
non-empty: `entity_top_bars` (3 charts), `entity_directory_table` (1 table,
7,117 rows), `risk_heat` (1 heatmap + 1 table), `theme_facets` (12 charts),
`geo_timeline` (3 swimlane charts), `geo_alerts_table` (1 table, 490 rows).

---

## 4. Alerts & notification poller

No delivery channel (Slack/email/webhook) was specified for this project —
per `docs/GEO_ONTOLOGY_CONTEXT.md`, the deliverable is the **substrate**:
`geo_alert_candidates` (§2.4, SOUND) plus the clean `UI/data/geo_alerts.json`
export, and a reference poller that proves the read → diff → notify loop
end to end.

`scripts/notify_poller.py`:

- Reads `UI/data/geo_alerts.json` (already FDR q<0.05 gated — every row
  qualifies).
- Identity key `(day, dimension_type, dimension_key, metric)` — matches the
  DB's own uniqueness (0 duplicate keys, §2.4) — so a rebuild that nudges
  `value`/`q_value`/`rationale` slightly without changing the underlying
  finding does not re-fire.
- `send_notification()` is the single integration point a real channel would
  replace; today it only logs (`reports/notify_poller.log`, structured JSON
  lines, plus a human-readable stdout line) — **nothing is sent anywhere**.
- Marker state in `reports/notify_poller_state.json` (seen-keys set,
  `last_run_at`, run count). `--dry-run` shows what would fire without
  advancing the marker; `--reset` forgets all previously-seen alerts.

**Run twice to prove it, both confirmed**: first run against a clean state →
490/490 new, marker advanced to `runs: 1`, 490 lines appended to the log.
Second run, same input → `0 new` (idempotent), marker advanced to `runs: 2`.
`--reset --dry-run` correctly re-surfaced all 490 as new without touching the
persisted state.

---

## 5. Known gaps for a future session

1. **t010 mega-cluster** (§2.1) needs a second pass — likely either a finer
   `min_cluster_size`/`min_samples` for the noise/fallback path specifically,
   or a manual split of this one cluster; do not brief off `t010` as a single
   coherent topic today.
2. **Entity precision is unmeasured** (§2.2) — the next verification pass
   should pull a stratified sample of real `entity_mentions` rows against
   source text before this table is used for anything precision-sensitive.
   The gazetteer overreach risk (Djibouti/Colombo/Gwadar) is cheap to check
   first.
3. **Risk/theme labels are UNSOUND** (§2.3) — needs per-category threshold
   calibration (or a stronger classifier) before being trusted as ground
   truth; the flat 0.45 threshold is the root cause of both the precision and
   recall failures measured.
4. **`risk_theme_labels.py` was never committed to `scripts/`** — it currently
   only exists in this session's scratchpad
   (`/tmp/.../scratchpad/risk_theme_labels.py`). The rest of this pipeline
   (`09_ontology_v2_cluster_fix.py`, `10_entity_extraction.py`,
   `11_geo_timeline_metrics.py`) is in `scripts/` and reproducible;
   `risk_labels`/`theme_labels` currently are not, from this repo alone. Move
   or recreate it in `scripts/` before relying on this layer being
   rebuildable end to end.
5. The zero-shot entity **subtyping** pass named in the original taxonomy
   (`subtype_source: gazetteer|zero_shot|unresolved`) was not run this
   session — every non-gazetteer mention currently has
   `subtype_source='unresolved'`, not `'zero_shot'`.

---

## 6. Remediation pass (2026-07-30, later — IN PROGRESS, check live state before trusting this section)

Follow-up direct verification (not an automated agent) closed part of gap #2 above and
found two new, more specific problems, both now being fixed:

- **Base NER precision, now measured**: two fresh 25-mention stratified samples
  (gazetteer-resolved and unresolved) both scored **25/25** surface-form-present-in-
  source-text, spanning English/Russian/Chinese/Bengali. The underlying extraction is
  solid; the taxonomy's *subtyping* is where the real gaps are.
- **Subtype coverage is real but tiny**: only 1,649/110,983 mentions (1.5%) have any
  subtype — the zero-shot fallback named in the taxonomy (§1.1) was designed but never
  implemented, so 98.5% of entities are typed only as generic PERSON/ORG/LOCATION.
- **Entity-type/subtype consistency bug**: 42/1,649 gazetteer matches (2.5%) have an
  `entity_type` inconsistent with the matched subtype (e.g. "Narendra Modi" tagged
  `political_leader` but under `entity_type=ORG`/`LOCATION` instead of `PERSON`) — the
  upstream generic NER inconsistently classifies famous names by sentence context, and
  the gazetteer match didn't override it. Concentrated in `political_leader` (42/983,
  4.3%) and negligible in `diplomat` (2/98); every other subtype: 0 mismatches.

**Remediation attempt #1 — failed on infrastructure, not logic.** A workflow targeting
both the §2.3 UNSOUND risk/theme labels and the subtype-coverage gap above was launched;
all 5 stages hit the session's usage limit before any completed (0/5 done). **No data was
touched** — `data/geo_ontology.duckdb` verified unchanged (same row counts and mtime as
the original build). One artifact survived the crash: `scripts/12_risk_theme_classification.py`
(complete, ~367 lines), whose docstring documents real calibration work already done
before the crash — a fresh 105-post hand-judged sample confirmed per-category threshold
tuning alone caps out around 48% precision (the same conclusion as §2.3, now with a wider
sample), so it pivots to a bi-encoder-recall-net + mDeBERTa-v3 multilingual-NLI final
decision, calibrated to `NLI_THRESHOLD=0.22` (72%/46% lenient precision/recall, 61%/65%
strict, on that sample). **This script has not yet been run against the full corpus** —
`risk_labels`/`theme_labels` are still the original UNSOUND values. The entity-subtype fix
(`scripts/13_entity_subtype_resolution.py`) was never started.

Remediation is being resumed. This section will be updated with final before/after
numbers once it completes — until then, treat §2.3 (UNSOUND) and the subtype-coverage
gap above as still the live, accurate state of `data/geo_ontology.duckdb`.

**Update — remediation completed.** The risk/theme reclassification finished (5 hours,
clean run, no crashes this time): `risk_labels` 20,777→11,733 rows, `theme_labels`
24,566→7,876 (both smaller — the fix is more conservative, rejecting more of the old
false-positive labels rather than accepting them). Its own calibration sample scored
72%/46% precision/recall (lenient), 61%/65% (strict) — a real improvement over the old
29-45%. `entity_metrics_daily`/`risk_metrics_daily`/`theme_metrics_daily`/
`geo_alert_candidates` were regenerated from the fixed labels (46/46 self-checks still
pass; alerts 490→463). **This has not yet been independently re-verified with a fresh
hand-judged sample the way the original UNSOUND finding was** — until that happens,
treat risk/theme as *improved* rather than *proven*. The entity-subtype fix
(98.5% unresolved) was not reattempted this pass.

---

## 7. Topic clustering v3 (2026-07-30, later — fixed-k=33, cuML)

### 7.1 Why a v3

Investigating whether GPU (cuML) could accelerate the v2 clustering fix surfaced a
separate, real finding: cuML's HDBSCAN does not have a usable parameter region on this
embedding set. An 85-combination sweep (3 `n_neighbors` × ~8 `min_cluster_size` × several
`min_samples`) found only two regimes — severe over-fragmentation (100-300+ clusters,
30-43% noise, largest cluster 1-5%) or a sharp collapse into a single 92-96% mega-cluster
— with no stable middle ground the way the CPU reference library has. cuML's UMAP
produces a measurably different embedding geometry from `umap-learn`, so HDBSCAN
parameters don't transfer between them.

The fix: stop fighting HDBSCAN's instability and use fixed-k clustering instead — the
same class of algorithm the CPU pipeline already falls back to (`MiniBatchKMeans`) when
HDBSCAN misbehaves, here GPU-accelerated (`cuml.cluster.KMeans`, k=33) for speed (0.6s
vs the multi-minute CPU HDBSCAN pass) with none of the discontinuity, since fixed-k
clustering can't collapse to one mega-cluster or explode into hundreds by construction.

### 7.2 Result

`n_clusters=33` on the same 21,228 bge-m3 embeddings (L2-normalized, random_state=42,
n_init=10): sizes range 128–1,202, **largest cluster only 5.66% of the corpus** — far
better balanced than v2 (23-29%) or either cuML HDBSCAN regime (13-96%). The 2,128 posts
with no embedding are explicitly `assign_method='no_embedding'` in `post_topic_v3`, never
silently dropped (23,356 total rows = exact `post_facts` count).

**Every cluster was read, not guessed** — 10 real posts sampled per cluster (seed 42)
and read against source text before naming, the same standard as v2's naming pass.
27 of 33 are genuinely coherent single topics; 6 are marked `coherent=false` honestly
rather than force-named:

| topic_id | name | coherent |
|---|---|:---:|
| g000 | General World News & Lifestyle Miscellany | ❌ loose |
| g002 | Geopolitical & Security Analyst Commentary | ❌ loose |
| g004 | International Politics Miscellany | ❌ loose |
| g008 | Low-Signal / Off-Topic Grab-Bag | ❌ artifact |
| g011 | Broken LinkedIn Pages (404) | ❌ **data artifact, not a topic** |
| g026 | International Organizations & Diaspora/Development News | ❌ loose |
| g027 | Generic LinkedIn/Career Content (mistagged with geopolitical keywords) | ❌ **keyword-mistagging artifact** |

The remaining 27 include Ransomware Leak-Site Victim Feed, FIFA World Cup 2026 Coverage,
Bangladesh Jamaat-e-Islami Political Statements, India Border Security & South Asia
Regional Tensions, India Defence Capability & Acquisitions, and — the corpus's core
strategic-analysis cluster — **China-India-Neighbourhood Strategic Geopolitics (BRI /
String of Pearls)** (g025). Full list with descriptions: `topic_dim_v3` in
`data/geo_ontology.duckdb` / `delivery.geo_ontology.topic_dim_v3`.

### 7.3 A structural finding, not just a bigger number

K=33 revealed real internal structure that v2's coarser 11 clusters hid. What was a
single "Middle-East" mega-topic in v2 split into **six** genuinely distinct
sub-narratives here: real-time Gulf strike alerts (g005), a US-strikes-on-Iran tactical
ticker (g006), the Strait of Hormuz/energy-security angle (g032), Israel-Iran-US
intelligence commentary (g020), opinion/diplomacy discussion (g015), and the raw
strike/drone-downing reports (g003) — each with a different character and, presumably,
different downstream risk/theme signal. Russia-Ukraine similarly split into four
(military operations, Kremlin rhetoric, Russian MOD daily bulletins, and the Ukrainian
real-time tactical alert feed). **This is more useful for analyst triage than a single
coarse label**, at the cost of the same honest caveat as v2: not every cluster the
algorithm produces is a clean topic, and this document says exactly which ones aren't.

`topic_dim_v2`/`post_topic_v2` are retained for comparison, not deleted.

---

## 8. Small recommendation system (2026-07-30, latest)

Two small, deliberately transparent pieces built on top of the click-to-inspect layer
(`UI/api_server.py`, `UI/assets/dash.js`'s `DRILL` module) — no opaque ML, every number
shown is explainable from the query or formula behind it.

### 8.1 Related items (per drill-down)

Opening any drill panel now also computes a small `related` block, added to the existing
`/api/drill` response (`compute_related()` in `api_server.py`):

- **Co-occurring entities** — other entities mentioned in the same posts, ranked by
  co-mention count *normalized by the candidate's own overall frequency*
  (`co_count / entity_dim.distinct_posts`), not raw counts. Raw counts would just surface
  the globally most-frequent entities (e.g. "India") on every card regardless of what's
  actually being viewed — this is a cheap lift-style correction, using a column
  (`distinct_posts`) already present in `entity_dim`, no extra query.
- **Co-occurring risk/theme categories** — same idea, normalized by each category's own
  total post count across `risk_labels`/`theme_labels`.
- **Related topics** — via `topic_dim_v3` only (the 33-cluster geo ontology, §7), not the
  older `topic_dim` (metrics v1, `t0xx`). Deliberate simplification: one topic ontology
  for this feature, the more refined one. **Known limitation, not a bug**: because
  `post_topic_v3` is a hard, mutually-exclusive assignment (one cluster per post), a
  topic_v3 view can never show "related topics" for *itself* — every post in scope
  already belongs to exactly that one cluster by construction, so the co-occurrence join
  is structurally empty. Related topics still work normally for every other input view
  (entity, risk, theme, tag, ...), since those tables are multi-label per post.
- **Similar posts** — content-based, via the bge-m3 embeddings already in
  `analytics.duckdb.post_embeddings`. Uses DuckDB's built-in `list_cosine_similarity`
  against a **single seed post** (the top row of the main result), not a computed
  centroid of the whole selection — deliberately simpler, avoids needing a UDF or
  elementwise-average aggregation in SQL, and keeps the server fully stateless (no
  embeddings cache, no new dependency; a quick manual check found similarity scores in
  the 0.65–0.81 range for genuinely related posts, never near 1.0 unless the seed post
  leaked back into its own neighbor list, which it does not — excluded explicitly).

Every related-item list explicitly excludes the dimension currently being viewed (e.g.
browsing risk category X never recommends X back as "related"). If `compute_related()`
fails for any reason, the main `rows` response is still returned — `related` is simply
omitted, never blocks the underlying posts from showing.

### 8.2 "Recommended for review" feed (Basic view)

`P.basic_alerts_list` in `dash.js` (mounted in the same Basic-view slot previously titled
"Recent alerts") now re-ranks the two alert tables already loaded into the dashboard
(`geo_alert_candidates`, `alert_candidates`) into one merged, deduplicated, explainable
feed — no new queries, since both tables are already client-side.

```
score = severity_weight × (0.5 + 0.5 × (1 − q_value)) × recency_weight
severity_weight = critical:3, high:2, medium:1
recency_weight  = 1 / (1 + days_since_max_day_in_either_table)
```

Sorted descending, deduplicated by (grain, dimension key) keeping the best-scoring row,
top 8 shown, each with a plain-language line ("High severity · unusual activity · 2 days
ago") instead of a bare severity chip. Clicking any item opens the same drill panel used
everywhere else in the dashboard — which now also shows §8.1's related items, tying the
two pieces together as one system built from shared signals.

### 8.3 Verification

- Curl-tested `related` blocks for entity/risk/theme/topic_v3/tag/alert views: results
  are plausible (e.g. entity=China's related entities included India Border Region,
  Depsang Plains, Zojila Tunnel; related topics included the China-India strategic
  geopolitics cluster) and the current dimension is never suggested back as its own
  related item (confirmed for both entity and risk/theme self-exclusion).
- jsdom smoke test: 0 runtime errors; the Recommended-for-review feed renders top items
  from real data (top row matched a known extremely-unusual entity alert from the live
  extract); opening a drill shows the Related section; clicking a related chip opens a
  fresh drill (title and fetch URL both change) without breaking the open/close/cleanup
  behavior verified for the base click-to-inspect layer.
- Per-request latency for a drill + full related computation (~5-6 small queries) is
  under 1 second on this corpus size — acceptable for an interactive click, though
  noticeably more than a bare drill query; not optimized further given the "small"
  scope, and no caching was added (would reintroduce the memory-growth concern this
  whole layer was built to avoid).

---

## 9. CAMEO/GDELT two-axis event coding (2026-08-06)

Implements the **Global Geopolitical Event & Topic Coding Framework v1.0**
(actor-agnostic, CAMEO/GDELT-compatible). Phases 1 and 2 of 7 are complete.
`risk_labels` / `theme_labels` are **untouched** — the migration is additive so
old-vs-new stays measurable and nothing downstream breaks mid-flight.

### 9.1 Why the axis split is a modelling fix, not a relabelling

The 9 legacy "risk" categories conflate *subject matter* with *action type*:
`sanctions` = subject 02.4 + action 16; `border_conflicts` = subject 07.4 +
action 18/19; `international_pressure` is almost purely action 13. Because two
independent questions are crammed into one label, a post can satisfy several at
once — the audit measured **192 posts carrying ≥6 of the 9 risk categories**.
Separating the axes makes each separately answerable, and turns "6 of 9" from
expected behaviour into a legible defect.

### 9.2 Taxonomy is data, and the UI can no longer silently drop a category

`scripts/lib/taxonomy_cameo.py` → `data/taxonomy_cameo.json` (59 classes:
10 Section A parents, 41 sub-codes, 8 Section B actions; codes 11–12 reserved
so 13 remains the start of the escalation ladder).

`scripts/lib/taxonomy.py` → `data/taxonomy.json` composes those with the legacy
risk/theme/entity-subtype axes into **one contract**, which
`UI/build_dashboard.py` ships inside `metrics.json`.

This replaced four object literals in `dash.js` (`RISK_META`, `RISK_ORDER`,
`THEME_META`, `THEME_ORDER`) that had quietly become the effective UI schema.
Both of their failure modes were **silent**:

* `ORDER.filter(c => present[c])` **dropped** any category present in the data
  but absent from the array — one fewer row, no error anywhere.
* `META[k][0] || k` rendered an undeclared key as a raw snake_case string that
  still looked like a label.

`TAX.order()` now applies the declared order first and **appends** anything
undeclared, warns once, and records it in `TAX.unknown()`.
`UI/tests/check_taxonomy_drop.mjs` injects an undeclared category and asserts it
is *rendered and warned about* rather than dropped (6/6 pass).
`scripts/lib/taxonomy.py` also parses `12_risk_theme_classification.py` with
`ast` and **fails the build** if the classifier's category set and the UI's
disagree in either direction. All 8 injected defects are caught.

### 9.3 The decisive measurement: NLI is the *weaker* signal on this corpus

`data/calibration_cameo.csv` — **872 hand-judged pairs**, stratified by
class × language × bi-encoder score band, 38 languages (including 38 Bengali and
35 Ukrainian, both absent from XNLI *and* from the previous calibration sample),
with Horvitz–Thompson inclusion weights carried in the file.

Sampling-weighted per-class AUC:

| class | NLI | bi-encoder |
|---|---:|---:|
| a01 political/diplomatic | 0.551 | **0.754** |
| a02 economic relations | 0.725 | **0.884** |
| a03 infrastructure | 0.870 | **0.988** |
| a04 energy security | 0.532 | **0.740** |
| a05 maritime | 0.759 | **0.839** |
| a06 security/military | 0.416 | **0.590** |
| a07 border issues | 0.485 | **0.981** |
| a08 internal conflict | 0.711 | **0.730** |
| a09 technology/digital | 0.500 | **0.866** |
| a10 humanitarian | 0.983 | **0.997** |
| **mean** | **0.653** | **0.837** |

**The bi-encoder ranks better on 10 of 10 classes** (sign test p ≈ 0.001), and
NLI is at or below chance on four — a06 at 0.416 is *below* chance, i.e. a
higher NLI score there predicts the class is **absent**.

This contradicts the predecessor's central architectural assumption, which
treats NLI as the decisive precision gate and discards the cosine. The likely
cause is the corpus: a large share of premises are 5–10-word multilingual alert
fragments, and mDeBERTa-v3-mnli-xnli degenerates toward entailment on them —
one Ukrainian air-raid line scored 0.95 for "maritime", 0.73 for "technology"
and 0.70 for "internal conflict". bge-m3 is built for exactly this
multilingual-retrieval setting and does not.

**Consequence:** NLI is no longer a veto. The decision score is
`0.8 × rescaled_cosine + 0.2 × P(entail)`. The weight was chosen on the
calibration set by mean per-class F1 (0.0→0.497, 0.1→0.532, **0.2→0.538**,
0.3→0.496, 0.5→0.473) and is a **single global constant on purpose** — with
4–17 judged positives per class, a per-class weight would be fitting noise.
That is still one tuned degree of freedom on this sample, and it is stated
rather than hidden.

*This finding applies to the existing `risk_labels`/`theme_labels` too — they
were produced by exactly the NLI gate shown here to be near-chance on several
classes. The previously reported 72%/46% remediation figures were never measured
this way and should not be relied on.*

### 9.4 Per-class thresholds — and three classes that refuse to be calibrated

`data/thresholds_cameo.json`. F1-optimal per class on the **weighted** sample,
ties broken toward the **stricter** cut (the documented defect was over-tagging).
Wilson intervals are computed on raw judged counts, because weights change which
population a rate refers to but cannot manufacture sample size.

| class | status | t | F1 | P [95% CI] | pop. rate |
|---|---|---:|---:|---|---:|
| a03 infrastructure | calibrated | 0.430 | 0.93 | 0.91 [0.52–0.95] | 2.3% |
| a02 economic | calibrated | 0.291 | 0.67 | 0.68 [0.33–0.79] | 11.1% |
| a05 maritime | calibrated | 0.219 | 0.63 | 0.52 [0.30–0.70] | 16.3% |
| a01 political | calibrated | 0.164 | 0.51 | 0.37 [0.23–0.55] | 19.3% |
| a08 internal conflict | calibrated | 0.376 | 0.44 | 0.67 [0.30–0.90] | 9.9% |
| a06 security/military | calibrated | 0.235 | 0.34 | 0.26 [0.20–0.48] | 11.1% |
| a04 energy | calibrated | 0.345 | 0.30 | 0.67 [0.35–0.88] | 6.7% |
| a07 border issues | **uncalibrated** | — | — | 6 judged positives | 2.5% |
| a09 technology | **uncalibrated** | — | — | 7 judged positives | 5.4% |
| a10 humanitarian | **uncalibrated** | — | — | 4 judged positives | 0.16% |

Fitted thresholds span **0.164 to 0.430** — a 2.6× range. A single global cut
across all classes, as the predecessor used, was never defensible.

Classes below 8 judged positives are written as `uncalibrated`, fall back to the
global constant, and **every row they produce carries
`threshold_source='global_fallback'` in the database**, so nothing downstream can
mistake them for calibrated. A targeted second sampling round (band allocation
1:2:3:4, inclusion weights preserved so the estimator stays population-correct)
rescued a03 and a06 but not these three.

**Evidence-based taxonomy change:** a10 humanitarian measures a **0.16%**
population rate — ~34 posts across the 46-day window, ~0.7/day — against a
support gate needing ≥0.5/day. It is now flagged `alertable=False` with that
measurement as the stored reason, joining codes 16 and 20. It is still *coded*;
it is not *trend-alerted*. Fixing this exposed a real bug: `build()` hardcoded
`alertable: True` for Section A, so an authored `False` was validated and then
silently discarded on the way out.

### 9.5 Honest limits

1. **The judgements are model-adjudicated, not analyst-verified.** Every row
   records `judged_by = "claude-opus-5 (model-adjudicated; NOT analyst-verified)"`.
   A stronger model adjudicating a weaker classifier is standard practice, but
   both are language models and may share blind spots. Analyst review of
   `data/calibration_cameo.csv` is the single highest-value next step — the file
   is in-repo precisely so it can be re-opened and re-judged.
2. **Absolute quality is modest.** Mean per-class F1 ≈ 0.54; precision 0.26–0.91.
   Against population base rates of 0.2–19%, that is a real lift, but it is not
   a high-precision classifier and should not be described as one.
3. **The blend weight (0.2) was selected on the same sample it is evaluated on.**
   One degree of freedom, not cross-validated.
4. **a07's hypothesis may be too narrow.** "…describes a land border between
   countries" excludes maritime delimitation by design (that is code 05), but the
   highest-scoring a07 candidate in round 1 was a Japan–Philippines EEZ dispute —
   a false positive at the top of the ranking, worth re-examining.
5. Phases 3–7 (sub-code cascade, action axis, dyads, metrics/FDR, UI, ontology)
   are **not** done. `--level all` currently warns and writes level-1 only.

### 9.6 Extension points (where to edit when the taxonomy grows)

Adding a dimension used to mean editing five places, four of which failed
*silently* if you missed them. It is now three registries, each of which fails
loudly or is covered by a test:

| registry | file | governs |
|---|---|---|
| `ALERT_ROUTES` | `UI/api_server.py` | which drill view serves each `dimension_type`, and which query param carries the key |
| `CAMEO_RELATED_AXES` | `UI/api_server.py` | which `event_codes` (axis, level) each dimension covers in the related list |
| `DIM_AXIS` | `UI/assets/dash.js` | which taxonomy axis names each `dimension_type`, and its drill param |

The single source of truth for the classes themselves remains
`scripts/lib/taxonomy.py` → `data/taxonomy.json` → `metrics.json`; the registries
only say *how a dimension is routed and named*, never *what the classes are*.

Three properties are now asserted rather than assumed, because each previously
failed without any error:

1. **Undeclared keys are rendered and reported, never dropped.** The old
   `RISK_ORDER.filter(...)` silently discarded any category `dash.js` had not
   been updated for. (`check_taxonomy_drop.mjs`)
2. **Each dimension resolves through its own axis.** The three label resolvers
   were binary ternaries; a CAMEO row fell through to `themeName()` — the wrong
   axis — or rendered a bare `05.2`. (`check_dim_labels.mjs`)
3. **Self-exclusion covers the ancestor/descendant chain.** A parent code
   co-occurs with its own child in 100% of the child's posts, i.e. maximal lift
   *by construction*. With exact-key matching the parent would top the related
   list for every one of its sub-codes, in every slot — a tautology presented as
   an association. Siblings are deliberately kept (they are real associations),
   and lexical prefix twins (`05.2` vs `052`) must not match.
   (`check_related_hierarchy.py`)

A declared-but-unbuilt axis answers **503 `not_yet_populated`**, not 400 — the
taxonomy declares `theme_code` / `theme_subcode` / `action` before the coding
pass writes them, and a 400 would make a correctly-declared axis
indistinguishable from a malformed request.

**Gap:** the plan lists `check_drill.mjs`, `check_related.mjs` and
`check_filter2.mjs` as verification gates. They are not in the repo — they
existed only in a scratchpad, so they cannot be re-run. Drill and related are
re-covered by the harnesses above; **the filter path is not covered by anything.**

### 9.7 Parent-code run: measured results

`scripts/14_cameo_event_coding.py --level parent`, 2026-08-06. 21,228 posts with
embeddings → 146,185 candidate pairs after the 0.35 bi-encoder floor → **110.7
min** of NLI on 6 workers × 8 threads → **14,526 rows over 7,525 distinct posts
(1.93 codes/post)**.

| code | class | n | threshold | source |
|---|---|---:|---:|---|
| 01 | Political & Diplomatic Relations | 5,561 | 0.164 | fitted |
| 02 | Economic Relations | 924 | 0.291 | fitted |
| 03 | Infrastructure & Connectivity | 141 | 0.430 | fitted |
| 04 | Energy Security | 318 | 0.345 | fitted |
| 05 | Maritime | 3,292 | 0.219 | fitted |
| 06 | Security & Military Relations | 3,158 | 0.235 | fitted |
| 07 | Border Issues | 259 | 0.420 | *global fallback* |
| 08 | Internal Conflict, External Role | 410 | 0.376 | fitted |
| 09 | Technology & Digital | 448 | 0.420 | *global fallback* |
| 10 | Humanitarian | 15 | 0.420 | *global fallback* |

Verified by `scripts/tests/verify_event_codes.py` (7/7): every declared class
accepted at least one post, no code was written that the taxonomy does not
declare, every row carries a threshold source, no row scored below the threshold
it was judged against, and each class used the threshold its calibration status
implies (7 fitted / 3 fallback).

**Over-tagging.** The audit's motivating defect was 192 posts carrying ≥6 of 9
risk categories. On the new parent axis: **146 of 7,525 posts carry ≥6 of 10**
(1.9%). This is a *different denominator* on a *different axis* and is reported,
not claimed as an improvement — the honest comparison needs the same posts scored
both ways, which is task #8 and has not been done.

**a10 humanitarian produced 15 rows**, consistent with the 0.16%/day population
rate that justified flagging it non-alertable. It is coded for the record.

#### Keys: `code_key` is canonical

`event_codes` carries both `code` (`'05'`) and `code_key` (`'a05_maritime'`).
**`code_key` is the canonical identifier for display and routing** — bare numeric
codes repeat across axes and levels, which is exactly the collision the code_key
scheme exists to prevent, and the taxonomy axes are keyed by it. An API emitting
`'05'` would make every UI label fall back to the raw string.

Hierarchy, however, lives in the numeric code (`05` → `05.2`); `code_key` does
**not** nest lexically (`a05_maritime` is not a prefix of
`a05_2_maritime_disputes`). So `_resolve_code()` carries both, accepts either
form on input, and subtree matching uses the numeric while labelling uses the
key. A parent drill on `a05_maritime` and on `05` both return 3,292 posts.

#### Operational note

The run holds the `geo_ontology.duckdb` write lock only at the very end, and
`open_writer_with_retry` allows **6 attempts × 20 s = 2 minutes**. A dev
`api_server.py` left running holds that lock *indefinitely* (DuckDB permits one
writer or many readers), so a 110-minute run would burn its full compute and then
fail to write. Stop the API server before any pipeline write, or raise the retry
budget. NLI scores are checkpointed per chunk, so such a failure is recoverable,
but it is silent until the very end.

### 9.8 Phase 3: the cascade (sub-codes + action axis)

`--level all --reuse-parents`, 2026-08-06. **95 min** total.

| axis | level | rows | codes | posts |
|---|---|---:|---:|---:|
| theme | 1 | 14,526 | 10 | 7,525 |
| theme | 2 | 5,444 | 41 | 2,525 |
| action | — | 1,982 | 8 | 1,241 |
| **total** | | **21,952** | **59** | **7,525** (2.92 codes/post) |

**How the cascade stays affordable.** The grid is pruned *before* the bi-encoder
floor: a sub-code is scored only against posts whose parent was accepted, and an
action only against posts carrying ≥1 thematic code. Stage 2 was 60,499 pairs
(9.0% accepted), stage 3 was 54,744 (3.6% accepted) — together less than one
flat pass over 41 codes would have cost. The pruning is not only cheaper but
more correct: a maritime sub-code has no business being evaluated on a
non-maritime post, and Section B is conditional on Section A by design.

The map **fails closed**: a code missing from it is scored against *nothing*, so
a taxonomy entry the cascade forgot to route produces zero rows rather than a
silent full-corpus scan. `scripts/tests/test_cascade.py` (12) covers both
directions, since pruning too little and pruning too much are both silent.

`--reuse-parents` routes the cascade off level-1 rows already in the table
instead of re-running stage 1. Stage 1 is deterministic given the same posts and
taxonomy, so this is equivalent, and it saves ~110 min that would otherwise be
spent rediscovering verified rows — the checkpoints that would have made a
re-run cheap are cleared on completion by design.

**Verified** by `scripts/tests/verify_event_codes.py` (12/12): no orphaned
sub-code rows, every sub-code declares a parent, a parent drill returns a
superset of its children's posts, no sub-code outside the taxonomy, and no
action code on a post with no thematic code.

#### A schema defect worth recording

`parent_code` was stored **INTEGER 1–10** while `code` was **VARCHAR '01'–'10'**,
so `parent_code = code` never matched as strings. Nothing raised.

The cause was schema *inference*. The parent-only run wrote a frame whose
`parent_code` was entirely `NULL` (level 1 has no parent); DuckDB typed that
column INTEGER; the first cascade insert then coerced `'05'` → `5`, discarding
the zero-pad that every join and `LIKE`-prefix in the API depends on. The
verification check initially *passed* only because DuckDB casts `'01'` → `1` when
comparing VARCHAR to INTEGER — an accidental pass, which is worse than a failure.

Fixed by declaring the table in explicit DDL rather than inferring it from
whatever the first run happened to contain, and repairing in place
(`lpad(parent_code::VARCHAR, 2, '0')`): 21,952 rows preserved, all 5,444
sub-code rows now joining strictly on strings. No re-scoring was needed — only
the column type was wrong.

**It happened a second time (2026-08-07), which is why it is now a test.**
`15_topic_clustering_v4.py` published via `CREATE OR REPLACE TABLE … AS SELECT *`,
and `topic_parent_v4.parent_topic_id` — `NULL` on every row, because a parent has
no parent — came out **INTEGER** while the identically-named column is VARCHAR in
`topic_dim_v4` and `post_topic_v4`. Nothing was corrupted and the served UI was
unaffected (the API reads only `post_topic_v4.parent_topic_id`), but

```sql
topic_dim_v4 d JOIN topic_parent_v4 p USING (parent_topic_id)
```

resolved to a common type, matched nothing, and returned **0 rows silently**.
Both instances failed the same way: a plausible wrong answer, not an error.

Two changes, because fixing the instance does not fix the class:

1. `15_topic_clustering_v4.py` now projects through `_projection()`, casting
   identifier columns by name (`ID_TYPES`) whether or not they are populated.
   Repaired in place with `ALTER … TYPE VARCHAR`; 24 rows preserved.
2. `scripts/tests/test_id_column_types.py` (14/14) asserts the invariant across
   all five published tables, including the cross-table one that catches this
   exact shape: *one column name must not have two types*. It takes an optional
   DB path so it can be run against a deliberately-corrupted copy — confirmed to
   exit 1 with 2 failures, since a check that only ever passes proves nothing.

The rule, stated once: **an id is a name, not a number.** Nothing is ordered by
it or has arithmetic done on it, so it is VARCHAR even when the values look
numeric — and especially when there are no values to look at.

#### Calibration coverage — the limit on all of this

**52 of the 59 coded classes are UNCALIBRATED**: all 41 sub-codes, all 8 actions,
and parents a07 / a09 / a10. They run on the global fallback of **0.42**, which
was itself selected against *parent*-class behaviour; applying it to much
narrower sub-codes is an assumption, not a measurement.

The row counts in the table above are **coverage, not accuracy**. Sub-code and
action precision is unestablished, and `verify_event_codes.py` now emits this as
a standing note rather than leaving it to be inferred. Action codes 16 and 20 are
flagged non-alertable with measured reasons (1.2% and 0.3% English-cue floors,
neither able to clear the 0.5/day support gate).

### 9.9 The FDR family boundary (`fdr_scope` is now load-bearing)

`PARAMS["fdr_scope"]` was declared at `11_geo_timeline_metrics.py:62` and **never
read**. The family boundary was instead *structural*: the Benjamini–Hochberg loop
sat inside `build_dimension_panel`, so "how many families per day" was exactly
"how many times that function happens to be called" — 3 today, and 6 once the
CAMEO axes are wired in, each spending its own α = 0.05. That roughly doubles the
expected false positives while no line of code looks like it changed. A params
entry documenting a choice the code does not make is worse than no entry.

`apply_fdr_family()` now pools every dimension into **one family per day**, and
`fdr_scope` selects it (`"day_global"` shipped; `"day"` = legacy per-dimension).

**Measured, 32 families, 73.2 tests/family across 3 dimensions:**

| dimension | 3 families/day | 1 family/day |
|---|---:|---:|
| entity | 260 | 266 |
| risk | 95 | **58** |
| theme | 31 | 34 |
| **total** | **386** | **358** |

Risk alerts fell from 36% of support_ok cells to 22% (58/261), which is the
direction the D12 over-alerting finding called for.

#### A claim I got wrong, and the test that now prevents it

I first wrote into that function's docstring that pooling is *strictly stricter* —
"more tests in a family can only raise q, never lower it, so no alert appears
that would not have appeared before." **That is false**, and the run disproved it
immediately: entity rose 260 → 266 and theme 31 → 34.

BH's q depends on a test's **rank** within its family as well as the family size
(`q = p · n / rank`). Pooling a marginal test in with strongly-significant ones
raises its rank faster than it raises `n`, which *lowers* q.
`scripts/tests/test_fdr_family.py` constructs both directions explicitly —
q 0.08 → 0.05 (lowered) and 0.04 → 0.20 (raised) — so the claim cannot quietly
revert.

The justification for pooling is therefore **not** that it is conservative. It is
that one day's decisions are one family of decisions: it is the correct family
definition, and the net effect here happens to be fewer alerts (386 → 358).

`stats_v2.run_self_checks()` remains 52/52.
