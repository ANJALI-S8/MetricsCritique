# OSINT metrics dashboard

## Model

| Piece | Behavior |
|-------|----------|
| **UI** | Dynamic — filters, charts, tooltips re-render in the browser |
| **Data** | Snapshot — `data/metrics.json` written at build time (not live DB) |
| **Click-to-inspect** | Live — every clickable mark queries `api_server.py`, which runs one bounded, read-only DuckDB query per click and returns just those rows. Nothing is preloaded. |

This is **not** a live connection to Postgres while you browse — the charts
run off the snapshot. Click-to-inspect *is* a live (read-only) query against
`data/analytics.duckdb` / `data/geo_ontology.duckdb` / `data/metrics.duckdb`,
scoped to whatever you clicked.
After re-running metrics v2, rebuild the snapshot so the UI sees new numbers.

## Run

```bash
# 1) (re)build snapshot + shell from metrics.duckdb
cd /home/hariramanan/NTRO/EDA
.venv/bin/python UI/build_dashboard.py

# 2) serve UI + the click-to-inspect API together
cd UI
../.venv/bin/python3 api_server.py 8877
# open http://127.0.0.1:8877/
```

Click-to-inspect needs `api_server.py`, not a plain `python3 -m http.server`
— a plain static server still shows the dashboard fine, but clicking a bar,
row, or card will show "could not reach the live data API" instead of the
underlying posts.

Optional offline single HTML with data embedded (no click-to-inspect, since
there's no server behind it):

```bash
python UI/build_dashboard.py --static-embed
# then open index.html via file://
```
