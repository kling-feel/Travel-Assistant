"""Check P1 schema, seed and traveler API against the running demo PostgreSQL."""

from datetime import date, timedelta
from pathlib import Path
import os
import sys

from fastapi.testclient import TestClient
from sqlalchemy import select, func

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (airports, demo_state, flight_offers, flight_orders,
                              flights, hotel_orders, hotel_products,
                              policy_documents, rental_products, travelers)
from main import Server
from services.travel import OrderError, change_flight


def main():
    with engine.connect() as connection:
        network_day = connection.execute(select(demo_state.c.as_of).where(
            demo_state.c.id == 1
        )).scalar_one_or_none()
        expected = ((travelers, 2), (airports, 115 if network_day else 2),
                    (flights, 33121 if network_day else 14),
                    (flight_offers, 15374 if network_day else 14),
                    (flight_orders, 2), (hotel_products, 312 if network_day else 3),
                    (hotel_orders, 2), (policy_documents, 10),
                    (rental_products, 208 if network_day else 0))
        counts = {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                  for table, _ in expected}
        for table, amount in expected:
            assert counts[table.name] == amount, (table.name, counts[table.name])
        own = connection.execute(select(flight_orders).where(
            flight_orders.c.traveler_id == "demo_traveler_1"
        )).mappings().one()
        old_flight = connection.execute(select(flights).where(
            flights.c.id == own["flight_id"]
        )).mappings().one()
        replacement = connection.execute(select(flights.c.id).where(
            flights.c.id != own["flight_id"],
            flights.c.departure_airport == old_flight["departure_airport"],
            flights.c.arrival_airport == old_flight["arrival_airport"],
            flights.c.status == "Scheduled",
        ).limit(1)).scalar_one()
        try:
            change_flight(connection, "demo_traveler_2", own["id"], 1, replacement, own["amount"])
        except OrderError:
            pass
        else:
            raise AssertionError("cross-account change was accepted")
        change_flight(connection, "demo_traveler_1", own["id"], 1, replacement, own["amount"])
        assert connection.execute(select(flight_orders.c.flight_id).where(
            flight_orders.c.id == own["id"])).scalar_one() == replacement
        connection.rollback()  # Verification must leave the demo itinerary unchanged.
        assert connection.execute(select(flight_orders.c.flight_id).where(
            flight_orders.c.id == own["id"])).scalar_one() == own["flight_id"]

    server = Server()
    server.init_app()
    with TestClient(server.app) as client:
        order_ids = []
        for number in (1, 2):
            password = os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD", "")
            login = client.post("/api/v1/login", json={
                "username": f"demo{number}", "password": password})
            assert login.status_code == 200, (number, login.status_code)
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            me = client.get("/api/v1/me", headers=headers)
            itinerary = client.get("/api/v1/me/itinerary", headers=headers,
                                   params={"traveler_id": f"demo_traveler_{3-number}"})
            assert me.status_code == 200 and itinerary.status_code == 200
            assert me.json()["id"] == f"demo_traveler_{number}"
            assert len(itinerary.json()["flight_orders"]) == 1
            order_ids.append(itinerary.json()["flight_orders"][0]["id"])
            assert client.get("/api/v1/policies/flight", headers=headers).status_code == 200
            flights_response = client.get("/api/v1/flights", headers=headers, params={
                "departure": "HAM", "arrival": "BSL",
                "travel_date": ((network_day or date.today()) + timedelta(days=1)).isoformat(),
            })
            assert flights_response.status_code == 200 and flights_response.json()
            assert client.get("/api/v1/hotels", headers=headers, params={
                "city": "Basel", "check_in": ((network_day or date.today()) + timedelta(days=1)).isoformat(),
                "check_out": ((network_day or date.today()) + timedelta(days=2)).isoformat(),
            }).status_code == 200
        assert order_ids[0] != order_ids[1]
        assert client.get("/api/v1/me/itinerary").status_code in (401, 403)
        assert client.get("/api/v1/me/itinerary", headers={
            "Authorization": "Bearer invalid"}).status_code == 401
    print("P1 PostgreSQL and API verified:", counts, "; two accounts isolated")


if __name__ == "__main__":
    main()
