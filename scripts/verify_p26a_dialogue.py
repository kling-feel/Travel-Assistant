"""Verify read-only dialogue against the real PostgreSQL API without model keys."""

import os
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_assistant.db import engine
from travel_assistant.db.travel_models import (action_results, audit_events, chat_messages,
                              chat_sessions, chat_turns, flight_orders,
                              hotel_orders, pending_actions, rental_orders,
                              travelers)
from main import Server
from travel_assistant.services.checkpoints import open_checkpointer
from travel_assistant.services.agent_reads import demo_date


class FixedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def invoke(self, _messages):
        if not self.decisions:
            raise AssertionError("Unexpected model call")
        return AIMessage(content=self.decisions.pop(0))


def _app():
    server = Server()
    server.init_app()
    return server.app


def _auth(client, username, password):
    response = client.post("/api/v1/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _counts():
    with engine.connect() as db:
        return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (travelers, flight_orders, hotel_orders, rental_orders,
                              pending_actions, action_results, audit_events,
                              chat_sessions, chat_messages, chat_turns)}


def main():
    passwords = [os.getenv("DEMO_TRAVELER_1_PASSWORD"),
                 os.getenv("DEMO_TRAVELER_2_PASSWORD")]
    if not all(passwords):
        raise RuntimeError("Configure demo traveler passwords in the ignored .env")
    before = _counts()
    if before["chat_sessions"] or before["chat_turns"] or before["pending_actions"]:
        raise RuntimeError("Existing session/action data found; test requires a clean demo snapshot")
    sid = None
    try:
        with engine.connect() as db:
            travel_date = (demo_date(db) + timedelta(days=1)).isoformat()
        first_model = FixedModel([
            '{"intent":"policy","params":{"scope":"flight"},"answer":"错误规则 9999"}',
            '{"intent":"flight_search","params":{"departure":"HAM","arrival":"BSL",'
            f'"travel_date":"{travel_date}"}},"answer":"模拟价格 9999"}}',
        ])
        with patch("travel_assistant.api.travel.get_chat_model", return_value=first_model):
            with TestClient(_app()) as client:
                one = _auth(client, "demo1", passwords[0])
                two = _auth(client, "demo2", passwords[1])
                sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
                policy = client.post(f"/api/v1/sessions/{sid}/turns", headers=one,
                                     json={"request_key": "real-policy-001",
                                           "content": "机票取消政策是什么？"})
                assert policy.status_code == 200, policy.text
                assert policy.json()["references"][0]["type"] == "policy"
                assert "9999" not in policy.json()["answer"]
                replay = client.post(f"/api/v1/sessions/{sid}/turns", headers=one,
                                     json={"request_key": "real-policy-001",
                                           "content": "机票取消政策是什么？"})
                assert replay.json() == policy.json()
                search = client.post(f"/api/v1/sessions/{sid}/turns", headers=one,
                                     json={"request_key": "real-flight-002",
                                           "content": f"查 {travel_date} HAM 到 BSL 的航班"})
                assert search.status_code == 200, search.text
                assert "9999" not in search.json()["answer"]
                assert search.json()["references"][0]["type"] == "flight"
                foreign = client.post(f"/api/v1/sessions/{sid}/turns", headers=two,
                                      json={"request_key": "foreign-001",
                                            "content": "查订单"})
                assert foreign.status_code == 404
        with open_checkpointer() as saver:
            assert saver.get_tuple({"configurable": {"thread_id": sid}}) is not None
        second_model = FixedModel([
            '{"intent":"itinerary","params":{},"answer":""}',
        ])
        with patch("travel_assistant.api.travel.get_chat_model", return_value=second_model):
            with TestClient(_app()) as restarted:
                one = _auth(restarted, "demo1", passwords[0])
                itinerary = restarted.post(f"/api/v1/sessions/{sid}/turns", headers=one,
                                           json={"request_key": "real-trip-003",
                                                 "content": "我的行程有哪些？"})
                assert itinerary.status_code == 200, itinerary.text
                assert "本人模拟行程" in itinerary.json()["answer"]
                history = restarted.get(f"/api/v1/sessions/{sid}", headers=one).json()
                assert len(history["messages"]) == 6
        during = _counts()
        for name in ("travelers", "flight_orders", "hotel_orders", "rental_orders",
                     "pending_actions", "action_results", "audit_events"):
            assert during[name] == before[name], (name, before[name], during[name])
        assert during["chat_sessions"] == 1 and during["chat_turns"] == 3
        print("P2.6a real API, ownership, evidence, replay and app rebuild: OK")
    finally:
        if sid:
            with open_checkpointer() as saver:
                saver.delete_thread(sid)
            with engine.begin() as db:
                db.execute(delete(chat_turns).where(chat_turns.c.session_id == sid))
                db.execute(delete(chat_messages).where(chat_messages.c.session_id == sid))
                db.execute(delete(chat_sessions).where(chat_sessions.c.id == sid))
            assert _counts() == before, "Cleanup changed the demo baseline"


if __name__ == "__main__":
    main()
