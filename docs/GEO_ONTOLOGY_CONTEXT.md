# Geo-Ontology Build — Shared Reference

Read this once per agent instead of receiving it inline. Only task-specific instructions
are in your prompt; everything else needed is here.

## Environment
- Root: `/home/hariramanan/NTRO/EDA`. Venv: `source .venv/bin/activate`.
- Source DB (read-only, always `read_only=True`, never write): `data/analytics.duckdb`.
  Key tables: `post_facts` (23,356 rows: post_uid, platform, text, lang, posted_at,
  keyword, hashtags, likes/comments/shares/views, has_engagement), `post_embeddings`
  (bge-m3 1024-d, ~21K rows, join on post_uid), `post_scores` (tone/hate/readability),
  `post_topic`/`topic_dim` (**currently broken**, see Known Bug), `collection_effort`.
- Production Postgres (`shaktidb_social`) is read-only and out of scope — everything
  needed is already in `analytics.duckdb`.
- Output DB for this effort: `data/geo_ontology.duckdb`. **DuckDB single-writer**: only
  one process may hold it `read_only=False` at a time. Each Implement stage is the sole
  writer for its turn; Verify stages open it `read_only=True` only, and only after the
  relevant Implement stage has finished (never concurrent with a writer).
- GPU: reserved by vLLM (44/49GB), usually ~0% utilization (idle but memory-held). Don't
  assume large free GPU memory; check `nvidia-smi` first, always have a CPU fallback.
- For any transformer model (NER, zero-shot, embeddings), don't pip install torch fresh —
  reuse the working lab-env install:
  `export PYTHONPATH="/home/hariramanan/NTRO/Entity_extraction/.venv/lib/python3.12/site-packages:$PYTHONPATH"`
  (torch 2.13, transformers 4.57, sentence_transformers 3.3 — this exact trick scored
  23K posts with multilingual sentiment/toxicity in ~45 min on CPU earlier this session).
- Embedding model already used for `post_embeddings`: **BAAI/bge-m3, 1024-d**. Any new
  embedding you compute for comparison (e.g. zero-shot category descriptions) must use
  this same model via the same PYTHONPATH trick.
- Corpus: India-perspective OSINT feed on China's regional influence / Belt-and-Road
  activity across the Indo-Pacific/South Asia. Telegram channels: rtnews, rybar,
  war_monitor, xhnews, BBC_Chinese_news. Keywords: drdo, string of pearls, chittagong,
  djibouti, bangladesh, myanmar, pak, drone attack india, military movement india.
  Existing domain hints in `scripts/03_ontology.py` `KEYWORD_DOMAIN_HINTS`: Conflict/
  Middle-East, Conflict/Russia-Ukraine, India/Defence, Security/Terrorism, Cyber/Threats,
  Civil/Disaster, Civil/Politics, Markets/Finance, Tech/AI, Geopolitics/China,
  Geopolitics/Neighbourhood. Multilingual: Telegram ~62% English, then Russian, Chinese,
  Bengali, Ukrainian, Hindi, ~11% unknown.

## Known bug: topic clustering coarsening
`scripts/03_ontology.py` sets `min_cluster_size = max(25, len(posts_e)//80)`, which
scales with corpus size N. At N=23,356 this gave only 3 clusters, one holding 85%+ of
posts (t000=635, t001=562, t002=19,923, unassigned=2,236). At smaller N≈16.6K it gave 8
clusters. Decouple cluster count from N — do not just retune the same formula.

## Taxonomy

**Entity types** (multi-type NER + gazetteer + zero-shot hybrid):
- PERSON subtypes: `political_leader`, `military_leader`, `diplomat`, `business_executive`
- ORG subtypes: `ministry`, `state_owned_enterprise`, `armed_group`, `international_org`, `business_org`
- LOCATION subtypes: `port`, `border_town`, `economic_zone`, `pipeline`, `railway`

Every mention: `entity_uid` (canonical name where resolvable), `surface_form`,
`entity_type`, `entity_subtype`, `subtype_confidence` (0-1), `subtype_source`
(`gazetteer`|`zero_shot`|`unresolved`), `post_uid`. Unresolved entities are **kept**,
never dropped.

**Risk categories** (9, multi-label per post, each with confidence):
`domestic_instability`, `policy_shifts`, `project_delays`, `sanctions`,
`border_conflicts`, `insurgencies`, `shipping_disruptions`, `pipeline_vulnerabilities`,
`international_pressure`

**Thematic dimensions** (12, multi-label per post, each with confidence):
`political_relations`, `economic_relations`, `bri`, `energy_security`,
`maritime_security`, `maritime_dimensions`, `security_military_relations`,
`border_issues`, `internal_conflict_china_role`, `tech_digital_influence`,
`humanitarian_dimension`, `international_relations`

"Timeline Analysis" is a capability (apply time-series metrics to these dimensions),
not a text label.

## Timeline metrics — reuse, don't reinvent
`scripts/lib/stats_v2.py` already has NB-surprise baselines, Benjamini-Hochberg FDR,
AR(1) persistence/half-life, Savitzky-Golay acceleration, empirical-Bayes shrinkage, and
a 46-check self-test (`run_self_checks()`). Use these functions for entity/risk/theme
timeline metrics — do not reimplement the math. Same support-gating philosophy as
Metrics v2: `NULL` + `support_ok=false` beats a confident number from a sparse cell.

## Alerts / notifications
No delivery channel (Slack/email/webhook) was specified. Build the **substrate**:
`geo_alert_candidates` (FDR q<0.05, same philosophy as the existing `alert_candidates`
in `data/metrics.duckdb`) plus a clean JSON export at `UI/data/geo_alerts.json`. A
reference poller script demonstrates consumption — wiring to a real channel is a
follow-up once a destination exists.

## UI
Extend the existing dashboard (`UI/index.html`, `UI/build_dashboard.py`,
`UI/assets/dash.js`, `UI/assets/dash.css`) — same z-theme (black/white/gray/gold), same
conventions (plain-English one-liners per section, table-view fallback, light+dark).
Do not redesign from scratch.
