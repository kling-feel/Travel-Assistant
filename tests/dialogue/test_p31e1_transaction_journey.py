"""A traveler can select a shown flight and continue hotel and rental tasks."""

import unittest
from datetime import date, datetime, timezone
from sqlalchemy import insert, select, update
from unittest.mock import patch

from travel_assistant.db.travel_models import (airports, flight_offers, flight_orders, flights,
                              hotel_products, hotels, pending_actions)
from travel_assistant.services.dialogue_context import read_request, requested_actions
from tests.dialogue import test_p26a_dialogue as dialogue_cases


class _FixedActionClock(datetime):
    """Keep the 2026-10-25 booking fixture in its test-time future."""

    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class TransactionJourneyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dialogue_cases.DialogueTests("runTest")
        self.fixture.setUp()
        with self.fixture.engine.begin() as db:
            db.execute(insert(airports), [
                {"code": "SHA", "name": "Shanghai Hongqiao", "city": "Shanghai",
                 "timezone": "Asia/Shanghai"},
                {"code": "LGW", "name": "London Gatwick", "city": "London",
                 "timezone": "Europe/London"},
            ])
            db.execute(insert(flights).values(
                id=3831, flight_no="SIM-3831", departure_airport="SHA",
                arrival_airport="LGW", status="Scheduled",
                departure_at=datetime(2026, 10, 25, 1, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 25, 13, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=3831, amount=6585, version=1, rule_id="network-fare-v1"))
            db.execute(insert(hotels).values(
                id=20, name="Fictional London Hotel", city="London",
                price_tier="Simulated", nearby_airport="LGW"))
            db.execute(insert(hotel_products).values(
                id="room-LGW-1", hotel_id=20, room_type="Standard",
                nightly_price=140, available_from=date(2026, 10, 24),
                available_until=date(2026, 11, 10)))

    def tearDown(self):
        self.fixture.tearDown()

    def test_write_words_and_new_rental_task(self):
        self.assertEqual(requested_actions("我要预定这个航班"), ["flight_book"])
        self.assertEqual(requested_actions("预订前面说的机票，顺便找落地酒店"),
                         ["flight_book"])
        self.assertEqual(requested_actions("机票取消规则是什么"), [])
        self.assertEqual(requested_actions("查询订单"), [])
        self.assertEqual(requested_actions("取消我的机票，并预订酒店"),
                         ["flight_cancel", "hotel_book"])
        self.assertEqual(requested_actions("取消机票，顺便找酒店"), ["flight_cancel"])
        self.assertEqual(requested_actions("把我的酒店改到后天入住"), ["hotel_change"])
        self.assertEqual(requested_actions("把我的机票改到后天"), ["flight_change"])
        self.assertEqual(requested_actions("把租车改成后天下午取车"), ["rental_change"])
        self.assertEqual(requested_actions("预订 rental-LGW-1，29日10点取车，31日18点还车"),
                         ["rental_book"])
        self.assertEqual(requested_actions("取消机票，并把酒店改到后天"),
                         ["flight_cancel", "hotel_change"])
        intent, params, _ = read_request(
            "我要在上海租车，明天用大后天还车", "general", {},
            {"intent": "hotel_search", "city": "London"}, date(2026, 10, 21))
        self.assertEqual(intent, "rental_search")
        self.assertEqual(params["city"], "Shanghai")
        self.assertEqual(params["pickup_date"], "2026-10-22")
        self.assertEqual(params["drop_off_date"], "2026-10-24")

    def test_shown_flight_booking_with_hotel_query_and_idempotency(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with (model_patch, saver_patch,
              patch("travel_assistant.agents.controlled.local_today",
                    return_value=date(2026, 10, 21)),
              patch("travel_assistant.services.actions.datetime", _FixedActionClock), client):
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            found = self.fixture.turn(client, sid, auth, "flight-search-001",
                                      "查10月25日从上海到伦敦的航班")
            self.assertEqual(found.status_code, 200, found.text)
            self.assertIn("SIM-3831", found.json()["answer"])
            booked = self.fixture.turn(client, sid, auth, "flight-hotel-001",
                                       "我要预定前面说的机票，顺便找落地附近酒店")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(len(booked.json()["actions"]), 1)
            self.assertEqual(booked.json()["actions"][0]["action_type"], "flight_book")
            self.assertEqual(booked.json()["actions"][0]["parameters"]["flight_id"], 3831)
            self.assertIn("酒店", booked.json()["answer"])
            self.assertIn("入住日期", booked.json()["answer"])
            replay = self.fixture.turn(client, sid, auth, "flight-hotel-001",
                                       "我要预定前面说的机票，顺便找落地附近酒店")
            self.assertEqual(replay.json(), booked.json())
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(flight_orders.c.id)).all(), [])
            self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)

    def test_changed_date_does_not_book_old_flight(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "transaction", "params": {"actions": [
                {"action_type": "flight_book", "parameters": {"flight_id": 3831}}]},
             "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("travel_assistant.agents.controlled.local_today",
                                            return_value=date(2026, 10, 21)), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            first = self.fixture.turn(client, sid, auth, "flight-search-002",
                                      "查10月25日从上海到伦敦的航班")
            self.assertEqual(first.status_code, 200, first.text)
            changed = self.fixture.turn(client, sid, auth, "flight-book-002",
                                        "我要预订明天的那个航班")
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()["actions"], [])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])

    def test_changed_quote_requires_new_search(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""},
            {"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, patch("travel_assistant.agents.controlled.local_today",
                                            return_value=date(2026, 10, 21)), client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            found = self.fixture.turn(client, sid, auth, "quote-search-001",
                                      "查10月25日从上海到伦敦的航班")
            self.assertEqual(found.status_code, 200, found.text)
            with self.fixture.engine.begin() as db:
                db.execute(update(flight_offers).where(
                    flight_offers.c.flight_id == 3831).values(amount=6600, version=2))
            booked = self.fixture.turn(client, sid, auth, "quote-book-001",
                                       "我要预订这个航班")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(booked.json()["actions"], [])
            self.assertIn("报价已变化", booked.json()["answer"])
        with self.fixture.engine.connect() as db:
            self.assertEqual(db.execute(select(pending_actions.c.id)).all(), [])


if __name__ == "__main__":
    unittest.main()
