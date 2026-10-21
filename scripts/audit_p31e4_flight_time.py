"""Read-only distance/duration audit of the isolated fictional flight snapshot."""

import json
import math
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from db import engine
from db.travel_models import airports, flights


def km_between(one, two):
    lat1, lon1 = map(math.radians, one)
    lat2, lon2 = map(math.radians, two)
    value = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(min(1, math.sqrt(value)))


def main():
    assert settings.POSTGRES_PORT == 5434 and not settings.DATABASE_URL
    with engine.connect() as db:
        locations = {row.code: row for row in db.execute(select(
            airports.c.code, airports.c.city, airports.c.latitude,
            airports.c.longitude))}
        rows = db.execute(select(flights.c.flight_no, flights.c.departure_airport,
                                 flights.c.arrival_airport, flights.c.departure_at,
                                 flights.c.arrival_at).where(
                                     flights.c.status == "Scheduled"))
        groups = Counter()
        examples = []
        routes = set()
        for flight in rows:
            origin = locations[flight.departure_airport]
            destination = locations[flight.arrival_airport]
            if any(value is None for value in (
                    origin.latitude, origin.longitude,
                    destination.latitude, destination.longitude)):
                groups["missing_coordinates"] += 1
                continue
            km = km_between((origin.latitude, origin.longitude),
                            (destination.latitude, destination.longitude))
            hours = (flight.arrival_at - flight.departure_at).total_seconds() / 3600
            groups["scheduled_with_coordinates"] += 1
            if km >= 5000:
                groups["long_distance_flights"] += 1
                routes.add((flight.departure_airport, flight.arrival_airport))
                if hours < 5:
                    groups["long_distance_under_five_hours"] += 1
                    if len(examples) < 8:
                        examples.append({"flight": flight.flight_no,
                                         "route": f"{origin.city} → {destination.city}",
                                         "distance_km": round(km),
                                         "flight_hours": round(hours, 2)})
    result = {**groups, "long_distance_routes": len(routes), "examples": examples}
    path = ROOT / "data/generated/p31e4-flight-time-audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
