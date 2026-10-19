"""Verify all dialogue proposals on PostgreSQL across a database restart.

Run ``prepare``, restart db, then run ``finish``. Temporary credentials stay
only in Git-ignored data/generated and are removed on successful completion.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, func, insert, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import engine
from db.travel_models import (action_results, audit_events, chat_messages,
                              chat_sessions, chat_turns, flight_orders,
                              hotel_orders, pending_actions, rental_orders,
                              travelers)
from main import Server
from scripts.verify_p25_all_actions import _inputs
from services.checkpoints import open_checkpointer
from utils.password_hash import get_hashed_password

STATE = ROOT / "data/generated/p26b-verifier-state.json"
TABLES = (travelers, flight_orders, hotel_orders, rental_orders,
          chat_sessions, chat_messages, chat_turns, pending_actions,
          action_results, audit_events)


class FixedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def invoke(self, _messages):
        if not self.decisions:
            raise AssertionError("Unexpected model call")
        return AIMessage(content=json.dumps(self.decisions.pop(0)))


def _app():
    server = Server()
    server.init_app()
    assert not any(route.path.startswith("/api/graph") for route in server.app.routes)
    return server.app


def _auth(client, username, password):
    result = client.post("/api/v1/login", json={"username": username,
                                                "password": password})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["access_token"]}


def _counts():
    with engine.connect() as db:
        return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
                for table in TABLES}


def _turn(client, sid, auth, key, content):
    result = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                         json={"request_key": key, "content": content})
    assert result.status_code == 200, result.text
    return result.json()


def _decision(*actions):
    return {"intent": "transaction", "params": {"actions": [
        {"action_type": action_type, "parameters": params}
        for action_type, params in actions]}, "answer": ""}


def _cleanup(state):
    sid = state["session"]
    with open_checkpointer() as saver:
        saver.delete_thread(sid)
    with engine.begin() as db:
        for table in (action_results, audit_events, pending_actions,
                      chat_turns, chat_messages):
            db.execute(delete(table).where(table.c.session_id == sid))
        db.execute(delete(chat_sessions).where(chat_sessions.c.id == sid))
        for table in (rental_orders, hotel_orders, flight_orders):
            db.execute(delete(table).where(table.c.traveler_id == state["owner"]))
        db.execute(delete(travelers).where(
            travelers.c.id.in_((state["owner"], state["outsider"]))))
    assert _counts() == state["baseline"], "Verifier did not restore baseline"
    STATE.unlink(missing_ok=True)


def prepare():
    if STATE.exists():
        raise RuntimeError("Previous P2.6b verifier state exists")
    baseline = _counts()
    if baseline["pending_actions"] or baseline["chat_sessions"]:
        raise RuntimeError("P2.6b verifier requires a clean demo snapshot")
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        first, replacement, extras, hotel, rental, check_in, pickup, dropoff = _inputs(db, now)
    marker, password = uuid4().hex[:16], uuid4().hex
    owner, outsider, sid = "p26b_owner_" + marker, "p26b_other_" + marker, str(uuid4())
    state = {"owner": owner, "outsider": outsider, "session": sid,
             "password": password, "baseline": baseline}
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=person, username=person, password_hash=get_hashed_password(password),
                display_name="P2.6b verifier", active=1)
                for person in (owner, outsider)])
            db.execute(insert(chat_sessions).values(
                id=sid, traveler_id=owner, status="active"))
            db.execute(insert(flight_orders).values(
                id=str(uuid4()), traveler_id=owner, flight_id=first["id"],
                amount=6000, status="confirmed", version=1))
            db.execute(insert(hotel_orders).values(
                id=str(uuid4()), traveler_id=owner, product_id=hotel["id"],
                check_in=check_in, check_out=check_in + timedelta(days=1),
                total_price=hotel["nightly_price"], status="confirmed", version=1))
            db.execute(insert(rental_orders).values(
                id=str(uuid4()), traveler_id=owner, product_id=rental["id"],
                pickup_at=pickup, dropoff_at=dropoff,
                daily_price=rental["daily_price"], billable_days=2,
                total_price=rental["daily_price"] * 2,
                status="confirmed", version=1))
        day = check_in.isoformat()
        next_day = (check_in + timedelta(days=1)).isoformat()
        later_day = (check_in + timedelta(days=2)).isoformat()
        book_pickup = (pickup + timedelta(days=2)).isoformat()
        book_dropoff = (dropoff + timedelta(days=2)).isoformat()
        change_pickup = (pickup + timedelta(days=1)).isoformat()
        change_dropoff = (dropoff + timedelta(days=1)).isoformat()
        cases = [
            ("flight_book", {"flight_id": extras[0].id},
             f"预订航班 SIM-{extras[0].id}"),
            ("flight_change", {"new_flight_id": replacement.id},
             f"把我的机票改签到 SIM-{replacement.id}"),
            ("flight_cancel", {}, "取消我的机票"),
            ("hotel_book", {"product_id": hotel["id"], "check_in": day,
                            "check_out": next_day},
             f"预订酒店产品 {hotel['id']}，{day} 入住一晚"),
            ("hotel_change", {"check_in": next_day, "check_out": later_day},
             f"把我的酒店改到 {next_day} 入住，{later_day} 退房"),
            ("hotel_cancel", {}, "取消我的酒店"),
            ("rental_book", {"product_id": rental["id"],
                             "pickup_at": book_pickup, "dropoff_at": book_dropoff},
             f"预订租车产品 {rental['id']}，{book_pickup} 取车，{book_dropoff} 还车"),
            ("rental_change", {"pickup_at": change_pickup,
                               "dropoff_at": change_dropoff},
             f"把我的租车改成 {change_pickup} 取车，{change_dropoff} 还车"),
            ("rental_cancel", {}, "取消我的租车"),
        ]
        decisions = [_decision((kind, params)) for kind, params, _ in cases]
        decisions.append(_decision(
            ("flight_change", {"new_flight_id": replacement.id}),
            ("hotel_book", {"product_id": hotel["id"],
                            "check_in": day, "check_out": next_day})))
        decisions.append(_decision(
            ("flight_change", {"new_flight_id": replacement.id}),
            ("hotel_book", {"product_id": "missing-hotel-product",
                            "check_in": day, "check_out": next_day})))
        model = FixedModel(decisions)
        with patch("api.travel.get_chat_model", return_value=model):
            with TestClient(_app()) as client:
                auth = _auth(client, owner, password)
                foreign = _auth(client, outsider, password)
                cards = []
                for index, (kind, _, content) in enumerate(cases):
                    result = _turn(client, sid, auth, f"p26b-kind-{index:02d}", content)
                    assert len(result["actions"]) == 1, (kind, result)
                    assert result["actions"][0]["action_type"] == kind
                    cards.append(result["actions"][0]["id"])
                dual = _turn(client, sid, auth, "p26b-dual-10",
                             f"把我的机票改签到 SIM-{replacement.id}，并订酒店产品 {hotel['id']}，{day} 入住一晚")
                assert len(dual["actions"]) == 2, dual
                replay = _turn(client, sid, auth, "p26b-dual-10",
                               f"把我的机票改签到 SIM-{replacement.id}，并订酒店产品 {hotel['id']}，{day} 入住一晚")
                assert replay == dual
                before_failed = _counts()
                failed = _turn(client, sid, auth, "p26b-fail-11",
                               f"改签到 SIM-{replacement.id}，再订 missing-hotel-product")
                assert failed["actions"] == []
                after_failed = _counts()
                assert after_failed["pending_actions"] == before_failed["pending_actions"]
                assert after_failed["audit_events"] == before_failed["audit_events"]
                assert client.get(f"/api/v1/sessions/{sid}/actions/{cards[0]}",
                                  headers=foreign).status_code == 404
                assert client.post(f"/api/v1/sessions/{sid}/messages", headers=auth,
                                   json={"content": "split"}).status_code == 409
        assert len(model.decisions) == 0
        during = _counts()
        assert during["pending_actions"] == 11 and during["chat_turns"] == 11
        assert during["flight_orders"] == baseline["flight_orders"] + 1
        assert during["hotel_orders"] == baseline["hotel_orders"] + 1
        assert during["rental_orders"] == baseline["rental_orders"] + 1
        state.update({"cards": cards, "dual": dual})
        STATE.write_text(json.dumps(state), encoding="utf-8")
        print("P2.6b nine dialogue cards, atomic dual cards and no-write baseline: OK")
    except Exception:
        _cleanup(state)
        raise


def finish():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    sid = state["session"]
    with open_checkpointer() as saver:
        assert saver.get_tuple({"configurable": {"thread_id": sid}}) is not None
    with patch("api.travel.get_chat_model", return_value=FixedModel([])):
        with TestClient(_app()) as client:
            auth = _auth(client, state["owner"], state["password"])
            # Replaying the saved key needs no model invocation.
            with engine.connect() as db:
                original = db.execute(select(chat_turns.c.user_content).where(
                    chat_turns.c.session_id == sid,
                    chat_turns.c.request_key == "p26b-dual-10",
                )).scalar_one()
            replay = _turn(client, sid, auth, "p26b-dual-10",
                           original)
            assert replay["actions"] == state["dual"]["actions"]
            confirmed = client.post(
                f"/api/v1/sessions/{sid}/actions/{state['cards'][0]}/confirm",
                headers=auth, json={})
            assert confirmed.status_code == 200, confirmed.text
            rejected = client.post(
                f"/api/v1/sessions/{sid}/actions/{state['cards'][3]}/reject",
                headers=auth, json={})
            assert rejected.status_code == 200, rejected.text
            itinerary = client.get("/api/v1/me/itinerary", headers=auth)
            assert itinerary.status_code == 200
            assert len(itinerary.json()["flight_orders"]) == 2
    _cleanup(state)
    print("P2.6b database restart, replay, separate confirm/reject and cleanup: OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finish"))
    phase = parser.parse_args().phase
    prepare() if phase == "prepare" else finish()
