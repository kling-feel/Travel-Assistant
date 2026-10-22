"""Verified stream delivery must preserve turn and proposal semantics."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.travel_models import chat_messages, chat_turns, flight_orders, hotel_orders, pending_actions
from services.chat_turns import begin_turn
from tests import test_p26a_dialogue as dialogue_cases
from tests import test_p31f2_itinerary_cancel as itinerary_cases


def events(response):
    frames = response.text.replace("\r\n", "\n").split("\n\n")
    parsed = []
    for frame in frames:
        lines = frame.splitlines()
        kind = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        data = next((line[5:].strip() for line in lines if line.startswith("data:")), None)
        if kind and data:
            parsed.append((kind, json.loads(data)))
    return parsed


class StreamTurnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dialogue_cases.DialogueTests("runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_stream_is_owned_saved_and_idempotent(self):
        answer = "中文答复" * 45
        model = dialogue_cases.ScriptedModel({"intent": "general", "params": {}, "answer": answer})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            one = self.fixture.auth(client, "demo1", "password1")
            two = self.fixture.auth(client, "demo2", "password2")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            url = f"/api/v1/sessions/{sid}/turns/stream"
            payload = {"request_key": "stream-001", "content": "介绍旅行"}
            response = client.post(url, headers=one, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            frames = events(response)
            kinds = [kind for kind, _ in frames]
            self.assertEqual(kinds[0], "progress")
            self.assertIn("accepted", kinds)
            self.assertLess(kinds.index("accepted"), kinds.index("delta"))
            self.assertEqual(kinds[-1], "completed")
            streamed = "".join(data["text"] for kind, data in frames if kind == "delta")
            saved = client.get(f"/api/v1/sessions/{sid}/turns/stream-001", headers=one)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["answer"], streamed)
            self.assertEqual(saved.json()["status"], "completed")
            self.assertEqual(client.get(f"/api/v1/sessions/{sid}/turns/status/latest",
                                        headers=one).json()["request_key"], "stream-001")
            self.assertEqual(client.get(f"/api/v1/sessions/{sid}/turns/stream-001",
                                        headers=two).status_code, 404)
            self.assertEqual(client.get(f"/api/v1/sessions/{sid}/turns/status/latest",
                                        headers=two).status_code, 404)
            replay = client.post(url, headers=one, json=payload)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual("".join(data["text"] for kind, data in events(replay)
                                     if kind == "delta"), streamed)
            changed = client.post(url, headers=one,
                                  json={**payload, "content": "另一条问题"})
            self.assertEqual(events(changed)[-1][0], "failed")
            self.assertEqual(events(changed)[-1][1]["status"], 409)
        with self.fixture.engine.connect() as db:
            self.assertEqual(len(db.execute(select(chat_turns.c.id)).all()), 1)
            self.assertEqual([row[0] for row in db.execute(select(chat_messages.c.role))],
                             ["user", "assistant"])

    def test_failed_turn_retries_same_key_without_duplicate_user_message(self):
        model = dialogue_cases.ScriptedModel(RuntimeError("provider failed"),
                              {"intent": "general", "params": {}, "answer": "已恢复"})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            url = f"/api/v1/sessions/{sid}/turns/stream"
            payload = {"request_key": "stream-retry-001", "content": "你好"}
            first = client.post(url, headers=auth, json=payload)
            self.assertEqual(events(first)[-1][0], "failed")
            self.assertEqual(client.get(f"/api/v1/sessions/{sid}/turns/stream-retry-001",
                                        headers=auth).json()["status"], "failed")
            latest = client.get(f"/api/v1/sessions/{sid}/turns/status/latest",
                                headers=auth).json()
            self.assertEqual((latest["status"], latest["content"]), ("failed", "你好"))
            second = client.post(url, headers=auth, json=payload)
            self.assertEqual(events(second)[-1][0], "completed")
            history = client.get(f"/api/v1/sessions/{sid}", headers=auth).json()
            self.assertEqual([row["role"] for row in history["messages"]],
                             ["user", "assistant"])

    def test_batch_proposals_are_not_confirmed_by_stream(self):
        case = itinerary_cases.ItineraryCancelTests("runTest")
        case.setUp()
        try:
            model = dialogue_cases.ScriptedModel({"intent": "general", "params": {},
                                                  "answer": "正在核对"})
            client, model_patch, saver_patch = case.fixture.client(model)
            with model_patch, saver_patch, client:
                auth = case.fixture.auth(client, "demo1", "password1")
                sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
                response = client.post(f"/api/v1/sessions/{sid}/turns/stream",
                                       headers=auth, json={
                                           "request_key": "stream-cards-001",
                                           "content": "保留伦敦，取消其他安排",
                                       })
                self.assertEqual(events(response)[-1][0], "completed")
                cards = client.get(f"/api/v1/sessions/{sid}/actions", headers=auth).json()
                self.assertEqual(len(cards), 2)
                self.assertEqual({card["status"] for card in cards}, {"pending"})
                with case.fixture.engine.connect() as db:
                    self.assertEqual(db.execute(select(flight_orders.c.status).where(
                        flight_orders.c.id == "basel-flight")).scalar_one(), "confirmed")
                    self.assertEqual(db.execute(select(hotel_orders.c.status).where(
                        hotel_orders.c.id == "basel-hotel")).scalar_one(), "confirmed")
                    self.assertEqual(len(db.execute(select(pending_actions.c.id)).all()), 2)
        finally:
            case.tearDown()

    def test_processing_key_is_not_rerun_until_lease_expires(self):
        model = dialogue_cases.ScriptedModel({"intent": "general", "params": {},
                                               "answer": "继续处理完成"})
        client, model_patch, saver_patch = self.fixture.client(model)
        with model_patch, saver_patch, client:
            auth = self.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            with Session(self.fixture.engine) as db:
                turn, created = begin_turn(db, "t1", sid, "stream-lease-001", "你好")
                self.assertTrue(created)
                db.commit()
            url = f"/api/v1/sessions/{sid}/turns/stream"
            payload = {"request_key": "stream-lease-001", "content": "你好"}
            blocked = client.post(url, headers=auth, json=payload)
            self.assertEqual(events(blocked)[-1][1]["code"], "processing")
            with self.fixture.engine.begin() as db:
                db.execute(update(chat_turns).where(chat_turns.c.id == turn["id"]).values(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            latest = client.get(f"/api/v1/sessions/{sid}/turns/status/latest",
                                headers=auth).json()
            self.assertEqual((latest["status"], latest["error_code"]),
                             ("failed", "interrupted"))
            retried = client.post(url, headers=auth, json=payload)
            self.assertEqual(events(retried)[-1][0], "completed")
            history = client.get(f"/api/v1/sessions/{sid}", headers=auth).json()
            self.assertEqual([row["role"] for row in history["messages"]],
                             ["user", "assistant"])
