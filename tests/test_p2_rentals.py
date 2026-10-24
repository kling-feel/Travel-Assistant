"""Rental orders remain traveler-owned and require one explicit confirmation."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from travel_assistant.db.travel_models import (action_results, demo_state, policy_documents,
                              rental_orders, rental_products)
from migrations import v006_demo_state
from services.actions import ActionError, create_proposal
from services.confirmations import confirm_action, reject_action
from services.rentals import _billable_days, _local_time
from services.travel import OrderError
from test_p2_actions import ActionFixture


class RentalActionTests(ActionFixture):
    def setUp(self):
        super().setUp()
        self.zone = ZoneInfo("Europe/Zurich")
        # Keep fixed-price assertions away from DST transitions; that edge is
        # covered explicitly by test_time_validation_and_24_hour_billing.
        self.check_in = self.now.date() + timedelta(days=1)
        while len({datetime.combine(self.check_in + timedelta(days=offset),
                                    time(12), self.zone).utcoffset()
                   for offset in (0, 1, 2)}) != 1:
            self.check_in += timedelta(days=1)
        self.check_out = self.check_in + timedelta(days=1)
        self.pickup = datetime.combine(self.check_in, time(12)).isoformat()
        self.dropoff = datetime.combine(self.check_out, time(12)).isoformat()
        with self.engine.begin() as db:
            v006_demo_state.upgrade(db)
            db.execute(insert(demo_state).values(
                id=1, as_of=self.now.date(), fixture_sha256="0" * 64))
            db.execute(insert(rental_products).values(
                id="car-1", airport_code="BSL", city="Basel",
                name="虚构经济型租车", vehicle_type="模拟经济型", daily_price=90,
                latitude=47.59, longitude=7.61,
                available_from=self.check_in,
                available_until=self.check_in + timedelta(days=31),
                generation_rule="test"))

    def _book(self, db, *, pickup=None, dropoff=None, traveler="t1", session="session-1"):
        return create_proposal(db, traveler, session, "rental_book", {
            "product_id": "car-1", "pickup_at": pickup or self.pickup,
            "dropoff_at": dropoff or self.dropoff}, now=self.now)

    def _confirmed_rental(self, db, *, dropoff=None):
        booked = self._book(db, dropoff=dropoff)
        db.commit()
        result = confirm_action(db, "t1", "session-1", booked["id"], now=self.now)
        db.commit()
        return result["order_id"]

    def _change(self, db, order_id, version, *, pickup=None, dropoff=None,
                traveler="t1", now=None):
        return create_proposal(db, traveler, "session-1" if traveler == "t1" else "session-2",
                               "rental_change", {
                                   "order_id": order_id, "expected_version": version,
                                   "pickup_at": pickup or self.pickup,
                                   "dropoff_at": dropoff or self.dropoff}, now=now or self.now)

    def test_change_quote_confirmation_refund_ownership_and_replay(self):
        long_dropoff = datetime.combine(self.check_out + timedelta(days=1), time(13)).isoformat()
        with Session(self.engine) as db:
            order_id = self._confirmed_rental(db)
            longer = self._change(db, order_id, 1, dropoff=long_dropoff)
            self.assertEqual(longer["policy"]["version"], 3)
            self.assertEqual(longer["price"]["payable"], 180)
            self.assertEqual(longer["before"]["billable_days"], 1)
            self.assertEqual(longer["after"]["billable_days"], 3)
            self.assertEqual(longer["after"]["product_id"], "car-1")
            self.assertEqual(db.execute(select(rental_orders.c.version)).scalar_one(), 1)
            with self.assertRaises(ActionError):
                self._change(db, order_id, 1, dropoff=long_dropoff, traveler="t2")
            with self.assertRaises(ActionError):
                self._change(db, order_id, 1, dropoff=long_dropoff,
                             pickup=self.pickup + "+09:00")
            db.commit()
        with TestClient(self._app()) as client:
            owner = self._auth(client, "demo1", "password1")
            other = self._auth(client, "demo2", "password2")
            path = f"/api/v1/sessions/session-1/actions/{longer['id']}"
            self.assertEqual(client.post(path + "/confirm", headers=other).status_code, 404)
            response = client.post(path + "/confirm", headers=owner)
            self.assertEqual(response.status_code, 200, response.text)
            changed = response.json()
            self.assertEqual(changed["order"]["version"], 2)
            self.assertEqual(changed["order"]["total_price"], 270)
            self.assertEqual(client.post(path + "/confirm", headers=owner).json(), changed)
        with Session(self.engine) as db:
            shorter = self._change(db, order_id, 2, dropoff=self.dropoff)
            self.assertEqual(shorter["price"]["refundable"], 180)
            db.commit()
            result = confirm_action(db, "t1", "session-1", shorter["id"], now=self.now)
            self.assertEqual(result["order"]["total_price"], 90)
            self.assertEqual(result["order"]["version"], 3)
            db.commit()

    def test_change_boundary_stale_policy_price_and_competing_cancel(self):
        later = datetime.combine(self.check_out, time(13)).isoformat()
        with Session(self.engine) as db:
            order_id = self._confirmed_rental(db)
            with self.assertRaises(ActionError):
                self._change(db, order_id, 1)
            with self.assertRaises(ActionError):
                self._change(db, order_id, 1, dropoff=later,
                             pickup=datetime.combine(self.now.date(), time(1)).isoformat())
            with self.assertRaises(ActionError):
                self._change(db, order_id, 1, dropoff=later,
                             pickup="2026-03-29T02:30")
            change = self._change(db, order_id, 1, dropoff=later)
            cancel = create_proposal(db, "t1", "session-1", "rental_cancel", {
                "order_id": order_id, "expected_version": 1}, now=self.now)
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", cancel["id"],
                                            now=self.now)["status"], "confirmed")
            self.assertEqual(confirm_action(db, "t1", "session-1", change["id"],
                                            now=self.now)["status"], "invalidated")
            db.commit()

    def test_change_pickup_cutoff_and_policy_version(self):
        later = datetime.combine(self.check_out, time(13)).isoformat()
        with Session(self.engine) as db:
            order_id = self._confirmed_rental(db)
            pickup = db.execute(select(rental_orders.c.pickup_at)).scalar_one()
            pickup = pickup.replace(tzinfo=timezone.utc) if pickup.tzinfo is None else pickup
            change = self._change(db, order_id, 1, dropoff=later,
                                  now=pickup - timedelta(minutes=1))
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", change["id"],
                                            now=pickup)["status"], "invalidated")
            db.commit()
            old_card = self._change(db, order_id, 1, dropoff=later)
            db.execute(update(policy_documents).where(
                policy_documents.c.id == "rental-actions-demo-v3").values(body="changed"))
            self.assertEqual(confirm_action(db, "t1", "session-1", old_card["id"],
                                            now=self.now)["status"], "invalidated")
            self.assertEqual(db.execute(select(rental_orders.c.version)).scalar_one(), 1)

    def test_change_rejects_duplicate_period_and_product_window_change(self):
        later = datetime.combine(self.check_out, time(13)).isoformat()
        with Session(self.engine) as db:
            order_id = self._confirmed_rental(db)
            db.execute(insert(rental_orders).values(
                id="other-rental", traveler_id="t1", product_id="car-1",
                pickup_at=datetime.fromisoformat(self.pickup).replace(
                    tzinfo=self.zone).astimezone(timezone.utc),
                dropoff_at=datetime.fromisoformat(later).replace(
                    tzinfo=self.zone).astimezone(timezone.utc),
                daily_price=90, billable_days=2, total_price=180,
                status="confirmed", version=1))
            with self.assertRaises(ActionError):
                self._change(db, order_id, 1, dropoff=later)
            db.execute(delete(rental_orders).where(rental_orders.c.id == "other-rental"))
            card = self._change(db, order_id, 1, dropoff=later)
            db.execute(update(rental_products).where(
                rental_products.c.id == "car-1").values(
                    available_from=self.check_out))
            self.assertEqual(confirm_action(db, "t1", "session-1", card["id"],
                                            now=self.now)["status"], "invalidated")
            self.assertEqual(db.execute(select(rental_orders.c.version).where(
                rental_orders.c.id == order_id)).scalar_one(), 1)

    def test_booking_cancel_api_ownership_and_replay(self):
        with Session(self.engine) as db:
            card = self._book(db)
            self.assertIsNone(card["before"])
            self.assertEqual(card["price"]["payable"], 90)
            self.assertEqual(card["after"]["billable_days"], 1)
            self.assertEqual(card["after"]["airport_timezone"], "Europe/Zurich")
            self.assertEqual(db.execute(select(func.count()).select_from(
                rental_orders)).scalar_one(), 0)
            db.commit()
        with TestClient(self._app()) as client:
            owner = self._auth(client, "demo1", "password1")
            other = self._auth(client, "demo2", "password2")
            path = f"/api/v1/sessions/session-1/actions/{card['id']}"
            self.assertEqual(client.get(path, headers=other).status_code, 404)
            self.assertEqual(client.post(path + "/confirm", headers=other).status_code, 404)
            result = client.post(path + "/confirm", headers=owner)
            self.assertEqual(result.status_code, 200)
            booked = result.json()
            self.assertEqual(booked["order"]["total_price"], 90)
            self.assertEqual(booked["order"]["status"], "confirmed")
            self.assertEqual(client.post(path + "/confirm", headers=owner).json(), booked)
            trip = client.get("/api/v1/me/itinerary", headers=owner).json()
            self.assertEqual(len(trip["rental_orders"]), 1)
            self.assertIn("+", trip["rental_orders"][0]["pickup_local"])
            self.assertEqual(client.get("/api/v1/me/itinerary", headers=other)
                             .json()["rental_orders"], [])
        with Session(self.engine) as db:
            cancel = create_proposal(db, "t1", "session-1", "rental_cancel", {
                "order_id": booked["order_id"], "expected_version": 1}, now=self.now)
            second_cancel = create_proposal(db, "t1", "session-1", "rental_cancel", {
                "order_id": booked["order_id"], "expected_version": 1}, now=self.now)
            with self.assertRaises(ActionError):
                create_proposal(db, "t2", "session-2", "rental_cancel", {
                    "order_id": booked["order_id"], "expected_version": 1}, now=self.now)
            self.assertEqual(cancel["price"]["refundable"], 90)
            self.assertEqual(cancel["price"]["cancellation_fee"], 0)
            self.assertEqual(db.execute(select(rental_orders.c.status).where(
                rental_orders.c.id == booked["order_id"])).scalar_one(), "confirmed")
            db.commit()
        with TestClient(self._app()) as client:
            owner = self._auth(client, "demo1", "password1")
            path = f"/api/v1/sessions/session-1/actions/{cancel['id']}"
            cancelled = client.post(path + "/confirm", headers=owner)
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["order"]["status"], "cancelled")
            self.assertEqual(cancelled.json()["order"]["version"], 2)
            self.assertEqual(client.post(path + "/confirm", headers=owner).json(),
                             cancelled.json())
            other_path = f"/api/v1/sessions/session-1/actions/{second_cancel['id']}"
            self.assertEqual(client.post(other_path + "/confirm", headers=owner)
                             .status_code, 409)
        with Session(self.engine) as db:
            self.assertEqual(db.execute(select(func.count()).select_from(
                rental_orders)).scalar_one(), 1)
            self.assertEqual(db.execute(select(func.count()).select_from(
                action_results)).scalar_one(), 2)
            self.assertEqual(db.execute(select(rental_orders.c.status)).scalar_one(),
                             "cancelled")

    def test_two_cards_stale_price_policy_and_database_uniqueness(self):
        with Session(self.engine) as db:
            first, second = self._book(db), self._book(db)
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", first["id"],
                                            now=self.now)["status"], "confirmed")
            self.assertEqual(confirm_action(db, "t1", "session-1", second["id"],
                                            now=self.now)["status"], "invalidated")
            db.commit()
            with self.assertRaises(ActionError):
                self._book(db)
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as db:
                order = db.execute(select(rental_orders)).mappings().one()
                db.execute(insert(rental_orders).values(
                    id="duplicate", traveler_id="t1", product_id="car-1",
                    pickup_at=order["pickup_at"], dropoff_at=order["dropoff_at"],
                    daily_price=90, billable_days=1, total_price=90,
                    status="confirmed", version=1))
        with Session(self.engine) as db:
            db.execute(update(rental_orders).values(status="cancelled"))
            new_card = self._book(db)
            db.commit()
            db.execute(update(rental_products).where(
                rental_products.c.id == "car-1").values(daily_price=91))
            self.assertEqual(confirm_action(db, "t1", "session-1", new_card["id"],
                                            now=self.now)["status"], "invalidated")
            db.commit()
            another = self._book(db)
            db.commit()
            db.execute(update(policy_documents).where(
                policy_documents.c.id == "rental-actions-demo-v3").values(body="changed"))
            self.assertEqual(confirm_action(db, "t1", "session-1", another["id"],
                                            now=self.now)["status"], "invalidated")
            self.assertEqual(db.execute(select(func.count()).select_from(
                rental_orders)).scalar_one(), 1)

    def test_reject_expire_and_pickup_boundary(self):
        with Session(self.engine) as db:
            rejected, expired = self._book(db), self._book(db)
            db.commit()
            self.assertEqual(reject_action(db, "t1", "session-1", rejected["id"],
                                           now=self.now)["status"], "rejected")
            self.assertEqual(confirm_action(db, "t1", "session-1", expired["id"],
                                            now=self.now + timedelta(minutes=16))["status"],
                             "expired")
            db.commit()
            booked = self._book(db)
            db.commit()
            order_id = confirm_action(db, "t1", "session-1", booked["id"],
                                      now=self.now)["order_id"]
            db.commit()
            pickup = db.execute(select(rental_orders.c.pickup_at)).scalar_one()
            pickup = pickup.replace(tzinfo=timezone.utc) if pickup.tzinfo is None else pickup
            cancel = create_proposal(db, "t1", "session-1", "rental_cancel", {
                "order_id": order_id, "expected_version": 1},
                now=pickup - timedelta(minutes=1))
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", cancel["id"],
                                            now=pickup)["status"], "invalidated")
            self.assertEqual(db.execute(select(rental_orders.c.status)).scalar_one(),
                             "confirmed")

    def test_time_validation_and_24_hour_billing(self):
        with self.assertRaises(OrderError):
            _local_time("2026-03-29T02:30", self.zone)
        with self.assertRaises(OrderError):
            _local_time("2026-10-25T02:30", self.zone)
        first = _local_time("2026-10-25T02:30+02:00", self.zone)
        second = _local_time("2026-10-25T02:30+01:00", self.zone)
        self.assertNotEqual(first.astimezone(timezone.utc),
                            second.astimezone(timezone.utc))
        with self.assertRaises(OrderError):
            _local_time("2026-10-25T02:30+03:00", self.zone)
        fall_pickup = _local_time("2026-10-25T01:30+02:00", self.zone)
        fall_dropoff = _local_time("2026-10-26T01:30+01:00", self.zone)
        self.assertEqual(_billable_days(fall_pickup.astimezone(timezone.utc),
                                        fall_dropoff.astimezone(timezone.utc)), 2)
        spring_pickup = _local_time("2026-03-29T01:30+01:00", self.zone)
        spring_dropoff = _local_time("2026-03-30T01:30+02:00", self.zone)
        self.assertEqual(_billable_days(spring_pickup.astimezone(timezone.utc),
                                        spring_dropoff.astimezone(timezone.utc)), 1)
        pickup = _local_time(self.pickup, self.zone)
        for hours, expected in ((24, 1), (25, 2)):
            dropoff = (pickup.astimezone(timezone.utc) + timedelta(hours=hours)
                       ).astimezone(self.zone).isoformat()
            with Session(self.engine) as db:
                card = self._book(db, dropoff=dropoff)
                self.assertEqual(card["after"]["billable_days"], expected)
                self.assertEqual(card["price"]["payable"], expected * 90)
                db.rollback()
        with Session(self.engine) as db:
            with self.assertRaises(ActionError):
                create_proposal(db, "t1", "session-1", "rental_book", {
                    "product_id": "car-1", "pickup_at": self.pickup}, now=self.now)
            with self.assertRaises(ActionError):
                self._book(db, dropoff=self.pickup)
            with self.assertRaises(ActionError):
                self._book(db, pickup=self.pickup + "+09:00")
            db.execute(update(rental_products).where(
                rental_products.c.id == "car-1").values(
                    available_from=self.check_in + timedelta(days=1)))
            with self.assertRaises(ActionError):
                self._book(db)
