"""Bounded DeepSeek dialogue evaluation on disposable PostgreSQL demo records.

Run only after taking a verified pg_dump. The report and recovery state stay in
Git-ignored data/generated. No proposal is confirmed by this script.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import engine
from db.travel_models import (action_results, airports, audit_events, flights,
                              chat_messages, chat_sessions, chat_turns,
                              flight_orders, hotel_orders, pending_actions,
                              rental_orders, travelers)
from main import Server
from scripts.verify_p25_all_actions import _inputs
from services.checkpoints import open_checkpointer
from services.model_config import get_chat_model
from services.actions import FIELDS
from services.travel import find_flights
from utils.password_hash import get_hashed_password


STATE = ROOT / "data/generated/p26c-live-state.json"
REPORT = ROOT / "data/generated/p26c-live-report.json"
TABLES = (travelers, flight_orders, hotel_orders, rental_orders,
          chat_sessions, chat_messages, chat_turns, pending_actions,
          action_results, audit_events)
MAX_CALLS = 30


def _counts():
    with engine.connect() as db:
        return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
                for table in TABLES}


def _multiple_flight_query(db, now):
    zones = dict(db.execute(select(airports.c.code, airports.c.timezone)).all())
    groups = defaultdict(int)
    rows = db.execute(select(flights.c.departure_airport,
                             flights.c.arrival_airport,
                             flights.c.departure_at).where(
        flights.c.status == "Scheduled",
        flights.c.departure_at > now + timedelta(hours=2))).all()
    for departure, arrival, at in rows:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        local_date = at.astimezone(ZoneInfo(zones[departure])).date()
        groups[(departure, arrival, local_date)] += 1
    for departure, arrival, day in sorted(groups, key=lambda key: (key[2], key[0], key[1])):
        if groups[(departure, arrival, day)] >= 2 and len(
                find_flights(db, departure, arrival, day, limit=3)) >= 2:
            return departure, arrival, day.isoformat()
    raise RuntimeError("No real multiple-candidate demo route remains")


def _app():
    server = Server()
    server.init_app()
    assert not any(route.path.startswith("/api/graph") for route in server.app.routes)
    return server.app


def _cleanup(state):
    owner, outsider = state["owner"], state["outsider"]
    with engine.connect() as db:
        session_ids = db.execute(select(chat_sessions.c.id).where(
            chat_sessions.c.traveler_id.in_((owner, outsider)))).scalars().all()
    with open_checkpointer() as saver:
        for sid in session_ids:
            saver.delete_thread(sid)
    with engine.begin() as db:
        for table in (action_results, audit_events, pending_actions,
                      chat_turns, chat_messages):
            db.execute(delete(table).where(table.c.session_id.in_(session_ids)))
        db.execute(delete(chat_sessions).where(
            chat_sessions.c.traveler_id.in_((owner, outsider))))
        for table in (rental_orders, hotel_orders, flight_orders):
            db.execute(delete(table).where(table.c.traveler_id == owner))
        db.execute(delete(travelers).where(
            travelers.c.id.in_((owner, outsider))))
    if _counts() != state["baseline"]:
        raise RuntimeError("Evaluation cleanup did not restore baseline counts")
    STATE.unlink(missing_ok=True)


class MeteredModel:
    def __init__(self, model, limit):
        self.model = model
        self.limit = limit
        self.calls = []

    def invoke(self, messages):
        if len(self.calls) >= self.limit:
            raise RuntimeError("P2.6c model call limit reached")
        started = perf_counter()
        record = {"seconds": None, "usage": {}, "valid_json": False,
                  "error": None}
        self.calls.append(record)  # failed requests count toward the limit
        try:
            reply = self.model.invoke(messages)
            usage = getattr(reply, "usage_metadata", None)
            if not usage:
                usage = (getattr(reply, "response_metadata", {}) or {}).get(
                    "token_usage", {})
            record["usage"] = {key: value for key, value in (usage or {}).items()
                               if key in ("input_tokens", "output_tokens",
                                          "total_tokens", "prompt_tokens",
                                          "completion_tokens") and type(value) is int}
            try:
                decision = json.loads(reply.content)
                record["valid_json"] = isinstance(decision, dict)
                if record["valid_json"]:
                    record["decision"] = decision
            except (TypeError, ValueError):
                pass
            return reply
        except Exception as error:
            record["error"] = type(error).__name__
            raise
        finally:
            record["seconds"] = round(perf_counter() - started, 3)


def _login(client, username, password):
    response = client.post("/api/v1/login", json={
        "username": username, "password": password})
    assert response.status_code == 200, response.status_code
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _session(client, auth):
    response = client.post("/api/v1/sessions", headers=auth, json={})
    assert response.status_code == 201, response.status_code
    return response.json()["id"]


def _turn(client, auth, sid, label, content, expected=(), *, no_card=False):
    response = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                           json={"request_key": "p26c-" + label,
                                 "content": content})
    body = response.json()
    cards = body.get("actions", []) if response.status_code == 200 else []
    kinds = [card["action_type"] for card in cards]
    refs = body.get("references", []) if response.status_code == 200 else []
    ref_types = [ref.get("type") for ref in refs if isinstance(ref, dict)]
    if expected:
        passed = (response.status_code == 200 and
                  (kinds == list(expected) if all(x in FIELDS for x in expected)
                   else all(x in ref_types for x in expected)))
    else:
        passed = response.status_code == 200 and (not cards if no_card else True)
    if label == "real-policy" and response.status_code == 200:
        answer = body.get("answer", "")
        passed = passed and any(phrase in answer for phrase in
                                ("无法核实", "不能核实", "无法确认"))
    if label == "no-flight" and response.status_code == 200:
        passed = passed and "flight" not in ref_types
    result = {"label": label, "status_code": response.status_code,
              "passed": passed, "action_types": kinds, "reference_types": ref_types,
              "answer_excerpt": str(body.get("answer", body.get("detail", "")))[:300]}
    print(f"{label}: {'PASS' if passed else 'FAIL'} ({response.status_code}, {kinds})")
    return result, body


def evaluate(only=(), max_calls=MAX_CALLS):
    if STATE.exists():
        raise RuntimeError("Previous P2.6c state exists; inspect or run cleanup first")
    baseline = _counts()
    if baseline["chat_sessions"] or baseline["pending_actions"]:
        raise RuntimeError("P2.6c requires a clean demo snapshot")
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        first, replacement, extras, hotel, rental, check_in, pickup, dropoff = _inputs(db, now)
        multi_departure, multi_arrival, multi_day = _multiple_flight_query(db, now)
        departure_zone = db.execute(select(airports.c.timezone).where(
            airports.c.code == first["departure_airport"])).scalar_one()
    marker = uuid4().hex[:16]
    password = uuid4().hex
    owner, outsider = "p26c_owner_" + marker, "p26c_other_" + marker
    state = {"owner": owner, "outsider": outsider,
             "password": password, "baseline": baseline}
    report = {"model": None, "calls": [], "scenarios": [],
              "selected": list(only),
              "baseline_restored": False}
    model = None
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=person, username=person,
                password_hash=get_hashed_password(password),
                display_name="P2.6c evaluation", active=1)
                for person in (owner, outsider)])
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
        STATE.write_text(json.dumps(state), encoding="utf-8")

        model = MeteredModel(get_chat_model(), max_calls)
        report["model"] = model.model.model_name
        with patch("api.travel.get_chat_model", return_value=model):
            with TestClient(_app()) as client:
                auth = _login(client, owner, password)
                foreign = _login(client, outsider, password)

                def run(label, content, expected=(), *, no_card=False, sid=None):
                    if only and label not in only:
                        return sid, {}
                    sid = sid or _session(client, auth)
                    result, body = _turn(client, auth, sid, label, content,
                                         expected, no_card=no_card)
                    report["scenarios"].append(result)
                    return sid, body

                day = check_in.isoformat()
                next_day = (check_in + timedelta(days=1)).isoformat()
                later_day = (check_in + timedelta(days=2)).isoformat()
                departure_at = first["departure_at"]
                if departure_at.tzinfo is None:
                    departure_at = departure_at.replace(tzinfo=timezone.utc)
                flight_day = departure_at.astimezone(
                    ZoneInfo(departure_zone)).date().isoformat()
                book_pickup = (pickup + timedelta(days=2)).isoformat()
                book_dropoff = (dropoff + timedelta(days=2)).isoformat()
                change_pickup = (pickup + timedelta(days=1)).isoformat()
                change_dropoff = (dropoff + timedelta(days=1)).isoformat()

                sid, _ = run("hello", "你好，我想安排一次模拟旅行。")
                run("followup", "请查一下我的行程。", ("order",), sid=sid)
                run("policy", "本虚构平台机票取消政策是什么？", ("policy",))
                run("real-policy", "真实汉莎航空的改签政策是什么？请不要猜测。",
                    no_card=True)
                run("flight-search", f"查 {flight_day} "
                    f"{first['departure_airport']} 到 {first['arrival_airport']} 的航班。",
                    ("flight",))
                run("hotel-search", f"查 Basel {day} 入住、{next_day} 退房的酒店。",
                    ("hotel_product",))
                run("rental-search", f"查 BSL 机场 {day} 到 {next_day} 的租车。",
                    ("rental_product",))
                sid, _ = run("select-search", f"查 {multi_day} "
                    f"{multi_departure} 到 {multi_arrival} 的航班。",
                    ("flight",))
                run("select-second", "预订第二班航班。", ("flight_book",), sid=sid)
                run("no-flight", "查 2099-01-01 HAM 到 BSL 的航班。",
                    no_card=True)

                cases = [
                    ("flight-book", f"预订航班 SIM-{extras[0].id}", "flight_book"),
                    ("flight-change", f"把我的机票改签到 SIM-{replacement.id}",
                     "flight_change"),
                    ("flight-cancel", "取消我的机票", "flight_cancel"),
                    ("hotel-book", f"预订酒店产品 {hotel['id']}，{day} 入住一晚",
                     "hotel_book"),
                    ("hotel-change", f"把我的酒店改到 {next_day} 入住，"
                     f"{later_day} 退房", "hotel_change"),
                    ("hotel-cancel", "取消我的酒店", "hotel_cancel"),
                    ("rental-book", f"预订租车产品 {rental['id']}，"
                     f"{book_pickup} 取车，{book_dropoff} 还车", "rental_book"),
                    ("rental-change", f"把我的租车改成 {change_pickup} 取车，"
                     f"{change_dropoff} 还车", "rental_change"),
                    ("rental-cancel", "取消我的租车", "rental_cancel"),
                ]
                for label, content, expected in cases:
                    run(label, content, (expected,))
                dual_text = (f"把我的机票改签到 SIM-{replacement.id}，并订酒店产品 "
                             f"{hotel['id']}，{day} 入住一晚")
                dual_sid, dual = run("dual", dual_text,
                                     ("flight_change", "hotel_book"))
                if dual_sid:
                    replay = client.post(f"/api/v1/sessions/{dual_sid}/turns",
                                         headers=auth,
                                         json={"request_key": "p26c-dual",
                                               "content": dual_text})
                    report["replay_equal"] = (replay.status_code == 200 and
                                              replay.json() == dual)
                    report["foreign_status"] = client.get(
                        f"/api/v1/sessions/{dual_sid}", headers=foreign).status_code
                run("missing-date", "帮我订这家酒店，日期还没决定。",
                    no_card=True)

        during = _counts()
        report["orders_unchanged_before_cleanup"] = all(
            during[name] == baseline[name] + 1
            for name in ("flight_orders", "hotel_orders", "rental_orders"))
    finally:
        try:
            if STATE.exists():
                _cleanup(state)
                report["baseline_restored"] = True
        finally:
            report["calls"] = model.calls if model is not None else []
            report["scenario_passes"] = sum(
                item["passed"] for item in report["scenarios"])
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"P2.6c: {report['scenario_passes']}/{len(report['scenarios'])} "
          f"scenarios, {len(report['calls'])} model requests; report: {REPORT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("evaluate", "cleanup"))
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--only", nargs="*", default=())
    args = parser.parse_args()
    if args.mode == "cleanup":
        _cleanup(json.loads(STATE.read_text(encoding="utf-8")))
        print("P2.6c temporary records cleaned")
    else:
        if not 1 <= args.max_calls <= MAX_CALLS:
            parser.error("max-calls must be between 1 and 30")
        evaluate(set(args.only), args.max_calls)
