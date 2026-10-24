"""Read-only PostgreSQL and authenticated API checks for P2.4c supply."""

from datetime import timedelta
from pathlib import Path
import json
import os
import sys

from fastapi.testclient import TestClient
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (airports, demo_state, hotel_orders, hotel_products,
                              hotels, pending_actions, rental_products)
from main import Server
from services.policy_catalog import CURRENT_POLICIES
from services.travel import find_hotels, find_rentals


def main():
    with engine.connect() as db:
        as_of = db.execute(select(demo_state.c.as_of).where(demo_state.c.id == 1)).scalar_one()
        stay = as_of + timedelta(days=8)
        cities = db.execute(select(hotels.c.city).distinct()).scalars().all()
        assert len(cities) == 95
        for city in cities:
            assert find_hotels(db, city, stay, stay + timedelta(days=1), limit=100)
            assert find_rentals(db, stay, stay + timedelta(days=1), city=city, limit=100)
        assert db.execute(select(func.count()).select_from(hotels)).scalar_one() == 312
        assert db.execute(select(func.count()).select_from(hotel_products)).scalar_one() == 312
        assert db.execute(select(func.count()).select_from(rental_products)).scalar_one() == 208
        assert db.execute(select(func.count()).select_from(pending_actions)).scalar_one() == 0
        basel = db.execute(select(hotel_orders.c.product_id)).scalars().all()
        assert basel == ["room-1", "room-2"], basel
        london_airports = db.execute(select(airports.c.code).where(
            airports.c.city == "London").order_by(airports.c.code)).scalars().all()
        assert len(london_airports) > 1
    server = Server()
    server.init_app()
    assert "/api/graph/" not in {route.path for route in server.app.routes}
    with TestClient(server.app) as client:
        assert client.get("/api/v1/rentals", params={
            "city": "Basel", "pickup_date": stay.isoformat(),
            "drop_off_date": (stay + timedelta(days=1)).isoformat(),
        }).status_code in (401, 403)
        for number in (1, 2):
            login = client.post("/api/v1/login", json={
                "username": f"demo{number}",
                "password": os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD", ""),
            })
            assert login.status_code == 200
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            hotels_response = client.get("/api/v1/hotels", headers=headers, params={
                "city": "London", "airport": london_airports[0],
                "check_in": stay.isoformat(),
                "check_out": (stay + timedelta(days=1)).isoformat(),
                "sort": "distance", "limit": 3,
            })
            assert hotels_response.status_code == 200, hotels_response.text
            hotel_rows = hotels_response.json()
            assert len(hotel_rows) == 3 and all(
                row["nearby_airport"] == london_airports[0] and
                row["distance_km"] is not None for row in hotel_rows)
            rentals_response = client.get("/api/v1/rentals", headers=headers, params={
                "airport": london_airports[0], "pickup_date": stay.isoformat(),
                "drop_off_date": (stay + timedelta(days=1)).isoformat(),
                "sort": "price", "limit": 2,
            })
            assert rentals_response.status_code == 200, rentals_response.text
            rental_rows = rentals_response.json()
            assert len(rental_rows) == 2 and all(
                row["airport_code"] == london_airports[0] for row in rental_rows)
            for scope, current in CURRENT_POLICIES.items():
                response = client.get(f"/api/v1/policies/{scope}", headers=headers)
                assert response.status_code == 200 and [
                    row["id"] for row in response.json()] == [current["id"]]
            missing = client.get("/api/v1/hotels", headers=headers, params={
                "city": "Unknown", "check_in": stay.isoformat(),
                "check_out": (stay + timedelta(days=1)).isoformat(),
            })
            assert missing.status_code == 200 and missing.json() == []
            invalid = client.get("/api/v1/rentals", headers=headers, params={
                "city": "London", "pickup_date": stay.isoformat(),
                "drop_off_date": stay.isoformat(),
            })
            assert invalid.status_code == 422
    print(json.dumps({"as_of": as_of.isoformat(), "hotels": 312, "rentals": 208,
                      "cities": 95, "london_airports": london_airports,
                      "accounts": 2, "status": "verified"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
