"""Build auditable airport geography from the public fixture and IANA zone.tab."""

import json
import sqlite3
from importlib.resources import files
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "data/fixtures/network_source.sqlite"
OUTPUT = ROOT / "data/fixtures/recommendation_geography.json"
COUNTRY_NAMES = {
    "AE": "United Arab Emirates", "AO": "Angola", "AT": "Austria",
    "AU": "Australia", "BD": "Bangladesh", "BE": "Belgium", "BR": "Brazil",
    "CA": "Canada", "CH": "Switzerland", "CN": "China", "CO": "Colombia",
    "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "ES": "Spain",
    "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GR": "Greece", "HK": "Hong Kong", "ID": "Indonesia", "IE": "Ireland",
    "IN": "India", "IT": "Italy", "JP": "Japan", "KE": "Kenya",
    "KH": "Cambodia", "KR": "South Korea", "KW": "Kuwait",
    "LV": "Latvia", "MM": "Myanmar", "MX": "Mexico", "MY": "Malaysia",
    "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand",
    "PE": "Peru", "PF": "French Polynesia", "PH": "Philippines",
    "PL": "Poland", "PT": "Portugal", "QA": "Qatar", "RU": "Russia",
    "SC": "Seychelles", "SE": "Sweden", "SG": "Singapore",
    "SN": "Senegal", "SV": "El Salvador", "TH": "Thailand",
    "TR": "Turkey", "TW": "Taiwan", "TZ": "Tanzania",
    "US": "United States", "VE": "Venezuela", "ZA": "South Africa",
    "ZW": "Zimbabwe",
}
ZONE_ALIASES = {"America/Montreal": "CA"}


def build() -> dict:
    from tzdata import __version__ as tzdata_version

    zone_codes = {}
    zone_tab = files("tzdata").joinpath("zoneinfo/zone.tab")
    for line in zone_tab.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            country, _coordinates, zone, *_ = line.split("\t")
            zone_codes[zone] = country
    with sqlite3.connect(FIXTURE) as db:
        airports = db.execute(
            "SELECT airport_code, city, timezone FROM airports_data ORDER BY airport_code"
        ).fetchall()
    if len(airports) != 115:
        raise ValueError("airport fixture count changed; review geography first")
    entries = {}
    for code, city, zone in airports:
        country = zone_codes.get(zone, ZONE_ALIASES.get(zone))
        if not country or country not in COUNTRY_NAMES:
            raise ValueError(f"unmapped airport {code}/{zone}")
        entries[code] = {"city": city, "timezone": zone,
                         "country_code": country, "country": COUNTRY_NAMES[country]}
    by_city = {}
    for entry in entries.values():
        by_city.setdefault(entry["city"], set()).add(entry["country_code"])
    ambiguous = {city: sorted(codes) for city, codes in by_city.items() if len(codes) > 1}
    if ambiguous:
        raise ValueError(f"cross-country fixture cities require review: {ambiguous}")
    return {"source": "IANA zone.tab via tzdata", "tzdata_version": tzdata_version,
            "airport_count": len(entries), "city_count": len(by_city),
            "airports": entries}


def main() -> None:
    content = json.dumps(build(), ensure_ascii=False, indent=2) + "\n"
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTPUT.name}: 115 airports, 106 cities")


if __name__ == "__main__":
    main()
