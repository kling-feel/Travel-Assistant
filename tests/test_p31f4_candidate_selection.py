"""A visible candidate creates only a review card and is rechecked at selection."""

import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from db.travel_models import (demo_state, flight_offers, flight_orders, flights,
                              hotel_orders, hotel_products, metadata, pending_actions,
                              rental_orders, rental_products)
from tests.test_p2_actions import ActionFixture
from tests import test_p26a_dialogue as dialogue_cases


class CandidateFlightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dialogue_cases.DialogueTests("runTest")
        self.fixture.setUp()
        with self.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=20, flight_no="SIM-20", departure_airport="HAM",
                arrival_airport="BSL", status="Scheduled",
                departure_at=datetime(2026, 10, 25, 13, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 25, 15, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=20, amount=420, version=1, rule_id="network-fare-v1"))

    def tearDown(self):
        self.fixture.tearDown()

    def test_flight_selection_rechecks_quote_and_owner_without_order_write(self):
        with TestClient(self.fixture.app()) as client:
            auth = self.fixture.auth(client, "demo1", "password1")
            other = self.fixture.auth(client, "demo2", "password2")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            data = {"kind": "flight", "candidate_id": 20, "unit_price": 420,
                    "offer_version": 1, "departure": "HAM", "arrival": "BSL",
                    "travel_date": "2026-10-25"}
            path = f"/api/v1/sessions/{sid}/candidate-proposals"
            self.assertEqual(client.post(path, headers=other, json=data).status_code, 404)
            self.assertEqual(client.post(path, headers=auth,
                                         json={**data, "arrival": "HAM"}).status_code, 409)
            created = client.post(path, headers=auth, json=data)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(created.json()["status"], "pending")
            repeated = client.post(path, headers=auth, json=data)
            self.assertEqual(repeated.json()["id"], created.json()["id"])
            with self.fixture.engine.begin() as db:
                self.assertEqual(db.execute(select(flight_orders.c.id)).all(), [])
                self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)
                db.execute(update(flight_offers).where(flight_offers.c.flight_id == 20)
                           .values(amount=430, version=2))
            self.assertEqual(client.post(path, headers=auth, json=data).status_code, 409)

    def test_latest_visible_references_survive_page_refresh(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "flight_search", "params": {"departure": "Hamburg",
             "arrival": "Basel", "travel_date": "2026-10-25"}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            other = self.fixture.auth(client, "demo2", "password2")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            turn = self.fixture.turn(client, sid, auth, "visible-ref-001",
                                     "查询10月25日汉堡到巴塞尔的航班")
            self.assertEqual(turn.status_code, 200, turn.text)
            path = f"/api/v1/sessions/{sid}/references"
            shown = client.get(path, headers=auth)
            self.assertEqual(shown.status_code, 200, shown.text)
            self.assertEqual(shown.json(), turn.json()["references"])
            self.assertEqual(client.get(path, headers=other).status_code, 404)


class CandidateHotelTests(ActionFixture):
    def test_hotel_selection_requires_dates_and_rechecks_price(self):
        with self.engine.begin() as db:
            metadata.create_all(db, tables=[demo_state])
            db.execute(insert(demo_state).values(
                id=1, as_of=self.check_in - timedelta(days=1),
                fixture_sha256="0" * 64))
        with TestClient(self._app()) as client:
            auth = self._auth(client, "demo1", "password1")
            path = "/api/v1/sessions/session-1/candidate-proposals"
            data = {"kind": "hotel", "candidate_id": "room-1", "unit_price": 80,
                    "city": "Basel", "check_in": self.check_in.isoformat(),
                    "check_out": self.check_out.isoformat()}
            created = client.post(path, headers=auth, json=data)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(created.json()["status"], "pending")
            with self.engine.begin() as db:
                self.assertEqual(len(db.execute(select(hotel_orders.c.id)).all()), 2)
                db.execute(update(hotel_products).where(hotel_products.c.id == "room-1")
                           .values(nightly_price=90))
            self.assertEqual(client.post(path, headers=auth, json=data).status_code, 409)

    def test_rental_selection_requires_times_and_keeps_order_unchanged(self):
        with self.engine.begin() as db:
            metadata.create_all(db, tables=[demo_state])
            db.execute(insert(demo_state).values(
                id=1, as_of=self.check_in - timedelta(days=1),
                fixture_sha256="0" * 64))
            db.execute(insert(rental_products).values(
                id="rental-BSL-1", airport_code="BSL", city="Basel",
                name="Basel car", vehicle_type="Economy", daily_price=80,
                latitude=47.6, longitude=7.5, available_from=self.check_in,
                available_until=self.check_out + timedelta(days=5),
                generation_rule="test"))
        with TestClient(self._app()) as client:
            auth = self._auth(client, "demo1", "password1")
            path = "/api/v1/sessions/session-1/candidate-proposals"
            data = {"kind": "rental", "candidate_id": "rental-BSL-1",
                    "unit_price": 80, "airport": "BSL", "city": "Basel",
                    "pickup_date": self.check_in.isoformat(),
                    "drop_off_date": self.check_out.isoformat()}
            self.assertEqual(client.post(path, headers=auth, json=data).status_code, 409)
            chosen = client.post(path, headers=auth, json={**data,
                "pickup_clock": "12:00", "dropoff_clock": "12:00"})
            self.assertEqual(chosen.status_code, 201, chosen.text)
            self.assertEqual(chosen.json()["action_type"], "rental_book")
            with self.engine.connect() as db:
                self.assertEqual(db.execute(select(rental_orders.c.id)).all(), [])


if __name__ == "__main__":
    unittest.main()
