"""Regressions from the first real traveler conversation."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from travel_assistant.db.travel_models import (airports, flight_offers, flight_orders, flights,
                              hotel_orders, hotel_products, hotels, pending_actions)
from travel_assistant.services.agent_feedback import cancellation_overview, explain_action_error
from travel_assistant.services.agent_reads import read_for_assistant
from travel_assistant.services.recommendations import ReviewedCatalogProvider
from tests import test_p26a_dialogue as dialogue_cases


class RealConversationRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture = dialogue_cases.DialogueTests("runTest")
        self.fixture.setUp()
        with self.fixture.engine.begin() as db:
            db.execute(insert(flight_orders).values(
                id="own-flight", traveler_id="t1", flight_id=1,
                amount=120, status="confirmed", version=1))

    def tearDown(self):
        self.fixture.tearDown()

    def test_itinerary_followups_show_owned_local_times(self):
        model = dialogue_cases.ScriptedModel({"intent": "itinerary", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            one = self.fixture.auth(client, "demo1", "password1")
            two = self.fixture.auth(client, "demo2", "password2")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            first = self.fixture.turn(client, sid, one, "p31c-itinerary-1", "我有什么行程订单")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertNotIn("own-flight", first.json()["answer"])
            follow = self.fixture.turn(client, sid, one, "p31c-itinerary-2", "告诉我对应的时间")
            self.assertNotIn("own-flight", follow.json()["answer"])
            focused = self.fixture.turn(client, sid, one, "p31c-itinerary-3", "告诉我航班的起飞时间")
            self.assertNotIn("own-flight", focused.json()["answer"])
            self.assertNotIn("酒店：", focused.json()["answer"])
            history = self.fixture.turn(client, sid, one, "p31c-itinerary-4", "查看历史订单")
            self.assertIn("起飞 2026-10-19 17:43", history.json()["answer"])
            self.assertIn("已结束", history.json()["answer"])
            self.assertEqual(len(model.history_lengths), 1)
            foreign = client.get("/api/v1/me/itinerary", headers=two)
            self.assertEqual(foreign.json()["flight_orders"], [])

    def test_past_order_error_is_localized_and_feedback_does_not_retry(self):
        model = dialogue_cases.ScriptedModel({"intent": "transaction", "params": {"actions": [
            {"action_type": "flight_cancel", "parameters": {"order_id": "own-flight"}}]},
            "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            one = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            result = self.fixture.turn(client, sid, one, "p31c-cancel-1", "取消机票订单 own-flight")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["actions"], [])
            self.assertIn("2026-10-19 17:43", result.json()["answer"])
            self.assertNotIn("flight no longer available", result.json()["answer"])
            complaint = self.fixture.turn(client, sid, one, "p31c-cancel-2", "你听不懂我的话吗")
            self.assertEqual(complaint.status_code, 200, complaint.text)
            self.assertEqual(complaint.json()["actions"], [])
            self.assertEqual(len(model.history_lengths), 1)
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])
            self.assertEqual(db.execute(select(flight_orders.c.status).where(
                flight_orders.c.id == "own-flight")).scalar_one(), "confirmed")

    def test_feedback_after_new_card_says_order_is_unchanged(self):
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        with self.fixture.engine.begin() as db:
            db.execute(update(flights).where(flights.c.id == 1).values(
                departure_at=tomorrow, arrival_at=tomorrow + timedelta(hours=2)))
        model = dialogue_cases.ScriptedModel({"intent": "transaction", "params": {"actions": [
            {"action_type": "flight_cancel", "parameters": {}}]}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            one = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            first = self.fixture.turn(client, sid, one, "p31c-card-1", "取消我的机票")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(len(first.json()["actions"]), 1)
            second = self.fixture.turn(client, sid, one, "p31c-card-2", "你听不懂吗")
            self.assertIn("只生成了待确认卡", second.json()["answer"])
            self.assertEqual(second.json()["actions"], [])
            self.assertEqual(len(model.history_lengths), 1)
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(flight_orders.c.status).where(
                flight_orders.c.id == "own-flight")).scalar_one(), "confirmed")

    def test_all_cancel_summarizes_every_order_and_has_no_card_for_past_trip(self):
        model = dialogue_cases.ScriptedModel({"intent": "general", "params": {}, "answer": "请再说一次"})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            one = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            result = self.fixture.turn(client, sid, one, "p31c-all-001", "取消所有机票和酒店")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn("机票订单 own-flight", result.json()["answer"])
            self.assertIn("已过起飞时间", result.json()["answer"])
            self.assertEqual(result.json()["actions"], [])

    def test_country_request_overrides_old_trip_without_paid_search(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(airports).values(
                code="PEK", name="Fictional Beijing", city="Beijing",
                timezone="Asia/Shanghai"))
            db.execute(insert(flights).values(
                id=2, flight_no="SIM-2", departure_airport="HAM",
                arrival_airport="PEK", status="Scheduled",
                departure_at=datetime(2026, 10, 20, 8, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 20, 18, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=2, amount=200, version=1, rule_id="network-fare-v1"))
        model = dialogue_cases.ScriptedModel()
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("api.travel.get_recommendation_provider",
                                             return_value=ReviewedCatalogProvider()), client:
            one = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            result = self.fixture.turn(client, sid, one, "p31c-country-1", "我想去中国旅游，有什么地方推荐")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn("北京", result.json()["answer"])
            self.assertNotIn("巴塞尔", result.json()["answer"])
            self.assertEqual(result.json()["references"], [])
            self.assertEqual(model.history_lengths, [])

    def test_hotel_cutoff_uses_owned_date_and_mass_summary(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(hotels).values(
                id=1, name="Example hotel", city="Basel", price_tier="Budget",
                nearby_airport="BSL"))
            db.execute(insert(hotel_products).values(
                id="room-1", hotel_id=1, room_type="Standard", nightly_price=80,
                available_from=date(2026, 10, 18), available_until=date(2026, 11, 18)))
            db.execute(insert(hotel_orders).values(
                id="own-hotel", traveler_id="t1", product_id="room-1",
                check_in=date(2026, 10, 19), check_out=date(2026, 10, 20),
                total_price=80, status="confirmed", version=1))
        with Session(self.fixture.engine) as db:
            msg = explain_action_error(db, "t1", [{"action_type": "hotel_cancel",
                "parameters": {"order_id": "own-hotel"}}],
                ValueError("hotel cancellation closes on local check-in date"))
            self.assertIn("2026-10-19", msg)
            self.assertNotIn("hotel cancellation", msg)
            summary, plans = cancellation_overview(db, "t1")
            self.assertIn("酒店订单 own-hotel", summary)
            self.assertEqual(plans, [])


if __name__ == "__main__":
    unittest.main()
