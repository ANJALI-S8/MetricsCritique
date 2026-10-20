#!/usr/bin/env python3
"""
Reference notification poller for the geo-ontology alert layer.

Reads UI/data/geo_alerts.json -- the FDR q<0.05 gated alert-candidate export
(entity/risk/theme grains, mirrored 1:1 from data/geo_ontology.duckdb's
geo_alert_candidates table; see scripts/11_geo_timeline_metrics.py and
docs/GEO_ONTOLOGY_CONTEXT.md, "Alerts / notifications") -- and logs every
alert not already logged on a prior run.

No delivery channel (Slack / email / webhook) has been specified for this
project. This is a REFERENCE poller: it demonstrates the full loop --
read -> diff against a last-run marker -> notify -- end to end, with
send_notification() as the single integration point a real channel would
replace. Until a destination exists, "notify" means "append a structured
JSON line to reports/notify_poller.log and print a human-readable line" --
nothing is sent anywhere.

Idempotent by construction: an alert's identity is (day, dimension_type,
dimension_key, metric) -- the same key geo_alert_candidates is unique on
(0 duplicate keys, per verification) -- not the whole record, so a rebuild
that nudges value/q_value/rationale slightly without changing the underlying
finding does not re-fire. Run it as often as you like (e.g. on a cron/systemd
timer once a real channel exists); a run with nothing new logs "0 new".

--dry-run is genuinely side-effect-free: it PREVIEWS to stdout only and never
calls send_notification(). This matters because send_notification() is the
function a maintainer is told to replace with a real Slack/webhook POST -- if
--dry-run reached it, the safe way to test would become the way to page
everyone. (It used to: the send loop sat above the dry-run guard, and
reports/notify_poller.log shows every key logged exactly twice as a result.)

Two blast-radius limits, both overridable:
  --max-send=N       per-run cap (default 50). Held-back alerts are NOT marked
                     seen, so the next run picks them up.
  --max-age-days=N   ignore alerts dated older than N days (default 14). BH
                     q-values shift when a day's family changes, so old cells
                     can cross the threshold and resurface as "new".

"Stale" is measured against the freshest day actually present in the export
(the pipeline's own idea of "now"), NOT the OS wall clock. This corpus is a
periodically-rebuilt extract, not a live stream -- data/analytics.duckdb can
sit unchanged for weeks between rebuilds. A wall-clock cutoff was tried first
and was wrong: on 2026-08-09, with the export's freshest alert dated
2026-07-28, `datetime.now()` put every single alert outside a 14-day window,
so a real run reported "0 new" while 123 unseen alerts (21 high-severity) sat
in the export -- and every future run would have done the same, permanently,
once real calendar time passed 2026-08-11, regardless of how important a
finding was. Anchoring to max(day) instead means an alert's age is relative to
what the pipeline currently knows: correct while the corpus is frozen, and
still correct if live ingestion ever resumes (a fresher day would then age out
today's alerts too, same as a real wall clock would).

Alerts are also required to carry a positive support_n, and support_ok=true
if that field is present at all, before they can be sent --
belt-and-suspenders for the requirement in docs/ALERTING_KEYING.md ("never
alert on gated cells"). Upstream already enforces this before a row can reach
geo_alert_candidates, so this should never trigger in practice; it exists so
that guarantee isn't just convention.

    python scripts/notify_poller.py                  # normal run
    python scripts/notify_poller.py --dry-run        # preview only; no sends, no state change
    python scripts/notify_poller.py --reset          # forget previously-seen alerts, start fresh
    python scripts/notify_poller.py --max-send=200   # raise the per-run cap
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_MAX_SEND = 50      # per-run send cap; a cold start must not fire the whole backlog
DEFAULT_MAX_AGE_DAYS = 14  # don't page about alerts dated older than this

EDA_ROOT = Path(__file__).resolve().parents[1]
ALERTS_JSON = EDA_ROOT / "UI" / "data" / "geo_alerts.json"
STATE_PATH = EDA_ROOT / "reports" / "notify_poller_state.json"
LOG_PATH = EDA_ROOT / "reports" / "notify_poller.log"

KEY_FIELDS = ("day", "dimension_type", "dimension_key", "metric")


def alert_key(a: dict) -> str:
    """Identity of an alert candidate, stable across rebuilds that don't
    change the underlying finding -- not the whole record (value/q_value/
    severity/rationale can drift a little run to run without this being a
    genuinely *new* alert)."""
    return "|".join(str(a.get(k)) for k in KEY_FIELDS)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as exc:
            print(f"WARN: {STATE_PATH} unreadable ({type(exc).__name__}: {exc}); "
                  f"treating as no prior state")
    return {"seen_keys": [], "last_run_at": None, "runs": 0}


def save_state(state: dict) -> None:
    """Atomic write: a truncated/half-written state file is read back as "no
    prior state" by load_state(), which would silently replay the entire
    backlog as new. tmp+rename makes that unreachable."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def partition_new(alerts: list, seen: set) -> list:
    """Alerts not already recorded in the seen-set, by alert_key()."""
    return [a for a in alerts if alert_key(a) not in seen]


def age_guard(alerts: list, max_age_days: int, reference_day: str) -> tuple:
    """Split alerts into (fresh, stale) relative to reference_day.

    reference_day is deliberately a parameter, not datetime.now() -- see the
    module docstring. Callers should pass the freshest day actually present
    in the export, not the OS wall clock, because this corpus is rebuilt
    periodically rather than streamed live."""
    cutoff = (date.fromisoformat(reference_day) - timedelta(days=max_age_days)).isoformat()
    fresh = [a for a in alerts if (a.get("day") or "") >= cutoff]
    stale = [a for a in alerts if (a.get("day") or "") < cutoff]
    return fresh, stale


def apply_send_cap(alerts: list, max_send: int) -> tuple:
    """Split alerts into (to_send, held) at the per-run cap."""
    return alerts[:max_send], alerts[max_send:]


def support_gated(alert: dict) -> bool:
    """True if this alert lacks the support docs/ALERTING_KEYING.md requires
    ("never alert on gated cells"). UI/data/geo_alerts.json does not carry an
    explicit support_ok field -- every row is support_ok=true by construction
    upstream (11_geo_timeline_metrics.py asserts it before a row can reach
    geo_alert_candidates), so the export doesn't re-state it. If a future
    export version does add support_ok, honor it; either way, support_n must
    be present and positive -- that part of the contract this consumer can
    and does check for itself."""
    if alert.get("support_ok") is False:
        return True
    support_n = alert.get("support_n")
    return support_n is None or not (isinstance(support_n, (int, float)) and support_n > 0)


def preview_notification(alert: dict) -> None:
    """Print-only. Same line send_notification() prints, with NO channel
    side-effect -- this is what --dry-run uses."""
    sev = str(alert.get("severity", "?")).upper()
    q = alert.get("q_value")
    q_s = f"{q:.4g}" if isinstance(q, (int, float)) else str(q)
    print(f"  [{sev:6s}] {alert.get('dimension_type')}:{alert.get('dimension_key')} "
          f"on {alert.get('day')} -- q={q_s} -- {alert.get('rationale')}")


def send_notification(alert: dict) -> None:
    """The single integration point for a real delivery channel.

    No channel (Slack/email/webhook) has been specified for this project, so
    this reference implementation only logs: a structured JSON line to
    reports/notify_poller.log, plus a human-readable line to stdout. Every
    upstream concern -- what counts as "qualifying" (already FDR-gated by the
    metrics builder), dedup against prior runs, formatting -- is
    channel-agnostic and already done by the time this function is called.
    Replace this function's body to actually deliver (POST to a webhook, call
    a Slack SDK, send mail, ...) once a destination is specified.
    """
    line = {
        "notified_at": datetime.now(timezone.utc).isoformat(),
        "channel": "none-configured (reference poller: log only)",
        "alert": alert,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    preview_notification(alert)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    reset = "--reset" in sys.argv

    def _int_arg(flag: str, default: int) -> int:
        for a in sys.argv[1:]:
            if a.startswith(flag + "="):
                try:
                    return max(0, int(a.split("=", 1)[1]))
                except ValueError:
                    print(f"WARN: could not parse {a!r}; using {flag}={default}")
        return default

    max_send = _int_arg("--max-send", DEFAULT_MAX_SEND)
    max_age_days = _int_arg("--max-age-days", DEFAULT_MAX_AGE_DAYS)

    if not ALERTS_JSON.exists():
        print(f"ERROR: {ALERTS_JSON} not found -- run scripts/11_geo_timeline_metrics.py "
              f"first (it writes this export from geo_alert_candidates)")
        return 1

    try:
        alerts = json.loads(ALERTS_JSON.read_text())
    except Exception as exc:
        print(f"ERROR: could not parse {ALERTS_JSON}: {type(exc).__name__}: {exc}")
        return 1

    if not isinstance(alerts, list):
        print(f"ERROR: {ALERTS_JSON} is not a flat list of alert records "
              f"(got {type(alerts).__name__})")
        return 1

    state = {"seen_keys": [], "last_run_at": None, "runs": 0} if reset else load_state()
    seen = set(state.get("seen_keys", []))

    # Support gate: never alert on gated cells (docs/ALERTING_KEYING.md).
    # Gated alerts are excluded entirely -- not sent, not marked seen -- so if
    # a future bug ever produced one, it stays visible as "new" rather than
    # being silently absorbed into the seen-set.
    gated = [a for a in alerts if support_gated(a)]
    qualifying = [a for a in alerts if not support_gated(a)]
    if gated:
        print(f"WARN: {len(gated)} alert(s) lack support_ok/support_n -- dropped, per "
              f"docs/ALERTING_KEYING.md (\"never alert on gated cells\"). This should never "
              f"happen; upstream is supposed to guarantee it.")

    new = partition_new(qualifying, seen)
    new.sort(key=lambda a: (
        a.get("day") or "",
        a.get("q_value") if isinstance(a.get("q_value"), (int, float)) else 1.0,
    ))

    # Max-age guard: BH q-values are computed per-day across that day's whole
    # family, so adding one series shifts every q that day -- previously
    # sub-threshold cells can cross 0.05 and surface as "new" alerts dated
    # weeks in the past. Don't page anyone about those.
    #
    # Anchored to the freshest day actually present in the export, NOT
    # datetime.now() -- this corpus is a periodically-rebuilt extract, not a
    # live stream, so wall-clock age and data age can (and did) diverge by
    # weeks. See the module docstring for the incident this replaced.
    reference_day = max((a.get("day") for a in qualifying if a.get("day")), default=None)
    stale = []
    if reference_day and new:
        new, stale = age_guard(new, max_age_days, reference_day)

    print(f"notify_poller: {len(qualifying):,} qualifying alerts in {ALERTS_JSON.relative_to(EDA_ROOT)} "
          f"({len(seen):,} already seen as of the last run) -> {len(new)} new"
          + (f", {len(stale)} suppressed as older than {max_age_days}d "
             f"(cutoff relative to data's freshest day {reference_day}, not wall-clock now)" if stale else "")
          + (" [--reset: prior state discarded]" if reset else ""))

    # Send cap: an unbounded loop here means a cold start, a --reset, or an
    # unreadable state file fires the entire backlog in one burst (490/min was
    # observed). Cap it, and say plainly what was held back.
    to_send, held = apply_send_cap(new, max_send)
    if held:
        print(f"WARN: send cap {max_send} reached -- {len(held)} alert(s) held back this run. "
              f"They stay unseen and will be picked up next run (raise with --max-send=N).")

    if dry_run:
        # PREVIEW ONLY -- no channel side-effect. send_notification() must not
        # be reached here: once its body is a real Slack/webhook POST, calling
        # it under --dry-run would mean "testing safely" actually pages people.
        for a in to_send:
            preview_notification(a)
    else:
        for a in to_send:
            send_notification(a)

    if not dry_run:
        # Mark seen: everything already seen, everything actually sent this run,
        # and the age-suppressed ones (deliberately never wanted). NOT the
        # cap-held ones -- marking those seen would silently drop them forever,
        # which is the failure the cap is supposed to prevent.
        held_keys = {alert_key(a) for a in held}
        state["seen_keys"] = sorted(
            (set(state.get("seen_keys", [])) | {alert_key(a) for a in qualifying}) - held_keys
        )
        state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        state["runs"] = state.get("runs", 0) + 1
        save_state(state)
        print(f"marker advanced: {STATE_PATH.relative_to(EDA_ROOT)} now tracks "
              f"{len(state['seen_keys']):,} seen alert keys (run #{state['runs']})")
    else:
        print("--dry-run: marker NOT advanced (state file unchanged)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
