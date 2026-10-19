"""Model suggestions may identify a target, but only owned, selected targets pass."""

import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, insert, inspect, text
from sqlalchemy.orm import Session

from db.travel_models import (flight_orders, hotel_orders, metadata,
                              rental_orders, travelers)
from services.agent_proposals import SelectionNeeded, _ordinal, _product_choice, prepare_actions
from migrations import v013_turn_action_cards


class ProposalSelectionTests(unittest.TestCase):
    def test_short_ordinals_select_only_shown_candidate(self):
        shown = [[{"type": "flight", "id": 11}, {"type": "flight", "id": 22}]]
        for phrase in ("第二班", "第 2 班", "第二班航班", "第二个"):
            self.assertEqual(_ordinal(phrase, "flight"), 1)
            self.assertEqual(_product_choice("flight", None, shown, phrase), 22)
        with self.assertRaises(SelectionNeeded):
            _product_choice("flight", 11, shown, "第 2 班")
        with self.assertRaises(SelectionNeeded):
            _product_choice("flight", None, [[shown[0][0]]], "第二班")
        with self.assertRaises(SelectionNeeded):
            _product_choice("flight", 22, [[]], "第二班")

    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.execute(insert(travelers), [
            {"id": "one", "username": "one", "password_hash": "x",
             "display_name": "One", "active": 1},
            {"id": "two", "username": "two", "password_hash": "x",
             "display_name": "Two", "active": 1},
        ])
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.db.execute(insert(flight_orders).values(
            id="flight-own", traveler_id="one", flight_id=1, amount=100,
            status="confirmed", version=2))
        self.db.execute(insert(hotel_orders).values(
            id="hotel-own", traveler_id="one", product_id="room-1",
            check_in=date(2026, 10, 20), check_out=date(2026, 10, 21),
            total_price=200, status="confirmed", version=3))
        self.db.execute(insert(rental_orders).values(
            id="rental-own", traveler_id="one", product_id="car-1",
            pickup_at=now, dropoff_at=now + timedelta(days=1), daily_price=50,
            billable_days=1, total_price=50, status="confirmed", version=4))
        self.db.execute(insert(flight_orders).values(
            id="foreign-flight", traveler_id="two", flight_id=2, amount=100,
            status="confirmed", version=1))
        self.db.commit()
        self.refs = [[
            {"type": "flight", "id": 30, "price": 80},
            {"type": "flight", "id": 31, "price": 70},
            {"type": "hotel_product", "id": "room-1", "price": 200},
            {"type": "rental_product", "id": "rental-product-1", "price": 50},
        ]]

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_all_nine_types_resolve_owned_versions_and_candidates(self):
        cases = [
            ("flight_book", {"flight_id": 31}, "帮我订最便宜的航班", "flight_id", 31),
            ("flight_change", {"new_flight_id": 31}, "改签到第二班航班", "expected_version", 2),
            ("flight_cancel", {}, "取消我的机票", "order_id", "flight-own"),
            ("hotel_book", {"product_id": "room-1", "check_in": "2026-10-22",
                            "check_out": "2026-10-23"}, "订这家酒店", "product_id", "room-1"),
            ("hotel_change", {"check_in": "2026-10-22", "check_out": "2026-10-23"},
             "酒店改期", "expected_version", 3),
            ("hotel_cancel", {}, "取消我的酒店", "order_id", "hotel-own"),
            ("rental_book", {"product_id": "rental-product-1",
                             "pickup_at": "2026-10-22T12:00:00+02:00",
                             "dropoff_at": "2026-10-23T12:00:00+02:00"},
             "预订这辆租车", "product_id", "rental-product-1"),
            ("rental_change", {"pickup_at": "2026-10-22T12:00:00+02:00",
                               "dropoff_at": "2026-10-23T12:00:00+02:00"},
             "租车改期", "expected_version", 4),
            ("rental_cancel", {}, "取消我的租车", "order_id", "rental-own"),
        ]
        for action_type, params, text, key, expected in cases:
            with self.subTest(action_type=action_type):
                resolved = prepare_actions(self.db, "one", [
                    {"action_type": action_type, "parameters": params}],
                    self.refs, text)
                self.assertEqual(resolved[0][0], action_type)
                self.assertEqual(resolved[0][1][key], expected)

    def test_foreign_order_unshown_product_and_extra_fields_are_rejected(self):
        cases = [
            ({"action_type": "flight_cancel", "parameters": {"order_id": "foreign-flight"}},
             "取消订单 foreign-flight"),
            ({"action_type": "flight_book", "parameters": {"flight_id": 999}},
             "预订航班 SIM-999"),
            ({"action_type": "hotel_book", "parameters": {
                "product_id": "room-1", "check_in": "2026-10-22",
                "check_out": "2026-10-23", "traveler_id": "two"}}, "订酒店"),
            ({"action_type": "flight_book", "parameters": {
                "flight_id": 30, "price": 1}}, "订第一班航班"),
        ]
        for item, text in cases:
            with self.subTest(item=item):
                with self.assertRaises(SelectionNeeded):
                    prepare_actions(self.db, "one", [item], self.refs, text)

    def test_multiple_candidates_require_clear_selection(self):
        plan = [{"action_type": "flight_book", "parameters": {"flight_id": 30}}]
        with self.assertRaises(SelectionNeeded):
            prepare_actions(self.db, "one", plan, self.refs, "帮我订一张机票")
        with self.assertRaises(SelectionNeeded):
            prepare_actions(self.db, "one", plan, self.refs, "帮我订第二班航班")
        picked = prepare_actions(self.db, "one", plan, self.refs, "帮我订第一班航班")
        self.assertEqual(picked[0][1]["flight_id"], 30)

    def test_transaction_dates_need_user_evidence_and_dual_intent_is_atomic(self):
        hotel = {"action_type": "hotel_book", "parameters": {
            "product_id": "room-1", "check_in": "2026-10-22",
            "check_out": "2026-10-23"}}
        with self.assertRaises(SelectionNeeded):
            prepare_actions(self.db, "one", [hotel], self.refs,
                            "订这家酒店", today=date(2026, 10, 20))
        resolved = prepare_actions(self.db, "one", [hotel], self.refs,
                                   "订这家酒店，10月22日入住一晚",
                                   today=date(2026, 10, 20))
        self.assertEqual(resolved[0][1]["check_out"], "2026-10-23")
        with self.assertRaises(SelectionNeeded):
            prepare_actions(self.db, "one", [hotel], self.refs,
                            "改签到第二班航班，并订这家酒店，10月22日入住一晚",
                            today=date(2026, 10, 20))
        rental = {"action_type": "rental_change", "parameters": {
            "pickup_at": "2026-10-22T12:00:00+02:00",
            "dropoff_at": "2026-10-23T12:00:00+02:00"}}
        with self.assertRaises(SelectionNeeded):
            prepare_actions(self.db, "one", [rental], self.refs,
                            "租车改到10月22日至23日", today=date(2026, 10, 20))

    def test_v013_adds_cards_to_old_turn_table_and_repeats(self):
        old = create_engine("sqlite+pysqlite:///:memory:")
        with old.begin() as db:
            db.execute(text("CREATE TABLE chat_turns (id TEXT PRIMARY KEY)"))
            v013_turn_action_cards.upgrade(db)
            v013_turn_action_cards.upgrade(db)
            self.assertIn("action_cards", {column["name"] for column in
                                           inspect(db).get_columns("chat_turns")})
        old.dispose()


if __name__ == "__main__":
    unittest.main()
