"""Verify P2.2 against PostgreSQL and remove only this run's test records."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (audit_events, chat_sessions, flight_offers, flight_orders,
                              flights, hotel_orders, hotel_products,
                              pending_actions)
from main import Server
from services.actions import create_proposal
from services.demo_pricing import cancellation_fee


def app():
    server = Server()
    server.init_app()
    assert "/api/graph/" not in {route.path for route in server.app.routes}
    return server.app


def auth(client, number):
    login = client.post("/api/v1/login", json={
        "username": f"demo{number}",
        "password": os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD", ""),
    })
    assert login.status_code == 200, login.status_code
    return {"Authorization": "Bearer " + login.json()["access_token"]}


def order_snapshot(connection):
    return (
        connection.execute(select(flight_orders.c.id, flight_orders.c.flight_id,
                                  flight_orders.c.amount, flight_orders.c.status,
                                  flight_orders.c.version).order_by(flight_orders.c.id)).all(),
        connection.execute(select(hotel_orders.c.id, hotel_orders.c.product_id,
                                  hotel_orders.c.check_in, hotel_orders.c.check_out,
                                  hotel_orders.c.total_price, hotel_orders.c.status,
                                  hotel_orders.c.version).order_by(hotel_orders.c.id)).all(),
    )


def main():
    with engine.connect() as db:
        before = order_snapshot(db)
        order = db.execute(select(flight_orders).where(
            flight_orders.c.traveler_id == "demo_traveler_1")).mappings().one()
        old_flight = db.execute(select(flights).where(
            flights.c.id == order["flight_id"])).mappings().one()
        now = datetime.now(timezone.utc)
        if old_flight["departure_at"] <= now:
            raise RuntimeError("Demo flight has passed; follow README import and seed steps for a new demo day")
        candidate = db.execute(select(flights.c.id).join(
            flight_offers, flight_offers.c.flight_id == flights.c.id).where(
            flights.c.id != old_flight["id"],
            flights.c.departure_airport == old_flight["departure_airport"],
            flights.c.arrival_airport == old_flight["arrival_airport"],
            flights.c.status == "Scheduled",
            flights.c.departure_at > now).order_by(flights.c.departure_at)
            .limit(1)).scalar_one_or_none()
        assert candidate is not None, "no candidate flight with demo offer"
        hotel_order = db.execute(select(hotel_orders).where(
            hotel_orders.c.traveler_id == "demo_traveler_1")).mappings().one()
        product = db.execute(select(hotel_products).where(
            hotel_products.c.id == hotel_order["product_id"])).mappings().one()
        check_in = max(now.date() + timedelta(days=1), product["available_from"])
        check_out = check_in + timedelta(days=1)
        assert product["available_until"] >= check_out + timedelta(days=1)

    session_ids, action_ids = [], []
    try:
        with TestClient(app()) as client:
            one, two = auth(client, 1), auth(client, 2)
            first = client.post("/api/v1/sessions", headers=one, json={})
            second = client.post("/api/v1/sessions", headers=two, json={})
            for response in (first, second):
                if response.status_code == 201:
                    session_ids.append(response.json()["id"])
            assert first.status_code == second.status_code == 201
            own_session, other_session = session_ids

        cases = [
            ("flight_change", {"order_id": order["id"], "expected_version": order["version"],
                               "new_flight_id": candidate}),
            ("flight_cancel", {"order_id": order["id"], "expected_version": order["version"]}),
            ("hotel_book", {"product_id": product["id"], "check_in": check_in.isoformat(),
                            "check_out": check_out.isoformat()}),
            ("hotel_change", {"order_id": hotel_order["id"],
                              "expected_version": hotel_order["version"],
                              "product_id": product["id"], "check_in": check_in.isoformat(),
                              "check_out": (check_out + timedelta(days=1)).isoformat()}),
            ("hotel_cancel", {"order_id": hotel_order["id"],
                              "expected_version": hotel_order["version"]}),
        ]
        with engine.begin() as db:
            for kind, params in cases:
                action = create_proposal(db, "demo_traveler_1", own_session, kind, params)
                action_ids.append(action["id"])
            assert order_snapshot(db) == before

        path = f"/api/v1/sessions/{own_session}/actions"
        with TestClient(app()) as restarted:
            one, two = auth(restarted, 1), auth(restarted, 2)
            listed = restarted.get(path, headers=one)
            assert listed.status_code == 200 and len(listed.json()) == 5
            assert restarted.get(path, headers=two).status_code == 404
            assert restarted.get(path + "/" + action_ids[0], headers=two).status_code == 404
            assert restarted.get(f"/api/v1/sessions/{other_session}/actions",
                                 headers=one).status_code == 404
            assert restarted.post(path, headers=one, json={}).status_code == 405
            cancel = next(item for item in listed.json()
                          if item["action_type"] == "flight_cancel")
            fee = cancellation_fee(order["amount"])
            assert cancel["price"]["cancellation_fee"] == fee
            assert cancel["price"]["refundable"] == order["amount"] - fee
        with engine.connect() as db:
            assert order_snapshot(db) == before
        print("P2.2 PostgreSQL verified: five cards, fare rule, isolation, restart, unchanged orders")
    finally:
        with engine.begin() as db:
            if action_ids:
                db.execute(delete(audit_events).where(audit_events.c.action_id.in_(action_ids)))
                db.execute(delete(pending_actions).where(pending_actions.c.id.in_(action_ids)))
            if session_ids:
                db.execute(delete(chat_sessions).where(chat_sessions.c.id.in_(session_ids)))


if __name__ == "__main__":
    main()
