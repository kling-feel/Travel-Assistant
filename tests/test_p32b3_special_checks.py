"""Preserve useful recovery checks without legacy database test runners."""

import unittest
from unittest.mock import patch

from sqlalchemy import select

from db.travel_models import chat_messages, flight_orders, hotel_orders, pending_actions, rental_orders
from services.weather_search import WeatherSearchError
from tests import test_p26a_dialogue as dialogue_cases


class SpecialChecksTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dialogue_cases.DialogueTests("runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _business_counts(self):
        with self.fixture.engine.connect() as db:
            return tuple(len(db.execute(select(table.c.id)).all()) for table in (
                flight_orders, hotel_orders, rental_orders, pending_actions))

    def test_recommendation_survives_app_recreation_with_owned_history(self):
        provider = dialogue_cases.FixedPlaceProvider()
        first_model = dialogue_cases.ScriptedModel({"intent": "recommendation", "params": {"city": "Basel"},
                                     "answer": "景点已预订"})
        client, model_patch, saver_patch = self.fixture.client(first_model)
        with model_patch, saver_patch, patch(
                "api.travel.get_recommendation_provider", return_value=provider), client:
            owner = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=owner, json={}).json()["id"]
            first = self.fixture.turn(client, sid, owner, "place-before-restart",
                                      "推荐 Basel 的经典地点")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(len(first.json()["references"]), 2)
            self.assertEqual(first.json()["actions"], [])
            self.assertNotIn("景点已预订", first.json()["answer"])

        second_model = dialogue_cases.ScriptedModel({"intent": "recommendation", "params": {"city": "Basel"},
                                      "answer": ""})
        recreated, model_patch, saver_patch = self.fixture.client(second_model)
        with model_patch, saver_patch, patch(
                "api.travel.get_recommendation_provider", return_value=provider), recreated:
            owner = self.fixture.auth(recreated, "demo1", "password1")
            outsider = self.fixture.auth(recreated, "demo2", "password2")
            second = self.fixture.turn(recreated, sid, owner, "place-after-restart",
                                       "再推荐巴塞尔的经典地点")
            self.assertEqual(second.status_code, 200, second.text)
            self.assertTrue(second.json()["references"])
            self.assertTrue(all(ref["type"] == "place_source"
                                for ref in second.json()["references"]))
            self.assertEqual(second.json()["actions"], [])
            history = recreated.get(f"/api/v1/sessions/{sid}", headers=owner).json()
            self.assertEqual([message["role"] for message in history["messages"]],
                             ["user", "assistant", "user", "assistant"])
            self.assertEqual(recreated.get(f"/api/v1/sessions/{sid}",
                                             headers=outsider).status_code, 404)
        self.assertIsNotNone(self.fixture.saver.get_tuple(
            {"configurable": {"thread_id": sid}}))
        self.assertEqual(self._business_counts(), (0, 0, 0, 0))

    def test_weather_timeout_has_no_evidence_card_or_order_change(self):
        before = self._business_counts()
        model = dialogue_cases.ScriptedModel({"intent": "weather", "params": {
            "city": "Shanghai", "target_date": "2026-10-19"}, "answer": "明天晴天"})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch(
                "services.agent_reads.search_weather",
                side_effect=WeatherSearchError("timeout")), client:
            owner = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=owner, json={}).json()["id"]
            response = self.fixture.turn(client, sid, owner, "weather-timeout",
                                         "2026-10-19 上海天气怎么样？")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("无法核实", response.json()["answer"])
            self.assertNotIn("明天晴天", response.json()["answer"])
            self.assertEqual(response.json()["references"], [])
            self.assertEqual(response.json()["actions"], [])
            history = client.get(f"/api/v1/sessions/{sid}", headers=owner).json()
            self.assertEqual([message["role"] for message in history["messages"]],
                             ["user", "assistant"])
        self.assertEqual(self._business_counts(), before)
        with self.fixture.engine.connect() as db:
            self.assertEqual(len(db.execute(select(chat_messages.c.id)).all()), 2)


if __name__ == "__main__":
    unittest.main()
