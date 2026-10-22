# News Trending & Emerging Events engine

Adapts the repository's statistical machinery (`scripts/lib/stats_v2.py`) from social posts to **news articles**:
group articles that report the same event, compute trending metrics inside a selected **Area of Interest (AOI)**, and
raise FDR-controlled alerts. The social-media pipeline (scripts `01`–`15`, `UI/index.html`, `UI/api_server.py`) is
**untouched** and still works; everything here is additive (`news_*` modules, stages `20`–`23`, `UI/news.*`).

Status: **pilot validated on 14 days (2026-10-06 → 2026-10-19), India + Bangladesh. No historical backfill has been run.**
Read `reports/news/pilot_report.md` (generated, git-ignored) before approving one.

## Safety invariants

1. PostgreSQL and OpenSearch are **read-only**, enforced in three layers: SELECT-only client guard, a read-only session
   with `default_transaction_read_only=on` (the server rejected a deliberate `CREATE TEMP TABLE`), and an OpenSearch
   whitelist that only allows `_search`/`_count`/`_mapping` (no scroll/PIT/bulk/delete). Tests: `scripts/tests/test_news_sources.py`.
2. Derived data lives only in `data/news_*.duckdb` (git-ignored). Source tables are never modified.
3. Credentials come from the environment only (`scripts/lib/news_config.py`); nothing is hardcoded and nothing is defaulted.
4. Publication dates: `published_at` only. Rows without a usable date are **excluded and counted**; `scraped_at` is never substituted.
5. Missing data is `NULL`, never `0`. Days with too little exposure are data gaps, not zero volume.
6. The dashboard server binds to `127.0.0.1`, serves a static allowlist and `/api/news/*`, and opens the database read-only.

## Data model (what was verified, not assumed)

| Need | Source | Notes |
|---|---|---|
| Article identity | OpenSearch `_id = "{source_table}-{article_id}"` | 300/300 sample matched `tmc.article_label`; `_mget` fails on the 2-index alias → `ids` query |
| Publication date | `article_label.published_at` == OpenSearch `published_at` | 0 h difference; provenance/confidence/conflict flags in OpenSearch; conflicts excluded |
| Taxonomy | `tmc.article_label` + `tmc.taxonomy_node` (v4.1) | subtopic confidence is low (median 0.38) → taxonomy is a *label*, not a clustering partition. Domain `D00` (non-relevant) is excluded from topic series and alerts |
| Embeddings | OpenSearch `embedding` (bge-m3, 1024-d, unit norm) | reused unchanged |
| Event location | OpenSearch `gdelt.action_*` (GDELT ActionGeo) | ~35–40 % of articles; FIPS country codes → ISO; precision country/adm1/city |
| Mentioned-in | OpenSearch `gkg.locations_country_code` | country level only; separate AOI mode |
| Publisher location | `data/publisher_registry.csv` | separate registry; never feeds event location or metrics |
| Boundaries | Natural Earth 1:10m v5.1.2 (public domain), `data/geo/` | offline after `scripts/21_news_geo_data.py --download`; checksums pinned |

## Pipeline (one code path for pilot, backfill and incremental)

```
discover (published-day range | OpenSearch ingested_at watermark)
  → ingest_docs   date validation, label join, content-hash dedupe, geo, per-day atomic commit
  → resolve_pending  attach labels that arrive later (unlabelled articles still form events)
  → cluster       online centroid clustering (cosine ≥ 0.80, 72 h gap), deterministic event ids
  → recompute     AOI node panels, AOI-day coverage, event metrics, emerging tests, developments, alerts
```

* **Clustering**: story-level groups; an *event* needs ≥3 articles from ≥2 publishers. Re-running reproduces identical
  cluster ids (verified: 100 % identical on 56,635 articles across two independent runs).
* **Metrics** (`news_metrics.py`): negative-binomial baseline fitted on the **AOI's own** history, with a smoothed,
  *leave-one-out* exposure offset (a node's spike cannot inflate its own baseline; exposure noise would otherwise inflate
  false positives to 9 %), drift adjustment, support gates, BH-FDR per (AOI, day), causal acceleration, Kleinberg/CUSUM
  bursts, AR(1) persistence, Hurlbert PIE publisher diversity. Definitions ship in `metric_meta`.
* **Emerging events** (`news_events.py`): empirical upper-tail test of distinct publishers at the event's age against the
  same AOI's own recent clusters (stratified by taxonomy domain); NULL below 50 reference clusters; BH-FDR; high-confidence
  location required.
* **New developments**: new articles outside the envelope of the event's earlier coverage (rank test vs leave-one-out
  similarity + effect floor). A first version (label permutation) flagged 56 % of events and was **rejected**.
* **AOIs**: country, admin-1, polygon, box, radius; *event* vs *mentioned-in*. Baselines are computed per AOI. Ad-hoc AOIs
  (drawn in the UI) run the same functions in memory (`persist=False`) and are flagged ad-hoc.

## Run it

```bash
cd /home/sherya/anjali/MetricsCritique
export NEWS_ENV_FILE=/path/to/env-file-with-SOURCE_DATABASE_URL-and-OPENSEARCH_*   # or the NEWS_* variables
.venv/bin/python scripts/21_news_geo_data.py --verify                 # once: boundaries (use --download on a new machine)
.venv/bin/python scripts/22_news_publisher_registry.py --top 600       # optional refresh (read-only)
```

### 1. Pilot (isolated database `data/news_pilot_india_bangladesh.duckdb`)

```bash
.venv/bin/python scripts/20_news_run.py plan  --countries IN,BD --from 2026-10-06 --to 2026-10-19   # read-only sizing
.venv/bin/python scripts/20_news_run.py pilot --from 2026-10-06 --to 2026-10-19                      # ~9 min
.venv/bin/python scripts/23_news_pilot_report.py --validate-geo-live                                 # reports/news/pilot_report.md
.venv/bin/python scripts/20_news_run.py status --db data/news_pilot_india_bangladesh.duckdb
```

Re-running a range skips days already ingested (`--force-days` re-scans them) and is idempotent: no duplicate rows.

### 2. Full historical backfill — **only after you have reviewed the pilot and approved**

`backfill` refuses to run without `--i-approve-backfill`. Before approving, review the pilot report, fill
`analyst_verdict` in `reports/news/*_inspection_sample.csv`, and decide on the open items below.

```bash
.venv/bin/python scripts/20_news_run.py plan --from 2026-08-17 --to 2026-10-19            # all corpus: size it first
.venv/bin/python scripts/20_news_run.py backfill --from 2026-08-17 --to 2026-10-19 \
     --countries IN,BD --max-days 7 --i-approve-backfill                                   # chunked, resumable
```

Repeat the last command until `days_remaining` is 0 (each run skips finished days). The dated, labelled corpus starts
2026-08-17; earlier data is negligible. A run for *all* countries needs an explicit AOI family (today the AOI family is
built from `--countries`); that is a deliberate extension to scope with you, not an accident waiting to happen.

### 3. Ongoing incremental processing

```bash
.venv/bin/python scripts/20_news_run.py incremental --since 2026-10-19T00:00:00Z --db data/news_metrics.duckdb  # first run only
.venv/bin/python scripts/20_news_run.py incremental --db data/news_metrics.duckdb
```

It processes articles whose OpenSearch `ingested_at` is newer than the stored watermark (6 h overlap, idempotent), attaches
late labels, updates only the affected clusters/AOIs and recomputes their metrics and alerts. Alerts revised by late data are
marked `retracted`, never deleted. **This is batch processing on a schedule, not real-time.**

**Scheduling.** No scheduler belonging to this project was found (`auto_scheduler.log` points at a missing script; the
scraper's APScheduler drives scraping only). The command is a one-shot, lock-protected process (a second invocation exits
with code 3 instead of overlapping), so any scheduler can call it. Example (not installed):

```cron
*/30 * * * *  cd /home/sherya/anjali/MetricsCritique && NEWS_ENV_FILE=/path/to/env .venv/bin/python scripts/20_news_run.py incremental --db data/news_metrics.duckdb >> logs/news_incremental.log 2>&1
```

Exit codes: 0 success · 1 failure (logged in `run_log`, resumable) · 2 refused (missing approval) · 3 another run holds the lock.
Failures never leave half-written days: each day commits data and its checkpoint in one transaction.

### 4. Dashboard (localhost only)

```bash
cd /home/sherya/anjali/MetricsCritique
NEWS_UI_DB=data/news_pilot_india_bangladesh.duckdb .venv/bin/python UI/news_server.py 8878
# open http://127.0.0.1:8878/
```

The server also copes with a running pipeline (it reads a point-in-time copy when DuckDB's write lock is held).


## Trending news page (the default UI at `/`)

Simple page, no map: choose a **country**, a **topic** (Domain → Topic → Subtopic), or **both**, and a period (24 h / 3 days / 7 days);
it shows the **top 10 trending stories** ranked strongest first ("Show more" up to 30). Nothing selected → it asks the user to choose
(trending is defined relative to an area/topic). The advanced dashboard (map, alerts, baselines, methodology) is still at `/news.html`.

* **Country only**: stories that belong to that country *on real evidence*, all topics: ≥ 60 % of the story's located articles are in the country,
  ≥ 2 articles are located there, and they are ≥ 25 % of the whole story (`trend_country_*` in `news_config.py`). One stray GDELT hit no longer
  puts a 68-article film story in the UK. **Topic only**: all countries, including stories with no usable location. **Both**: the intersection.
* **Story** = an event cluster; clusters that are the same story split in two (centroid cosine ≥ 0.90 and ≤ 72 h apart) are merged
  (`news_trending.merge_events`). At display time a lower-ranked story ≥ 0.88 similar to one already listed is shown under it as
  "also covered" instead of taking a slot. Exact duplicate articles are counted once.
* **Ranking** (`scripts/lib/news_trending.py`, computed on request, nothing stored): volume, publisher breadth, growth (NULL below
  5 articles across the two windows; a story with no earlier coverage is "new"), recency and statistical evidence, as weighted
  percentile ranks (30/30/20/10/10, same design as `emergence_score`; missing components dropped, weights renormalised).
* **Expected baseline**: how the story's window volume compares with comparable stories (same domain when ≥50 exist) in the same
  geographic scope in earlier, fully observed windows - descriptive, **not** a significance test. A first version reported an FDR q for
  this and gave q = 1 to a 306-article story (candidates and reference come from the same population, so the biggest story is an
  extreme of it by construction); it was rejected.
* **Statistical evidence** comes only from tests with a proper null that the pipeline already ran (emerging event, new development,
  negative-binomial topic surge, all FDR-controlled) and only where they exist (pre-computed country areas). Elsewhere the card says no
  significance claim is made - missing is never shown as "not significant".
* **Taxonomy path** (`lib/news_taxonomy.py`, one rule for the Trending page *and* the dashboard): a level is shown only when **more than half**
  of the story's labelled articles carry the same value there, resolved top-down (domain → topic inside it → category → subtopic). A plurality
  is not enough: a story whose articles are scattered over eight domains is reported as **Mixed topics** (or **Outside the taxonomy** when most
  are `D00`, **Unlabelled** when none is labelled) and is not listed under any single topic; it still appears in a country-only view.
  The path stops at the deepest level that has agreement. Labels are the stored classifier labels, unchanged (`taxonomy_min_agreement`).
  The per-event modal labels stored in `event_cluster` by the pipeline are no longer read by any user-facing view.
* API: `GET /api/news/trending_meta`, `GET /api/news/trending?country=&tax=&window=&limit=`, `GET /api/news/story_articles?ids=&offset=&limit=`.
  Read-only; verified not to change the database file.
* Tests: `scripts/tests/test_news_trending.py` (planted-answer synthetic world), `scripts/tests/test_news_trending_real.py` (every displayed
  number recomputed with SQL on the pilot DB, 3 windows × 5 scope cases), `UI/tests/check_trending_ui.py` (browser).

## Topic filter and source citations (dashboard)

* **Filter**: Domain → Topic → Subtopic dropdowns plus an **All Topics** reset, built from the stored `taxonomy_node` hierarchy
  (in-scope nodes only). Subtopics are grouped under their real parent category. It combines with the area (country / state /
  drawn area / *event* vs *mentioned-in*): every number is recomputed for *area AND topic*.
* **Matching** uses each article's **stored label** only - never headline text. An **event** matches when a **strict majority**
  of its labelled, non-duplicate articles carry the selected node at that level (and its ancestors); the share of its articles that carry
  the node is shown. An event whose articles disagree matches no single topic, only the level they do agree on. Trend rows and node alerts match when the node is the selection or one of its descendants.
* **Subtopic series** are not stored by the pipeline; they are computed on demand, read-only, with the same function
  (`persist=False`), and cached per database version. Their FDR family is the subtopic series of the area.
* **Citations**: every article is shown as *publisher — title ↗ date (UTC)* linked to the original URL (http/https only; a record
  without a usable URL is shown as "no link"). Events list up to 3 sources (and the full list in the drawer), trend rows load the
  source articles of that node, and every alert lists the articles it was computed from (`Sources:` + "all N").
* There is **no date-range control** in this dashboard (it shows the whole processed period).
* Test: `UI/tests/check_news_filter_ui.py` compares every displayed number and citation with independent SQL on the pilot DB.

## Tests

```bash
.venv/bin/python -m pytest scripts/tests/test_news_*.py -q          # no network, no production data
.venv/bin/python UI/tests/check_news_ui.py --url http://127.0.0.1:8878 --shots /tmp/shots          # headless-browser dashboard test (needs playwright + server)
.venv/bin/python UI/tests/check_news_filter_ui.py --url http://127.0.0.1:8878 --shots /tmp/shots   # topic filter + citations vs independent SQL (advanced page)
.venv/bin/python UI/tests/check_trending_ui.py --url http://127.0.0.1:8878 --shots /tmp/shots      # the Top-trending page
```

Legacy tests (`scripts/tests/test_*.py` without `news`) are standalone scripts; run them as before.

## Known limitations (also shown in the dashboard)

* Event location is machine-extracted (GDELT) for ~35–40 % of articles; ~20 % of events with several located articles
  have members in more than one country. Only **high-confidence** locations (≥3 located members, ≥80 % agreeing) can raise
  alerts. No cheap automatic validator exists (mention-support is circular; a headline place-name check was measured and
  rejected). Human review of `location_inspection_sample.csv` is required.
* Bangladesh has very few event-located articles; its AOI is mostly NULL-with-reason.
* Cluster quality is not human-validated; silver GDELT labels are weak (pairwise recall 0.31).
* A 14-day pilot leaves 7 testable days per series (`baseline_min_days=7`); production should use 28-day baselines.
* New-development detection flags a high share of large, evolving events; needs review before alerts are acted on.
* Ad-hoc AOIs get no multiplicity control across the areas a user tries.
* Publisher registry: 53 % of corpus volume classified (reviewed / ccTLD / pattern), none human-verified yet.
