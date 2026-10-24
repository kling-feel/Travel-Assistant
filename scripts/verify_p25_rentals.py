"""Verify rental transactions before and after a deliberate PostgreSQL restart.

Run --phase prepare, restart only the db container, then run --phase finish.
Temporary credentials and IDs stay in a Git-ignored local state file.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo
import sys

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import (action_results, audit_events, chat_sessions,
                              flight_orders, hotel_orders, pending_actions,
                              rental_orders, rental_products, travelers, airports,
                              demo_state)
from main import Server
from travel_assistant.services.actions import create_proposal
from travel_assistant.services.confirmations import confirm_action, reject_action
from travel_assistant.core.password_hash import get_hashed_password


STATE = Path(__file__).resolve().parents[1] / "data/generated/p25b-verifier-state.json"


def _app():
    server = Server()
    server.init_app()
    assert not any(route.path.startswith("/api/graph/") for route in server.app.routes)
    return server.app


def _auth(client, username, password):
    response = client.post("/api/v1/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _counts(db):
    return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (travelers, flight_orders, hotel_orders, rental_orders,
                          chat_sessions, pending_actions, action_results, audit_events)}


def _cleanup(state):
    ids = (state["owner"], state["outsider"])
    with engine.begin() as db:
        for table in (action_results, audit_events, pending_actions,
                      rental_orders, chat_sessions, travelers):
            db.execute(delete(table).where(table.c.traveler_id.in_(ids)
                                           if table is not travelers else table.c.id.in_(ids)))
    with engine.connect() as db:
        assert _counts(db) == state["baseline"], "Verifier did not restore baseline counts"
    STATE.unlink(missing_ok=True)


def prepare():
    if STATE.exists():
        raise RuntimeError("Previous verifier state exists; finish or inspect it first")
    marker, password = uuid4().hex[:18], uuid4().hex
    owner, outsider = f"p25b_owner_{marker}", f"p25b_other_{marker}"
    first_session, second_session, outsider_session = (str(uuid4()) for _ in range(3))
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
    params = {"product_id": product["id"], "pickup_at": pickup.isoformat(),
              "dropoff_at": dropoff.isoformat()}
    state = {"owner": owner, "outsider": outsider, "password": password,
             "sessions": [first_session, second_session, outsider_session],
             "baseline": baseline}
    seeded = False
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=traveler_id, username=traveler_id,
                password_hash=get_hashed_password(password),
                display_name="P2.5b verifier", active=1,
            ) for traveler_id in (owner, outsider)])
            db.execute(insert(chat_sessions), [dict(
                id=session_id, traveler_id=traveler_id, status="active")
                for session_id, traveler_id in ((first_session, owner),
                                                (second_session, owner),
                                                (outsider_session, outsider))])
            first = create_proposal(db, owner, first_session, "rental_book", params)
        seeded = True
        with engine.connect() as db:
            assert db.execute(select(func.count()).select_from(rental_orders).where(
                rental_orders.c.traveler_id == owner)).scalar_one() == 0
        with TestClient(_app()) as client:
            own_auth, other_auth = (_auth(client, name, password)
                                    for name in (owner, outsider))
            path = f"/api/v1/sessions/{first_session}/actions/{first['id']}"
            assert client.get(path, headers=other_auth).status_code == 404
            assert client.post(path + "/confirm", headers=other_auth).status_code == 404
            response = client.post(path + "/confirm", headers=own_auth)
            assert response.status_code == 200, response.text
            booked = response.json()
            assert booked["order"]["billable_days"] == 2
            assert booked["price"]["payable"] == 2 * product["daily_price"]
            assert client.post(path + "/confirm", headers=own_auth).json() == booked
            assert len(client.get("/api/v1/me/itinerary", headers=own_auth)
                       .json()["rental_orders"]) == 1
            assert (client.get("/api/v1/me/itinerary", headers=other_auth)
                    .json()["rental_orders"] == [])
        with engine.begin() as db:
            cancel = create_proposal(db, owner, first_session, "rental_cancel", {
                "order_id": booked["order_id"], "expected_version": 1})
        assert cancel["price"]["refundable"] == booked["price"]["payable"]
        with TestClient(_app()) as client:
            auth = _auth(client, owner, password)
            path = f"/api/v1/sessions/{first_session}/actions/{cancel['id']}"
            response = client.post(path + "/confirm", headers=auth)
            assert response.status_code == 200, response.text
            assert response.json()["order"]["status"] == "cancelled"
        with engine.begin() as db:
            one = create_proposal(db, owner, first_session, "rental_book", params)
            two = create_proposal(db, owner, second_session, "rental_book", params)
            rejected = create_proposal(db, outsider, outsider_session,
                                       "rental_book", params)
            expired = create_proposal(db, outsider, outsider_session,
                                      "rental_book", params)
        barrier = Barrier(2)

        def worker(session_id, action_id):
            with Session(engine) as db:
                barrier.wait()
                outcome = confirm_action(db, owner, session_id, action_id)
                db.commit()
                return outcome

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(worker, first_session, one["id"])
            b = pool.submit(worker, second_session, two["id"])
            outcomes = [a.result(timeout=30), b.result(timeout=30)]
        assert {row["status"] for row in outcomes} == {"confirmed", "invalidated"}
        winner = next(row for row in outcomes if row["status"] == "confirmed")
        with engine.begin() as db:
            assert reject_action(db, outsider, outsider_session,
                                 rejected["id"])["status"] == "rejected"
            assert confirm_action(db, outsider, outsider_session, expired["id"],
                                  now=now + timedelta(minutes=16))["status"] == "expired"
        state["winning_action"] = winner["action_id"]
        state["winning_session"] = (first_session if winner["action_id"] == one["id"]
                                    else second_session)
        state["winning_order"] = winner["order_id"]
        STATE.write_text(json.dumps(state), encoding="utf-8")
        print("P2.5b prepare verified: API book/cancel, owner isolation, replay, "
              "25-hour quote, two-card concurrency, reject and expiry")
        print("Restart the db container, then run --phase finish")
    finally:
        if seeded and not STATE.exists():
            _cleanup(state)


def finish():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    try:
        with engine.connect() as db:
            row = db.execute(select(rental_orders).where(
                rental_orders.c.id == state["winning_order"],
                rental_orders.c.traveler_id == state["owner"])).mappings().one()
            assert row["status"] == "confirmed" and row["billable_days"] == 2
        with TestClient(_app()) as client:
            auth = _auth(client, state["owner"], state["password"])
            path = (f"/api/v1/sessions/{state['winning_session']}/actions/"
                    f"{state['winning_action']}")
            replay = client.post(path + "/confirm", headers=auth)
            assert replay.status_code == 200 and replay.json()["order_id"] == row["id"]
            trip = client.get("/api/v1/me/itinerary", headers=auth).json()
            assert any(item["id"] == row["id"] for item in trip["rental_orders"])
        print("P2.5b restart verified: rental order, result replay and itinerary persisted")
    finally:
        _cleanup(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "finish"), required=True)
    args = parser.parse_args()
    prepare() if args.phase == "prepare" else finish()
