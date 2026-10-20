"""Every dimension_type must resolve through the drill API.

`build_query`'s alert router used to end in a bare `raise ValueError` on any
unrecognised dimension_type, which made it the most brittle coupling in the
repo: adding a dimension anywhere upstream returned HTTP 400 on every drill
until someone remembered to edit that one function. This asserts the registry
covers reality, and that a declared-but-unbuilt axis reports "not ready"
(503) rather than "you asked wrong" (400).

    .venv/bin/python UI/tests/check_api_dimensions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server as a  # noqa: E402

RESULTS: list[bool] = []


def chk(name: str, cond, detail: str = "") -> None:
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")


def main() -> int:
    live = a._main_con.execute(
        "SELECT dimension_type, count(*) FROM geo.geo_alert_candidates "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    chk("geo_alert_candidates is non-empty", bool(live))

    missing = [dt for dt, _ in live if dt not in a.ALERT_ROUTES]
    chk("every live dimension_type is in ALERT_ROUTES", not missing, ", ".join(missing))

    for dt, n in live:
        key, day = a._main_con.execute(
            "SELECT dimension_key, day FROM geo.geo_alert_candidates "
            "WHERE dimension_type = ? LIMIT 1", [dt]).fetchone()
        try:
            sql, params = a.build_query(
                "alert", {"dimension_type": [dt], "dimension_key": [key], "day": [str(day)]})
            rows = a._main_con.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
            chk(f"{dt}: alert drill resolves and returns rows", rows > 0,
                f"{n} alerts, example {str(key)[:30]!r} -> {rows} rows")
        except Exception as exc:                                     # noqa: BLE001
            chk(f"{dt}: alert drill resolves", False, f"{type(exc).__name__}: {exc}")

    # Declared-but-unbuilt axes: expected state, must not look like a client error.
    for dt in ("theme_code", "theme_subcode", "action"):
        try:
            a.build_query("alert", {"dimension_type": [dt], "dimension_key": ["01"]})
            chk(f"{dt}: routes (event_codes is built)", True)
        except a.NotYetPopulated:
            chk(f"{dt}: unbuilt axis reports not-ready, not a client error", True,
                "503 not_yet_populated")
        except Exception as exc:                                     # noqa: BLE001
            chk(f"{dt}: unbuilt axis handled cleanly", False, f"{type(exc).__name__}: {exc}")

    # An unknown type must fail loudly AND say how to fix it.
    try:
        a.build_query("alert", {"dimension_type": ["nonexistent"], "dimension_key": ["x"]})
        chk("unknown dimension_type is rejected", False, "it was accepted")
    except ValueError as exc:
        msg = str(exc)
        chk("unknown dimension_type is rejected with an actionable message",
            "ALERT_ROUTES" in msg and "routable types" in msg, msg[:80])

    n_ok = sum(RESULTS)
    print(f"\n  {n_ok}/{len(RESULTS)} checks passed")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
