# Adoption assessment — is this correct for social media?

**Last assessed:** 2026-07-29

## Short answer

| Question | Answer |
|----------|--------|
| Approach right for **OSINT / social-intel monitoring**? | **Yes** |
| Adopt **v1** metrics as decision inputs? | **No** |
| Adopt **v2** metrics for analyst triage? | **Yes, with human oversight** |
| Ready as unattended brand/hate enforcement product? | **No** |
| “Truth about social media at large”? | **No** — truth about **this corpus under these collectors** |

## What is solid enough to adopt

- Read-only production; local DuckDB/parquet analytics  
- Dedup, effort-normalised rates, collection-window gating  
- Count models (NegBinom surprise) instead of exploding robust-z  
- FDR-controlled alerts; support gates (NULL beats fake spikes)  
- Honest proxies (reach); hate gated until calibration  
- Topic-primary alerting (hashtags only ~18% coverage)

## What not to over-claim

- Corpus is Telegram-heavy and collection-shaped  
- Ontology is heuristic (needs human card review)  
- Hate/toxicity uncalibrated on war/terror text  
- Short history (~6 weeks dense); half-life/accel exploratory  
- Engagement is single-observation per post until snapshots accrue  
- UI is interactive over a **snapshot**, not live social firehose  

## Use-case matrix

| Use case | Adopt? |
|----------|--------|
| Internal OSINT narrative monitoring | **Yes** |
| “What is spiking today?” analyst triage | **Yes** (v2 alerts + UI) |
| Marketing / brand listening suite replacement | Partial / no |
| Automated legal enforcement on hate | **No** |
| Causal claims / true audience reach | **No** |

## Operating rules if adopted

1. Query **`metrics.duckdb`**, not v1 `intensity_z`.  
2. Key ops on **topic × platform × day**.  
3. Treat surprise/emergence as **look here**, not automated truth.  
4. Keep hate off automated actions until labels exist.  
5. Rebuild UI snapshot after each metrics publish.  
6. Cron Layer-0 extract for future per-post half-life.  

Detail discussion lived in the chat session; this file is the frozen go/no-go summary.
