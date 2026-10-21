"""Publisher registry: where an OUTLET is based (NOT where the news event happened).

Kept strictly separate from event location (news_geo.py). Used for display and, later, for an
explicit "published in" filter; it never feeds event location or trend metrics.

Status per entry (a registry that hides its own uncertainty is worse than none):
  reviewed       assigned from knowledge of the outlet by the reviewer named in `reviewed_by`
  auto_cctld     inferred from a country-code TLD (.co.uk -> GB). Reliable for national outlets, wrong for
                 the occasional offshore-registered site
  auto_pattern   inferred from a naming pattern (US local-broadcaster call signs); lower confidence
  unknown        no basis - shown as unknown, never guessed
  verified       a human (column `verified_by`) confirmed it
`PUBLISHER_COUNTRY_TRUSTED` = statuses allowed to be shown without an "unverified" marker.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Optional

from . import news_config as C

REGISTRY_FILE = C.DATA_DIR / "publisher_registry.csv"
PUBLISHER_COUNTRY_TRUSTED = {"verified"}

# generic / non-geographic TLDs that look like ccTLDs
NON_GEO_TLD = {"tv", "co", "io", "me", "ai", "fm", "ly", "cc", "ws", "to", "gg", "gl", "sh", "ac", "am", "dj", "la", "nu", "vc"}
TLD_TO_ISO = {"uk": "GB", "su": "RU"}

US_CALLSIGN = re.compile(r"^(www\.)?[kw][a-z0-9]{2,5}(tv)?\.(com|org)$")


def normalise_domain(d: Optional[str]) -> Optional[str]:
    if not d:
        return None
    d = d.strip().lower().split(":")[0]
    return d[4:] if d.startswith("www.") else d


def tld_country(domain: str) -> Optional[str]:
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) != 2 or tld in NON_GEO_TLD:
        return None
    return TLD_TO_ISO.get(tld, tld.upper())


# Outlets on generic TLDs, assigned from knowledge of the outlet (the reviewer should spot-check).
CURATED = {
    # United States
    **{d: "US" for d in """aol.com yahoo.com finance.yahoo.com foxnews.com cnbc.com cbsnews.com nbcnews.com abcnews.com
        businessinsider.com forbes.com fortune.com newsweek.com latimes.com chicagotribune.com nydailynews.com washingtonpost.com
        nytimes.com usatoday.com breitbart.com townhall.com rawstory.com zerohedge.com thedailybeast.com hollywoodreporter.com
        variety.com deadline.com tmz.com billboard.com mashable.com gizmodo.com screenrant.com comicbook.com gamerant.com ign.com
        fool.com seekingalpha.com insidermonkey.com 247wallst.com themarketsdaily.com dailypolitical.com tickerreport.com si.com
        usmagazine.com eonline.com yardbarker.com bleedingcool.com movieweb.com radaronline.com americansongwriter.com
        medicaldaily.com thebiglead.com wwd.com prnewswire.com newsday.com theepochtimes.com mercurynews.com
        sandiegouniontribune.com bostonherald.com orlandosentinel.com sun-sentinel.com courant.com eastbaytimes.com ocregister.com
        dailynews.com twincities.com mcall.com inquirer.com pressdemocrat.com lasvegassun.com bangordailynews.com
        clickondetroit.com clickorlando.com click2houston.com news4jax.com mynewsla.com investinglive.com
        dailypress.com citizensvoice.com thetimes-tribune.com greeleytribune.com reporterherald.com pilotonline.com
        sitkasentinel.com mymotherlode.com foxbaltimore.com mynbc5.com fox5atlanta.com news4sanantonio.com local21news.com
        news3lv.com okcfox.com abc3340.com turnto10.com fingerlakes1.com gulfcoastnewsnow.com""".split()},
    # United Kingdom (generic TLD)
    **{d: "GB" for d in """dailymail.com theguardian.com bbc.com bbc.co.uk independent.co.uk mirror.co.uk express.co.uk metro.co.uk
        standard.co.uk lbc.co.uk techradar.com hellomagazine.com heraldscotland.com thenational.scot herefordtimes.com bicesteradvertiser.net""".split()},
    # India (generic TLD)
    **{d: "IN" for d in """economictimes.indiatimes.com timesofindia.indiatimes.com thehindu.com hindustantimes.com
        thehindubusinessline.com livemint.com business-standard.com tribuneindia.com deccanchronicle.com newindianexpress.com
        prokerala.com siasat.com rediff.com jagranjosh.com organiser.org newstodaynet.com mangalorean.com pragativadi.com
        nagalandpost.com outlookindia.com orissapost.com sentinelassam.com adgully.com dailyexcelsior.com newsx.com""".split()},
    # Rest of world
    **{"english.news.cn": "CN", "news.cgtn.com": "CN", "en.people.cn": "CN", "scmp.com": "HK", "channelnewsasia.com": "SG",
       "straitstimes.com": "SG", "asiaone.com": "SG", "koreaherald.com": "KR", "taipeitimes.com": "TW", "manilatimes.net": "PH",
       "philstar.com": "PH", "bworldonline.com": "PH", "freemalaysiatoday.com": "MY", "tribune.com.pk": "PK",
       "thenews.com.pk": "PK", "arynews.tv": "PK", "geo.tv": "PK", "dailysabah.com": "TR", "aljazeera.com": "QA",
       "arabnews.com": "SA", "gulfnews.com": "AE", "jpost.com": "IL", "trend.az": "AZ", "azernews.az": "AZ", "en.apa.az": "AZ",
       "en.interfax.com.ua": "UA", "kyivpost.com": "UA", "cyprus-mail.com": "CY", "euronews.com": "FR",
       "riotimesonline.com": "BR", "el-balad.com": "EG", "sana.sy": "SY", "en.tempo.co": "ID", "en.antaranews.com": "ID",
       "bdnews24.com": "BD", "lankanewspapers.com": "LK", "punchng.com": "NG", "vanguardngr.com": "NG", "dailytrust.com": "NG",
       "thenationonlineng.net": "NG", "tribuneonlineng.com": "NG", "thesouthafrican.com": "ZA", "jamaicaobserver.com": "JM",
       "jamaica-gleaner.com": "JM", "dominicanrepublicpost.com": "DO", "theglobeandmail.com": "CA", "cbc.ca": "CA",
       "winnipegfreepress.com": "CA", "globalnews.ca": "CA", "cp24.com": "CA", "tsn.ca": "CA", "boredpanda.com": "LT",
       "abc.net.au": "AU", "afr.com": "AU", "news.cn": "CN", "chinadaily.com.cn": "CN", "thejournal.ie": "IE",
       "irishtimes.com": "IE", "irishexaminer.com": "IE"},
}
# deliberately NOT assigned (multinational / ambiguous ownership): aawsat, ianslive wire reposts, aggregators.


def classify(domain: str) -> tuple[Optional[str], str, str]:
    """-> (country_iso2 | None, status, basis)."""
    d = normalise_domain(domain)
    if not d:
        return None, "unknown", "no domain"
    if d in CURATED:
        return CURATED[d], "reviewed", "outlet knowledge"
    parts = d.split(".")
    for k in range(len(parts) - 1):       # 'news.cgtn.com' -> also try 'cgtn.com'
        tail = ".".join(parts[k:])
        if tail in CURATED:
            return CURATED[tail], "reviewed", "outlet knowledge (parent domain)"
    c = tld_country(d)
    if c:
        return c, "auto_cctld", f".{d.rsplit('.', 1)[-1]} ccTLD"
    if US_CALLSIGN.match("www." + d):
        return "US", "auto_pattern", "US broadcaster call-sign pattern"
    return None, "unknown", "generic TLD, not in curated list"


def load_registry(path: Path = REGISTRY_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["domain"]: r for r in csv.DictReader(fh)}


def publisher_country(domain: Optional[str], registry: Optional[dict] = None) -> dict:
    """{'country': iso2|None, 'status': ..., 'trusted': bool}. Falls back to `classify` for domains not in the file."""
    d = normalise_domain(domain)
    reg = registry if registry is not None else load_registry()
    if d in reg:
        r = reg[d]
        c, s = (r["country_iso2"] or None), r["status"]
        if r.get("verified_by"):
            s = "verified"
    else:
        c, s, _ = classify(d or "")
    return {"country": c, "status": s, "trusted": s in PUBLISHER_COUNTRY_TRUSTED}
