"""Only reviewed fixture airports can supply a recommendation's country context."""

import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from db.travel_models import airports, flight_offers, flights


_SOURCE = Path(__file__).resolve().parent.parent / "data/fixtures/recommendation_geography.json"


@lru_cache(maxsize=1)
def airport_geography() -> dict[str, dict]:
    return json.loads(_SOURCE.read_text(encoding="utf-8"))["airports"]


def country_for_fixture_city(city: str) -> dict | None:
    countries = {(entry["country_code"], entry["country"])
                 for entry in airport_geography().values() if entry["city"] == city}
    if len(countries) != 1:
        return None
    code, name = countries.pop()
    return {"code": code, "name": name}


def country_for_reachable_city(db, city: str) -> dict | None:
    rows = db.execute(
        select(airports.c.code, airports.c.city, airports.c.timezone)
        .join(flights, flights.c.arrival_airport == airports.c.code)
        .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
        .where(airports.c.city == city, flights.c.status == "Scheduled")
        .distinct()
    ).all()
    countries = set()
    for code, db_city, timezone in rows:
        entry = airport_geography().get(code)
        if (entry is None or entry["city"] != db_city
                or entry["timezone"] != timezone):
            return None
        countries.add((entry["country_code"], entry["country"]))
    if len(countries) != 1:
        return None
    code, name = countries.pop()
    return {"code": code, "name": name}
