"""Orchestration. Pilot, historical backfill and incremental runs differ ONLY in how articles are
discovered; they then go through exactly the same stages:

  discover -> ingest_docs -> resolve_pending -> cluster -> recompute(AOI panels, aoi_day,
  event metrics, emerging tests, developments, alerts)

Reliability
  * ingest is committed per published day together with its checkpoint (`day_stage`)
  * clustering selects `cluster_id IS NULL` work, commits per chunk
  * every write is an upsert on a natural key; a failed run is simply re-run
  * recompute is a pure function of the stored articles, so it is always safe to repeat
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import news_aoi as A
from . import news_cluster as K
from . import news_config as C
from . import news_events as EV
from . import news_geo as G
from . import news_ingest as I
from . import news_metrics as M
from .news_store import Store, now_utc


def daterange(d0: dt.date, d1: dt.date) -> list[dt.date]:
    return [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]


def as_of_for_day(ctx: I.Ctx, day: dt.date) -> dt.datetime:
    """Evaluation time for a published day: its end, but never in the future."""
    _, e = I.day_bounds(day, ctx.P["day_tz"])
    return min(e, ctx.now)


def scope_aois(ctx: I.Ctx) -> list[tuple[G.AOI, Optional[str]]]:
    if not ctx.scope.countries:
        raise ValueError("an AOI family needs an explicit country list (scope 'all' has none registered; "
                         "register AOIs with --aoi-countries)")
    return A.pilot_aois(ctx.geo, ctx.scope.countries)


def recompute(ctx: I.Ctx, aois: list[tuple[G.AOI, Optional[str]]], as_of_list: list[dt.datetime],
              event_ids: Optional[set[str]] = None, run_developments: bool = True) -> dict:
    """Recompute every derived metric/alert for the given AOIs. Safe to repeat."""
    P, st = ctx.P, ctx.store
    out = {"aois": len(aois), "panels": [], "emerging": {"candidates": 0, "alerts": 0}, "developments": {},
           "event_metrics": 0}
    M.seed_metric_meta(st)
    for aoi, parent in aois:
        A.register_aoi(st, aoi, parent)
    ev_asof = as_of_list[-1] if as_of_list else ctx.now
    out["event_metrics"] = EV.compute_event_metrics(st, P, ev_asof, event_ids)
    for aoi, _ in aois:
        summ = M.compute_aoi_panel(st, aoi, P, ctx.run_id)
        out["panels"].append(summ)
        events = A.aoi_events(st, aoi, P) if aoi.mode == "event" else None
        with st.tx():
            st.delete_where("aoi_day", "aoi_id = ?", [aoi.aoi_id])
            st.upsert("aoi_day", M.compute_aoi_day(st, aoi, P, events))
        if aoi.mode == "event" and as_of_list:
            r = EV.emerging_tests(st, aoi, P, as_of_list, ctx.run_id)
            out["emerging"]["candidates"] += r["candidates"]
            out["emerging"]["alerts"] += r["alerts"]
    if run_developments and as_of_list:
        out["developments"] = EV.detect_developments(ctx, [a for a, _ in aois], as_of_list)
    return out


def process_new_articles(ctx: I.Ctx) -> dict:
    """Stages shared by every mode after docs have been ingested."""
    pend = I.resolve_pending(ctx)
    cl = K.cluster_new_articles(ctx)
    return {"pending": {k: v for k, v in pend.items() if k != "new_keys"},
            "cluster": {k: v for k, v in cl.items() if k != "touched"}, "touched": cl.get("touched", set())}


def run_range(ctx: I.Ctx, d0: dt.date, d1: dt.date, force_days: bool = False,
              max_days: Optional[int] = None, run_developments: bool = True) -> dict:
    """Pilot / historical backfill: process published days d0..d1 (inclusive), resumably."""
    days = daterange(d0, d1)
    done_days, skipped = [], 0
    for d in days:
        if max_days is not None and len(done_days) >= max_days:
            break
        r = I.ingest_day(ctx, d, force=force_days)
        if r.get("skipped"):
            skipped += 1
        else:
            done_days.append(d)
        I.measure_corpus_day(ctx, d)
    proc = process_new_articles(ctx)
    processed_days = [d for d in days if ctx.store.stage_done("ingest", ctx.scope.name, d)]
    as_of_list = [as_of_for_day(ctx, d) for d in processed_days]
    aois = scope_aois(ctx)
    rec = recompute(ctx, aois, as_of_list, event_ids=None, run_developments=run_developments)
    for d in processed_days:
        ctx.store.mark_stage("recompute", ctx.scope.name, d, "done", ctx.run_id)
    return {"days_requested": len(days), "days_ingested": len(done_days), "days_skipped_done": skipped,
            "days_remaining": len(days) - len(processed_days), "process": {k: v for k, v in proc.items() if k != "touched"},
            "recompute": _slim(rec)}


def run_incremental(ctx: I.Ctx, since: Optional[dt.datetime] = None, run_developments: bool = True) -> dict:
    """Newly ingested articles only (OpenSearch `ingested_at` watermark with overlap). Not a
    streaming system: it processes whatever arrived since the last run, when invoked."""
    st = ctx.store
    key = f"ingest_wm:{ctx.scope.name}"
    wm_s = st.get_kv(key)
    if since is None:
        if wm_s is None:
            raise RuntimeError("no ingest watermark for this scope yet; pass --since <ISO time> for the first "
                               "incremental run (a bounded start, so nothing scans the whole corpus by accident)")
        since = dt.datetime.fromisoformat(wm_s) - dt.timedelta(hours=ctx.P["incremental_overlap_hours"])
    docs = I.discover_incremental(ctx, since)
    with st.tx():
        res = I.ingest_docs(ctx, docs)
    if docs:
        mx = max((d["ingested_at"] for d in docs if d.get("ingested_at")), default=None)
        if mx is not None:
            st.set_kv(key, mx.isoformat())
    proc = process_new_articles(ctx)
    # AOIs affected by the new articles (any AOI whose membership could change)
    new_keys = res["new_keys"]
    aois = scope_aois(ctx)
    affected = _affected_aois(st, aois, new_keys)
    if not new_keys and not proc["touched"] and not proc["pending"].get("resolved"):
        return {"new_articles": 0, "note": "nothing new", "process": proc["pending"]}
    rec = recompute(ctx, affected or [], [ctx.now], event_ids=proc["touched"] or None,
                    run_developments=run_developments) if affected else {"note": "no AOI affected"}
    return {"new_articles": len(new_keys), "affected_aois": len(affected), "since": since.isoformat(),
            "process": {"pending": proc["pending"], "cluster": proc["cluster"]}, "recompute": _slim(rec)}


def _affected_aois(st: Store, aois, new_keys) -> list:
    if not new_keys:
        return []
    k = pd.DataFrame(new_keys, columns=["source_table", "article_id"])
    st.con.register("_nk", k)
    try:
        g = st.df("""SELECT g.* FROM article_geo g JOIN _nk USING (source_table, article_id)""")
    finally:
        st.con.unregister("_nk")
    return [(a, p) for a, p in aois if len(g) and G.aoi_mask(a, g).any()]


def _slim(rec: dict) -> dict:
    d = dict(rec)
    if "panels" in d:
        d["panels"] = {"n": len(d["panels"]), "supported": sum(1 for p in d["panels"] if p["supported"]),
                       "alerts": sum(p["n_alerts"] for p in d["panels"]),
                       "tests": sum(p["n_tests"] for p in d["panels"])}
    return d


def export_alerts(st: Store, path, aoi_id: Optional[str] = None) -> int:
    """Write active alerts as JSON, same shape family as UI/data/geo_alerts.json so the reference
    notify_poller pattern can consume it."""
    q = "SELECT * FROM alert WHERE status = 'active'"
    args = []
    if aoi_id:
        q += " AND aoi_id = ?"
        args.append(aoi_id)
    df = st.df(q + " ORDER BY as_of DESC, q", args)
    for c in ("as_of", "first_seen_at", "last_seen_at", "retracted_at", "day"):
        if c in df:
            df[c] = df[c].astype(str)
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"generated_at": now_utc().isoformat(), "alerts": df.to_dict("records")},
                                     default=str, indent=1))
    return len(df)
