"""Verify P2.5d1 rental changes across API calls and a database restart.

Run --phase prepare, restart only db, then run --phase finish. Test rows and
credentials are removed after finish; the local state file is Git ignored.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (action_results, audit_events, airports,
                              chat_sessions, demo_state, flight_orders,
                              hotel_orders, pending_actions, policy_documents,
                              rental_orders, rental_products, travelers)
from main import Server
from services.actions import ActionError, create_proposal
from services.confirmations import confirm_action, reject_action
from services.policy_catalog import RENTAL_POLICY
from travel_assistant.core.password_hash import get_hashed_password

STATE = Path(__file__).resolve().parents[1] / "data/generated/p25d1-verifier-state.json"


def _counts(db):
    return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (travelers, flight_orders, hotel_orders, rental_orders,
                          chat_sessions, pending_actions, action_results, audit_events)}


def _app():
    server = Server()
    server.init_app()
    return server.app


def _auth(client, username, password):
    response = client.post("/api/v1/login", json={
        "username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _cleanup(state):
    ids = (state["owner"], state["outsider"])
    with engine.begin() as db:
        for table in (action_results, audit_events, pending_actions,
                      rental_orders, chat_sessions, travelers):
            column = table.c.id if table is travelers else table.c.traveler_id
            db.execute(delete(table).where(column.in_(ids)))
    with engine.connect() as db:
        assert _counts(db) == state["baseline"], "Verifier did not restore counts"
    STATE.unlink(missing_ok=True)


def prepare():
    if STATE.exists():
        raise RuntimeError("Previous verifier state exists; finish or inspect it first")
    marker, password = uuid4().hex[:18], uuid4().hex
    owner, outsider, session, other_session = (
        f"p25d1_owner_{marker}", f"p25d1_other_{marker}", str(uuid4()), str(uuid4()))
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        baseline = _counts(db)
        assert baseline["pending_actions"] == 0
        product = db.execute(select(rental_products, airports.c.timezone).join(
            airports, airports.c.code == rental_products.c.airport_code
        ).where(rental_products.c.airport_code == "BSL")
            .order_by(rental_products.c.id).limit(1)).mappings().one()
        as_of = db.execute(select(demo_state.c.as_of)).scalar_one()
    zone = ZoneInfo(product["timezone"])
    pickup = datetime.combine(as_of + timedelta(days=1), time(12), zone)
    assert pickup.astimezone(timezone.utc) > now + timedelta(hours=1), (
        "Demo rental window expired; reset explicitly after backup")
    dropoff = (pickup.astimezone(timezone.utc) + timedelta(hours=25)).astimezone(zone)
    shorter = (pickup.astimezone(timezone.utc) + timedelta(hours=23)).astimezone(zone)
    longer = (pickup.astimezone(timezone.utc) + timedelta(hours=49)).astimezone(zone)
    state = {"owner": owner, "outsider": outsider, "password": password,
             "session": session, "baseline": baseline}
    seeded = False
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=person, username=person,
                password_hash=get_hashed_password(password),
                display_name="P2.5d1 verifier", active=1,
            ) for person in (owner, outsider)])
            db.execute(insert(chat_sessions), [
                dict(id=session, traveler_id=owner, status="active"),
                dict(id=other_session, traveler_id=outsider, status="active")])
            book = create_proposal(db, owner, session, "rental_book", {
                "product_id": product["id"], "pickup_at": pickup.isoformat(),
                "dropoff_at": dropoff.isoformat()})
        seeded = True
        with TestClient(_app()) as client:
            auth = _auth(client, owner, password)
            booked = client.post(
                f"/api/v1/sessions/{session}/actions/{book['id']}/confirm",
                headers=auth)
            assert booked.status_code == 200, booked.text
            order_id = booked.json()["order_id"]
        params = {"order_id": order_id, "expected_version": 1,
                  "pickup_at": pickup.isoformat(), "dropoff_at": shorter.isoformat()}
        with engine.begin() as db:
            change = create_proposal(db, owner, session, "rental_change", params)
            assert change["price"]["refundable"] == product["daily_price"]
            assert change["after"]["billable_days"] == 1
            assert change["policy"]["version"] == 3
            assert db.execute(select(rental_orders.c.version).where(
                rental_orders.c.id == order_id)).scalar_one() == 1
            try:
                create_proposal(db, outsider, other_session, "rental_change", params)
            except ActionError:
                pass
            else:
                raise AssertionError("Cross-account change was accepted")
        with TestClient(_app()) as client:
            auth = _auth(client, owner, password)
            other = _auth(client, outsider, password)
            path = f"/api/v1/sessions/{session}/actions/{change['id']}"
            assert client.get(path, headers=other).status_code == 404
            assert client.post(path + "/confirm", headers=other).status_code == 404
            response = client.post(path + "/confirm", headers=auth)
            assert response.status_code == 200, response.text
            changed = response.json()
            assert changed["order"]["version"] == 2
            assert changed["order"]["total_price"] == product["daily_price"]
            assert client.post(path + "/confirm", headers=auth).json() == changed
            assert any(row["id"] == order_id for row in client.get(
                "/api/v1/me/itinerary", headers=auth).json()["rental_orders"])
        with engine.begin() as db:
            next_params = {"order_id": order_id, "expected_version": 2,
                           "pickup_at": pickup.isoformat(),
                           "dropoff_at": longer.isoformat()}
            one = create_proposal(db, owner, session, "rental_change", next_params)
            two = create_proposal(db, owner, session, "rental_cancel", {
                "order_id": order_id, "expected_version": 2})
            refused = create_proposal(db, owner, session, "rental_change", next_params)
            expired = create_proposal(db, owner, session, "rental_change", next_params)
            legacy = create_proposal(db, owner, session, "rental_change", next_params)
            db.execute(update(pending_actions).where(
                pending_actions.c.id == legacy["id"]).values(
                    policy_id=RENTAL_POLICY["id"],
                    policy_version=RENTAL_POLICY["version"]))
        with engine.begin() as db:
            assert reject_action(db, owner, session, refused["id"])["status"] == "rejected"
            assert confirm_action(db, owner, session, expired["id"],
                                  now=now + timedelta(minutes=16))["status"] == "expired"
            assert confirm_action(db, owner, session, legacy["id"])["status"] == "invalidated"
        barrier = Barrier(2)

        def worker(card):
            with Session(engine) as db:
                barrier.wait()
                result = confirm_action(db, owner, session, card["id"])
                db.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker, one)
            second = pool.submit(worker, two)
            outcomes = [first.result(timeout=30), second.result(timeout=30)]
        assert {result["status"] for result in outcomes} == {"confirmed", "invalidated"}
        state["order_id"] = order_id
        state["change_id"] = change["id"]
        state["final_status"] = next(row for row in outcomes if row["status"] == "confirmed")["order"]["status"]
        STATE.write_text(json.dumps(state), encoding="utf-8")
        print("P2.5d1 prepare verified: API change/refund, ownership, replay, "
              "v2 card rejection, reject/expiry and change/cancel race")
    finally:
        if seeded and not STATE.exists():
            _cleanup(state)


def finish():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    try:
        with engine.connect() as db:
            row = db.execute(select(rental_orders).where(
                rental_orders.c.id == state["order_id"],
                rental_orders.c.traveler_id == state["owner"])).mappings().one()
            assert row["status"] == state["final_status"] and row["version"] == 3
        with TestClient(_app()) as client:
            auth = _auth(client, state["owner"], state["password"])
            path = (f"/api/v1/sessions/{state['session']}/actions/"
                    f"{state['change_id']}/confirm")
            replay = client.post(path, headers=auth)
            assert replay.status_code == 200 and replay.json()["order_id"] == row["id"]
            assert any(item["id"] == row["id"] for item in client.get(
                "/api/v1/me/itinerary", headers=auth).json()["rental_orders"])
        print("P2.5d1 restart verified: order, action result and itinerary persisted")
    finally:
        _cleanup(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "finish"), required=True)
    args = parser.parse_args()
    prepare() if args.phase == "prepare" else finish()
