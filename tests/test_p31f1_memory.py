"""Conversation memory must preserve user-grounded travel intent safely."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select, update

from travel_assistant.db.travel_models import audit_events, flight_orders, pending_actions
from graph_chat.controlled import _fresh_flight_candidates
from travel_assistant.services.dialogue_context import read_request
from travel_assistant.services.model_config import ModelUnavailable
from travel_assistant.services.travel import find_earliest_flights
from tests.test_p26a_dialogue import ScriptedModel
from tests.test_p31e2a_earliest import EarliestJourneyTests, NOW


class WorkMemoryTests(unittest.TestCase):
    def test_old_candidate_scope_expires(self):
        task = {"departure": "Shanghai", "arrival": "London",
                "travel_date": "2026-10-25"}
        state = {"candidates": {"flight": [{"id": 3832}]},
                 "candidate_scopes": {"flight": {**task, "sort": None,
                     "not_before": None,
                     "observed_at": (datetime.now(timezone.utc) -
                                     timedelta(minutes=16)).isoformat()}}}
        self.assertEqual(_fresh_flight_candidates(state, task), [])

    def test_destination_correction_drops_old_date(self):
        intent, params, _ = read_request(
            "不去伦敦了，我要去巴塞尔旅行，帮我查航班", "general", {},
            {"intent": "flight_search", "departure": "Shanghai",
             "arrival": "London", "travel_date": "2026-10-25",
             "sort": "earliest", "_goal": "book"}, date(2026, 10, 21))
        self.assertEqual(intent, "flight_search")
        self.assertEqual(params["departure"], "Shanghai")
        self.assertEqual(params["arrival"], "Basel")
        self.assertNotIn("travel_date", params)
        self.assertNotIn("sort", params)
        self.assertNotIn("_goal", params)

    def setUp(self):
        self.fixture = EarliestJourneyTests("runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _test_context(self, model):
        client, model_patch, saver_patch = self.fixture.fixture.fixture.client(model)
        original = find_earliest_flights
        return (client, model_patch, saver_patch,
                patch("graph_chat.controlled.local_today", return_value=date(2026, 10, 21)),
                patch("travel_assistant.services.agent_reads.find_earliest_flights",
                      side_effect=lambda db, deps, arrs, **kwargs: original(
                          db, deps, arrs, now=NOW, **kwargs)))

    def _turn(self, client, sid, auth, number, message):
        response = self.fixture.fixture.fixture.turn(
            client, sid, auth, f"memory-turn-{number:03d}", message)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_london_booking_continues_and_recaps_without_model(self):
        model = ScriptedModel(*(
            {"intent": "general", "params": {}, "answer": "你好，我是旅行助手。"}
            for _ in range(22)))
        client, model_patch, saver_patch, clock_patch, search_patch = self._test_context(model)
        with model_patch, saver_patch, clock_patch, search_patch, client:
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            first = self._turn(client, sid, auth, 1, "我要去伦敦旅行，帮我购买机票")
            self.assertIn("伦敦", first["answer"])
            self.assertIn("出发地", first["answer"])
            self.assertEqual(first["actions"], [])
            second = self._turn(client, sid, auth, 2,
                                "我在上海明天就出发，没有的话就帮我预订最早的航班")
            self.assertEqual(len(second["actions"]), 1)
            card = second["actions"][0]
            self.assertEqual(card["parameters"]["flight_id"], 3832)
            recap = self._turn(client, sid, auth, 3, "我要去伦敦")
            self.assertIn("从上海", recap["answer"])
            self.assertIn("去伦敦", recap["answer"])
            self.assertIn("3832", recap["answer"])
            self.assertEqual(recap["actions"], [])
            for index in range(4, 26):
                other = self._turn(client, sid, auth, index, "你好")
                self.assertEqual(other["actions"], [])
            with patch("api.travel.get_chat_model", side_effect=ModelUnavailable("offline")):
                memory = self._turn(client, sid, auth, 26, "你记得我们前面说过什么吗")
            self.assertIn("从上海", memory["answer"])
            self.assertIn("去伦敦", memory["answer"])
            self.assertIn("待确认卡", memory["answer"])
            self.assertEqual(len(model.history_lengths), 22)
            repeat = self._turn(client, sid, auth, 27, "就预订最早那班")
            self.assertEqual(repeat["actions"][0]["id"], card["id"])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(flight_orders.c.id)).all(), [])
            self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)
            self.assertEqual(len(db.execute(select(audit_events.c.id).where(
                audit_events.c.event_type == "proposed")).all()), 1)

    def test_new_session_and_changed_route_do_not_reuse_old_candidate(self):
        model = ScriptedModel(*([{"intent": "transaction", "params": {"actions": [
            {"action_type": "flight_book", "parameters": {"flight_id": 3832}}]},
            "answer": ""}] * 4))
        client, model_patch, saver_patch, clock_patch, search_patch = self._test_context(model)
        with model_patch, saver_patch, clock_patch, search_patch, client:
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            first = self._turn(client, sid, auth, 21,
                               "查10月25日从上海到伦敦的航班")
            self.assertIn("SIM-3832", first["answer"])
            changed = self._turn(client, sid, auth, 22,
                                 "改成从上海到巴塞尔，查10月25日航班")
            self.assertEqual(changed["actions"], [])
            old_choice = self._turn(client, sid, auth, 23, "我要预订前面 SIM-3832 航班")
            self.assertEqual(old_choice["actions"], [])
            other_sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            isolated = self._turn(client, other_sid, auth, 24,
                                  "我要预订前面 SIM-3832 航班")
            self.assertEqual(isolated["actions"], [])
            with patch("api.travel.get_chat_model", side_effect=ModelUnavailable("offline")):
                other_memory = self._turn(client, other_sid, auth, 25,
                                          "你记得前面说过什么吗")
            self.assertNotIn("伦敦", other_memory["answer"])
            self.assertNotIn("上海", other_memory["answer"])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])

    def test_model_query_cannot_replace_user_route(self):
        model = ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "transaction", "params": {
                "actions": [{"action_type": "flight_book", "parameters": {"flight_id": 1}}],
                "queries": [{"intent": "flight_search", "params": {
                    "departure": "Hamburg", "arrival": "Basel",
                    "travel_date": "2026-10-25"}}]}, "answer": ""})
        client, model_patch, saver_patch, clock_patch, search_patch = self._test_context(model)
        with model_patch, saver_patch, clock_patch, search_patch, client:
            auth = self.fixture.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self._turn(client, sid, auth, 31, "查10月25日从上海到伦敦的航班")
            answer = self._turn(client, sid, auth, 32, "我要预订这个航班")
            self.assertEqual(answer["actions"], [])
            self.assertNotIn("汉堡", answer["answer"])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])


if __name__ == "__main__":
    unittest.main()
