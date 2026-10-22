"""Publisher registry: publisher location is separate from event location and never guessed."""
import pytest

from news_testutil import doc  # noqa: F401
from lib import news_publishers as PB


@pytest.mark.parametrize("domain,country,status", [
    ("www.thehindu.com", "IN", "reviewed"), ("economictimes.indiatimes.com", "IN", "reviewed"),
    ("news.cgtn.com", "CN", "reviewed"), ("www.bbc.co.uk", "GB", "reviewed"), ("www.nwemail.co.uk", "GB", "auto_cctld"),
    ("www.sbs.com.au", "AU", "auto_cctld"), ("www.asiaone.com:443", "SG", "reviewed"), ("kcra.com", "US", "auto_pattern"),
    ("example-unknown-outlet.com", None, "unknown"), ("something.tv", None, "unknown"), ("", None, "unknown")])
def test_classify(domain, country, status):
    c, s, _basis = PB.classify(domain)
    assert (c, s) == (country, status)


def test_unknown_is_never_guessed():
    assert PB.classify("randomblog.com")[0] is None


def test_only_human_verified_is_trusted():
    reg = {"x.com": {"country_iso2": "IN", "status": "reviewed", "verified_by": ""},
           "y.com": {"country_iso2": "IN", "status": "reviewed", "verified_by": "binduja"}}
    assert not PB.publisher_country("x.com", reg)["trusted"]
    assert PB.publisher_country("y.com", reg) == {"country": "IN", "status": "verified", "trusted": True}


def test_domain_normalisation_folds_port_and_www():
    assert PB.normalise_domain("WWW.AsiaOne.com:443") == "asiaone.com"


def test_publisher_country_is_not_part_of_event_location():
    """An Indian outlet reporting an event in China: publisher IN, event CN - two separate attributes."""
    from lib import news_geo as G
    if not G.COUNTRIES_FILE.exists():
        pytest.skip("boundary data missing")
    d = doc(1, fips="CH", loc="Beijing, Beijing, China", lat=39.9, lon=116.4, domain="thehindu.com")
    loc = G.GeoIndex().article_location(d)
    pub = PB.publisher_country(d["source_domain"])
    assert (loc["ev_country_iso"], pub["country"]) == ("CN", "IN")
    assert "source_domain" not in loc and "publisher" not in " ".join(loc)       # never merged into one record
