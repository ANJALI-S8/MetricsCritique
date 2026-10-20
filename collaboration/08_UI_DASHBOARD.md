# UI dashboard — dynamic shell, snapshot data

## Design (confirmed with user)

| Question | Answer |
|----------|--------|
| Live queries to DuckDB/Postgres while browsing? | **No** |
| Interactive filters / charts? | **Yes** — that is the “dynamic” part |
| Where do numbers come from? | **`UI/data/metrics.json`** rebuilt by `UI/build_dashboard.py` |
| How to refresh numbers? | Re-run metrics v2 (if needed) → `python UI/build_dashboard.py` → hard-refresh browser |

## Architecture

```
metrics.duckdb
      │  build_dashboard.py (read-only open)
      ▼
UI/data/metrics.json     ← full snapshot (~4 MB)
      │  fetch at page load
      ▼
UI/index.html            ← slim shell (~42 KB)
UI/assets/dash.js        ← filters re-render all panels client-side
UI/assets/dash.css
```

- **Shell** holds section layout, methodology copy, badges.  
- **JSON** holds all fact tables (metrics_daily, alerts, bursts, …).  
- **JS** loads JSON once, then filter changes never re-hit disk/DB.

## How to run

```bash
cd /home/hariramanan/NTRO/EDA
.venv/bin/python UI/build_dashboard.py

cd UI
python3 -m http.server 8877
# open http://127.0.0.1:8877/
```

Serve over **HTTP** (not `file://`) so `fetch('data/metrics.json')` works.

Optional offline single file:

```bash
python UI/build_dashboard.py --static-embed
# open index.html via file://  (data embedded; larger file)
```

## Status badge meanings

| Badge text | Meaning |
|------------|---------|
| `loading…` | Fetching metrics.json |
| `snapshot · interactive filters` | Data loaded; UI is interactive |
| `real data` | Payload mode from metrics.duckdb (not fixture) |
| `failed to load …` | Serve UI/ over HTTP or rebuild JSON |

## What is still “static”

- Methodology / contract prose in HTML is generated at build time from `metric_meta` + templates.  
- Numbers in charts are static until you rebuild `metrics.json`.  
- No auto-polling of the database.

## Files

| Path | Role |
|------|------|
| `UI/build_dashboard.py` | Build shell + metrics.json |
| `UI/assets/dash.js` | Interactive engine (`hydrate` → filters → render) |
| `UI/assets/dash.css` | z-theme (BW/gray/gold) |
| `UI/data/metrics.json` | Snapshot for UI + notifier consumers |
| `UI/README.md` | Short operator note |
| `UI/fixture.py` | Seeded stand-in if metrics.duckdb missing |

## Relation to notification service

`UI/data/metrics.json` → `derived.alerts` is the same feed a notifier should consume so dashboard and alerts never disagree. Key on **topic**, not hashtag — see `docs/ALERTING_KEYING.md`.
