"""P2.2 proposals: traveler isolation, fictional quotes, and zero order writes."""

import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, insert, inspect, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.travel import router
from travel_assistant.core.config import settings
from travel_assistant.db.travel_models import (airports, chat_sessions, flight_offers, flight_orders,
                              flights, hotel_orders, hotel_products, hotels,
                              metadata, pending_actions, rental_products, travelers)
from migrations import (v001_travel, v002_sessions, v003_actions,
                        v004_confirmations, v009_rental_orders,
                        v010_hotel_refunds, v011_rental_changes)
from services.actions import ActionError, create_proposal, get_proposal
from services.demo_pricing import cancellation_fee
from travel_assistant.core.dependencies import get_db
from travel_assistant.core.password_hash import get_hashed_password


class ActionFixture(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
                                    connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        tomorrow = self.now + timedelta(days=1)
        self.check_in = tomorrow.date()
        self.check_out = self.check_in + timedelta(days=1)
        with self.engine.begin() as db:
            v001_travel.upgrade(db)
            v002_sessions.upgrade(db)
            v003_actions.upgrade(db)
            v004_confirmations.upgrade(db)
            metadata.create_all(db, tables=[rental_products])
            v009_rental_orders.upgrade(db)
            v010_hotel_refunds.upgrade(db)
            v011_rental_changes.upgrade(db)
            db.execute(insert(travelers), [
                {"id": "t1", "username": "demo1", "password_hash": get_hashed_password("password1"),
                 "display_name": "One", "active": 1},
                {"id": "t2", "username": "demo2", "password_hash": get_hashed_password("password2"),
                 "display_name": "Two", "active": 1},
            ])
            db.execute(insert(airports), [
                {"code": "HAM", "name": "Hamburg", "city": "Hamburg", "timezone": "Europe/Berlin"},
                {"code": "BSL", "name": "Basel", "city": "Basel", "timezone": "Europe/Zurich"},
            ])
            db.execute(insert(flights), [
                {"id": number, "flight_no": f"DEMO{number}",
                 "departure_at": tomorrow + timedelta(hours=number),
                 "arrival_at": tomorrow + timedelta(hours=number + 2),
                 "departure_airport": "HAM", "arrival_airport": "BSL",
                 "status": "Scheduled"}
                for number in (1, 2, 3, 4)
            ])
            db.execute(insert(flight_offers), [
                {"flight_id": number, "amount": amount, "version": 1,
                 "rule_id": "flight-offer-demo-v1"}
                for number, amount in ((1, 100), (2, 120), (3, 80), (4, 100))
            ])
            db.execute(insert(flight_orders), [
                {"id": "f1", "traveler_id": "t1", "flight_id": 1,
                 "amount": 100, "status": "confirmed", "version": 1},
                {"id": "f2", "traveler_id": "t2", "flight_id": 4,
                 "amount": 105, "status": "confirmed", "version": 1},
            ])
            db.execute(insert(hotels).values(
                id=1, name="Demo Hotel", city="Basel", price_tier="Budget"))
            db.execute(insert(hotel_products).values(
                id="room-1", hotel_id=1, room_type="Standard", nightly_price=80,
                available_from=self.check_in, available_until=self.check_out + timedelta(days=5)))
            db.execute(insert(hotel_orders), [
                {"id": "h1", "traveler_id": "t1", "product_id": "room-1",
                 "check_in": self.check_in, "check_out": self.check_out,
                 "total_price": 80, "status": "confirmed", "version": 1},
                {"id": "h2", "traveler_id": "t2", "product_id": "room-1",
                 "check_in": self.check_in, "check_out": self.check_out,
                 "total_price": 80, "status": "confirmed", "version": 1},
            ])
            db.execute(insert(chat_sessions), [
                {"id": "session-1", "traveler_id": "t1", "status": "active"},
                {"id": "session-2", "traveler_id": "t2", "status": "active"},
            ])
        self.original_secret = settings.JWT_SECRET_KEY
        settings.JWT_SECRET_KEY = "test-only-secret-which-is-long-enough"

    def tearDown(self):
        settings.JWT_SECRET_KEY = self.original_secret
        self.engine.dispose()

    def _orders(self, db):
        return (db.execute(select(flight_orders).order_by(flight_orders.c.id)).mappings().all(),
                db.execute(select(hotel_orders).order_by(hotel_orders.c.id)).mappings().all())

    def _propose(self, db, action_type, params, traveler="t1", session="session-1"):
        return create_proposal(db, traveler, session, action_type, params, now=self.now)

    def _app(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        def test_db():
            with Session(self.engine) as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        return app

    @staticmethod
    def _auth(client, username, password):
        response = client.post("/api/v1/login", json={
            "username": username, "password": password})
        assert response.status_code == 200
        return {"Authorization": "Bearer " + response.json()["access_token"]}


class ActionTests(ActionFixture):
    def test_hotel_proposals_use_nearby_airport_local_date(self):
        late_utc = datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc)
        with Session(self.engine) as db:
            db.execute(update(hotels).where(hotels.c.id == 1).values(
                nearby_airport="BSL"))
            db.execute(update(hotel_orders).where(hotel_orders.c.id == "h1").values(
                check_in=late_utc.date(),
                check_out=late_utc.date() + timedelta(days=1)))
            db.execute(update(hotel_products).where(
                hotel_products.c.id == "room-1").values(
                    available_from=late_utc.date(),
                    available_until=late_utc.date() + timedelta(days=4)))
            with self.assertRaises(ActionError):
                create_proposal(db, "t1", "session-1", "hotel_cancel", {
                    "order_id": "h1", "expected_version": 1}, now=late_utc)
            with self.assertRaises(ActionError):
                create_proposal(db, "t1", "session-1", "hotel_book", {
                    "product_id": "room-1", "check_in": late_utc.date().isoformat(),
                    "check_out": (late_utc.date() + timedelta(days=1)).isoformat(),
                }, now=late_utc)

    def test_five_proposals_and_fare_rules_do_not_change_orders(self):
        with Session(self.engine) as db:
            before = self._orders(db)
            up = self._propose(db, "flight_change", {
                "order_id": "f1", "expected_version": 1, "new_flight_id": 2})
            down = self._propose(db, "flight_change", {
                "order_id": "f1", "expected_version": 1, "new_flight_id": 3})
            same = self._propose(db, "flight_change", {
                "order_id": "f1", "expected_version": 1, "new_flight_id": 4})
            cancel = self._propose(db, "flight_cancel", {
                "order_id": "f1", "expected_version": 1})
            book = self._propose(db, "hotel_book", {
                "product_id": "room-1", "check_in": self.check_in.isoformat(),
                "check_out": (self.check_out + timedelta(days=1)).isoformat()})
            change = self._propose(db, "hotel_change", {
                "order_id": "h1", "expected_version": 1, "product_id": "room-1",
                "check_in": self.check_in.isoformat(),
                "check_out": (self.check_out + timedelta(days=1)).isoformat()})
            hotel_cancel = self._propose(db, "hotel_cancel", {
                "order_id": "h1", "expected_version": 1})
            self.assertEqual((up["price"]["payable"], up["price"]["refundable"]), (20, 0))
            self.assertEqual((down["price"]["payable"], down["price"]["refundable"]), (0, 20))
            self.assertEqual(same["price"]["difference"], 0)
            self.assertEqual((cancel["price"]["cancellation_fee"],
                              cancel["price"]["refundable"]), (30, 70))
            self.assertEqual((book["price"]["new_amount"],
                              change["price"]["difference"]), (160, 80))
            self.assertEqual(hotel_cancel["after"]["status"], "cancelled")
            self.assertEqual(hotel_cancel["price"]["refundable"], 80)
            self.assertEqual(hotel_cancel["policy"],
                             {"id": "hotel-actions-demo-v4", "version": 4})
            self.assertIn("入住当日", book["cancellation_notice"])
            self.assertEqual(up["policy"], {"id": "flight-actions-demo-v3", "version": 3})
            self.assertEqual(up["offer_version"], 1)
            self.assertEqual(up["expires_at"] - up["created_at"], timedelta(minutes=15))
            db.commit()
            self.assertEqual(self._orders(db), before)
            self.assertEqual(len(db.execute(select(pending_actions)).all()), 7)
        self.assertEqual(cancellation_fee(105), 32)

    def test_invalid_or_foreign_targets_leave_no_action(self):
        invalid = [
            ("t2", "session-1", "flight_cancel", {"order_id": "f1", "expected_version": 1}),
            ("t1", "session-1", "flight_cancel", {"order_id": "f2", "expected_version": 1}),
            ("t1", "session-1", "hotel_cancel", {"order_id": "h2", "expected_version": 1}),
            ("t1", "session-1", "flight_cancel", {"order_id": "f1", "expected_version": 2}),
            ("t1", "session-1", "hotel_book", {"product_id": "room-1", "check_in":
             self.check_out.isoformat(), "check_out": self.check_in.isoformat()}),
            ("t1", "session-1", "flight_change", {"order_id": "f1",
             "expected_version": 1, "new_flight_id": 999}),
        ]
        with Session(self.engine) as db:
            before = self._orders(db)
            for traveler, session, kind, params in invalid:
                with self.assertRaises(ValueError):
                    self._propose(db, kind, params, traveler, session)
                self.assertEqual(db.execute(select(pending_actions)).all(), [])
            self.assertEqual(self._orders(db), before)

    def test_api_is_read_only_scoped_and_survives_app_rebuild(self):
        with Session(self.engine) as db:
            action = self._propose(db, "flight_cancel", {
                "order_id": "f1", "expected_version": 1})
            db.commit()
        with TestClient(self._app()) as client:
            one = self._auth(client, "demo1", "password1")
            two = self._auth(client, "demo2", "password2")
            path = "/api/v1/sessions/session-1/actions"
            self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(client.get(path, headers=two).status_code, 404)
            self.assertEqual(client.get(path + "/" + action["id"], headers=two).status_code, 404)
            self.assertEqual(client.post(path, headers=one, json={}).status_code, 405)
            self.assertEqual([item["id"] for item in client.get(path, headers=one).json()],
                             [action["id"]])
        with TestClient(self._app()) as restarted:
            one = self._auth(restarted, "demo1", "password1")
            detail = restarted.get(path + "/" + action["id"], headers=one)
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["price"]["cancellation_fee"], 30)
        with Session(self.engine) as db:
            self.assertEqual(get_proposal(db, "t1", "session-1", action["id"],
                                          now=self.now + timedelta(minutes=16))["status"],
                             "expired")

    def test_cancelled_order_missing_offer_and_closed_session_are_rejected(self):
        with Session(self.engine) as db:
            db.execute(update(flight_orders).where(flight_orders.c.id == "f1")
                       .values(status="cancelled"))
            with self.assertRaises(ActionError):
                self._propose(db, "flight_cancel", {
                    "order_id": "f1", "expected_version": 1})
            db.rollback()
            db.execute(delete(flight_offers).where(flight_offers.c.flight_id == 2))
            with self.assertRaises(ActionError):
                self._propose(db, "flight_change", {
                    "order_id": "f1", "expected_version": 1, "new_flight_id": 2})
            db.rollback()
            db.execute(update(chat_sessions).where(chat_sessions.c.id == "session-1")
                       .values(status="closed"))
            with self.assertRaises(ActionError):
                self._propose(db, "hotel_book", {
                    "product_id": "room-1", "check_in": self.check_in.isoformat(),
                    "check_out": self.check_out.isoformat()})
            db.rollback()
            self.assertEqual(db.execute(select(pending_actions)).all(), [])

    def test_v003_is_repeatable_and_backfills_known_offers(self):
        fresh = create_engine("sqlite+pysqlite:///:memory:")
        with fresh.begin() as db:
            v001_travel.upgrade(db)
            v002_sessions.upgrade(db)
            db.execute(insert(airports), [
                {"code": "HAM", "name": "Hamburg", "city": "Hamburg", "timezone": "Europe/Berlin"},
                {"code": "BSL", "name": "Basel", "city": "Basel", "timezone": "Europe/Zurich"},
            ])
            db.execute(insert(flights).values(
                id=10, flight_no="LH0130", departure_at=self.now,
                arrival_at=self.now + timedelta(hours=2),
                departure_airport="HAM", arrival_airport="BSL", status="Scheduled"))
            v003_actions.upgrade(db)
            v003_actions.upgrade(db)
            self.assertIn("pending_actions", inspect(db).get_table_names())
            self.assertEqual(db.execute(select(flight_offers.c.amount)).scalar_one(), 6400)
        fresh.dispose()


if __name__ == "__main__":
    unittest.main()
