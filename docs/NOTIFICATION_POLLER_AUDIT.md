# Notification poller audit — `scripts/notify_poller.py`

**Written 2026-08-09.** A validation pass on the notification-generation layer: what exists, a
live bug found and fixed, a schema mismatch found and fixed, a deliberate state reset, and what's
still open. Every number below is reproducible with the commands given.

---

## 1. What exists today

`scripts/notify_poller.py` is a **reference** poller — no Slack/email/webhook destination has ever
been specified (`grokplans/gaps.md:148`: "notifier not built"). It reads `UI/data/geo_alerts.json`
(the FDR q<0.05 gated export from `geo_alert_candidates`, written by
`scripts/11_geo_timeline_metrics.py`), dedups against `reports/notify_poller_state.json` by
`(day, dimension_type, dimension_key, metric)` — identity, not the whole record, so a rebuild that
nudges `value`/`q_value`/`rationale` doesn't re-fire — and "sends" by appending a JSON line to
`reports/notify_poller.log` plus a stdout line. `send_notification()` is the single integration
point a real channel would replace; nothing leaves the machine today.

What was already solid and is untouched by this pass: the dedup key choice, the atomic
tmp+rename state write (`save_state()`), the cap-vs-mark-seen distinction (held-back alerts are
never marked seen, so they resurface next run), and the dry-run/send-loop ordering — fixed once
already after a real incident where `--dry-run` reached `send_notification()` and every key got
logged twice (`reports/notify_poller.log.pre-2026-08-fix` still shows this: 980 lines for 490 keys,
exactly 2×).

## 2. The bug: staleness measured against the wrong clock

The `--max-age-days` guard (default 14) used to compute:

```python
cutoff = (datetime.now(timezone.utc).date() - timedelta(days=max_age_days)).isoformat()
```

— anchored to the **OS wall clock**. This corpus is a periodically-rebuilt extract, not a live
stream: `data/analytics.duckdb` was last written 2026-07-29, and dashboard rebuilds on
2026-08-06/07/08 all reused the same 2026-06-14→2026-07-29 window — the data genuinely stopped
moving forward. The freshest alert day in `UI/data/geo_alerts.json` is **2026-07-28**.

Measured directly against the real files, before any fix:

```
$ .venv/bin/python scripts/notify_poller.py --dry-run
notify_poller: 358 qualifying alerts ... (490 already seen) -> 0 new
```

Manual analysis of the same files: of 358 currently-exported alerts, **123 were not yet in the
seen-set** tracked by `reports/notify_poller_state.json` (**21 of them high-severity**), and every
one of the 123 was being suppressed as "stale" by the wall-clock cutoff. On **2026-08-11**
(`2026-07-28 + 14 days`) — two days after this was found — the guard would have suppressed **every**
alert in this export, permanently, on every future run, regardless of severity. Not because
anything went stale analytically, but because the clock the guard used and the clock the data
lived on had drifted apart by more than the window's own width.

**Fix:** anchor the cutoff to the freshest day actually present in the export, not the wall clock:

```python
reference_day = max((a.get("day") for a in qualifying if a.get("day")), default=None)
...
new, stale = age_guard(new, max_age_days, reference_day)   # age_guard: cutoff = reference_day - N
```

An alert's age is now relative to what the pipeline currently knows. This is correct while the
corpus is frozen (nothing ages out just because a calendar page turned), and stays correct if live
ingestion ever resumes (a fresher day would then age out today's alerts too, exactly like a real
wall clock would). The inline filtering in `main()` was pulled into three standalone functions —
`partition_new()`, `age_guard()`, `apply_send_cap()` (`scripts/notify_poller.py:113-133`) — purely
so this logic is unit-testable; `main()`'s CLI flags, output format, and file formats are
unchanged.

## 3. A second bug found while adding the belt-and-suspenders check

`docs/ALERTING_KEYING.md` requires: "Require `support_ok` / non-null `support_n` — never alert on
gated cells." `notify_poller.py` never checked either field — harmless only because
`11_geo_timeline_metrics.py` already asserts `support_ok` before a row can reach
`geo_alert_candidates`. Adding the consumer-side check exposed that **the export doesn't carry a
`support_ok` field at all**:

```
$ python3 -c "import json; a=json.load(open('UI/data/geo_alerts.json'))[0]; print(sorted(a.keys()))"
['day', 'dimension_key', 'dimension_type', 'metric', 'q_value', 'rationale', 'severity', 'support_n', 'value']
```

A first implementation of the gate checked `not alert.get("support_ok")`, which is `True` for every
record (the key is simply absent) — it would have silently dropped **all 358 real alerts**, caught
immediately by running `--dry-run` against the real file rather than trusting the synthetic test
alone. Fixed `support_gated()` (`scripts/notify_poller.py:136-146`) to match the schema actually
shipped: honor `support_ok` if present, otherwise require `support_n` to be a positive number —
which is what the doc's "support_ok / non-null support_n" phrasing already allows for. Re-run after
the fix: `0` alerts gated, as expected (upstream's guarantee holds).

## 4. State reset and log archival

`reports/notify_poller_state.json`'s seen-set (490 keys, 2 runs, last run 2026-07-30) predated
three regenerations of the qualifying-alert set: 490 → 463 (risk/theme relabel) → 386 → 358
(`fdr_scope` family-boundary fix). Reconciling old and new keys under a changed methodology would
produce a confusing partial result, so the state was reset deliberately rather than merged:

1. Archived `reports/notify_poller.log` (980 lines, the doubled pre-fix artifact) to
   `reports/notify_poller.log.pre-2026-08-fix` rather than appending onto it.
2. `--dry-run` after the fix, before touching anything: `358 qualifying alerts ... -> 93 new, 30
   suppressed as older than 14d (cutoff relative to data's freshest day 2026-07-28)` — 93 + 30 =
   123, exactly matching the pre-fix manual count. Confirms the fix behaves as analyzed.
3. `--reset` for real (default cap 50): sent 50, held back 220, marked 88 "stale" (>14d before
   2026-07-28) as seen without sending — `seen_keys` → 138.
4. One follow-up catch-up run with `--max-send=250` to drain the remaining 220-alert backlog in one
   step, since establishing a clean baseline was the explicit goal here (this is still
   local-log-only — no external channel exists to flood).
5. `--dry-run` again: `358 qualifying alerts ... (358 already seen) -> 0 new` — idempotency holds.

Final baseline: **358/358 alerts tracked as seen**, of which **270 were actually logged** (32
high-severity, 238 medium, spanning 2026-07-14 → 2026-07-28) and **88 were deliberately never
notified** (older than the 14-day window relative to the export's own freshest day). Going forward,
only genuinely new alerts in future rebuilds will surface.

**Caveat for later:** this reset+drain intentionally logged 270 alerts in one sitting. That's fine
against a local log file, but if/when a real channel is wired in, do **not** repeat a bare `--reset`
against an accumulated backlog — it would page 270 notifications in one burst. Decide on a
backlog-draining policy (e.g. `--dry-run` review first, or a higher one-time `--max-age-days` cutoff
to only catch up on recent items) before that day.

## 5. Test coverage

`scripts/tests/test_notify_poller.py` — none existed before this pass; `scripts/tests/` had 6 other
files, none touching the poller. 24 checks, following the repo's existing `check()`/PASS-FAIL
pattern:

- `alert_key()`: stable identity across value/q_value/severity drift; missing fields don't raise.
- `age_guard()`: **regression pin for the bug in §2** — replicates the old wall-clock logic inline
  (not imported, so resurrecting `datetime.now()` in `age_guard()` can't accidentally pass this
  test too) and shows it wrongly suppresses everything once real "today" has drifted past a frozen
  corpus; shows the fixed logic keeps alerts within the window regardless of wall-clock drift.
  Verified both directions by hand: reintroducing the old line makes the test fail
  (`fresh=1 stale=2`, wrong); the fixed version passes (`fresh=2 stale=1`, correct).
- `apply_send_cap()`: splits exactly at the cap.
- `support_gated()`: the real-schema case (no `support_ok` key at all) is NOT gated; explicit
  `support_ok=False`, missing `support_n`, and non-positive `support_n` all ARE gated.
- `load_state()`: a corrupt state file is treated as no-prior-state, not a crash.
- End-to-end `main()` (monkeypatched `ALERTS_JSON`/`STATE_PATH`/`LOG_PATH`, no real files touched):
  a clean run sends and marks everything seen; a re-run is idempotent; `--reset` reprocesses from
  scratch; a support-gated synthetic alert is never sent or marked seen; `--max-send` holds back
  the excess without marking it seen.

Run: `.venv/bin/python scripts/tests/test_notify_poller.py` → `24/24 checks passed`.

## 6. Found but not changed — needs a decision, not a hardening pass

- **Two disjoint alert substrates exist**, and design intent points at a different one than the
  poller actually reads. `UI/build_dashboard.py:511` (`JSON_NOTES["what_this_is"]`) says: "The
  notification service should read THIS file" — meaning `UI/data/metrics.json`'s `derived.alerts`
  (topic × platform × tag grain, from `alert_candidates` in `metrics.duckdb`). But
  `notify_poller.py` only reads `UI/data/geo_alerts.json` (entity/risk/theme grain, from
  `geo_alert_candidates` in `geo_ontology.duckdb` — the newer pipeline). Nobody consumes
  `derived.alerts` today. Whether the poller should consume both, one, or `derived.alerts` should
  be retired from that comment is a scope decision, not something folded into this pass.
- **Not wired into the operational runbook.** `collaboration/05_HOW_TO_RUN.md` never mentions
  `notify_poller.py` — it exists only as a delivered artifact in spec/manifest docs, with no cron
  or systemd unit despite the script's own docstring suggesting one.
- **No real delivery channel, no human-confirm gate.** Both still open per `grokplans/gaps.md:169`
  ("Build notifier only on alert_candidates with human confirm").

## Reproduction

```bash
cd /home/hariramanan/NTRO/EDA
.venv/bin/python scripts/tests/test_notify_poller.py     # 24/24 checks passed
.venv/bin/python scripts/notify_poller.py --dry-run       # 358 qualifying, 0 new (baseline is current)
python3 -c "import json; s=json.load(open('reports/notify_poller_state.json')); print(len(s['seen_keys']), s['runs'])"
wc -l reports/notify_poller.log reports/notify_poller.log.pre-2026-08-fix
```
