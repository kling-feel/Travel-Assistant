"""Canceled history and condition-based cancellation remain traveler-scoped."""

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update

from api.travel import _card_brief
from travel_assistant.db.travel_models import (airports, flight_orders, flights, hotel_orders,
                              hotel_products, hotels, policy_documents,
                              rental_orders, rental_products)
from travel_assistant.services.agent_reads import read_for_assistant
from travel_assistant.services.agent_feedback import bulk_cancellation_requested, cancellation_overview
from travel_assistant.services.order_targeting import matching_orders
from travel_assistant.services.policy_catalog import CURRENT_POLICIES
from travel_assistant.services.location_labels import airport_zh
from tests import test_p26a_dialogue as cases


class ItineraryCancelTests(unittest.TestCase):
    def test_cancel_card_keeps_meaningful_airport_name(self):
        card = {"action_type": "flight_cancel", "after": {
            "departure_airport": "HAM", "arrival_airport": "BSL",
            "flight_no": "SIM-8152", "departure_local": "2026-10-24T17:58:00+02:00"}}
        self.assertIn("汉堡机场 → 巴塞尔米卢斯机场", _card_brief(card))
        self.assertEqual(airport_zh("ZZZ", ""), "ZZZ")

    def setUp(self):
        self.fixture = cases.DialogueTests("runTest")
        self.fixture.setUp()
        now = datetime.now(timezone.utc)
        self.future = now + timedelta(days=3)
        self.stay = (now + timedelta(days=4)).date()
        with self.fixture.engine.begin() as db:
            db.execute(insert(policy_documents), [CURRENT_POLICIES["hotel"],
                                                  CURRENT_POLICIES["rental"]])
            db.execute(insert(airports), [
                {"code": "LGW", "name": "London", "city": "London", "timezone": "Europe/London"},
                {"code": "SHA", "name": "Shanghai", "city": "Shanghai", "timezone": "Asia/Shanghai"},
            ])
            db.execute(insert(flights), [
                {"id": 2, "flight_no": "SIM-2", "departure_airport": "HAM",
                 "arrival_airport": "BSL", "status": "Scheduled",
                 "departure_at": self.future, "arrival_at": self.future + timedelta(hours=2)},
                {"id": 3, "flight_no": "SIM-3", "departure_airport": "SHA",
                 "arrival_airport": "LGW", "status": "Scheduled",
                 "departure_at": self.future, "arrival_at": self.future + timedelta(hours=12)},
            ])
            db.execute(insert(flight_orders), [
                {"id": "basel-flight", "traveler_id": "t1", "flight_id": 2,
                 "amount": 400, "status": "confirmed", "version": 1},
                {"id": "london-flight", "traveler_id": "t1", "flight_id": 3,
                 "amount": 900, "status": "confirmed", "version": 1},
                {"id": "cancelled-flight", "traveler_id": "t1", "flight_id": 1,
                 "amount": 100, "status": "cancelled", "version": 2},
                {"id": "other-flight", "traveler_id": "t2", "flight_id": 2,
                 "amount": 400, "status": "confirmed", "version": 1},
            ])
            db.execute(insert(hotels), [
                {"id": 1, "name": "Basel stay", "city": "Basel", "nearby_airport": "BSL"},
                {"id": 2, "name": "London stay", "city": "London", "nearby_airport": "LGW"},
            ])
            db.execute(insert(hotel_products), [
                {"id": "basel-room", "hotel_id": 1, "room_type": "Standard", "nightly_price": 100,
                 "available_from": self.stay, "available_until": self.stay + timedelta(days=20)},
                {"id": "london-room", "hotel_id": 2, "room_type": "Standard", "nightly_price": 150,
                 "available_from": self.stay, "available_until": self.stay + timedelta(days=20)},
            ])
            db.execute(insert(hotel_orders), [
                {"id": "basel-hotel", "traveler_id": "t1", "product_id": "basel-room",
                 "check_in": self.stay, "check_out": self.stay + timedelta(days=1),
                 "total_price": 100, "status": "confirmed", "version": 1},
                {"id": "london-hotel", "traveler_id": "t1", "product_id": "london-room",
                 "check_in": self.stay, "check_out": self.stay + timedelta(days=1),
                 "total_price": 150, "status": "confirmed", "version": 1},
            ])
            db.execute(insert(rental_products), [
                {"id": "basel-car", "airport_code": "BSL", "city": "Basel", "name": "Basel car",
                 "vehicle_type": "Compact", "daily_price": 50, "latitude": 47.6, "longitude": 7.5,
                 "available_from": self.stay, "available_until": self.stay + timedelta(days=20),
                 "generation_rule": "test"},
                {"id": "london-car", "airport_code": "LGW", "city": "London", "name": "London car",
                 "vehicle_type": "Compact", "daily_price": 60, "latitude": 51.1, "longitude": -0.2,
                 "available_from": self.stay, "available_until": self.stay + timedelta(days=20),
                 "generation_rule": "test"},
            ])
            db.execute(insert(rental_orders), [
                {"id": "basel-rental", "traveler_id": "t1", "product_id": "basel-car",
                 "pickup_at": self.future, "dropoff_at": self.future + timedelta(days=1),
                 "daily_price": 50, "billable_days": 1, "total_price": 50,
                 "status": "confirmed", "version": 1},
                {"id": "london-rental", "traveler_id": "t1", "product_id": "london-car",
                 "pickup_at": self.future, "dropoff_at": self.future + timedelta(days=1),
                 "daily_price": 60, "billable_days": 1, "total_price": 60,
                 "status": "confirmed", "version": 1},
            ])

    def tearDown(self):
        self.fixture.tearDown()

    def test_bulk_command_rejects_negation_and_policy_questions(self):
        self.assertTrue(bulk_cancellation_requested("保留伦敦，取消其他安排"))
        self.assertTrue(bulk_cancellation_requested("取消所有机票和酒店"))
        for text in ("不要取消所有机票", "能取消所有酒店吗", "已取消的所有酒店有哪些",
                     "如何取消所有租车订单", "我取消过的全部机票"):
            self.assertFalse(bulk_cancellation_requested(text))

    def test_current_history_and_full_api_are_distinct(self):
        with self.fixture.engine.connect() as db:
            current = read_for_assistant(db, "t1", "itinerary", {"_user_text": "我现在有什么行程"})
            self.assertIn("london-flight", str(current["references"]))
            self.assertNotIn("cancelled-flight", str(current))
            history = read_for_assistant(db, "t1", "itinerary", {"_user_text": "我取消过什么机票"})
            self.assertIn("cancelled-flight", history["answer"])
            self.assertNotIn("basel-flight", history["answer"])
            self.assertIn("cancelled-flight", read_for_assistant(
                db, "t1", "itinerary", {"_user_text": "查看取消历史"})["answer"])
        client, model_patch, saver_patch = self.fixture.client(cases.ScriptedModel())
        with model_patch, saver_patch, client:
            one = self.fixture.auth(client, "demo1", "password1")
            two = self.fixture.auth(client, "demo2", "password2")
            self.assertEqual(len(client.get("/api/v1/me/itinerary", headers=one).json()["flight_orders"]), 3)
            self.assertEqual([row["id"] for row in client.get(
                "/api/v1/me/itinerary", headers=two).json()["flight_orders"]], ["other-flight"])

    def test_departure_attribute_and_multiple_match(self):
        with self.fixture.engine.connect() as db:
            own = db.execute(select(flight_orders).where(
                flight_orders.c.traveler_id == "t1", flight_orders.c.status == "confirmed"
            )).mappings().all()
            matches = matching_orders(db, "t1", "flight", "取消汉堡机场出发的航班", own)
            self.assertEqual([row["id"] for row in matches], ["basel-flight"])
            self.assertEqual([row["id"] for row in matching_orders(
                db, "t1", "flight", "取消从上海到伦敦的航班", own)], ["london-flight"])
            self.assertEqual(matching_orders(db, "t1", "flight", "取消订单 other-flight", own), [])
            self.assertEqual(matching_orders(db, "t1", "flight",
                                             "取消伦敦出发的订单 basel-flight", own), [])
            hotels = db.execute(select(hotel_orders).where(
                hotel_orders.c.traveler_id == "t1")).mappings().all()
            self.assertEqual([row["id"] for row in matching_orders(
                db, "t1", "hotel", "取消伦敦的酒店", hotels)], ["london-hotel"])
            rentals = db.execute(select(rental_orders).where(
                rental_orders.c.traveler_id == "t1")).mappings().all()
            self.assertEqual([row["id"] for row in matching_orders(
                db, "t1", "rental", "取消巴塞尔取车的租车", rentals)], ["basel-rental"])
        model = cases.ScriptedModel({"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            result = self.fixture.turn(client, sid, auth, "f2-origin-001", "帮我把汉堡机场出发的航班取消")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual([card["target_order_id"] for card in result.json()["actions"]],
                             ["basel-flight"])
            self.assertIn("待确认卡", result.json()["answer"])

    def test_duplicate_departure_requires_choice_and_other_account_is_hidden(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=4, flight_no="SIM-4", departure_airport="HAM",
                arrival_airport="BSL", status="Scheduled",
                departure_at=self.future + timedelta(days=1),
                arrival_at=self.future + timedelta(days=1, hours=2)))
            db.execute(insert(flight_orders).values(
                id="second-hamburg-flight", traveler_id="t1", flight_id=4,
                amount=420, status="confirmed", version=1))
        model = cases.ScriptedModel({"intent": "transaction", "params": {"actions": [
            {"action_type": "flight_cancel", "parameters": {"order_id": "basel-flight"}}]},
            "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            result = self.fixture.turn(client, sid, auth, "f2-multiple-001",
                                       "取消汉堡机场出发的航班")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["actions"], [])
            self.assertIn("SIM-2", result.json()["answer"])
            self.assertIn("SIM-4", result.json()["answer"])
            self.assertNotIn("other-flight", result.json()["answer"])

    def test_cross_city_flight_halts_cards_but_lists_other_affected_orders(self):
        with self.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=4, flight_no="SIM-4", departure_airport="LGW",
                arrival_airport="BSL", status="Scheduled",
                departure_at=self.future + timedelta(days=1),
                arrival_at=self.future + timedelta(days=1, hours=2)))
            db.execute(insert(flight_orders).values(
                id="cross-city-flight", traveler_id="t1", flight_id=4,
                amount=500, status="confirmed", version=1))
        with self.fixture.engine.connect() as db:
            answer, cards = cancellation_overview(
                db, "t1", "保留伦敦，取消其他安排")
        self.assertEqual(cards, [])
        self.assertIn("cross-city-flight", answer)
        self.assertIn("basel-flight", answer)
        self.assertIn("basel-hotel", answer)
        self.assertIn("basel-rental", answer)
        self.assertIn("london-flight", answer)
        self.assertIn("没有生成取消卡", answer)

    def test_bulk_hotel_cancellation_excludes_past_check_in(self):
        with self.fixture.engine.begin() as db:
            db.execute(update(hotel_orders).where(hotel_orders.c.id == "basel-hotel")
                       .values(check_in=(datetime.now(timezone.utc) - timedelta(days=1)).date(),
                               check_out=(datetime.now(timezone.utc) + timedelta(days=1)).date()))
        with self.fixture.engine.connect() as db:
            answer, cards = cancellation_overview(db, "t1", "取消所有酒店")
        self.assertEqual([row["parameters"]["order_id"] for row in cards],
                         ["london-hotel"])
        self.assertIn("basel-hotel", answer)
        self.assertIn("已过取消截止日期", answer)

    def test_preserve_london_lists_remainder_and_confirms_only_one(self):
        model = cases.ScriptedModel()
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            result = self.fixture.turn(client, sid, auth, "f2-bulk-001", "保留伦敦，取消其他安排")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(model.history_lengths, [])
            answer, cards = result.json()["answer"], result.json()["actions"]
            self.assertEqual(len(cards), 2)
            self.assertIn("basel-rental", answer)
            self.assertIn("尚未生成卡", answer)
            self.assertIn("london-flight", answer)
            self.assertNotIn("T10:00:00", answer)
            self.assertEqual(answer.count("本轮仅为"), 1)
            self.assertEqual(answer.count("请在下方逐张核对"), 1)
            self.assertNotIn("需逐笔确认。", answer)
            self.assertTrue(all(card["target_order_id"].startswith("basel-") for card in cards))
            before = client.get("/api/v1/me/itinerary", headers=auth).json()
            self.assertTrue(all(row["status"] == "confirmed" for row in before["hotel_orders"]))
            pointer = self.fixture.turn(client, sid, auth, "f2-pointer-001", "只取消刚才那一笔")
            self.assertEqual(pointer.status_code, 200, pointer.text)
            self.assertEqual(pointer.json()["actions"], [])
            self.assertIn("多张待确认", pointer.json()["answer"])
            card = cards[0]
            other = self.fixture.auth(client, "demo2", "password2")
            self.assertEqual(client.post(
                f"/api/v1/sessions/{sid}/actions/{card['id']}/confirm",
                headers=other).status_code, 404)
            done = client.post(f"/api/v1/sessions/{sid}/actions/{card['id']}/confirm", headers=auth)
            self.assertEqual(done.status_code, 200, done.text)
            repeated = client.post(f"/api/v1/sessions/{sid}/actions/{card['id']}/confirm",
                                   headers=auth)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json(), done.json())
            after = client.get("/api/v1/me/itinerary", headers=auth).json()
            statuses = {row["id"]: row["status"] for kind in after for row in after[kind]}
            self.assertEqual(statuses[card["target_order_id"]], "cancelled")
            self.assertEqual(statuses["london-flight"], "confirmed")
            self.assertEqual(statuses["london-hotel"], "confirmed")
            self.assertEqual(statuses["london-rental"], "confirmed")
            current = self.fixture.turn(client, sid, auth, "f2-current-001", "我现在有什么行程")
            self.assertEqual(current.status_code, 200, current.text)
            self.assertNotIn(card["target_order_id"], current.json()["answer"])
            self.assertNotIn("已取消", current.json()["answer"])
            history = self.fixture.turn(client, sid, auth, "f2-history-001", "查看历史订单")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertIn(card["target_order_id"], history.json()["answer"])
            cancel_history = self.fixture.turn(client, sid, auth, "f2-history-002", "查看取消历史")
            self.assertEqual(cancel_history.status_code, 200, cancel_history.text)
            self.assertIn(card["target_order_id"], cancel_history.json()["answer"])


if __name__ == "__main__":
    unittest.main()
