"""P1 identity, ownership and transaction-boundary regression tests."""

import os
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.travel import router
from travel_assistant.core.config import settings
from travel_assistant.db.travel_models import (airports, flight_orders, flights, hotel_orders,
                              hotel_products, hotels, metadata, travelers)
from fastapi import FastAPI
from services.travel import (OrderError, book_hotel, cancel_flight, change_flight,
                             change_hotel, itinerary)
from travel_assistant.core.dependencies import get_db
from travel_assistant.core.password_hash import get_hashed_password


class P1BusinessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
                                    connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.original_secret = settings.JWT_SECRET_KEY
        settings.JWT_SECRET_KEY = "test-only-secret-which-is-long-enough"
        tomorrow = date.today() + timedelta(days=1)
        self.tomorrow = tomorrow
        departure = datetime.combine(tomorrow, datetime.min.time(), timezone.utc)
        self.session.execute(insert(travelers), [
            {"id": "t1", "username": "demo1", "password_hash": get_hashed_password("password1"),
             "display_name": "One", "active": 1},
            {"id": "t2", "username": "demo2", "password_hash": get_hashed_password("password2"),
             "display_name": "Two", "active": 1},
        ])
        self.session.execute(insert(airports), [
            {"code": "HAM", "name": "Hamburg", "city": "Hamburg", "timezone": "Europe/Berlin"},
            {"code": "BSL", "name": "Basel", "city": "Basel", "timezone": "Europe/Zurich"},
        ])
        self.session.execute(insert(flights), [
            {"id": number, "flight_no": f"DEMO{number}",
             "departure_at": departure + timedelta(hours=number),
             "arrival_at": departure + timedelta(hours=number + 2),
             "departure_airport": "HAM", "arrival_airport": "BSL", "status": "Scheduled"}
            for number in (1, 2, 3)
        ])
        self.session.execute(insert(hotels).values(
            id=1, name="Demo Hotel", city="Basel", price_tier="Budget"))
        self.session.execute(insert(hotel_products).values(
            id="room-1", hotel_id=1, room_type="Standard", nightly_price=80,
            available_from=tomorrow, available_until=tomorrow + timedelta(days=10)))
        self.session.execute(insert(flight_orders), [
            {"id": "o1", "traveler_id": "t1", "flight_id": 1,
             "amount": 100, "status": "confirmed", "version": 1},
            {"id": "o2", "traveler_id": "t2", "flight_id": 3,
             "amount": 100, "status": "confirmed", "version": 1},
        ])
        self.session.commit()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.session.close()
        self.engine.dispose()
        settings.JWT_SECRET_KEY = self.original_secret

    def _token(self, username, password):
        response = self.client.post("/api/v1/login", json={
            "username": username, "password": password})
        self.assertEqual(response.status_code, 200)
        return response.json()["access_token"]

    def test_account_isolation_and_invalid_tokens(self):
        token1 = self._token("demo1", "password1")
        token2 = self._token("demo2", "password2")
        self.assertEqual(self.client.post("/api/v1/login", json={
            "username": "demo1", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/me/itinerary").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/me/itinerary", headers={
            "Authorization": "Bearer invalid"}).status_code, 401)
        one = self.client.get("/api/v1/me/itinerary", headers={
            "Authorization": f"Bearer {token1}"}, params={"traveler_id": "t2"})
        two = self.client.get("/api/v1/me/itinerary", headers={
            "Authorization": f"Bearer {token2}"})
        self.assertEqual([order["id"] for order in one.json()["flight_orders"]], ["o1"])
        self.assertEqual([order["id"] for order in two.json()["flight_orders"]], ["o2"])
        self.assertNotIn("password_hash", self.client.get("/api/v1/me", headers={
            "Authorization": f"Bearer {token1}"}).text)

    def test_order_writes_check_owner_and_version_without_committing(self):
        with self.assertRaises(OrderError):
            change_flight(self.session, "t2", "o1", 1, 2, 120)
        self.session.rollback()
        change_flight(self.session, "t1", "o1", 1, 2, 120)
        self.session.commit()
        row = self.session.execute(select(flight_orders).where(
            flight_orders.c.id == "o1")).mappings().one()
        self.assertEqual((row["flight_id"], row["version"]), (2, 2))
        with self.assertRaises(OrderError):
            change_flight(self.session, "t1", "o1", 1, 3, 80)
        self.session.rollback()
        cancel_flight(self.session, "t1", "o1", 2)
        self.session.rollback()
        self.assertEqual(self.session.execute(select(flight_orders.c.status).where(
            flight_orders.c.id == "o1")).scalar_one(), "confirmed")

    def test_hotel_booking_and_change_are_traveler_scoped(self):
        order_id = book_hotel(self.session, "t1", "room-1", self.tomorrow,
                              self.tomorrow + timedelta(days=1))
        self.session.commit()
        with self.assertRaises(OrderError):
            change_hotel(self.session, "t2", order_id, 1, "room-1",
                         self.tomorrow + timedelta(days=1),
                         self.tomorrow + timedelta(days=2))
        self.session.rollback()
        change_hotel(self.session, "t1", order_id, 1, "room-1",
                     self.tomorrow + timedelta(days=1),
                     self.tomorrow + timedelta(days=3))
        self.session.commit()
        row = self.session.execute(select(hotel_orders).where(
            hotel_orders.c.id == order_id)).mappings().one()
        self.assertEqual((row["total_price"], row["version"]), (160, 2))
        self.assertEqual(len(itinerary(self.session, "t2")["hotel_orders"]), 0)


if __name__ == "__main__":
    unittest.main()
