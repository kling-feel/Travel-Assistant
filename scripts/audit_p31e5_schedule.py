"""Read-only check of a generated fictional schedule against its fixture."""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_assistant.core.config import settings
from travel_assistant.db import engine
from travel_assistant.db.travel_models import demo_state, flight_offers, flights, hotel_products, hotels
from scripts.import_network_data import FIXTURE, prepare_snapshot
from travel_assistant.services.flight_schedule import FLIGHT_SCHEDULE_RULE, great_circle_km


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--formal-read-only", action="store_true",
                        help="Explicitly audit the default demo DB after its approved rebuild")
    args = parser.parse_args()
    expected_port = 5433 if args.formal_read_only else 5434
    if settings.POSTGRES_PORT != expected_port or settings.DATABASE_URL:
        raise RuntimeError(f"Audit requires the selected local DB on port {expected_port}")
    with engine.connect() as db:
        as_of = db.execute(select(demo_state.c.as_of)).scalar_one()
        if as_of != args.as_of:
            raise RuntimeError("Isolated demo date differs from --as-of")
        actual_flights = {row.id: row for row in db.execute(select(flights))}
        actual_offers = {row.flight_id: row for row in db.execute(select(flight_offers))}
        hotel_supply = {}
        for row in db.execute(select(hotels.c.nearby_airport,
                                     hotel_products.c.available_from,
                                     hotel_products.c.available_until).join(
                                         hotel_products,
                                         hotel_products.c.hotel_id == hotels.c.id)):
            hotel_supply.setdefault(row.nearby_airport, []).append(row)
    airports, expected_flights, expected_offers, report = prepare_snapshot(
        FIXTURE, as_of)
    airport_map = {row["code"]: row for row in airports}
    if set(actual_flights) != {row["id"] for row in expected_flights}:
        raise AssertionError("Imported flight IDs differ from the fixture")
    if set(actual_offers) != {row["flight_id"] for row in expected_offers}:
        raise AssertionError("Imported offer IDs differ from the fixture")
    long_total = long_under_five = 0
    for expected in expected_flights:
        actual = actual_flights[expected["id"]]
        for field in ("flight_no", "departure_at", "arrival_at",
                      "actual_departure_at", "actual_arrival_at",
                      "departure_airport", "arrival_airport", "status"):
            if getattr(actual, field) != expected[field]:
                raise AssertionError(f"Flight {expected['id']} differs in {field}")
        if expected["status"] != "Scheduled":
            continue
        distance = great_circle_km(airport_map[expected["departure_airport"]],
                                   airport_map[expected["arrival_airport"]])
        if distance >= 5000:
            long_total += 1
            long_under_five += (actual.arrival_at - actual.departure_at
                                < timedelta(hours=5))
    for expected in expected_offers:
        actual = actual_offers[expected["flight_id"]]
        for field in ("amount", "version", "rule_id"):
            if getattr(actual, field) != expected[field]:
                raise AssertionError(f"Offer {expected['flight_id']} differs in {field}")
    landing_unavailable = 0
    latest_arrival_offset = 0
    for flight_id in actual_offers:
        flight = actual_flights[flight_id]
        destination = airport_map[flight.arrival_airport]
        landing_day = flight.arrival_at.astimezone(
            ZoneInfo(destination["timezone"])).date()
        latest_arrival_offset = max(latest_arrival_offset,
                                    (landing_day - as_of).days)
        if not any(room.available_from <= landing_day
                   and room.available_until >= landing_day + timedelta(days=2)
                   for room in hotel_supply.get(flight.arrival_airport, ())):
            landing_unavailable += 1
    flight = actual_flights[3831]
    result = {
        "date": as_of.isoformat(), "rule": FLIGHT_SCHEDULE_RULE,
        "flights": len(actual_flights), "offers": len(actual_offers),
        "routes": report["scheduled_routes"],
        "scheduled_long_distance": long_total,
        "long_distance_under_five_hours": long_under_five,
        "latest_landing_day_offset": latest_arrival_offset,
        "offered_flights_without_two_night_landing_hotel": landing_unavailable,
        "flight_3831_departure_shanghai": flight.departure_at.astimezone(
            ZoneInfo("Asia/Shanghai")).isoformat(),
        "flight_3831_arrival_london": flight.arrival_at.astimezone(
            ZoneInfo("Europe/London")).isoformat(),
        "flight_3831_hours": round((flight.arrival_at - flight.departure_at)
                                   .total_seconds() / 3600, 2),
    }
    if long_under_five or landing_unavailable:
        raise AssertionError("Schedule or landing hotel supply failed full audit")
    output = ROOT / ("data/generated/p31e6-formal-schedule-audit.json"
                     if args.formal_read_only
                     else "data/generated/p31e5-schedule-audit.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
