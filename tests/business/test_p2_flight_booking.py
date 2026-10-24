"""Flight booking stays behind a traveler-owned, single-action confirmation."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from travel_assistant.db.travel_models import (action_results, demo_state, flight_offers,
                              flight_orders, flights, policy_documents)
from migrations import v006_demo_state, v008_flight_booking
from travel_assistant.services.actions import ActionError, create_proposal
from travel_assistant.services.confirmations import confirm_action, reject_action
from tests.business.test_p2_actions import ActionFixture


class FlightBookingTests(ActionFixture):
    def setUp(self):
        super().setUp()
        with self.engine.begin() as db:
            v006_demo_state.upgrade(db)
            v008_flight_booking.upgrade(db)
            db.execute(insert(demo_state).values(
                id=1, as_of=self.now.date(), fixture_sha256="0" * 64))

    def _book(self, db, flight_id=2, traveler="t1", chat="session-1"):
        return create_proposal(db, traveler, chat, "flight_book",
                               {"flight_id": flight_id}, now=self.now)

    def test_booking_card_then_api_confirmation_and_replay(self):
        with Session(self.engine) as db:
            before = db.execute(select(func.count()).select_from(flight_orders)).scalar_one()
            card = self._book(db)
            self.assertIsNone(card["before"])
            self.assertEqual(card["price"]["payable"], 120)
            self.assertEqual(card["policy"], {"id": "flight-actions-demo-v3", "version": 3})
            self.assertIn("+", card["after"]["departure_local"])
            self.assertEqual(db.execute(select(func.count()).select_from(
                flight_orders)).scalar_one(), before)
            db.commit()
        with TestClient(self._app()) as client:
            one = self._auth(client, "demo1", "password1")
            two = self._auth(client, "demo2", "password2")
            path = f"/api/v1/sessions/session-1/actions/{card['id']}"
            self.assertEqual(client.get(path, headers=two).status_code, 404)
            self.assertEqual(client.post(path + "/confirm", headers=two).status_code, 404)
            response = client.post(path + "/confirm", headers=one)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["order"]["flight_id"], 2)
            self.assertEqual(result["order"]["amount"], 120)
            self.assertEqual(result["price"]["payable"], 120)
            self.assertEqual(client.post(path + "/confirm", headers=one).json(), result)
            flights_in_trip = client.get("/api/v1/me/itinerary", headers=one).json()["flight_orders"]
            self.assertEqual(len(flights_in_trip), 2)
        with Session(self.engine) as db:
            self.assertEqual(db.execute(select(func.count()).select_from(
                flight_orders)).scalar_one(), 3)
            self.assertEqual(db.execute(select(func.count()).select_from(
                action_results)).scalar_one(), 1)

    def test_two_cards_for_same_flight_only_one_can_write(self):
        with Session(self.engine) as db:
            first = self._book(db)
            second = self._book(db)
            change = create_proposal(db, "t1", "session-1", "flight_change", {
                "order_id": "f1", "expected_version": 1, "new_flight_id": 2}, now=self.now)
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", first["id"],
                                            now=self.now)["status"], "confirmed")
            self.assertEqual(confirm_action(db, "t1", "session-1", second["id"],
                                            now=self.now)["status"], "invalidated")
            self.assertEqual(confirm_action(db, "t1", "session-1", change["id"],
                                            now=self.now)["status"], "invalidated")
            self.assertEqual(db.execute(select(func.count()).select_from(flight_orders).where(
                flight_orders.c.traveler_id == "t1", flight_orders.c.flight_id == 2,
                flight_orders.c.status == "confirmed")).scalar_one(), 1)
            self.assertEqual(db.execute(select(flight_orders.c.flight_id).where(
                flight_orders.c.id == "f1")).scalar_one(), 1)
            db.commit()
            with self.assertRaises(ActionError):
                self._book(db)
            with self.assertRaises(ActionError):
                create_proposal(db, "t1", "session-1", "flight_change", {
                    "order_id": "f1", "expected_version": 1,
                    "new_flight_id": 2}, now=self.now)

    def test_cancelled_ticket_can_be_booked_again(self):
        with Session(self.engine) as db:
            first = self._book(db)
            db.commit()
            result = confirm_action(db, "t1", "session-1", first["id"], now=self.now)
            db.commit()
            cancel = create_proposal(db, "t1", "session-1", "flight_cancel", {
                "order_id": result["order_id"], "expected_version": 1}, now=self.now)
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", cancel["id"],
                                            now=self.now)["status"], "confirmed")
            db.commit()
            second = self._book(db)
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", second["id"],
                                            now=self.now)["status"], "confirmed")
            self.assertEqual(db.execute(select(func.count()).select_from(flight_orders).where(
                flight_orders.c.traveler_id == "t1", flight_orders.c.flight_id == 2,
                flight_orders.c.status == "confirmed")).scalar_one(), 1)

    def test_stale_fare_and_policy_never_write(self):
        with Session(self.engine) as db:
            card = self._book(db)
            db.commit()
            db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(
                amount=121, version=2))
            self.assertEqual(confirm_action(db, "t1", "session-1", card["id"],
                                            now=self.now)["status"], "invalidated")
            db.commit()
            card = self._book(db)
            db.commit()
            db.execute(update(policy_documents).where(
                policy_documents.c.id == "flight-actions-demo-v3").values(body="changed"))
            self.assertEqual(confirm_action(db, "t1", "session-1", card["id"],
                                            now=self.now)["status"], "invalidated")
            db.commit()
        with Session(self.engine) as db:
            self.assertEqual(db.execute(select(func.count()).select_from(
                flight_orders)).scalar_one(), 2)

    def test_reject_and_expire_never_write(self):
        with Session(self.engine) as db:
            rejected = self._book(db)
            expired = self._book(db)
            db.commit()
            self.assertEqual(reject_action(db, "t1", "session-1", rejected["id"],
                                           now=self.now)["status"], "rejected")
            self.assertEqual(confirm_action(db, "t1", "session-1", expired["id"],
                                            now=self.now + timedelta(minutes=16))["status"],
                             "expired")
            self.assertEqual(db.execute(select(func.count()).select_from(
                flight_orders)).scalar_one(), 2)

    def test_invalid_candidates_and_database_uniqueness(self):
        with Session(self.engine) as db:
            with self.assertRaises(ActionError):
                create_proposal(db, "t1", "session-1", "flight_book",
                                {"flight_id": 2, "amount": 1}, now=self.now)
            with self.assertRaises(ActionError):
                self._book(db, flight_id=True)
            with self.assertRaises(ActionError):
                self._book(db, flight_id=1)
            db.execute(delete(flight_offers).where(flight_offers.c.flight_id == 2))
            with self.assertRaises(ActionError):
                self._book(db)
            db.rollback()
            db.execute(update(flights).where(flights.c.id == 2).values(status="Cancelled"))
            with self.assertRaises(ActionError):
                self._book(db)
            db.rollback()
            db.execute(update(flights).where(flights.c.id == 2).values(
                departure_at=self.now - timedelta(hours=1)))
            with self.assertRaises(ActionError):
                self._book(db)
            db.rollback()
            db.execute(update(demo_state).values(as_of=self.now.date() + timedelta(days=10)))
            with self.assertRaises(ActionError):
                self._book(db)
            db.rollback()
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as db:
                db.execute(insert(flight_orders).values(
                    id="duplicate", traveler_id="t1", flight_id=1,
                    amount=100, status="confirmed", version=1))
