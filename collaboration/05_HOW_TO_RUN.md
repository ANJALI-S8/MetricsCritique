# How to run

## Environment

```bash
cd /home/hariramanan/NTRO/EDA
python3 -m venv .venv          # if needed
source .venv/bin/activate
pip install -r requirements.txt
```

Credentials (2026-07-29: now supplied via `.env` at the repo root, loaded automatically
by `lib.config` — see `.env.example`). Only override in the shell if you need to point
at a different DB than `.env`:

```bash
export SHAKTI_PG_HOST=<PG_HOST>
export SHAKTI_PG_PORT=13366
export SHAKTI_PG_USER=<user>
export SHAKTI_PG_PASSWORD=***          # never commit the real value; see .env
export SHAKTI_PG_DB=shaktidb_social
```

### Torch / transformers (Layers 2–3)

```bash
export EE_SITE=/home/hariramanan/NTRO/Entity_extraction/.venv/lib/python3.12/site-packages
export PYTHONPATH="$EE_SITE:$PYTHONPATH"
# If GPU is full (e.g. vLLM): export CUDA_VISIBLE_DEVICES=
```

---

## Foundation pipeline (posts → scores → topics)

```bash
source .venv/bin/activate
export EE_SITE=/home/hariramanan/NTRO/Entity_extraction/.venv/lib/python3.12/site-packages
export PYTHONPATH="$EE_SITE:$PYTHONPATH"

python scripts/01_extract_base.py      # Postgres READ-ONLY → analytics.duckdb
python scripts/02_eda_diagnostics.py
python scripts/03_ontology.py          # GPU preferred for YT embeddings
python scripts/04_nlp_scores.py
# Legacy v1 (optional forensics only):
# python scripts/05_metrics.py && python scripts/06_validate.py
```

---

## Metrics v2 (authoritative signals)

```bash
python scripts/07_metrics_v2.py        # → data/metrics_stage/
python scripts/08_metrics_db.py        # → data/metrics.duckdb
python scripts/review/stat_review.py --json reports/stat_review_results.json  # optional
```

---

## Dashboard (interactive UI over snapshot)

```bash
python UI/build_dashboard.py           # → UI/data/metrics.json + slim UI/index.html

cd UI
python3 -m http.server 8877
# open http://127.0.0.1:8877/
```

- **Not live DB:** rebuild JSON when metrics change, then refresh browser.  
- **Offline embed:** `python UI/build_dashboard.py --static-embed`  
- Detail: [08_UI_DASHBOARD.md](08_UI_DASHBOARD.md)

---

## Query metrics

```bash
.venv/bin/python - <<'PY'
import duckdb
c = duckdb.connect("data/metrics.duckdb", read_only=True)
print(c.execute("""
  SELECT cast(day AS date) d, topic_id, platform, V,
         round(surprise,3) s, round(surprise_q,4) q, alert_flag
  FROM metrics_daily
  WHERE support_ok AND in_window
  ORDER BY surprise DESC NULLS LAST
  LIMIT 15
""").fetchdf())
print(c.execute("SELECT * FROM alert_candidates ORDER BY surprise_q NULLS LAST LIMIT 10").fetchdf())
PY
```

---

## Notifications (reference poller)

```bash
.venv/bin/python scripts/notify_poller.py --dry-run    # preview only, no state change
.venv/bin/python scripts/notify_poller.py               # normal run: logs new alerts, advances state
```

- **Reference only:** no Slack/email/webhook is wired up — `send_notification()` appends to
  `reports/notify_poller.log` and prints; nothing leaves the machine. See the script's docstring
  and `docs/NOTIFICATION_POLLER_AUDIT.md` before wiring a real channel.
- **Prerequisite:** `UI/data/geo_alerts.json`, written by `scripts/11_geo_timeline_metrics.py` —
  run that (part of the geo-ontology layer, not the metrics-v2 chain above) before the poller if
  it hasn't run since the last extraction.
- Dedup is by `(day, dimension_type, dimension_key, metric)` in
  `reports/notify_poller_state.json`, so re-running after a rebuild only surfaces genuinely new
  alerts — safe to run as often as you like.
- Not on a schedule today — run manually, or add to a cron block once a real channel exists.

---

## Daily snapshot accrual (recommended)

```cron
15 2 * * * cd /home/hariramanan/NTRO/EDA && .venv/bin/python scripts/01_extract_base.py >> logs/extract.log 2>&1
```

Then periodically: `07` → `08` → `UI/build_dashboard.py`.

---

## Read-only guarantee

- Postgres: `readonly=True`, REPEATABLE READ, always ROLLBACK.  
- Metrics and UI write only under `data/` and `UI/`.  
