"""Geography for the news engine: event location vs publisher location vs "mentioned in".

THREE DIFFERENT THINGS - never merged into one column
  * EVENT location     where the reported event happened. v1 source: GDELT ActionGeo
                       (OpenSearch `gdelt.action_*`). Carries a *precision* (country / adm1 /
                       city) because GDELT puts country-level events on the country centroid.
  * MENTIONED-IN       countries merely named in the text (GKG `locations_country_code`).
                       Country-level only in v1. A separate AOI mode.
  * PUBLISHER location where the outlet is based. Separate registry (news_publishers.py).

Boundaries are Natural Earth 1:10m (public domain), stored locally and used offline. They are
Natural Earth's default (de facto) lines - NOT an authoritative statement on disputed
territories. GDELT country codes are FIPS 10-4, not ISO; the mapping is derived from the
boundary file's FIPS_10 column plus the explicit overrides below, and `unmapped_codes()` is
used by the tests/pilot report to prove coverage on the real data.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import shapely
from shapely.geometry import Point, Polygon, box, shape
from shapely.prepared import prep
from shapely.strtree import STRtree

from . import news_config as C

NE_DIR = C.GEO_DIR / "natural_earth_v5.1.2"
COUNTRIES_FILE = NE_DIR / "ne_10m_admin_0_countries.geojson"
ADMIN1_FILE = NE_DIR / "ne_10m_admin_1_states_provinces.geojson"

PRECISION_RANK = {"country": 1, "adm1": 2, "city": 3}
RANK_PRECISION = {v: k for k, v in PRECISION_RANK.items()}

# FIPS 10-4 -> ISO 3166-1 alpha-2 where Natural Earth's FIPS_10 is missing ('-99') or where
# GDELT's usage differs. Every entry is checked by tests against the live code inventory.
FIPS_OVERRIDES = {
    "NO": "NO", "FR": "FR", "KV": "XK", "WE": "PS", "GZ": "PS", "TW": "TW",
    "OD": "SS", "RI": "RS", "RB": "RS", "MJ": "ME", "UV": "BF", "YI": "RS",
    "IS": "IL", "RE": "RE", "SV": "SJ", "MF": "YT", "GP": "GP", "MB": "MQ", "FG": "GF",
    "KT": "CX", "TL": "TK",
}
# Deliberately left UNMAPPED (not sovereign countries / ambiguous): OS, OC, PG (Spratly),
# HQ, LQ, ... They stay 'unlocated' rather than being guessed.
# GDELT/FIPS codes that are not countries at all (regions, oceans, sentinel values)
FIPS_NON_COUNTRY = {"", "-99"}

SNAP_MAX_DEG = 0.15  # snap a coastal point to a polygon at most this far away (~15 km)


def _norm(v) -> Optional[str]:
    if v is None:
        return None
    v = str(v).strip()
    return None if v in ("", "-99", "None", "nan") else v


# --------------------------------------------------------------------------
# precision inference from the ActionGeo FullName
# --------------------------------------------------------------------------
def _norm_name(s: str) -> str:
    return re.sub(r"\s*\(general\)\s*", "", str(s)).strip().lower()


def precision_from_fullname(fullname: Optional[str], admin1_names: Optional[set] = None) -> Optional[str]:
    """GDELT FullName is 'City, ADM1, Country' / 'ADM1, Country' / 'City, Country' / 'Country'.

    Proxy for ActionGeo_Type, validated against PostgreSQL `actiongeo_type` in the pilot report
    (the first version, parts-count only, agreed 89.8%: it called every 2-part name 'adm1', but
    'City, Country' is also 2 parts). With `admin1_names` (the country's admin-1 unit names), a
    2-part name is 'adm1' only if its first part IS an admin-1 unit, else 'city'.
    """
    if not fullname or not str(fullname).strip():
        return None
    parts = [p for p in str(fullname).split(",") if p.strip()]
    n = len(parts)
    if n <= 1:
        return "country"
    if n == 2:
        if admin1_names is None:
            return "adm1"
        return "adm1" if _norm_name(parts[0]) in admin1_names else "city"
    return "city"


def _is_integral(*vals) -> bool:
    return all(v is not None and float(v) == int(float(v)) for v in vals)


# --------------------------------------------------------------------------
# GeoIndex
# --------------------------------------------------------------------------
@dataclass
class Country:
    iso2: str
    iso3: str
    name: str
    fips: Optional[str]
    geom: object


@dataclass
class Admin1:
    code: str            # Natural Earth adm1_code, e.g. IND-3257
    iso2: str
    name: str
    iso_3166_2: Optional[str]
    fips: Optional[str]
    kind: Optional[str]
    geom: object
    names: frozenset = frozenset()


class GeoIndex:
    """Loads boundaries once; point-in-polygon through shapely STRtrees."""

    def __init__(self, countries_file: Path = COUNTRIES_FILE, admin1_file: Path = ADMIN1_FILE):
        if not countries_file.exists() or not admin1_file.exists():
            raise FileNotFoundError(
                f"Boundary data missing under {countries_file.parent}. "
                "Run: .venv/bin/python scripts/21_news_geo_data.py")
        self.countries: list[Country] = []
        with open(countries_file, encoding="utf-8") as fh:
            for f in json.load(fh)["features"]:
                p = f["properties"]
                iso2 = _norm(p.get("ISO_A2")) or _norm(p.get("ISO_A2_EH"))
                if iso2 and "-" in iso2:  # e.g. 'CN-TW'
                    iso2 = _norm(p.get("ISO_A2_EH"))
                if not iso2:
                    continue
                self.countries.append(Country(iso2, p.get("ADM0_A3"), p.get("NAME"),
                                              _norm(p.get("FIPS_10")), shape(f["geometry"])))
        self.admin1: list[Admin1] = []
        with open(admin1_file, encoding="utf-8") as fh:
            for f in json.load(fh)["features"]:
                p = f["properties"]
                iso2 = _norm(p.get("iso_a2"))
                if not iso2 or not f.get("geometry"):
                    continue
                nm = {_norm_name(x) for x in [p.get("name"), p.get("name_en"), p.get("gn_name"), p.get("woe_name")]
                      + str(p.get("name_alt") or "").split("|") if x}
                self.admin1.append(Admin1(p["adm1_code"], iso2, p.get("name"), _norm(p.get("iso_3166_2")),
                                          _norm(p.get("fips")), p.get("type_en"), shape(f["geometry"]),
                                          frozenset(nm)))
        self._c_tree = STRtree([c.geom for c in self.countries])
        self._a_tree = STRtree([a.geom for a in self.admin1])
        self._c_by_iso = {c.iso2: c for c in self.countries}
        self._a_by_code = {a.code: a for a in self.admin1}
        self._iso_countries_with_admin1 = {a.iso2 for a in self.admin1}
        self._adm1_names: dict[str, set] = {}
        for a in self.admin1:
            self._adm1_names.setdefault(a.iso2, set()).update(a.names)
        self._fips_to_iso = {c.fips: c.iso2 for c in self.countries if c.fips}
        self._fips_to_iso.update(FIPS_OVERRIDES)
        self._iso_to_fips = {}
        for f, i in self._fips_to_iso.items():
            self._iso_to_fips.setdefault(i, f)
        for c in self.countries:                      # prefer NE's own FIPS when present
            if c.fips:
                self._iso_to_fips[c.iso2] = c.fips

    # ----- codes -----------------------------------------------------------
    def fips_to_iso(self, code: Optional[str]) -> Optional[str]:
        code = _norm(code)
        return self._fips_to_iso.get(code) if code else None

    def iso_to_fips(self, iso2: str) -> Optional[str]:
        return self._iso_to_fips.get(iso2)

    def unmapped_codes(self, fips_codes: Iterable[str]) -> list[str]:
        return sorted({c for c in fips_codes if _norm(c) and c not in self._fips_to_iso})

    def country(self, iso2: str) -> Optional[Country]:
        return self._c_by_iso.get(iso2)

    def admin1_of(self, iso2: str) -> list[Admin1]:
        return sorted((a for a in self.admin1 if a.iso2 == iso2), key=lambda a: a.name or "")

    def admin1_by_code(self, code: str) -> Optional[Admin1]:
        return self._a_by_code.get(code)

    def has_admin1(self, iso2: str) -> bool:
        return iso2 in self._iso_countries_with_admin1

    # ----- point in polygon -------------------------------------------------
    def _hit(self, tree: STRtree, items: list, lon: float, lat: float):
        pt = Point(lon, lat)
        idx = tree.query(pt, predicate="intersects")
        if len(idx):
            return items[int(idx[0])], False
        near = tree.query_nearest(pt, max_distance=SNAP_MAX_DEG)
        if len(near):
            return items[int(near[0])], True
        return None, False

    def locate_point(self, lon: float, lat: float) -> dict:
        """-> {iso2, adm1_code, snapped}. Admin-1 is only looked up inside the matched country."""
        if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return {"iso2": None, "adm1_code": None, "snapped": False}
        c, snapped = self._hit(self._c_tree, self.countries, lon, lat)
        out = {"iso2": c.iso2 if c else None, "adm1_code": None, "snapped": snapped}
        if c and self.has_admin1(c.iso2):
            a, s2 = self._hit(self._a_tree, self.admin1, lon, lat)
            if a is not None and a.iso2 == c.iso2:
                out["adm1_code"] = a.code
                out["snapped"] = snapped or s2
        return out

    # ----- article level ----------------------------------------------------
    def article_location(self, doc: dict) -> dict:
        """Event location + separate mention countries for one normalised OS doc.

        Rules (all conservative; nothing is guessed):
          * no ActionGeo country and no point                -> 'unlocated'
          * ISO from FIPS; if a point exists its polygon country must AGREE with the FIPS
            country, otherwise the article is demoted to country precision with no point and
            flagged `geo_inconsistent` (border / disputed-area / geocoding error)
          * admin-1 only when precision is adm1/city AND the point agrees with the country
          * a country-centroid point (precision 'country') never fills a polygon/admin-1 AOI
        """
        fips = _norm(doc.get("action_country_fips"))
        iso_decl = self.fips_to_iso(fips) if fips else None
        lat, lon = doc.get("action_lat"), doc.get("action_lon")
        prec = precision_from_fullname(doc.get("action_location"),
                                       self._adm1_names.get(iso_decl or "", None) if (iso_decl and self.has_admin1(iso_decl)) else None)
        loc = {
            "ev_country_iso": None, "ev_adm1_code": None, "ev_place": doc.get("action_location"),
            "ev_lat": None, "ev_lon": None, "ev_precision": None,
            "geo_source": None, "geo_inconsistent": False, "geo_snapped": False,
            "loc_status": "unlocated", "country_code_unmapped": bool(fips and not iso_decl),
            "mention_countries_iso": sorted({i for i in (self.fips_to_iso(c) for c in
                                                         doc.get("mention_countries_fips") or []) if i}),
        }
        has_pt = lat is not None and lon is not None
        if not (fips or has_pt):
            return loc
        hit = self.locate_point(lon, lat) if has_pt else {"iso2": None, "adm1_code": None, "snapped": False}
        iso = iso_decl or hit["iso2"]
        if not iso:
            return loc  # a code we cannot map and no polygon hit: stay unlocated, flagged above
        loc.update(ev_country_iso=iso, geo_source="gdelt_action", loc_status="located",
                   ev_precision=prec or ("city" if has_pt and not _is_integral(lat, lon) else "country"))
        if has_pt and hit["iso2"] and iso_decl and hit["iso2"] != iso_decl:
            loc.update(geo_inconsistent=True, ev_precision="country")
            return loc
        if has_pt and loc["ev_precision"] != "country":
            loc.update(ev_lat=float(lat), ev_lon=float(lon), ev_adm1_code=hit["adm1_code"],
                       geo_snapped=hit["snapped"])
        elif has_pt:
            loc.update(ev_lat=float(lat), ev_lon=float(lon))  # kept for display only
        return loc


# --------------------------------------------------------------------------
# AOI
# --------------------------------------------------------------------------
@dataclass
class AOI:
    """An Area of Interest. `kind` in {country, adm1, polygon, bbox, radius}.

    mode 'event'     : the article's EVENT location is inside the AOI
    mode 'mentioned' : the AOI country is merely mentioned (country AOIs only in v1)
    Polygon-like AOIs require `min_precision` (default 'city'): country-centroid points are
    excluded because they say nothing about where inside the country the event happened.
    """
    kind: str
    key: str                       # iso2 | adm1_code | user label
    name: str
    mode: str = "event"
    geom: Optional[object] = None  # shapely geometry for polygon/bbox/radius
    min_precision: str = "city"
    adhoc: bool = False
    iso2: Optional[str] = None
    params: dict = field(default_factory=dict)

    @property
    def aoi_id(self) -> str:
        if self.kind in ("country", "adm1"):
            return f"{self.kind}:{self.key}:{self.mode}"
        return f"{self.kind}:{self.key}:{self.mode}"

    def validate(self) -> None:
        if self.kind not in ("country", "adm1", "polygon", "bbox", "radius"):
            raise ValueError(f"unknown AOI kind {self.kind}")
        if self.mode not in ("event", "mentioned"):
            raise ValueError(f"unknown AOI mode {self.mode}")
        if self.mode == "mentioned" and self.kind != "country":
            raise ValueError("'mentioned' AOIs are country-level only in v1")
        if self.min_precision not in PRECISION_RANK:
            raise ValueError("bad min_precision")
        if self.kind in ("polygon", "bbox", "radius") and self.geom is None:
            raise ValueError("geometry required")

    def to_dict(self) -> dict:
        return {"aoi_id": self.aoi_id, "kind": self.kind, "key": self.key, "name": self.name,
                "mode": self.mode, "min_precision": self.min_precision, "adhoc": self.adhoc,
                "iso2": self.iso2,
                "geometry": None if self.geom is None else shapely.geometry.mapping(self.geom)}


def country_aoi(geo: GeoIndex, iso2: str, mode: str = "event") -> AOI:
    c = geo.country(iso2)
    if c is None:
        raise KeyError(f"unknown country {iso2}")
    a = AOI("country", iso2, c.name, mode, iso2=iso2)
    a.validate()
    return a


def admin1_aoi(geo: GeoIndex, code: str) -> AOI:
    a1 = geo.admin1_by_code(code)
    if a1 is None:
        raise KeyError(f"unknown admin-1 {code}")
    a = AOI("adm1", code, f"{a1.name} ({a1.iso2})", "event", iso2=a1.iso2)
    a.validate()
    return a


def polygon_aoi(coords: list[list[float]], label: str, min_precision: str = "city") -> AOI:
    """`coords` = [[lon, lat], ...] ring. Ad-hoc by definition."""
    poly = Polygon(coords)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area == 0:
        raise ValueError("polygon is empty or degenerate")
    a = AOI("polygon", label, label, "event", geom=poly, min_precision=min_precision, adhoc=True)
    a.validate()
    return a


def bbox_aoi(west: float, south: float, east: float, north: float, label: str,
             min_precision: str = "city") -> AOI:
    if not (west < east and south < north):
        raise ValueError("bbox must satisfy west<east and south<north")
    a = AOI("bbox", label, label, "event", geom=box(west, south, east, north),
            min_precision=min_precision, adhoc=True)
    a.validate()
    return a


def radius_aoi(lon: float, lat: float, radius_km: float, label: str, min_precision: str = "city") -> AOI:
    """Geodesic-ish circle (equirectangular polygon, 64 vertices). Adequate below ~1,000 km."""
    if radius_km <= 0:
        raise ValueError("radius must be positive")
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    ring = [[lon + dlon * math.cos(t), lat + dlat * math.sin(t)]
            for t in np.linspace(0, 2 * math.pi, 65)]
    a = AOI("radius", label, label, "event", geom=Polygon(ring), min_precision=min_precision,
            adhoc=True, params={"lon": lon, "lat": lat, "radius_km": radius_km})
    a.validate()
    return a


def aoi_from_request(geo: GeoIndex, spec: dict) -> AOI:
    """Build an AOI from an API/JSON spec. Raises ValueError/KeyError on bad input."""
    kind = spec.get("kind")
    if kind == "country":
        return country_aoi(geo, spec["key"], spec.get("mode", "event"))
    if kind == "adm1":
        return admin1_aoi(geo, spec["key"])
    if kind == "polygon":
        return polygon_aoi(spec["coords"], spec.get("label", "custom polygon"),
                           spec.get("min_precision", "city"))
    if kind == "bbox":
        w, s, e, n = spec["bbox"]
        return bbox_aoi(w, s, e, n, spec.get("label", "custom bbox"), spec.get("min_precision", "city"))
    if kind == "radius":
        return radius_aoi(spec["lon"], spec["lat"], spec["radius_km"], spec.get("label", "custom radius"),
                          spec.get("min_precision", "city"))
    raise ValueError(f"unknown AOI kind {kind!r}")


def aoi_mask(aoi: AOI, geo_rows) -> np.ndarray:
    """Boolean mask over a DataFrame of `article_geo` rows. Single implementation shared by
    the pilot, backfill, incremental and ad-hoc paths."""
    import pandas as pd  # local: keep module import light
    n = len(geo_rows)
    if n == 0:
        return np.zeros(0, dtype=bool)
    if aoi.mode == "mentioned":
        return geo_rows["mention_countries_iso"].map(
            lambda l: aoi.iso2 in (l if l is not None else [])).to_numpy(dtype=bool)
    if aoi.kind == "country":
        return (geo_rows["ev_country_iso"] == aoi.iso2).to_numpy(dtype=bool)
    if aoi.kind == "adm1":
        return (geo_rows["ev_adm1_code"] == aoi.key).to_numpy(dtype=bool)
    rank = geo_rows["ev_precision"].map(PRECISION_RANK).fillna(0).to_numpy()
    ok = (rank >= PRECISION_RANK[aoi.min_precision]) & geo_rows["ev_lat"].notna().to_numpy() \
        & ~geo_rows["geo_inconsistent"].fillna(False).to_numpy(dtype=bool)
    out = np.zeros(n, dtype=bool)
    if ok.any():
        x = geo_rows["ev_lon"].to_numpy(dtype=float)
        y = geo_rows["ev_lat"].to_numpy(dtype=float)
        out[ok] = shapely.contains_xy(aoi.geom, x[ok], y[ok])
    return out
