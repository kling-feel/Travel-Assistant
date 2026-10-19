"""Versioned, manually reviewed public place facts for reachable cities."""

import csv
import ipaddress
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from services.recommendation_geography import country_for_fixture_city

_ROOT = Path(__file__).resolve().parent.parent / "data/fixtures"
_SOURCE = _ROOT / "reviewed_places_v2.psv"
_PREVIOUS_SOURCE = _ROOT / "reviewed_places_v1.psv"
_NETWORK = _ROOT / "network_source.sqlite"
_FIELDS = ("id", "city", "country_code", "name", "aliases", "category",
           "intro", "url", "source_class", "reviewed_on")
_CATEGORIES = {"history", "culture", "nature", "architecture", "family", "food"}
_SOURCE_CLASSES = {"venue_official", "city_institution", "encyclopedia"}
_SOURCE_HOSTS = {
    "venue_official": {"colosseo.it", "www.staedelmuseum.de",
                       "www.rijksmuseum.nl", "www.theacropolismuseum.gr",
                       "denkongeligesamling.dk", "www.jinshasitemuseum.com",
                       "nmk.go.ke", "www.carnavalet.paris.fr",
                       "www.nationalgallery.org.uk", "www.palmengarten.de",
                       "nationalmuseet.dk", "www.munch.no", "www.operaen.no",
                       "www.shanghaimuseum.cn", "hk.history.museum",
                       "www.museum.go.kr", "www.nationalgallery.sg",
                       "www.gardensbythebay.com.sg", "www.metmuseum.org",
                       "www.centralparknyc.org", "www.getty.edu",
                       "ateneum.fi", "dbg.org", "dia.org", "lnmm.gov.lv",
                       "mia.org.qa", "mucem.org", "neonmuseum.org",
                       "newarkmuseumart.org", "sagradafamilia.org",
                       "walkerart.org", "www.agsa.sa.gov.au",
                       "www.bishopmuseum.org", "www.denverartmuseum.org",
                       "www.deutsches-museum.de", "www.georgiaaquarium.org",
                       "www.kunstsammlung.de", "www.mfah.org",
                       "www.miniatur-wunderland.com", "www.museum.qld.gov.au",
                       "www.nagoyajo.city.nagoya.jp", "www.nationalgallery.ie",
                       "www.nms.ac.uk", "www.oceanario.pt", "www.osc.org",
                       "www.pamm.org", "www.shwedagonpagoda.com",
                       "www.spaceneedle.com", "www.vasamuseet.se"},
    "city_institution": {"www.basel.com", "english.visitkorea.or.kr",
                         "www.dm.gov.ae", "english.beijing.gov.cn",
                         "www.gotokyo.org", "www.tourismthailand.org",
                         "griffithobservatory.lacity.gov", "sfrecpark.org",
                         "www.choosechicago.com",
                         "centrohistorico.gob.sv", "www.nmt.go.tz",
                         "www.phuket.go.th", "www.service-public.pf"},
    "encyclopedia": {"en.wikipedia.org"},
}
_UNSAFE = re.compile(r"[\r\n<>\[\]{}]|忽略|系统提示|开发者|密钥|下单|付款|"
                     r"ignore|system prompt|developer|api key|password|token", re.I)


def _safe_url(value: str) -> bool:
    if len(value) > 500 or any(c.isspace() or c in "[]()<>\\" for c in value):
        return False
    host = None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".local", ".internal")):
            return False
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return bool(host and "." in host and re.fullmatch(r"[A-Za-z0-9.-]+", host))


@lru_cache(maxsize=1)
def reviewed_places() -> tuple[dict, ...]:
    """Reject the entire catalog if any entry is malformed or geographically wrong."""
    with _PREVIOUS_SOURCE.open(encoding="utf-8", newline="") as previous:
        old_rows = list(csv.DictReader(previous, delimiter="|"))
    with _SOURCE.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="|")
        if tuple(reader.fieldnames or ()) != _FIELDS:
            raise ValueError("invalid reviewed-place header")
        rows = list(reader)
    if rows[:len(old_rows)] != old_rows or len(old_rows) != 60:
        raise ValueError("reviewed-place v1 entries changed")
    ids, urls, names = set(), set(), set()
    places = []
    for row in rows:
        if None in row or any(not isinstance(row[field], str) for field in _FIELDS):
            raise ValueError("invalid reviewed-place row")
        place_id, city, code, name = (row[field] for field in
                                      ("id", "city", "country_code", "name"))
        country = country_for_fixture_city(city)
        aliases = tuple(alias.strip() for alias in row["aliases"].split(";") if alias.strip())
        try:
            reviewed_on = date.fromisoformat(row["reviewed_on"])
        except ValueError as exc:
            raise ValueError("invalid reviewed-place date") from exc
        if (not re.fullmatch(r"[a-z0-9-]{4,80}", place_id)
                or place_id in ids or row["url"] in urls
                or (city, name.casefold()) in names
                or not country or country["code"] != code
                or not 2 <= len(name) <= 100 or not 8 <= len(row["intro"]) <= 160
                or any(_UNSAFE.search(value) for value in (name, *aliases, row["intro"]))
                or row["category"] not in _CATEGORIES
                or row["source_class"] not in _SOURCE_CLASSES
                or not _safe_url(row["url"])
                or urlsplit(row["url"]).hostname not in
                _SOURCE_HOSTS[row["source_class"]]
                or reviewed_on > date.today()):
            raise ValueError(f"invalid reviewed place: {place_id}")
        ids.add(place_id)
        urls.add(row["url"])
        names.add((city, name.casefold()))
        places.append({"id": place_id, "city": city, "country_code": code,
                       "name": name, "aliases": aliases,
                       "category": row["category"], "intro": row["intro"],
                       "url": row["url"], "source_class": row["source_class"],
                       "reviewed_on": reviewed_on.isoformat()})
    with sqlite3.connect(f"file:{_NETWORK.as_posix()}?mode=ro", uri=True) as db:
        reachable = {city for (city,) in db.execute(
            "SELECT DISTINCT a.city FROM flights f "
            "JOIN airports_data a ON a.airport_code=f.arrival_airport "
            "WHERE f.status='Scheduled'")}
    if (len(places) != 190 or len(reachable) != 95
            or {place["city"] for place in places} != reachable):
        raise ValueError("incomplete reviewed-place catalog")
    if any(count != 2 for count in Counter(place["city"] for place in places).values()):
        raise ValueError("reviewed-place city coverage is uneven")
    return tuple(places)


def places_for_city(city: str, country_code: str,
                    category: str | None = None,
                    on: date | None = None) -> list[dict]:
    cutoff = (on or date.today()) - timedelta(days=180)
    return [place for place in reviewed_places()
            if place["city"] == city and place["country_code"] == country_code
            and date.fromisoformat(place["reviewed_on"]) >= cutoff
            and (category is None or place["category"] == category)]
