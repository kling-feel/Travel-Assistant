"""Exercise the formal demo journey with temporary accounts, then remove them.

Run ``prepare`` before checking the page and ``finish`` afterward. The
temporary password and cleanup state stay in Git-ignored data/generated/.
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from db import engine
from db.travel_models import (airports, chat_sessions, demo_state, flight_orders,
                              flights, hotel_orders, travelers)
from scripts.verify_p26b_dialogue import _app, _auth, _cleanup, _counts
from scripts.verify_p31e4_journey import FixedModel
from utils.password_hash import get_hashed_password


STATE = ROOT / "data/generated/p31e6-formal-state.json"
REPORT = ROOT / "data/generated/p31e6-formal-journey.json"


def require_formal():
    if settings.POSTGRES_PORT != 5433 or settings.DATABASE_URL:
        raise RuntimeError("This verifier requires the local formal DB on port 5433")
    with engine.connect() as db:
        actual = db.execute(select(demo_state.c.as_of)).scalar_one()
    if actual != date.today():
        raise RuntimeError(f"Formal demo date differs from today: {actual}")


def turn(client, sid, auth, key, content, report):
    response = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                           json={"request_key": key, "content": content})
    if response.status_code != 200:
        raise AssertionError((key, response.status_code, response.text))
    body = response.json()
    report["turns"].append({"key": key, "answer": body["answer"],
                            "actions": [row["action_type"]
                                        for row in body["actions"]]})
    return body


def prepare():
    require_formal()
    if STATE.exists():
        raise RuntimeError("Existing temporary journey state must be finished first")
    baseline = _counts()
    if baseline["chat_sessions"] or baseline["pending_actions"]:
        raise RuntimeError("Expected a clean formal demo snapshot")
    marker = uuid4().hex[:12]
    owner, outsider = f"p31e6_owner_{marker}", f"p31e6_other_{marker}"
    sid, password = str(uuid4()), uuid4().hex
    state = {"owner": owner, "outsider": outsider, "session": sid,
             "password": password, "baseline": baseline}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")
    report = {"date": date.today().isoformat(), "baseline": baseline,
              "turns": [], "temporary_session": sid}
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=person, username=person,
                password_hash=get_hashed_password(password),
                display_name="旅程验收账号", active=1)
                for person in (owner, outsider)])
            db.execute(insert(chat_sessions).values(
                id=sid, traveler_id=owner, status="active"))
        model = FixedModel()
        with patch("api.travel.get_chat_model", return_value=model), TestClient(_app()) as client:
            auth = _auth(client, owner, password)
            foreign = _auth(client, outsider, password)
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            empty = turn(client, sid, auth, "p31e6-empty",
                         f"查{tomorrow}从上海到伦敦的航班", report)
            assert not empty["actions"]
            earliest = turn(client, sid, auth, "p31e6-earliest",
                            "那最早什么时候能走", report)
            assert "SIM-3831" in earliest["answer"]
            proposed = turn(client, sid, auth, "p31e6-flight-card",
                            "就预订最早那班，顺便找落地附近酒店", report)
            assert [card["action_type"] for card in proposed["actions"]] == ["flight_book"]
            flight_card = proposed["actions"][0]
            assert flight_card["parameters"]["flight_id"] == 3831
            replay = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                                 json={"request_key": "p31e6-flight-card",
                                       "content": "就预订最早那班，顺便找落地附近酒店"})
            assert replay.status_code == 200 and replay.json() == proposed
            assert client.get(f"/api/v1/sessions/{sid}/actions/{flight_card['id']}",
                              headers=foreign).status_code == 404
            with engine.connect() as db:
                landing = db.execute(select(flights.c.arrival_at, airports.c.timezone).join(
                    airports, flights.c.arrival_airport == airports.c.code).where(
                    flights.c.id == 3831)).one()
            arrival = landing.arrival_at.astimezone(ZoneInfo(landing.timezone)).date()
            hotel = turn(client, sid, auth, "p31e6-hotel-search",
                         f"伦敦落地当天 {arrival.isoformat()} 入住，住两晚，找附近酒店", report)
            assert "room-LGW-1" in hotel["answer"]
            booked = turn(client, sid, auth, "p31e6-hotel-card",
                          "预订刚才最贵的酒店房间，住两晚", report)
            assert [card["action_type"] for card in booked["actions"]] == ["hotel_book"]
            hotel_card = booked["actions"][0]
            assert hotel_card["parameters"]["check_in"] == arrival.isoformat()
            assert hotel_card["parameters"]["check_out"] == (
                arrival + timedelta(days=2)).isoformat()
            assert hotel_card["price"]["new_amount"] == 420
            with engine.connect() as db:
                assert db.execute(select(flight_orders.c.id).where(
                    flight_orders.c.traveler_id == owner)).all() == []
                assert db.execute(select(hotel_orders.c.id).where(
                    hotel_orders.c.traveler_id == owner)).all() == []
            report.update({"arrival_london": arrival.isoformat(),
                           "flight_card": flight_card["id"],
                           "hotel_card": hotel_card["id"],
                           "fixed_model_calls": model.calls})
        state.update({"flight_card": report["flight_card"],
                      "hotel_card": report["hotel_card"]})
        STATE.write_text(json.dumps(state), encoding="utf-8")
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({"status": "prepared", "arrival_london": arrival.isoformat(),
                          "hotel_two_nights": 420, "orders_before_confirm": 0,
                          "fixed_model_calls": model.calls}, ensure_ascii=False))
    except Exception:
        _cleanup(state)
        raise


def finish():
    require_formal()
    state = json.loads(STATE.read_text(encoding="utf-8"))
    with engine.connect() as db:
        action_rows = db.execute(select(  # Browser may confirm one card and reject the other.
            flight_orders.c.id).where(flight_orders.c.traveler_id == state["owner"])).all()
    _cleanup(state)
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["cleanup_passed"] = True
    report["temporary_flight_orders_before_cleanup"] = len(action_rows)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps({"status": "clean", "baseline_restored": True,
                      "temporary_flight_orders_removed": len(action_rows)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "finish"))
    args = parser.parse_args()
    prepare() if args.phase == "prepare" else finish()
