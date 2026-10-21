#!/usr/bin/env python3
"""Stage 21 - boundary data for the news engine (offline after the first download).

  python scripts/21_news_geo_data.py --download      # fetch Natural Earth v5.1.2 (public domain)
  python scripts/21_news_geo_data.py --verify        # checksum + load + sanity counts
  python scripts/21_news_geo_data.py --verify --check-live
                                                     # also prove every FIPS code seen in
                                                     # OpenSearch (read-only aggregation) maps

Files land in data/geo/natural_earth_v5.1.2/ (git-ignored; reproducible by --download).
Downloaded files are treated as untrusted input: they are only parsed as JSON, never executed.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import news_config as C  # noqa: E402
from lib import news_geo as G  # noqa: E402

TAG = "v5.1.2"
FILES = ["ne_10m_admin_0_countries", "ne_10m_admin_1_states_provinces"]
URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{tag}/geojson/{name}.geojson"
# SHA-256 of the files as first downloaded on 2026-10-20 (pinned so a changed upstream is noticed)
PINNED = {
    "ne_10m_admin_0_countries.geojson": "239eec57ac17f100a11e2536cffc56752c318b50ae765b0918ff7aab4ce8f255",
    "ne_10m_admin_1_states_provinces.geojson": "22d0e3ad85eb3e27f17cabf8ba2d50e554fbc27a87796ff891d958185da62fb5",
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download() -> None:
    G.NE_DIR.mkdir(parents=True, exist_ok=True)
    for n in FILES:
        dest = G.NE_DIR / f"{n}.geojson"
        print(f"downloading {n} ...")
        urllib.request.urlretrieve(URL.format(tag=TAG, name=n), dest)
        print(f"  {dest.stat().st_size / 1e6:.1f} MB")


def verify() -> bool:
    ok = True
    for fn, want in PINNED.items():
        p = G.NE_DIR / fn
        if not p.exists():
            print(f"MISSING {p}")
            ok = False
            continue
        got = sha256(p)
        flag = "OK" if got == want else "CHECKSUM MISMATCH"
        ok &= got == want
        print(f"{flag:18s} {fn}")
    if not ok:
        return False
    geo = G.GeoIndex()
    print(f"countries={len(geo.countries)}  admin1={len(geo.admin1)}  "
          f"IN admin1={len(geo.admin1_of('IN'))}  BD admin1={len(geo.admin1_of('BD'))}")
    # known-point sanity checks
    for name, lon, lat, iso in [("Delhi", 77.21, 28.61, "IN"), ("Dhaka", 90.41, 23.81, "BD"),
                                ("Mumbai", 72.88, 19.08, "IN"), ("Chittagong", 91.78, 22.36, "BD")]:
        r = geo.locate_point(lon, lat)
        status = "OK" if r["iso2"] == iso else "FAIL"
        ok &= r["iso2"] == iso
        print(f"  {status:4s} {name:11s} -> {r['iso2']} {r['adm1_code']}")
    return ok


def check_live(geo: G.GeoIndex) -> bool:
    from lib.news_sources import ReadOnlyOpenSearch
    os_ = ReadOnlyOpenSearch()
    unmapped = {}
    for field in ("gdelt.action_country", "gkg.locations_country_code"):
        r = os_.search({"size": 0, "aggs": {"c": {"terms": {"field": field, "size": 400}}}})
        buckets = r["aggregations"]["c"]["buckets"]
        tot = sum(b["doc_count"] for b in buckets)
        miss = [(b["key"], b["doc_count"]) for b in buckets if geo.fips_to_iso(b["key"]) is None]
        miss_docs = sum(n for _, n in miss)
        print(f"{field}: {len(buckets)} codes, unmapped {len(miss)} covering {miss_docs}/{tot} "
              f"({100 * miss_docs / max(tot, 1):.2f}%) docs: {miss[:15]}")
        unmapped[field] = miss
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--check-live", action="store_true")
    a = ap.parse_args()
    if a.download:
        download()
    ok = True
    if a.verify or a.download or a.check_live:
        ok = verify()
    if ok and a.check_live:
        check_live(G.GeoIndex())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
