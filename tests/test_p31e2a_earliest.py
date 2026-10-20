"""Global earliest departure and traveler-scoped pending card reuse."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import insert, select, update

from db.travel_models import (airports, audit_events, flight_offers, flight_orders,
                              flights, pending_actions)
from services.actions import create_proposal
from services.dialogue_context import read_request
from services.travel import find_earliest_flights
from tests import test_p26a_dialogue as dialogue_cases
from tests import test_p31e1_transaction_journey as prior_cases


NOW = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)


class EarliestJourneyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = prior_cases.TransactionJourneyTests("runTest")
        self.fixture.setUp()
        with self.fixture.fixture.engine.begin() as db:
            db.execute(insert(airports), [
                {"code": "PVG", "name": "Shanghai Pudong", "city": "Shanghai",
                 "timezone": "Asia/Shanghai"},
                {"code": "LHR", "name": "London Heathrow", "city": "London",
                 "timezone": "Europe/London"},
            ])
            db.execute(insert(flights), [
                {"id": 3832, "flight_no": "SIM-3832", "departure_airport": "PVG",
                 "arrival_airport": "LHR", "status": "Scheduled",
                 "departure_at": datetime(2026, 10, 24, 20, tzinfo=timezone.utc),
                 "arrival_at": datetime(2026, 10, 25, 9, tzinfo=timezone.utc)},
                {"id": 3833, "flight_no": "SIM-3833", "departure_airport": "SHA",
                 "arrival_airport": "LHR", "status": "Scheduled",
                 "departure_at": datetime(2026, 10, 28, 3, tzinfo=timezone.utc),
                 "arrival_at": datetime(2026, 10, 28, 16, tzinfo=timezone.utc)},
            ])
            db.execute(insert(flight_offers), [
                {"flight_id": 3832, "amount": 6200, "version": 1,
                 "rule_id": "network-fare-v1"},
                {"flight_id": 3833, "amount": 6100, "version": 1,
                 "rule_id": "network-fare-v1"},
            ])

    def tearDown(self):
        self.fixture.tearDown()

    @property
    def engine(self):
        return self.fixture.fixture.engine

    def test_global_sort_lower_bound_and_tie(self):
        with self.engine.begin() as db:
            result = find_earliest_flights(db, ["SHA", "PVG"], ["LGW", "LHR"],
                                           not_before=date(2026, 10, 22), now=NOW)
            self.assertEqual([row["id"] for row in result["rows"]], [3832, 3831, 3833])
            self.assertEqual(result["tie_count"], 1)
            later = find_earliest_flights(db, ["SHA", "PVG"], ["LGW", "LHR"],
                                          not_before=date(2026, 10, 27), now=NOW)
            self.assertEqual(later["rows"][0]["id"], 3833)
            db.execute(insert(flights).values(
                id=3834, flight_no="SIM-3834", departure_airport="SHA",
                arrival_airport="LHR", status="Scheduled",
                departure_at=datetime(2026, 10, 24, 20, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 25, 9, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=3834, amount=6300, version=1, rule_id="network-fare-v1"))
            tied = find_earliest_flights(db, ["SHA", "PVG"], ["LGW", "LHR"],
                                         not_before=date(2026, 10, 22), now=NOW)
            self.assertEqual(tied["tie_count"], 2)
            db.execute(update(flight_offers).where(
                flight_offers.c.flight_id == 3832).values(rule_id="invalid-rule"))
            valid = find_earliest_flights(db, ["SHA", "PVG"], ["LGW", "LHR"],
                                          not_before=date(2026, 10, 22), now=NOW)
            self.assertEqual(valid["rows"][0]["id"], 3834)
            self.assertEqual(valid["tie_count"], 1)
            expired_window = find_earliest_flights(
                db, ["SHA", "PVG"], ["LGW", "LHR"],
                not_before=date(2026, 11, 30), now=NOW)
            self.assertEqual(expired_window["rows"], [])

    def test_route_change_clears_old_date_and_explicit_bound_wins(self):
        old = {"intent": "flight_search", "departure": "Hamburg",
               "arrival": "Basel", "travel_date": "2026-10-22"}
        intent, params, _ = read_request(
            "从上海到伦敦最早什么时候能飞", "general", {}, old,
            date(2026, 10, 21))
        self.assertEqual(intent, "flight_search")
        self.assertEqual((params["departure"], params["arrival"]),
                         ("Shanghai", "London"))
        self.assertIsNone(params["not_before"])
        _, new_params, _ = read_request(
            "从10月27日起最早能飞哪班", "general", {}, params,
            date(2026, 10, 21))
        self.assertEqual(new_params["not_before"], "2026-10-27")
        with self.engine.begin() as db:
            found = find_earliest_flights(db, ["SHA", "PVG"], ["LGW", "LHR"],
                                          not_before=date.fromisoformat(new_params["not_before"]),
                                          now=NOW)
            self.assertEqual(found["rows"][0]["id"], 3833)

    def test_conversation_earliest_book_and_reuse(self):
        model = dialogue_cases.ScriptedModel(
            {"intent": "general", "params": {}, "answer": ""})
        client, model_patch, saver_patch = self.fixture.fixture.client(model)
        original = find_earliest_flights

        def fixed_search(db, departures, arrivals, **kwargs):
            return original(db, departures, arrivals, now=NOW, **kwargs)

        with (model_patch, saver_patch,
              patch("graph_chat.controlled.local_today", return_value=date(2026, 10, 21)),
              patch("services.agent_reads.find_earliest_flights", side_effect=fixed_search),
              client):
            auth = self.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            first = self.fixture.fixture.turn(client, sid, auth, "e2a-turn-1",
                                              "查明天从上海到伦敦的航班")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertIn("该日没有", first.json()["answer"])
            found = self.fixture.fixture.turn(client, sid, auth, "e2a-turn-2",
                                              "那最早什么时候能走")
            self.assertEqual(found.status_code, 200, found.text)
            self.assertIn("SIM-3832", found.json()["answer"])
            booked = self.fixture.fixture.turn(client, sid, auth, "e2a-turn-3",
                                               "就预订最早那班")
            self.assertEqual(booked.status_code, 200, booked.text)
            self.assertEqual(len(booked.json()["actions"]), 1)
            card = booked.json()["actions"][0]
            self.assertEqual(card["parameters"]["flight_id"], 3832)
            repeated = self.fixture.fixture.turn(client, sid, auth, "e2a-turn-4",
                                                 "就预订最早那班")
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(repeated.json()["actions"][0]["id"], card["id"])
            self.assertIn("沿用已有", repeated.json()["answer"])
            self.assertEqual(len(model.history_lengths), 1)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(select(flight_orders.c.id)).all(), [])
            self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 1)
            self.assertEqual(len(db.execute(select(audit_events.c.id).where(
                audit_events.c.event_type == "proposed")).all()), 1)

    def test_tie_requires_choice(self):
        with self.engine.begin() as db:
            db.execute(update(flights).where(flights.c.id == 3831).values(
                departure_at=datetime(2026, 10, 24, 20, tzinfo=timezone.utc)))
        model = dialogue_cases.ScriptedModel()
        client, model_patch, saver_patch = self.fixture.fixture.client(model)
        original = find_earliest_flights
        with (model_patch, saver_patch,
              patch("graph_chat.controlled.local_today", return_value=date(2026, 10, 21)),
              patch("services.agent_reads.find_earliest_flights",
                    side_effect=lambda db, deps, arrs, **kwargs: original(
                        db, deps, arrs, now=NOW, **kwargs)), client):
            auth = self.fixture.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            answer = self.fixture.fixture.turn(client, sid, auth, "tie-turn-1",
                                               "我要预订最早的从上海到伦敦的机票")
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertEqual(answer.json()["actions"], [])
            self.assertIn("同为最早", answer.json()["answer"])

    def test_card_reuse_scope_quote_and_expiry(self):
        with self.engine.begin() as db:
            from services.sessions import create_session
            one = create_session(db, "t1")
            other_session = create_session(db, "t1")
            other_user = create_session(db, "t2")
            first = create_proposal(db, "t1", one["id"], "flight_book",
                                    {"flight_id": 3832}, now=NOW, reuse_pending=True)
            same = create_proposal(db, "t1", one["id"], "flight_book",
                                   {"flight_id": 3832}, now=NOW + timedelta(minutes=1),
                                   reuse_pending=True)
            self.assertEqual(first["id"], same["id"])
            self.assertNotEqual(first["id"], create_proposal(
                db, "t1", other_session["id"], "flight_book", {"flight_id": 3832},
                now=NOW, reuse_pending=True)["id"])
            self.assertNotEqual(first["id"], create_proposal(
                db, "t2", other_user["id"], "flight_book", {"flight_id": 3832},
                now=NOW, reuse_pending=True)["id"])
            db.execute(update(flight_offers).where(flight_offers.c.flight_id == 3832).values(
                amount=6400, version=2))
            repriced = create_proposal(db, "t1", one["id"], "flight_book",
                                       {"flight_id": 3832}, now=NOW + timedelta(minutes=2),
                                       reuse_pending=True)
            self.assertNotEqual(first["id"], repriced["id"])
            db.execute(update(pending_actions).where(
                pending_actions.c.id == repriced["id"]).values(status="rejected"))
            after_rejection = create_proposal(
                db, "t1", one["id"], "flight_book", {"flight_id": 3832},
                now=NOW + timedelta(minutes=3), reuse_pending=True)
            self.assertNotEqual(repriced["id"], after_rejection["id"])
            after_expiry = create_proposal(db, "t1", one["id"], "flight_book",
                                           {"flight_id": 3832},
                                           now=NOW + timedelta(minutes=19), reuse_pending=True)
            self.assertNotEqual(after_rejection["id"], after_expiry["id"])


if __name__ == "__main__":
    unittest.main()
