"""Location claims, landing hotels and active itinerary in one conversation."""

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from sqlalchemy import insert, select, update

from db.travel_models import (airports, flight_offers, flight_orders, flights,
                              hotel_orders, hotels, pending_actions)
from services.recommendations import ReviewedCatalogProvider
from tests import test_p26a_dialogue as dialogue_cases
from tests import test_p31e1_transaction_journey as earlier


class LocationJourneyTests(unittest.TestCase):
    def setUp(self):
        self.case = earlier.TransactionJourneyTests("runTest")
        self.case.setUp()
        self.fixture = self.case.fixture
        with self.fixture.engine.begin() as db:
            db.execute(update(airports).where(airports.c.code == "LGW").values(
                latitude=51.1537, longitude=-0.1821))
            db.execute(update(hotels).where(hotels.c.id == 20).values(
                latitude=51.1600, longitude=-0.1800))
            db.execute(insert(flights).values(
                id=3833, flight_no="SIM-3833", departure_airport="LGW",
                arrival_airport="SHA", status="Scheduled",
                departure_at=datetime(2026, 10, 29, 1, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 29, 13, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=3833, amount=6500, version=1,
                rule_id="network-fare-v1"))

    def tearDown(self):
        self.case.tearDown()

    def _start(self, model):
        client, model_patch, saver_patch = self.fixture.client(model)
        patches = (model_patch, saver_patch,
                   patch("graph_chat.controlled.local_today",
                         return_value=date(2026, 10, 21)),
                   patch("api.travel.get_recommendation_provider",
                         return_value=ReviewedCatalogProvider()))
        return client, patches

    def test_current_city_and_nearby_hotel_do_not_inherit_destination(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": "明白"},
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "location-01", "我现在在上海")
            flight = self.fixture.turn(client, sid, auth, "location-02",
                                       "查10月25日从上海到伦敦的航班")
            self.assertEqual(flight.status_code, 200, flight.text)
            ambiguous = self.fixture.turn(client, sid, auth, "location-03",
                                          "我附近有什么景点")
            self.assertIn("上海", ambiguous.json()["answer"])
            self.assertIn("伦敦", ambiguous.json()["answer"])
            self.assertEqual(ambiguous.json()["references"], [])
            nearby = self.fixture.turn(client, sid, auth, "location-04",
                                       "我现在附近有什么酒店")
            self.assertIn("上海", nearby.json()["answer"])
            self.assertIn("伦敦", nearby.json()["answer"])
            self.assertEqual(nearby.json()["actions"], [])
            chosen = self.fixture.turn(client, sid, auth, "location-05",
                                       "上海现在附近有什么酒店")
            self.assertIn("上海", chosen.json()["answer"])
            self.assertNotIn("伦敦", chosen.json()["answer"])
            self.assertIn("入住日期", chosen.json()["answer"])
            places = self.fixture.turn(client, sid, auth, "location-06",
                                       "我在上海附近有什么景点")
            self.assertIn("上海", places.json()["answer"])
            self.assertIn("城市级地点建议", places.json()["answer"])
            self.assertTrue(places.json()["references"])

    def test_landing_hotel_uses_verified_airport_day_and_distance(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            found = self.fixture.turn(client, sid, auth, "landing-01",
                                      "查10月25日从上海到伦敦的航班")
            self.assertEqual(found.status_code, 200, found.text)
            ask = self.fixture.turn(client, sid, auth, "landing-02",
                                    "找落地附近的酒店")
            self.assertIn("伦敦盖特威克机场", ask.json()["answer"])
            self.assertIn("2026-10-25", ask.json()["answer"])
            self.assertEqual(ask.json()["actions"], [])
            stay = self.fixture.turn(client, sid, auth, "landing-03",
                                     "到达当天入住，住两晚")
            self.assertEqual(stay.status_code, 200, stay.text)
            self.assertIn("room-LGW-1", stay.json()["answer"])
            self.assertIn("2026-10-27", stay.json()["answer"])
            self.assertIn("直线距离", stay.json()["answer"])
            self.assertEqual(stay.json()["actions"], [])

    def test_multiple_flights_need_selection_before_landing_hotel(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=3832, flight_no="SIM-3832", departure_airport="SHA",
                arrival_airport="LGW", status="Scheduled",
                departure_at=datetime(2026, 10, 25, 3, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 25, 16, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=3832, amount=6900, version=1,
                rule_id="network-fare-v1"))
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "multi-01",
                              "查10月25日从上海到伦敦的航班")
            unclear = self.fixture.turn(client, sid, auth, "multi-02",
                                        "找落地附近的酒店")
            self.assertIn("选择班次", unclear.json()["answer"])
            self.assertEqual(unclear.json()["actions"], [])
            selected = self.fixture.turn(client, sid, auth, "multi-03",
                                         "找 SIM-3831 落地附近的酒店")
            self.assertIn("伦敦盖特威克机场", selected.json()["answer"])

    def test_wrong_explicit_flight_id_never_borrows_unique_candidate(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "wrong-id-01",
                              "查10月25日从上海到伦敦的航班")
            wrong = self.fixture.turn(client, sid, auth, "wrong-id-02",
                                      "找 SIM-9999 落地附近的酒店")
            self.assertIn("选择班次", wrong.json()["answer"])
            self.assertEqual(wrong.json()["references"], [])

    def test_new_flight_search_invalidates_old_landing_hotel_context(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "replace-flight-01",
                              "查10月25日从上海到伦敦的航班")
            self.fixture.turn(client, sid, auth, "replace-flight-02",
                              "找落地附近的酒店")
            self.fixture.turn(client, sid, auth, "replace-flight-03",
                              "查10月29日从伦敦到上海的航班")
            old = self.fixture.turn(client, sid, auth, "replace-flight-04",
                                    "到达当天入住，住两晚")
            self.assertNotIn("room-LGW-1", old.json()["answer"])
            self.assertEqual(old.json()["actions"], [])

    def test_owned_future_flight_is_landing_fallback_only_for_owner(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flight_orders).values(
                id="owned-flight", traveler_id="t1", flight_id=3831,
                amount=6585, status="confirmed", version=1))
        model = dialogue_cases.ScriptedModel()
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            owner = self.fixture.auth(client, "demo1", "password1")
            other = self.fixture.auth(client, "demo2", "password2")
            owner_sid = client.post("/api/v1/sessions", headers=owner,
                                    json={}).json()["id"]
            other_sid = client.post("/api/v1/sessions", headers=other,
                                    json={}).json()["id"]
            owner_result = self.fixture.turn(client, owner_sid, owner,
                                             "owned-landing-01", "找落地附近的酒店")
            self.assertIn("伦敦盖特威克机场", owner_result.json()["answer"])
            other_result = self.fixture.turn(client, other_sid, other,
                                             "owned-landing-02", "找落地附近的酒店")
            self.assertIn("选择班次", other_result.json()["answer"])
            self.assertNotIn("伦敦盖特威克机场", other_result.json()["answer"])

    def test_missing_coordinates_do_not_invent_distance(self):
        with self.fixture.engine.begin() as db:
            db.execute(update(hotels).where(hotels.c.id == 20).values(
                latitude=None, longitude=None))
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "no-distance-01",
                              "查10月25日从上海到伦敦的航班")
            self.fixture.turn(client, sid, auth, "no-distance-02",
                              "找落地附近的酒店")
            result = self.fixture.turn(client, sid, auth, "no-distance-03",
                                       "到达当天入住，住两晚")
            self.assertIn("room-LGW-1", result.json()["answer"])
            self.assertNotIn("公里", result.json()["answer"])

    def test_location_survives_checkpointer_reopen(self):
        first_model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": "明白"})
        client, patches = self._start(first_model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "reopen-city-01", "我在上海")
        reopened, patches = self._start(dialogue_cases.ScriptedModel())
        with patches[0], patches[1], patches[2], patches[3], reopened:
            auth = self.fixture.auth(reopened, "demo1", "password1")
            result = self.fixture.turn(reopened, sid, auth, "reopen-city-02",
                                       "我附近有什么酒店")
            self.assertIn("上海", result.json()["answer"])
            self.assertIn("入住日期", result.json()["answer"])

    def test_current_city_does_not_cross_sessions_or_accounts(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": "明白"})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            owner = self.fixture.auth(client, "demo1", "password1")
            other = self.fixture.auth(client, "demo2", "password2")
            first_sid = client.post("/api/v1/sessions", headers=owner,
                                    json={}).json()["id"]
            fresh_sid = client.post("/api/v1/sessions", headers=owner,
                                    json={}).json()["id"]
            other_sid = client.post("/api/v1/sessions", headers=other,
                                    json={}).json()["id"]
            self.fixture.turn(client, first_sid, owner, "scope-city-01", "我在上海")
            for sid, auth, key in ((fresh_sid, owner, "scope-city-02"),
                                   (other_sid, other, "scope-city-03")):
                response = self.fixture.turn(client, sid, auth, key,
                                             "我附近有什么酒店")
                self.assertIn("告诉我你现在所在的城市", response.json()["answer"])

    def test_mixed_booking_keeps_flight_card_and_hotel_question(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "general", "params": {}, "answer": ""})
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.fixture.turn(client, sid, auth, "mixed-01",
                              "查10月25日从上海到伦敦的航班")
            booked = self.fixture.turn(client, sid, auth, "mixed-02",
                                       "预定前面这班机票，顺便找落地附近酒店")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(len(booked.json()["actions"]), 1)
            self.assertEqual(booked.json()["actions"][0]["parameters"]["flight_id"], 3831)
            self.assertIn("伦敦盖特威克机场", booked.json()["answer"])
            self.assertIn("住宿晚数", booked.json()["answer"])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(flight_orders.c.id)).all(), [])
            self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)

    def test_recent_itinerary_excludes_cancelled_orders(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flight_orders).values(
                id="cancelled-flight", traveler_id="t1", flight_id=3831,
                amount=6585, status="cancelled", version=2))
            db.execute(insert(hotel_orders).values(
                id="cancelled-hotel", traveler_id="t1", product_id="room-LGW-1",
                check_in=date(2026, 10, 25), check_out=date(2026, 10, 26),
                total_price=140, status="cancelled", version=2))
        model = dialogue_cases.ScriptedModel()
        client, patches = self._start(model)
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            recent = self.fixture.turn(client, sid, auth, "recent-01",
                                       "我最近有什么行程安排吗")
            self.assertIn("没有进行中或未来", recent.json()["answer"])
            self.assertNotIn("已取消", recent.json()["answer"])
            history = self.fixture.turn(client, sid, auth, "recent-02",
                                        "我的所有订单历史是什么")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertIn("cancelled-flight", history.json()["answer"])


if __name__ == "__main__":
    unittest.main()
