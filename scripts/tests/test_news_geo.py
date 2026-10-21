"""Geography: FIPS->ISO, precision, event vs mention separation, AOI membership. Uses the local
Natural Earth files (skipped if absent). No network."""
import numpy as np
import pandas as pd
import pytest

from news_testutil import doc
from lib import news_geo as G

pytestmark = pytest.mark.skipif(not G.COUNTRIES_FILE.exists(), reason="run scripts/21_news_geo_data.py --download")


@pytest.fixture(scope="module")
def geo():
    return G.GeoIndex()


def test_fips_is_not_iso(geo):
    # the classic traps: GDELT uses FIPS 10-4
    exp = {"UK": "GB", "AS": "AU", "EI": "IE", "CH": "CN", "NI": "NG", "BG": "BD", "IS": "IL", "RS": "RU",
           "GM": "DE", "SP": "ES", "UP": "UA", "CE": "LK", "BM": "MM", "PS": "PW", "WE": "PS"}
    for f, i in exp.items():
        assert geo.fips_to_iso(f) == i, f
    assert geo.fips_to_iso("") is None and geo.fips_to_iso("-99") is None and geo.fips_to_iso("ZZ") is None
    assert geo.iso_to_fips("BD") == "BG" and geo.iso_to_fips("IN") == "IN"


@pytest.mark.parametrize("name,prec", [("India", "country"), ("Maharashtra, India", "adm1"),
                                       ("Mumbai, Maharashtra, India", "city"), ("", None), (None, None)])
def test_precision_from_fullname(name, prec):
    assert G.precision_from_fullname(name) == prec


def test_point_in_polygon(geo):
    assert geo.locate_point(77.21, 28.61)["iso2"] == "IN"
    assert geo.locate_point(90.41, 23.81)["iso2"] == "BD"
    r = geo.locate_point(72.88, 19.08)
    assert r["iso2"] == "IN" and r["adm1_code"] and geo.admin1_by_code(r["adm1_code"]).name == "Maharashtra"
    assert geo.locate_point(-30, -40)["iso2"] is None      # open ocean: no polygon within snap distance
    assert geo.locate_point(None, 5)["iso2"] is None


def test_city_level_event_gets_adm1(geo):
    loc = geo.article_location(doc(1, fips="IN", loc="Mumbai, Maharashtra, India", lat=19.07, lon=72.88))
    assert loc["loc_status"] == "located" and loc["ev_country_iso"] == "IN"
    assert loc["ev_precision"] == "city" and loc["ev_adm1_code"] is not None


def test_country_centroid_never_fills_adm1(geo):
    # GDELT puts country-level events on the country centroid (India = 20N 77E)
    loc = geo.article_location(doc(2, fips="IN", loc="India", lat=20.0, lon=77.0))
    assert loc["ev_country_iso"] == "IN" and loc["ev_precision"] == "country"
    assert loc["ev_adm1_code"] is None


def test_unlocated_when_no_geo(geo):
    loc = geo.article_location(doc(3, fips=None, loc=None, lat=None, lon=None, mentions=("IN",)))
    assert loc["loc_status"] == "unlocated" and loc["ev_country_iso"] is None
    assert loc["mention_countries_iso"] == ["IN"]          # mention is kept, SEPARATELY


def test_event_and_mention_are_separate(geo):
    loc = geo.article_location(doc(4, fips="CH", loc="Beijing, Beijing, China", lat=39.9, lon=116.4,
                                   mentions=("IN", "CH")))
    assert loc["ev_country_iso"] == "CN"                   # event is in China ...
    assert "IN" in loc["mention_countries_iso"]            # ... India is only mentioned


def test_inconsistent_code_and_point_is_demoted(geo):
    # FIPS says India but the point is in Bangladesh -> do not trust the point
    loc = geo.article_location(doc(5, fips="IN", loc="Dhaka, Dhaka, Bangladesh", lat=23.81, lon=90.41))
    assert loc["geo_inconsistent"] and loc["ev_precision"] == "country" and loc["ev_adm1_code"] is None


def test_unmapped_country_code_is_flagged_not_guessed(geo):
    loc = geo.article_location(doc(6, fips="OS", loc="Somewhere", lat=None, lon=None))
    assert loc["loc_status"] == "unlocated" and loc["country_code_unmapped"]


def _rows(geo):
    docs = [doc(1, fips="IN", loc="Mumbai, Maharashtra, India", lat=19.07, lon=72.88),
            doc(2, fips="IN", loc="India", lat=20.0, lon=77.0, mentions=()),
            doc(3, fips="BG", loc="Dhaka, Dhaka, Bangladesh", lat=23.81, lon=90.41, mentions=("BG", "IN")),
            doc(4, fips=None, loc=None, lat=None, lon=None, mentions=("IN",)),
            doc(5, fips="CH", loc="Beijing, Beijing, China", lat=39.9, lon=116.4, mentions=("IN", "CH"))]
    return pd.DataFrame([geo.article_location(d) for d in docs]).assign(i=[1, 2, 3, 4, 5])


def test_aoi_country_event_vs_mentioned(geo):
    df = _rows(geo)
    ev = G.aoi_mask(G.country_aoi(geo, "IN"), df)
    me = G.aoi_mask(G.country_aoi(geo, "IN", "mentioned"), df)
    assert list(df.loc[ev, "i"]) == [1, 2]            # event location in India
    assert list(df.loc[me, "i"]) == [1, 3, 4, 5]      # India merely mentioned (incl. an unlocated article)
    assert set(df.loc[ev, "i"]) != set(df.loc[me, "i"])


def test_aoi_admin1(geo):
    df = _rows(geo)
    mh = next(a for a in geo.admin1_of("IN") if a.name == "Maharashtra")
    assert list(df.loc[G.aoi_mask(G.admin1_aoi(geo, mh.code), df), "i"]) == [1]


def test_polygon_aoi_excludes_country_level_points(geo):
    df = _rows(geo)
    # a box around central India that contains the India country centroid (20N,77E) AND Mumbai? no: Mumbai is west
    box = G.bbox_aoi(70, 15, 80, 25, "central-west India")
    got = set(df.loc[G.aoi_mask(box, df), "i"])
    assert 1 in got                       # Mumbai point is inside
    assert 2 not in got                   # centroid point is inside the box but is country-precision
    permissive = G.bbox_aoi(70, 15, 80, 25, "x", min_precision="country")
    assert 2 in set(df.loc[G.aoi_mask(permissive, df), "i"])   # unless the user explicitly opts in


def test_radius_aoi(geo):
    df = _rows(geo)
    near_mumbai = G.radius_aoi(72.88, 19.08, 50, "Mumbai 50km")
    assert list(df.loc[G.aoi_mask(near_mumbai, df), "i"]) == [1]


@pytest.mark.parametrize("spec", [
    {"kind": "mentioned"}, {"kind": "bbox", "bbox": [10, 0, 5, 1]}, {"kind": "radius", "lon": 0, "lat": 0, "radius_km": -1},
    {"kind": "polygon", "coords": [[0, 0], [1, 1], [2, 2]]}, {"kind": "country", "key": "XX"}])
def test_bad_aoi_specs_rejected(geo, spec):
    with pytest.raises((ValueError, KeyError)):
        G.aoi_from_request(geo, spec)


def test_mentioned_mode_is_country_only(geo):
    a = G.AOI("adm1", "IND-1", "x", "mentioned")
    with pytest.raises(ValueError):
        a.validate()


def test_boundary_inventory(geo):
    assert len(geo.admin1_of("IN")) == 36 and len(geo.admin1_of("BD")) == 7
    assert geo.country("IN").name == "India"


@pytest.mark.parametrize("fullname,iso,prec", [
    ("India", "IN", "country"), ("Maharashtra, India", "IN", "adm1"), ("Pune, India", "IN", "city"),
    ("Mumbai, Maharashtra, India", "IN", "city"), ("Dhaka, Dhaka, Bangladesh", "BD", "city")])
def test_precision_uses_admin1_names_to_split_two_part_names(geo, fullname, iso, prec):
    # 'City, Country' and 'ADM1, Country' are both two parts; the pilot measured 39/334 city points mislabelled adm1
    names = geo._adm1_names[iso]
    assert G.precision_from_fullname(fullname, names) == prec
