"""Traveler-facing recap, itinerary and nearby place regressions."""

import unittest

from datetime import timedelta

from sqlalchemy import insert, select

from travel_assistant.db.travel_models import flight_orders, flights, pending_actions
from travel_assistant.services.agent_reads import read_for_assistant
from tests.dialogue.test_p26a_dialogue import ScriptedModel
from tests.recommendations import test_p31e3a_location_journey as location_cases
from tests.dialogue import test_p31f2_itinerary_cancel as itinerary_cases


class ItinerarySummaryTests(unittest.TestCase):
    def setUp(self):
        self.case = itinerary_cases.ItineraryCancelTests("runTest")
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def test_current_summary_names_route_times_status_and_omits_canceled(self):
        with self.case.fixture.engine.connect() as db:
            result = read_for_assistant(db, "t1", "itinerary",
                                        {"_user_text": "我现在有什么行程"})
        answer = result["answer"]
        self.assertIn("汉堡机场 → 巴塞尔米卢斯机场", answer)
        self.assertIn("到达", answer)
        self.assertIn("已确认", answer)
        self.assertIn("退房", answer)
        self.assertIn("还车", answer)
        self.assertNotIn("cancelled-flight", str(result))

    def test_location_warning_only_for_relevant_upcoming_itinerary(self):
        with self.case.fixture.engine.connect() as db:
            result = read_for_assistant(db, "t1", "itinerary",
                                        {"_user_text": "我明天怎么安排出发", "_current_city": "London"})
        self.assertIn("伦敦", result["answer"])
        self.assertIn("汉堡", result["answer"])
        self.assertIn("核对", result["answer"])
        self.assertNotIn("赶不上", result["answer"])

    def test_long_summary_reports_hidden_count(self):
        with self.case.fixture.engine.begin() as db:
            db.execute(insert(flights), [
                {"id": 100 + number, "flight_no": f"SIM-{100 + number}",
                 "departure_airport": "HAM", "arrival_airport": "BSL",
                 "status": "Scheduled",
                 "departure_at": self.case.future + timedelta(hours=number + 1),
                 "arrival_at": self.case.future + timedelta(hours=number + 3)}
                for number in range(4)
            ])
            db.execute(insert(flight_orders), [
                {"id": f"extra-flight-{number}", "traveler_id": "t1", "flight_id": 100 + number,
                 "amount": 400, "status": "confirmed", "version": 1}
                for number in range(4)
            ])
        with self.case.fixture.engine.connect() as db:
            result = read_for_assistant(db, "t1", "itinerary",
                                        {"_user_text": "我现在有什么行程"})
        self.assertIn("另有 4 笔有效行程", result["answer"])
        self.assertEqual(len(result["references"]), 6)

    def test_two_cancel_cards_remain_pending_during_recap_and_itinerary(self):
        model = ScriptedModel({"intent": "general", "params": {}, "answer": "好的。"})
        client, model_patch, saver_patch = self.case.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.case.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            proposed = self.case.fixture.turn(client, sid, auth, "g2-cards-01",
                                              "保留伦敦，取消其他安排")
            self.assertEqual(proposed.status_code, 200, proposed.text)
            self.assertEqual(len(proposed.json()["actions"]), 2)
            recap = self.case.fixture.turn(client, sid, auth, "g2-cards-02",
                                           "我们前面说过了什么")
            self.assertEqual(recap.status_code, 200, recap.text)
            self.assertIn("2 张待确认卡", recap.json()["answer"])
            self.assertIn("订单目前没有因这些卡改变", recap.json()["answer"])
            itinerary = self.case.fixture.turn(client, sid, auth, "g2-cards-03",
                                               "我现在有什么行程")
            self.assertEqual(itinerary.status_code, 200, itinerary.text)
            self.assertNotIn("已取消", itinerary.json()["answer"])
            orders = client.get("/api/v1/me/itinerary", headers=auth).json()
            self.assertEqual({row["status"] for kind in orders for row in orders[kind]
                              if row["id"] in ("basel-flight", "basel-hotel")}, {"confirmed"})


class NearbyAndRecapTests(unittest.TestCase):
    def setUp(self):
        self.case = location_cases.LocationJourneyTests("runTest")
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def turn(self, client, sid, auth, number, text):
        response = self.case.fixture.turn(client, sid, auth,
                                          f"g2-turn-{number:02d}", text)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_london_self_report_recap_and_surrounding_places(self):
        general = {"intent": "general", "params": {}, "answer": "好的。"}
        client, patches = self.case._start(ScriptedModel(general))
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.case.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.turn(client, sid, auth, 1, "我在伦敦")
            recap = self.turn(client, sid, auth, 2, "我们前面说过了什么")
            self.assertIn("伦敦", recap["answer"])
            nearby = self.turn(client, sid, auth, 3, "我周围有什么好看的景点")
            self.assertIn("伦敦", nearby["answer"])
            self.assertIn("自述", nearby["answer"])
            self.assertNotIn("步行", nearby["answer"])
            self.assertTrue(nearby["references"])
            other = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            new_recap = self.turn(client, other, auth, 4, "我们前面说过了什么")
            self.assertNotIn("伦敦", new_recap["answer"])
            new_nearby = self.turn(client, other, auth, 5, "我周围有什么景点")
            self.assertIn("城市", new_nearby["answer"])
            self.assertNotIn("伦敦", new_nearby["answer"])
        with self.case.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])

    def test_surrounding_places_clarify_conflicting_anchors(self):
        general = {"intent": "general", "params": {}, "answer": "好的。"}
        client, patches = self.case._start(ScriptedModel(general, general))
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.case.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            self.turn(client, sid, auth, 11, "我在上海")
            self.turn(client, sid, auth, 12, "查10月25日从上海到伦敦的航班")
            answer = self.turn(client, sid, auth, 13, "我周围有什么景点")
            self.assertIn("上海", answer["answer"])
            self.assertIn("伦敦", answer["answer"])
            self.assertEqual(answer["references"], [])

    def test_model_identifier_is_only_a_configuration_label(self):
        client, patches = self.case._start(ScriptedModel())
        with patches[0], patches[1], patches[2], patches[3], client:
            auth = self.case.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            answer = self.turn(client, sid, auth, 21, "你是什么模型内核")
            self.assertIn("配置标识", answer["answer"])
            self.assertIn("底层", answer["answer"])
            self.assertEqual(answer["actions"], [])


if __name__ == "__main__":
    unittest.main()
