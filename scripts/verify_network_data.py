"""Read-only checks for an imported fictional network snapshot."""

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import sys

from fastapi.testclient import TestClient
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (airports, demo_state, flight_offers, flight_orders,
                              flights, hotel_orders, hotel_products, hotels,
                              policy_documents, rental_products, travelers)
from scripts.import_network_data import FIXTURE
from main import Server
from travel_assistant.services.travel import find_flights


def main():
    cross_zone_date = None
    with engine.connect() as db:
        state = db.execute(select(demo_state)).mappings().one()
        as_of = state["as_of"]
        assert state["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        counts = {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
                  for table in (airports, flights, flight_offers, travelers,
                                flight_orders, hotel_orders, hotels,
                                hotel_products, rental_products, policy_documents)}
        assert counts == {"airports": 115, "flights": 33121,
                          "flight_offers": 15374, "travelers": 2,
                          "flight_orders": 2, "hotel_orders": 2,
                          "hotels": 312, "hotel_products": 312,
                          "rental_products": 208, "policy_documents": 10}, counts
        assert db.execute(select(func.count(func.distinct(hotels.c.nearby_airport)))).scalar_one() == 104
        assert db.execute(select(func.count(func.distinct(hotels.c.city)))).scalar_one() == 95
        assert db.execute(select(func.count(func.distinct(rental_products.c.airport_code)))).scalar_one() == 104
        assert db.execute(select(func.count(func.distinct(rental_products.c.city)))).scalar_one() == 95
        assert db.execute(select(func.count()).select_from(flights).where(
            flights.c.status == "Scheduled")).scalar_one() == 15383
        assert db.execute(select(func.count()).select_from(airports).where(
            airports.c.latitude.is_(None) | airports.c.longitude.is_(None)
        )).scalar_one() == 0
        rows = db.execute(select(flights.c.departure_airport,
                                 flights.c.arrival_airport).join(
            flight_offers, flight_offers.c.flight_id == flights.c.id
        ).distinct()).all()
        assert len(rows) == 618
        for flight_id, minute in ((8144, 43), (8152, 58)):
            flight = db.execute(select(flights).where(
                flights.c.id == flight_id)).mappings().one()
            local = flight["departure_at"].astimezone(ZoneInfo("Europe/Berlin"))
            assert (local.date(), local.hour, local.minute) == (
                as_of + timedelta(days=1), 17, minute)
        for order in db.execute(select(flight_orders)).mappings():
            offer = db.execute(select(flight_offers).where(
                flight_offers.c.flight_id == order["flight_id"]
            )).mappings().one()
            assert order["amount"] == offer["amount"]
        for origin, destination in (("HAM", "BSL"), ("OSL", "HAM"),
                                    ("BNE", "OSL")):
            flight = db.execute(select(flights).join(
                flight_offers, flight_offers.c.flight_id == flights.c.id
            ).where(flights.c.departure_airport == origin,
                    flights.c.arrival_airport == destination)
                .order_by(flights.c.departure_at).limit(1)).mappings().one()
            zone = db.execute(select(airports.c.timezone).where(
                airports.c.code == origin)).scalar_one()
            local_date = flight["departure_at"].astimezone(ZoneInfo(zone)).date()
            if origin == "BNE":
                cross_zone_date = local_date
            results = find_flights(db, origin, destination, local_date)
            assert any(row["id"] == flight["id"] for row in results)
            assert all(row["demo_fare_amount"] is not None for row in results)
        assert find_flights(db, "HAM", "BSL", as_of) == []
        assert find_flights(db, "HAM", "BSL", as_of + timedelta(days=31)) == []
        assert find_flights(db, "ZZZ", "BSL", as_of + timedelta(days=1)) == []
    server = Server()
    server.init_app()
    assert "/api/graph/" not in {route.path for route in server.app.routes}
    with TestClient(server.app) as client:
        login = client.post("/api/v1/login", json={
            "username": "demo1",
            "password": os.getenv("DEMO_TRAVELER_1_PASSWORD", ""),
        })
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        response = client.get("/api/v1/flights", headers=headers, params={
            "departure": "BNE", "arrival": "OSL",
            "travel_date": cross_zone_date.isoformat(),
            "sort": "price", "limit": 3,
        })
        assert response.status_code == 200 and response.json()
        amounts = [item["demo_fare_amount"] for item in response.json()]
        assert amounts == sorted(amounts) and len(amounts) <= 3
        missing = client.get("/api/v1/flights", headers=headers, params={
            "departure": "ZZZ", "arrival": "OSL",
            "travel_date": (as_of + timedelta(days=1)).isoformat(),
        })
        assert missing.status_code == 200 and missing.json() == []
    print(json.dumps({"as_of": as_of.isoformat(), "counts": counts,
                      "bookable_routes": len(rows), "status": "verified"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
