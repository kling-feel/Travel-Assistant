"""Isolated PostgreSQL journey: prepare before a DB restart, finish afterward.

Only run with POSTGRES_PORT=5434 and the travel_p31e4 Compose project.
The state/report files live under ignored data/generated/.
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_assistant.core.config import settings
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (action_results, audit_events, chat_sessions,
                              airports, demo_state, flight_orders, flights, hotel_orders,
                              pending_actions, rental_orders)
from main import Server
from travel_assistant.services.recommendations import ReviewedCatalogProvider


STATE = ROOT / "data/generated/p31e4-journey-state.json"
REPORT = ROOT / "data/generated/p31e4-fixed-report.json"


class FixedModel:
    def __init__(self):
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return AIMessage(content='{"intent":"general","params":{},"answer":""}')


def app():
    server = Server()
    server.init_app()
    return server.app


def login(client, number):
    password = os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD")
    assert password, "Missing configured demo password"
    response = client.post("/api/v1/login", json={
        "username": f"demo{number}", "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def counts():
    with engine.connect() as db:
        return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (flight_orders, hotel_orders, rental_orders,
                              pending_actions, action_results, audit_events,
                              chat_sessions)}


def require_isolation():
    assert settings.POSTGRES_PORT == 5434 and not settings.DATABASE_URL, (
        "This acceptance script may only use the isolated database on port 5434")
    with engine.connect() as db:
        actual = db.execute(select(demo_state.c.as_of)).scalar_one()
    assert actual == date.today(), ("Isolated data date mismatch", actual)


def turn(client, session_id, auth, label, prompt, report):
    response = client.post(f"/api/v1/sessions/{session_id}/turns", headers=auth,
                           json={"request_key": "p31e4-" + label,
                                 "content": prompt})
    body = response.json()
    actions = body.get("actions", [])
    entry = {"label": label, "status": response.status_code,
             "answer": body.get("answer", str(body.get("detail", "")))[:450],
             "action_types": [row.get("action_type") for row in actions],
             "action_ids": [row.get("id") for row in actions],
             "references": [ref.get("type") for ref in body.get("references", [])]}
    report["turns"].append(entry)
    print(json.dumps(entry, ensure_ascii=False))
    assert response.status_code == 200, response.text
    return body


def action(client, session_id, auth, action_id, verb):
    response = client.post(
        f"/api/v1/sessions/{session_id}/actions/{action_id}/{verb}",
        headers=auth, json={})
    print(f"{verb}_status={response.status_code}")
    return response


def prepare():
    require_isolation()
    assert not STATE.exists(), "Existing restart state must be finished first"
    before = counts()
    assert before["pending_actions"] == 0, before
    report = {"date": date.today().isoformat(), "before": before, "turns": []}
    model = FixedModel()
    with (patch("api.travel.get_chat_model", return_value=model),
          patch("api.travel.get_recommendation_provider",
                return_value=ReviewedCatalogProvider()),
          TestClient(app()) as client):
        one, two = login(client, 1), login(client, 2)
        session = client.post("/api/v1/sessions", headers=one, json={})
        assert session.status_code == 201, session.text
        sid = session.json()["id"]
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        turn(client, sid, one, "itinerary", "我最近有什么行程安排吗", report)
        turn(client, sid, one, "flight-empty",
             f"查{tomorrow}从上海到伦敦的航班", report)
        earliest = turn(client, sid, one, "flight-earliest",
                        "那最早什么时候能走", report)
        assert "SIM-" in earliest["answer"] and earliest["references"], earliest
        booked = turn(client, sid, one, "flight-book",
                      "就预订最早那班，顺便找落地附近酒店", report)
        assert [card["action_type"] for card in booked["actions"]] == ["flight_book"]
        flight_card = booked["actions"][0]
        assert counts()["flight_orders"] == before["flight_orders"], "Pre-confirm write"
        replay = client.post(f"/api/v1/sessions/{sid}/turns", headers=one,
                             json={"request_key": "p31e4-flight-book",
                                   "content": "就预订最早那班，顺便找落地附近酒店"})
        assert replay.status_code == 200 and replay.json() == booked
        foreign = action(client, sid, two, flight_card["id"], "confirm")
        assert foreign.status_code in (403, 404)
        confirmed = action(client, sid, one, flight_card["id"], "confirm")
        assert confirmed.status_code == 200, confirmed.text
        again = action(client, sid, one, flight_card["id"], "confirm")
        assert again.status_code == 200 and again.json() == confirmed.json()
        assert counts()["flight_orders"] == before["flight_orders"] + 1
        with engine.connect() as db:
            flight = db.execute(select(flights.c.arrival_at, airports.c.timezone).join(
                airports, flights.c.arrival_airport == airports.c.code).where(
                flights.c.id == flight_card["parameters"]["flight_id"])).one()
        arrival = flight.arrival_at.astimezone(ZoneInfo(flight.timezone)).date()
        hotel = turn(client, sid, one, "landing-hotel",
                     f"伦敦落地当天 {arrival.isoformat()} 入住，住两晚，找附近酒店", report)
        assert "酒店" in hotel["answer"]
        hotel_book = turn(client, sid, one, "hotel-book",
                          "预订刚才最贵的酒店房间，住两晚", report)
        report["hotel_card"] = bool(hotel_book["actions"])
        assert [row["action_type"] for row in hotel_book["actions"]] == ["hotel_book"]
        assert counts()["hotel_orders"] == before["hotel_orders"]
        rejected = action(client, sid, one, hotel_book["actions"][0]["id"], "reject")
        assert rejected.status_code == 200, rejected.text
        assert counts()["hotel_orders"] == before["hotel_orders"]
        turn(client, sid, one, "rental-query",
             "我在上海租车，明天10点取，大后天18点还", report)
        rental = turn(client, sid, one, "rental-book",
                      "我要租最贵的那辆车", report)
        report["rental_card"] = bool(rental["actions"])
        assert [row["action_type"] for row in rental["actions"]] == ["rental_book"]
        assert counts()["rental_orders"] == before["rental_orders"]
        turn(client, sid, one, "places", "我在上海，附近有什么经典地点？", report)
        policy = turn(client, sid, one, "policy", "机票取消要收多少费用？", report)
        assert policy["actions"] == [] and any(
            ref.get("type") == "policy" for ref in policy["references"])
        report["fixed_model_calls"] = model.calls
        report["after_prepare"] = counts()
        report["session_id"] = sid
        report["flight_action_id"] = flight_card["id"]
        report["rental_action_id"] = (rental["actions"][0]["id"]
                                      if rental["actions"] else None)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({
            "session_id": sid, "before": before,
            "flight_action_id": flight_card["id"],
            "rental_action_id": report["rental_action_id"]},
            ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("P3.1e4 prepared. Restart only the isolated PostgreSQL project, then finish.")


def finish():
    require_isolation()
    state = json.loads(STATE.read_text(encoding="utf-8"))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    sid = state["session_id"]
    before = state["before"]
    model = FixedModel()
    with (patch("api.travel.get_chat_model", return_value=model),
          patch("api.travel.get_recommendation_provider",
                return_value=ReviewedCatalogProvider()),
          TestClient(app()) as client):
        one, two = login(client, 1), login(client, 2)
        history = client.get(f"/api/v1/sessions/{sid}", headers=one)
        assert history.status_code == 200 and len(history.json()["messages"]) >= 16
        assert client.get(f"/api/v1/sessions/{sid}", headers=two).status_code == 404
        before_finish = counts()
        assert before_finish == report["after_prepare"], (before_finish,
                                                           report["after_prepare"])
        turn(client, sid, one, "after-restart", "我现在有哪些有效行程？", report)
        if state["rental_action_id"]:
            confirmed = action(client, sid, one, state["rental_action_id"], "confirm")
            assert confirmed.status_code == 200, confirmed.text
            assert counts()["rental_orders"] == before["rental_orders"] + 1
        assert counts()["flight_orders"] == before["flight_orders"] + 1
        assert counts()["hotel_orders"] == before["hotel_orders"]
    report["after_restart"] = counts()
    report["restart_passed"] = True
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE.unlink()
    print("P3.1e4 isolated PostgreSQL restart journey: OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finish"))
    args = parser.parse_args()
    prepare() if args.phase == "prepare" else finish()
