#!/usr/bin/env python3
"""Tests for scripts/notify_poller.py.

Load-bearing regression pin: the poller's --max-age-days guard used to
measure "stale" against datetime.now() (the OS wall clock). This corpus is a
periodically-rebuilt extract, not a live stream, so wall-clock age and data
age can diverge by weeks -- and did: on 2026-08-09, with the freshest alert
in UI/data/geo_alerts.json dated 2026-07-28, a wall-clock-anchored 14-day
cutoff suppressed all 123 alerts not yet in reports/notify_poller_state.json
(21 of them high-severity), and every future run would have suppressed the
entire export permanently from 2026-08-11 onward. The fix anchors the cutoff
to max(day) in the export instead of datetime.now(). The regression check
below replicates the OLD logic inline (not imported) so a fix that resurrects
`datetime.now()` in age_guard() won't accidentally make this test pass too.

Also pins: support_ok/support_n gating (docs/ALERTING_KEYING.md -- "never
alert on gated cells"), the send cap's hold-back-not-mark-seen contract, and
load_state()'s corrupt-file fallback.

Run:  .venv/bin/python scripts/tests/test_notify_poller.py
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import notify_poller as poller  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def alert(day, dim_type="entity", key="x", metric="surprise", severity="high",
          support_n=10, q=0.01, value=1.0, support_ok=None):
    """Matches the real UI/data/geo_alerts.json schema by default: no
    support_ok key at all (every row is support_ok=true by construction
    upstream, so the export doesn't carry the field). Pass support_ok
    explicitly only to test that branch."""
    a = {"day": day, "dimension_type": dim_type, "dimension_key": key,
         "metric": metric, "severity": severity,
         "support_n": support_n, "q_value": q, "value": value, "rationale": "r"}
    if support_ok is not None:
        a["support_ok"] = support_ok
    return a


def run_main(argv_tail):
    old_argv = sys.argv
    sys.argv = ["notify_poller.py"] + argv_tail
    try:
        return poller.main()
    finally:
        sys.argv = old_argv


# ---- alert_key: identity, not the whole record ----------------------------

a1 = alert("2026-07-01", key="a")
a2 = alert("2026-07-01", key="a", q=0.02, value=99.0, severity="medium")
a3 = alert("2026-07-01", key="b")
check("alert_key is stable across value/q_value/severity drift",
      poller.alert_key(a1) == poller.alert_key(a2))
check("alert_key differs on a genuinely different dimension_key",
      poller.alert_key(a1) != poller.alert_key(a3))
check("alert_key handles missing fields without raising",
      isinstance(poller.alert_key({"day": None, "dimension_type": "entity"}), str))

# ---- age_guard: the actual bug this task fixes -----------------------------

window_alerts = [alert("2026-07-28"), alert("2026-07-14"), alert("2026-06-01")]


def _old_wall_clock_cutoff(max_age_days, today):
    """The logic notify_poller.py used to run. Replicated, not imported, so
    reintroducing it in age_guard() doesn't silently pass this test too."""
    return (today - timedelta(days=max_age_days)).isoformat()


# 2026-08-15 is chosen to be past 2026-07-28 + 14 days (2026-08-11) -- the
# exact date after which the old logic would suppress this export forever.
far_future_today = date.fromisoformat("2026-08-15")
old_cutoff = _old_wall_clock_cutoff(14, far_future_today)
old_survivors = [a for a in window_alerts if a["day"] >= old_cutoff]
check("regression pin: the OLD wall-clock logic wrongly suppresses everything "
      "once real 'today' has drifted far past a frozen corpus",
      len(old_survivors) == 0,
      f"{len(old_survivors)} would have survived under the old logic (expected 0)")

fresh, stale = poller.age_guard(window_alerts, 14, reference_day="2026-07-28")
check("fixed age_guard keeps alerts within max_age_days of the export's own freshest day",
      len(fresh) == 2 and len(stale) == 1,
      f"fresh={len(fresh)} stale={len(stale)}")
check("fixed age_guard's fresh set is exactly the two recent alerts",
      {a["day"] for a in fresh} == {"2026-07-28", "2026-07-14"})

# ---- apply_send_cap ---------------------------------------------------------

backlog = [alert(f"2026-07-{d:02d}", key=str(d)) for d in range(1, 11)]
to_send, held = poller.apply_send_cap(backlog, 4)
check("apply_send_cap sends exactly the cap", to_send == backlog[:4])
check("apply_send_cap holds back the remainder", held == backlog[4:])

# ---- support_gated: belt-and-suspenders on docs/ALERTING_KEYING.md ---------

check("support_gated is False for a normal alert with no support_ok key at all "
      "(the actual UI/data/geo_alerts.json shape)",
      not poller.support_gated(alert("2026-07-01")))
check("support_gated is False when support_ok is explicitly true",
      not poller.support_gated(alert("2026-07-01", support_ok=True)))
check("support_gated catches support_ok=False",
      poller.support_gated(alert("2026-07-01", support_ok=False)))
check("support_gated catches a missing support_n",
      poller.support_gated(alert("2026-07-01", support_n=None)))
check("support_gated catches a non-positive support_n",
      poller.support_gated(alert("2026-07-01", support_n=0)))

# ---- load_state(): corrupt file is no-prior-state, not a crash ------------

with tempfile.TemporaryDirectory() as td:
    corrupt = Path(td) / "state.json"
    corrupt.write_text("{not valid json")
    orig_state_path = poller.STATE_PATH
    poller.STATE_PATH = corrupt
    try:
        state = poller.load_state()
        check("load_state() treats a corrupt file as no-prior-state",
              state == {"seen_keys": [], "last_run_at": None, "runs": 0})
    finally:
        poller.STATE_PATH = orig_state_path

# ---- end-to-end main(): idempotency, --reset, gating, send cap ------------

with tempfile.TemporaryDirectory(dir=ROOT) as td:
    # dir=ROOT: main() computes ALERTS_JSON.relative_to(EDA_ROOT) for a log
    # line; a temp dir outside the repo would make that raise. In production
    # ALERTS_JSON is always under EDA_ROOT, so this is a test-harness
    # constraint, not a product bug.
    td = Path(td)
    orig_paths = (poller.ALERTS_JSON, poller.STATE_PATH, poller.LOG_PATH)
    poller.ALERTS_JSON = td / "geo_alerts.json"
    poller.STATE_PATH = td / "notify_poller_state.json"
    poller.LOG_PATH = td / "notify_poller.log"
    try:
        synthetic = [alert(f"2026-07-{20 + i:02d}", key=f"k{i}") for i in range(3)]
        poller.ALERTS_JSON.write_text(json.dumps(synthetic))

        rc = run_main([])
        state = json.loads(poller.STATE_PATH.read_text())
        check("main() exits 0 on a clean first run", rc == 0)
        check("first run marks all 3 alerts as seen", len(state["seen_keys"]) == 3,
              f"seen_keys={len(state['seen_keys'])}")
        check("first run logs 3 notifications",
              len(poller.LOG_PATH.read_text().splitlines()) == 3)

        run_main([])
        lines_after_rerun = len(poller.LOG_PATH.read_text().splitlines())
        state2 = json.loads(poller.STATE_PATH.read_text())
        check("re-running against the same export is idempotent (no new sends)",
              lines_after_rerun == 3, f"log now has {lines_after_rerun} lines")
        check("re-running leaves seen_keys count unchanged", len(state2["seen_keys"]) == 3)

        run_main(["--reset"])
        state3 = json.loads(poller.STATE_PATH.read_text())
        lines_after_reset = len(poller.LOG_PATH.read_text().splitlines())
        check("--reset reprocesses the full current export",
              lines_after_reset == 6, f"expected 6 log lines after reset, got {lines_after_reset}")
        check("--reset rebuilds seen_keys from the current export",
              len(state3["seen_keys"]) == 3)

        gated_alert = alert("2026-07-25", key="gated", support_ok=False)
        poller.ALERTS_JSON.write_text(json.dumps(synthetic + [gated_alert]))
        lines_before_gate = len(poller.LOG_PATH.read_text().splitlines())
        run_main([])
        lines_after_gate = len(poller.LOG_PATH.read_text().splitlines())
        state4 = json.loads(poller.STATE_PATH.read_text())
        check("a support-gated alert is never sent",
              lines_after_gate == lines_before_gate,
              f"log grew from {lines_before_gate} to {lines_after_gate}")
        check("a support-gated alert is never marked seen",
              poller.alert_key(gated_alert) not in set(state4["seen_keys"]))

        poller.STATE_PATH.unlink()
        big_backlog = [alert(f"2026-07-{10 + i:02d}", key=f"big{i}") for i in range(5)]
        poller.ALERTS_JSON.write_text(json.dumps(big_backlog))
        run_main(["--max-send=2"])
        state5 = json.loads(poller.STATE_PATH.read_text())
        check("send cap only marks the sent alerts as seen, holding the rest back",
              len(state5["seen_keys"]) == 2,
              f"expected 2 marked seen with --max-send=2, got {len(state5['seen_keys'])}")
    finally:
        poller.ALERTS_JSON, poller.STATE_PATH, poller.LOG_PATH = orig_paths

print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
