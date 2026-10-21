"""Hotel nights, global price choice and grounded rental times."""

import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import insert, select, update

from db.travel_models import (airports, hotel_orders, hotel_products, hotels,
                              pending_actions, policy_documents, rental_orders,
                              rental_products)
from services.dialogue_context import read_request, rental_clocks
from services.agent_proposals import SelectionNeeded, prepare_actions
from services.policy_catalog import CURRENT_POLICIES
from services.travel import find_hotels, find_rentals
from tests import test_p26a_dialogue as dialogue_cases


class StayPriceRentalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dialogue_cases.DialogueTests("runTest")
        self.fixture.setUp()
        with self.fixture.engine.begin() as db:
            db.execute(insert(airports), [
                {"code": "SHA", "name": "Shanghai Hongqiao", "city": "Shanghai",
                 "timezone": "Asia/Shanghai"},
                {"code": "PVG", "name": "Shanghai Pudong", "city": "Shanghai",
                 "timezone": "Asia/Shanghai"},
            ])
            db.execute(insert(policy_documents), [CURRENT_POLICIES["hotel"],
                                                  CURRENT_POLICIES["rental"]])
            db.execute(insert(hotels), [
                {"id": n, "name": f"Shanghai Test Hotel {n}", "city": "Shanghai",
                 "price_tier": "Simulated", "nearby_airport": "SHA"}
                for n in range(10, 14)
            ])
            db.execute(insert(hotel_products), [
                {"id": f"room-SHA-{n}", "hotel_id": n, "room_type": "Standard",
                 "nightly_price": price, "available_from": date(2026, 10, 21),
                 "available_until": date(2026, 11, 10)}
                for n, price in zip(range(10, 14), (100, 120, 140, 250))
            ])
            db.execute(insert(rental_products), [
                {"id": f"rental-SHA-{n}", "airport_code": "SHA", "city": "Shanghai",
                 "name": f"Shanghai Test Car {n}", "vehicle_type": "Economy",
                 "daily_price": price, "latitude": 31.2, "longitude": 121.3,
                 "available_from": date(2026, 10, 21),
                 "available_until": date(2026, 11, 10),
                 "generation_rule": "test-only"}
                for n, price in zip(range(10, 14), (80, 90, 100, 180))
            ])

    def tearDown(self):
        self.fixture.tearDown()

    def test_nights_and_time_context(self):
        base = date(2026, 10, 21)
        first = {"intent": "hotel_search", "city": "Shanghai",
                 "check_in": "2026-10-22", "check_out": "2026-10-23"}
        intent, params, _ = read_request("明天和后天都要住", "general", {}, first, base)
        self.assertEqual(intent, "hotel_search")
        self.assertEqual(params["check_out"], "2026-10-24")
        _, updated, _ = read_request("住两晚", "general", {}, first, base)
        self.assertEqual(updated["check_out"], "2026-10-24")
        _, conflict, _ = read_request("明天入住后天退房，住两晚", "general", {}, first, base)
        self.assertTrue(conflict["date_conflict"])
        self.assertEqual(rental_clocks("明天上午10点取车，大后天下午6点还车"),
                         {"pickup_clock": "10:00", "dropoff_clock": "18:00"})

    def test_full_scope_price_sort(self):
        with self.fixture.engine.connect() as db:
            rooms = find_hotels(db, "Shanghai", date(2026, 10, 22),
                                date(2026, 10, 24), sort="price_desc", limit=3)
            self.assertEqual(rooms[0]["product_id"], "room-SHA-13")
            cars = find_rentals(db, date(2026, 10, 22), date(2026, 10, 24),
                                city="Shanghai", sort="price_desc", limit=3)
            self.assertEqual(cars[0]["id"], "rental-SHA-13")
        client, model_patch, saver_patch = self.fixture.client(dialogue_cases.ScriptedModel())
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            hotel_api = client.get("/api/v1/hotels", headers=auth, params={
                "city": "Shanghai", "check_in": "2026-10-22",
                "check_out": "2026-10-24", "sort": "price_desc", "limit": 1})
            self.assertEqual(hotel_api.status_code, 200, hotel_api.text)
            self.assertEqual(hotel_api.json()[0]["product_id"], "room-SHA-13")
            rental_api = client.get("/api/v1/rentals", headers=auth, params={
                "city": "Shanghai", "pickup_date": "2026-10-22",
                "drop_off_date": "2026-10-24", "sort": "price_desc", "limit": 1})
            self.assertEqual(rental_api.status_code, 200, rental_api.text)
            self.assertEqual(rental_api.json()[0]["id"], "rental-SHA-13")

    def test_two_nights_global_max_booking(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("graph_chat.controlled.local_today",
                                            return_value=date(2026, 10, 21)), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            for key, prompt in (("hotel-e2b-1", "查明天到后天上海的酒店"),
                                ("hotel-e2b-2", "明天和后天都要住")):
                response = self.fixture.turn(client, sid, auth, key, prompt)
                self.assertEqual(response.status_code, 200, response.text)
            booked = self.fixture.turn(client, sid, auth, "hotel-e2b-3",
                                       "我要住最贵的酒店，住两晚")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(len(booked.json()["actions"]), 1)
            card = booked.json()["actions"][0]
            self.assertEqual(card["parameters"]["product_id"], "room-SHA-13")
            self.assertEqual(card["parameters"]["check_out"], "2026-10-24")
            self.assertEqual(card["price"]["new_amount"], 500)
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(hotel_orders.c.id)).all(), [])

    def test_global_max_rental_requires_explicit_times(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("graph_chat.controlled.local_today",
                                            return_value=date(2026, 10, 21)), patch(
                                                "api.travel.local_today",
                                                return_value=date(2026, 10, 21)), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            first = self.fixture.turn(client, sid, auth, "rental-e2b-1",
                                      "我要在上海租车，明天用大后天还车")
            self.assertEqual(first.status_code, 200, first.text)
            missing = self.fixture.turn(client, sid, auth, "rental-e2b-2",
                                        "我要租最贵的那个车，两天")
            self.assertEqual(missing.status_code, 200, missing.text)
            self.assertEqual(missing.json()["actions"], [])
            self.assertIn("时刻", missing.json()["answer"])
            booked = self.fixture.turn(client, sid, auth, "rental-e2b-3",
                                       "我要租最贵的车，明天10点取，大后天18点还")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(len(booked.json()["actions"]), 1)
            card = booked.json()["actions"][0]
            self.assertEqual(card["parameters"]["product_id"], "rental-SHA-13")
            self.assertEqual(card["after"]["billable_days"], 3)
            self.assertEqual(card["price"]["new_amount"], 540)
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(rental_orders.c.id)).all(), [])
            self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)

    def test_rental_times_across_turns_remain_grounded(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("graph_chat.controlled.local_today",
                                            return_value=date(2026, 10, 21)), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            for key, prompt in (("rental-split-1", "我在上海租车，明天取大后天还"),
                                ("rental-split-2", "明天10点取"),
                                ("rental-split-3", "大后天18点还")):
                response = self.fixture.turn(client, sid, auth, key, prompt)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["actions"], [])
            booked = self.fixture.turn(client, sid, auth, "rental-split-4",
                                       "我要租最贵的那个车")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(len(booked.json()["actions"]), 1)
            self.assertEqual(booked.json()["actions"][0]["parameters"]["product_id"],
                             "rental-SHA-13")
            self.assertEqual(len(model.history_lengths), 1)

    def test_night_override_and_tied_rental(self):
        base = date(2026, 10, 21)
        previous = {"intent": "hotel_search", "city": "Shanghai",
                    "check_in": "2026-10-22", "check_out": "2026-10-24", "nights": 2}
        _, changed, _ = read_request("改为后天退房", "general", {}, previous, base)
        self.assertEqual(changed["check_out"], "2026-10-23")
        self.assertEqual(changed["nights"], 1)
        self.assertNotIn("date_conflict", changed)
        with self.fixture.engine.begin() as db:
            db.execute(update(rental_products).where(
                rental_products.c.id == "rental-SHA-12").values(daily_price=180))
        model = dialogue_cases.ScriptedModel()
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("graph_chat.controlled.local_today",
                                            return_value=base), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            response = self.fixture.turn(client, sid, auth, "rental-tie-1",
                                         "我要预订上海明天10点取大后天18点还最贵的租车")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["actions"], [])
            self.assertIn("并列", response.json()["answer"])

    def test_model_cannot_swap_rental_clocks_or_hotel_dates(self):
        with self.fixture.engine.connect() as db:
            rental_context = {"intent": "rental_search", "city": "Shanghai",
                              "pickup_date": "2026-10-22", "drop_off_date": "2026-10-24",
                              "pickup_clock": "10:00", "dropoff_clock": "18:00"}
            rental_ref = [{"type": "rental_product", "id": "rental-SHA-13",
                           "price": 180, "city": "Shanghai", "extreme_unique": True,
                           "pickup_date": "2026-10-22", "drop_off_date": "2026-10-24"}]
            with self.assertRaises(SelectionNeeded):
                prepare_actions(db, "t1", [{"action_type": "rental_book", "parameters": {
                    "product_id": "rental-SHA-13",
                    "pickup_at": "2026-10-22T18:00",
                    "dropoff_at": "2026-10-24T10:00"}}], [rental_ref],
                    "我要租最贵的车，明天10点取，大后天18点还", today=date(2026, 10, 21),
                    grounded_context=rental_context)
            hotel_ref = [{"type": "hotel_product", "id": "room-SHA-13",
                          "price": 250, "city": "Shanghai", "extreme_unique": True,
                          "check_in": "2026-10-22", "check_out": "2026-10-24"}]
            with self.assertRaises(SelectionNeeded):
                prepare_actions(db, "t1", [{"action_type": "hotel_book", "parameters": {
                    "product_id": "room-SHA-13", "check_in": "2026-10-22",
                    "check_out": "2026-10-23"}}], [hotel_ref],
                    "我要住最贵的酒店，住两晚", today=date(2026, 10, 21))

    def test_tied_highest_hotel_requires_selection(self):
        with self.fixture.engine.begin() as db:
            db.execute(update(hotel_products).where(
                hotel_products.c.id == "room-SHA-12").values(nightly_price=250))
        model = dialogue_cases.ScriptedModel()
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("graph_chat.controlled.local_today",
                                            return_value=date(2026, 10, 21)), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            response = self.fixture.turn(client, sid, auth, "hotel-tie-1",
                                         "我要预订上海明天到后天最贵的酒店")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["actions"], [])
            self.assertIn("并列", response.json()["answer"])


if __name__ == "__main__":
    unittest.main()
