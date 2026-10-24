"""Country destination choices must come from sellable flights and stay in session."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import insert, select, update

from travel_assistant.db.travel_models import airports, flight_offers, flight_orders, flights, pending_actions
from tests.test_p26a_dialogue import ScriptedModel
from tests import test_p31e2a_earliest as earliest_cases
from travel_assistant.services.travel import find_earliest_flights
from travel_assistant.services.country_flights import find_country_flights

NOW = earliest_cases.NOW


class CountryBookingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = earliest_cases.EarliestJourneyTests("runTest")
        self.fixture.setUp()
        with self.fixture.engine.begin() as db:
            db.execute(insert(airports).values(
                code="PEK", name="Beijing Capital", city="Beijing",
                timezone="Asia/Shanghai"))
            db.execute(insert(flights), [
                {"id": 9901, "flight_no": "SIM-9901", "departure_airport": "LHR",
                 "arrival_airport": "PVG", "status": "Scheduled",
                 "departure_at": datetime(2026, 10, 25, 7, tzinfo=timezone.utc),
                 "arrival_at": datetime(2026, 10, 25, 19, tzinfo=timezone.utc)},
                {"id": 9902, "flight_no": "SIM-9902", "departure_airport": "LHR",
                 "arrival_airport": "PEK", "status": "Scheduled",
                 "departure_at": datetime(2026, 10, 25, 6, tzinfo=timezone.utc),
                 "arrival_at": datetime(2026, 10, 25, 18, tzinfo=timezone.utc)},
            ])
            db.execute(insert(flight_offers), [
                {"flight_id": 9901, "amount": 6500, "version": 1,
                 "rule_id": "network-fare-v1"},
                {"flight_id": 9902, "amount": 6200, "version": 1,
                 "rule_id": "network-fare-v1"},
            ])

    def tearDown(self):
        self.fixture.tearDown()

    def turn(self, client, sid, auth, number, message):
        response = self.fixture.fixture.fixture.turn(
            client, sid, auth, f"country-{number:02d}", message)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_london_to_china_earliest_booking_requires_city_choice(self):
        model = ScriptedModel({"intent": "general", "params": {}, "answer": "好的。"})
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        original_search = find_earliest_flights
        with (model_patch, saver_patch,
              patch("graph_chat.controlled.local_today", return_value=date(2026, 10, 21)),
              patch("travel_assistant.services.country_flights._utc_now", return_value=NOW),
              patch("travel_assistant.services.agent_reads.find_earliest_flights",
                    side_effect=lambda db, deps, arrs, **kwargs: original_search(
                        db, deps, arrs, now=NOW, **kwargs)), client):
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.turn(client, sid, auth, 1, "我现在在伦敦")
            choices = self.turn(client, sid, auth, 2, "我要去中国，给我订最早的机票")
            self.assertEqual(choices["actions"], [])
            self.assertIn("按你先前自述在伦敦", choices["answer"])
            self.assertLess(choices["answer"].index("北京"),
                            choices["answer"].index("上海"))
            booked = self.turn(client, sid, auth, 3, "选第一个城市")
            self.assertEqual(len(booked["actions"]), 1, booked["answer"])
            self.assertEqual(booked["actions"][0]["parameters"]["flight_id"], 9902)
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(flight_orders.c.id)).all(), [])
            self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)

    def test_missing_origin_is_asked_once_then_resumed_from_self_report(self):
        model = ScriptedModel()
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        with (model_patch, saver_patch,
              patch("travel_assistant.services.country_flights._utc_now", return_value=NOW), client):
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            first = self.turn(client, sid, auth, 11, "我要去中国，订最早的机票")
            self.assertIn("从哪座城市出发", first["answer"])
            self.assertEqual(first["actions"], [])
            resumed = self.turn(client, sid, auth, 12, "我在伦敦")
            self.assertIn("北京", resumed["answer"])
            self.assertNotIn("还需要出发地", resumed["answer"])
            self.assertEqual(resumed["actions"], [])

    def test_empty_country_route_and_changed_quote_do_not_create_card(self):
        model = ScriptedModel()
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        with (model_patch, saver_patch,
              patch("travel_assistant.services.country_flights._utc_now", return_value=NOW),
              patch("travel_assistant.services.agent_reads.find_earliest_flights",
                    side_effect=lambda db, deps, arrs, **kwargs: find_earliest_flights(
                        db, deps, arrs, now=NOW, **kwargs)), client):
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            choices = self.turn(client, sid, auth, 21,
                                "从伦敦到中国，给我订最早的机票")
            self.assertIn("北京", choices["answer"])
            with self.fixture.engine.begin() as db:
                db.execute(update(flight_offers).where(
                    flight_offers.c.flight_id == 9902).values(amount=6999, version=2))
            changed = self.turn(client, sid, auth, 22, "选第一个城市")
            self.assertEqual(changed["actions"], [])
            self.assertIn("报价已变化", changed["answer"])
            with self.fixture.engine.begin() as db:
                db.execute(update(flight_offers).where(
                    flight_offers.c.flight_id.in_((9901, 9902))).values(
                        rule_id="invalid-rule"))
            other = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            empty = self.turn(client, other, auth, 23,
                              "从伦敦到中国，给我订最早的机票")
            self.assertIn("没有可预订班次", empty["answer"])
            self.assertEqual(empty["actions"], [])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])

    def test_tied_earliest_does_not_auto_select_flight(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=9903, flight_no="SIM-9903", departure_airport="LHR",
                arrival_airport="PEK", status="Scheduled",
                departure_at=datetime(2026, 10, 25, 6, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 25, 18, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=9903, amount=6300, version=1, rule_id="network-fare-v1"))
        model = ScriptedModel()
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        with (model_patch, saver_patch,
              patch("travel_assistant.services.country_flights._utc_now", return_value=NOW),
              patch("travel_assistant.services.agent_reads.find_earliest_flights",
                    side_effect=lambda db, deps, arrs, **kwargs: find_earliest_flights(
                        db, deps, arrs, now=NOW, **kwargs)), client):
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.turn(client, sid, auth, 31, "从伦敦到中国，订最早的机票")
            selected = self.turn(client, sid, auth, 32, "选北京")
            self.assertEqual(selected["actions"], [])
            self.assertIn("同为最早", selected["answer"])

    def test_tomorrow_uses_london_calendar_near_china_midnight(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=9904, flight_no="SIM-9904", departure_airport="LHR",
                arrival_airport="PEK", status="Scheduled",
                departure_at=datetime(2026, 10, 22, 12, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 23, 1, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=9904, amount=6800, version=1, rule_id="network-fare-v1"))
        near_china_midnight = datetime(2026, 10, 21, 17, tzinfo=timezone.utc)
        with (patch("travel_assistant.services.country_flights._utc_now", return_value=near_china_midnight),
              self.fixture.engine.connect() as db):
            result = find_country_flights(db, "London", "CN", relative_offset=1)
        self.assertEqual(result["not_before"], "2026-10-22")
        self.assertEqual(result["options"][0]["flight_id"], 9904)

    def test_location_change_and_new_session_cannot_select_old_country_list(self):
        general = {"intent": "general", "params": {}, "answer": "好的。"}
        model = ScriptedModel(*([general] * 4))
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        with (model_patch, saver_patch,
              patch("travel_assistant.services.country_flights._utc_now", return_value=NOW), client):
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.turn(client, sid, auth, 41, "我在伦敦")
            self.turn(client, sid, auth, 42, "我要去中国，订最早的机票")
            self.turn(client, sid, auth, 43, "我现在在上海")
            old_choice = self.turn(client, sid, auth, 44, "选第一个城市")
            self.assertEqual(old_choice["actions"], [])
            other = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            new_session_choice = self.turn(client, other, auth, 45, "选第一个城市")
            self.assertEqual(new_session_choice["actions"], [])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])

    def test_expired_city_list_cannot_create_booking_card(self):
        model = ScriptedModel()
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        with (model_patch, saver_patch,
              patch("travel_assistant.services.country_flights._utc_now", return_value=NOW), client):
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.turn(client, sid, auth, 51, "从伦敦到中国，订最早的机票")
            with patch("graph_chat.controlled.datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=16)
                expired = self.turn(client, sid, auth, 52, "选第一个城市")
            self.assertEqual(expired["actions"], [])
            self.assertIn("已过期", expired["answer"])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])


if __name__ == "__main__":
    unittest.main()
