"""Shared helpers for the news-engine tests. No network, no production data."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import news_config as C  # noqa: E402
from lib import news_geo as G  # noqa: E402
from lib import news_ingest as I  # noqa: E402
from lib.news_store import Store  # noqa: E402

UTC = dt.timezone.utc
DIM = C.EMBEDDING_DIM


def unit(x):
    x = np.asarray(x, dtype=np.float32)
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def fact_row(i, day, *, st="scraped_articles", domain="D01", topic="D01.T01", cat="D01.T01.C01",
             publisher=None, cluster=None, dup=False, has_label=True, hour=12, title=None):
    d = dt.date(2026, 10, 1) + dt.timedelta(days=day) if isinstance(day, int) else day
    ts = dt.datetime(d.year, d.month, d.day, hour, tzinfo=UTC)
    return {"source_table": st, "article_id": i, "os_id": f"{st}-{i}", "published_at": ts, "day": d,
            "has_label": has_label, "domain_id": domain if has_label else None,
            "topic_id": topic if has_label else None, "category_id": cat if has_label else None,
            "content_hash": f"h{i}", "is_duplicate": dup, "source_domain": publisher or f"pub{i % 7}.com",
            "title": title or f"title {i}", "cluster_id": cluster,
            "cluster_note": "duplicate" if dup else None}


def geo_row(i, country="IN", adm1=None, prec="city", lat=19.0, lon=72.9, st="scraped_articles", mentions=()):
    located = country is not None
    return {"source_table": st, "article_id": i, "ev_country_iso": country, "ev_adm1_code": adm1,
            "ev_place": None, "ev_lat": lat if located else None, "ev_lon": lon if located else None,
            "ev_precision": prec if located else None, "geo_source": "gdelt_action" if located else None,
            "geo_inconsistent": False, "geo_snapped": False, "loc_status": "located" if located else "unlocated",
            "country_code_unmapped": False, "mention_countries_iso": list(mentions)}


def populate(store: Store, facts: list[dict], geos: list[dict]) -> None:
    store.upsert("article_fact", pd.DataFrame(facts))
    store.upsert("article_geo", pd.DataFrame(geos))


def doc(i, *, published="2026-10-10T12:00:00+00:00", conflict=False, content_hash=None, domain="a.com",
        fips="IN", loc="Mumbai, Maharashtra, India", lat=19.07, lon=72.88, st="scraped_articles", mentions=("IN",)):
    """A normalised OpenSearch doc as produced by news_sources.normalise_hit."""
    from lib import news_sources as NS
    hit = {"_id": f"{st}-{i}", "_index": "full-articles-000002", "_source": {
        "article_id": str(i), "source_table": st, "published_at": published,
        "published_at_conflict": conflict, "published_at_confidence": "high",
        "ingested_at": "2026-10-10T13:00:00+00:00", "source_domain": domain, "url": f"https://{domain}/{i}",
        "title": f"t{i}", "content_hash": content_hash or f"hash{i}",
        "gdelt": {"action_country": fips, "action_location": loc,
                  "action_geo": {"lat": lat, "lon": lon} if lat is not None else None},
        "gkg": {"locations_country_code": list(mentions)}}}
    if published is None:
        del hit["_source"]["published_at"]
    return NS.normalise_hit(hit)


class FakeCtxParts:
    """Minimal stand-ins so ingest/cluster code runs with no network."""

    class PG:  # never queried: fetch_labels is monkeypatched in tests
        pass

    class OS:
        def __init__(self, vectors=None):
            self.vectors = vectors or {}

        def fetch_by_ids(self, ids, include_embedding=True, batch=200):
            out = {}
            for oid in ids:
                if oid in self.vectors:
                    out[oid] = {"_id": oid, "_source": {"embedding": self.vectors[oid]}}
            return out


def label(domain="D01", topic="D01.T01", cat="D01.T01.C01"):
    return {"domain_id": domain, "topic_id": topic, "category_id": cat, "subtopic_id": cat + ".S01",
            "category_confidence": 0.8, "subtopic_confidence": 0.6, "taxonomy_version": "v4.1",
            "published_at": None, "content_hash": None, "url": None}
