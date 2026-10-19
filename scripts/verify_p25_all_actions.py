"""Check all eight confirmed demo actions across a PostgreSQL restart.

Run --phase prepare, restart only the db container, then run --phase finish.
The temporary login and IDs are kept in a Git-ignored local state file.
"""

import argparse
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import engine
from db.travel_models import (action_results, audit_events, chat_sessions,
                              demo_state, flight_offers, flight_orders, flights,
                              hotel_orders, hotel_products, pending_actions,
                              rental_orders, rental_products, travelers,
                              airports)
from main import Server
from services.actions import create_proposal
from services.demo_pricing import cancellation_fee
from services.policy_catalog import HOTEL_POLICY_V3
from utils.password_hash import get_hashed_password


STATE = Path(__file__).resolve().parents[1] / "data/generated/p25c-verifier-state.json"
COUNT_TABLES = (travelers, flight_orders, hotel_orders, rental_orders,
                chat_sessions, pending_actions, action_results, audit_events)


def _app():
    server = Server()
    server.init_app()
    assert not any(route.path.startswith("/api/graph") for route in server.app.routes)
    return server.app


def _auth(client, username, password):
    response = client.post("/api/v1/login", json={"username": username,
                                                  "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _counts(db):
    return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
            for table in COUNT_TABLES}


def _cleanup(state):
    owner, outsider = state["owner"], state["outsider"]
    ids = (owner, outsider)
    with engine.begin() as db:
        for table in (action_results, audit_events, pending_actions,
                      rental_orders, hotel_orders, flight_orders, chat_sessions):
            db.execute(delete(table).where(table.c.traveler_id.in_(ids)))
        db.execute(delete(travelers).where(travelers.c.id.in_(ids)))
    with engine.connect() as db:
        assert _counts(db) == state["baseline"], "Verifier did not restore baseline counts"
    STATE.unlink(missing_ok=True)


def _inputs(db, now):
    as_of = db.execute(select(demo_state.c.as_of)).scalar_one()
    first = db.execute(select(flights).join(
        flight_offers, flight_offers.c.flight_id == flights.c.id).where(
        flights.c.status == "Scheduled", flights.c.departure_at > now + timedelta(hours=2)
    ).order_by(flights.c.departure_at)).mappings().first()
    assert first is not None, "Demo flights expired; reset explicitly after backup"
    replacement = db.execute(select(flights.c.id, flight_offers.c.amount).join(
        flight_offers, flight_offers.c.flight_id == flights.c.id).where(
        flights.c.id != first["id"],
        flights.c.departure_airport == first["departure_airport"],
        flights.c.arrival_airport == first["arrival_airport"],
        flights.c.status == "Scheduled", flights.c.departure_at > now + timedelta(hours=2)
    ).order_by(flights.c.departure_at)).first()
    assert replacement is not None
    extras = db.execute(select(flights.c.id, flight_offers.c.amount).join(
        flight_offers, flight_offers.c.flight_id == flights.c.id).where(
        flights.c.id.not_in((first["id"], replacement.id)),
        flights.c.status == "Scheduled", flights.c.departure_at > now + timedelta(hours=2)
    ).order_by(flights.c.departure_at).limit(2)).all()
    assert len(extras) == 2
    hotel = db.execute(select(hotel_products).where(
        hotel_products.c.id == "room-1")).mappings().one()
    rental = db.execute(select(rental_products, airports.c.timezone).join(
        airports, airports.c.code == rental_products.c.airport_code).where(
        rental_products.c.airport_code == "BSL")
        .order_by(rental_products.c.id).limit(1)).mappings().one()
    zone = ZoneInfo(rental["timezone"])
    check_in = max(as_of + timedelta(days=2), now.astimezone(zone).date() + timedelta(days=1))
    assert hotel["available_from"] <= check_in
    assert hotel["available_until"] >= check_in + timedelta(days=3)
    pickup = datetime.combine(check_in, time(12), zone)
    dropoff = (pickup.astimezone(timezone.utc) + timedelta(hours=25)).astimezone(zone)
    assert rental["available_from"] <= check_in
    assert dropoff.date() <= rental["available_until"]
    return first, replacement, extras, hotel, rental, check_in, pickup, dropoff


def prepare():
    if STATE.exists():
        raise RuntimeError("Previous verifier state exists; finish or inspect it first")
    marker, password = uuid4().hex[:18], uuid4().hex
    owner, outsider = f"p25c_owner_{marker}", f"p25c_other_{marker}"
    own_session, other_session = str(uuid4()), str(uuid4())
    order_ids = [str(uuid4()) for _ in range(4)]
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        baseline = _counts(db)
        assert baseline["pending_actions"] == 0
        first, replacement, extras, hotel, rental, check_in, pickup, dropoff = _inputs(db, now)
    state = {"owner": owner, "outsider": outsider, "password": password,
             "session": own_session, "baseline": baseline}
    seeded = False
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=person, username=person, password_hash=get_hashed_password(password),
                display_name="P2.5c verifier", active=1)
                for person in (owner, outsider)])
            db.execute(insert(chat_sessions), [
                dict(id=own_session, traveler_id=owner, status="active"),
                dict(id=other_session, traveler_id=outsider, status="active")])
            db.execute(insert(flight_orders), [
                dict(id=order_ids[0], traveler_id=owner, flight_id=first["id"],
                     amount=6000, status="confirmed", version=1),
                dict(id=order_ids[1], traveler_id=owner, flight_id=extras[0].id,
                     amount=6000, status="confirmed", version=1)])
            db.execute(insert(hotel_orders), [
                dict(id=order_id, traveler_id=owner, product_id=hotel["id"],
                     check_in=check_in, check_out=check_in + timedelta(days=1),
                     total_price=hotel["nightly_price"], status="confirmed", version=1)
                for order_id in order_ids[2:]])
        seeded = True
        stay = {"product_id": hotel["id"], "check_in": check_in.isoformat(),
                "check_out": (check_in + timedelta(days=1)).isoformat()}
        rental_params = {"product_id": rental["id"], "pickup_at": pickup.isoformat(),
                         "dropoff_at": dropoff.isoformat()}
        cases = [
            ("flight_book", {"flight_id": extras[1].id}),
            ("flight_change", {"order_id": order_ids[0], "expected_version": 1,
                               "new_flight_id": replacement.id}),
            ("flight_cancel", {"order_id": order_ids[1], "expected_version": 1}),
            ("hotel_book", stay),
            ("hotel_change", {"order_id": order_ids[2], "expected_version": 1,
                              **stay, "check_out": (check_in + timedelta(days=2)).isoformat()}),
            ("hotel_cancel", {"order_id": order_ids[3], "expected_version": 1}),
            ("rental_book", rental_params),
        ]
        with engine.begin() as db:
            proposals = [(kind, create_proposal(db, owner, own_session, kind, params))
                         for kind, params in cases]
            assert _counts(db)["flight_orders"] == baseline["flight_orders"] + 2
            assert _counts(db)["hotel_orders"] == baseline["hotel_orders"] + 2
            assert _counts(db)["rental_orders"] == baseline["rental_orders"]
        outcomes = []
        with TestClient(_app()) as client:
            own_auth = _auth(client, owner, password)
            other_auth = _auth(client, outsider, password)
            hotel_policy = client.get("/api/v1/policies/hotel", headers=own_auth)
            assert hotel_policy.status_code == 200
            assert hotel_policy.json()[0]["version"] == 4
            for kind, proposal in proposals:
                path = f"/api/v1/sessions/{own_session}/actions/{proposal['id']}"
                assert client.get(path, headers=other_auth).status_code == 404
                assert client.post(path + "/confirm", headers=other_auth).status_code == 404
                response = client.post(path + "/confirm", headers=own_auth)
                assert response.status_code == 200, (kind, response.status_code, response.text)
                result = response.json()
                assert result["status"] == "confirmed"
                assert client.post(path + "/confirm", headers=own_auth).json() == result
                outcomes.append((kind, proposal, result))
            assert outcomes[0][2]["price"]["payable"] == extras[1].amount
            assert outcomes[1][2]["price"]["difference"] == replacement.amount - 6000
            assert outcomes[2][2]["price"]["cancellation_fee"] == cancellation_fee(6000)
            assert outcomes[3][2]["price"]["payable"] == hotel["nightly_price"]
            assert outcomes[4][2]["price"]["new_amount"] == 2 * hotel["nightly_price"]
            assert outcomes[5][2]["price"]["refundable"] == hotel["nightly_price"]
            assert outcomes[6][2]["price"]["payable"] == 2 * rental["daily_price"]
            assert client.get("/api/v1/me/itinerary", headers=other_auth).json()[
                "rental_orders"] == []
        rental_order = outcomes[6][2]["order_id"]
        with engine.begin() as db:
            cancel = create_proposal(db, owner, own_session, "rental_cancel", {
                "order_id": rental_order, "expected_version": 1})
        with TestClient(_app()) as client:
            own_auth = _auth(client, owner, password)
            response = client.post(
                f"/api/v1/sessions/{own_session}/actions/{cancel['id']}/confirm",
                headers=own_auth)
            assert response.status_code == 200, response.text
            cancelled = response.json()
            assert cancelled["price"]["refundable"] == 2 * rental["daily_price"]
            assert cancelled["order"]["status"] == "cancelled"
            outcomes.append(("rental_cancel", cancel, cancelled))
        with engine.begin() as db:
            refused = create_proposal(db, owner, own_session, "hotel_book", stay)
            expired = create_proposal(db, owner, own_session, "hotel_book", stay)
            legacy = create_proposal(db, owner, own_session, "hotel_cancel", {
                "order_id": order_ids[2], "expected_version": 2})
            db.execute(update(pending_actions).where(
                pending_actions.c.id == legacy["id"]).values(
                    policy_id=HOTEL_POLICY_V3["id"],
                    policy_version=HOTEL_POLICY_V3["version"]))
            db.execute(update(pending_actions).where(
                pending_actions.c.id == expired["id"]).values(
                    created_at=now - timedelta(minutes=20),
                    expires_at=now - timedelta(minutes=5)))
        with TestClient(_app()) as client:
            own_auth = _auth(client, owner, password)
            base = f"/api/v1/sessions/{own_session}/actions/"
            assert client.post(base + refused["id"] + "/reject", headers=own_auth).status_code == 200
            assert client.post(base + refused["id"] + "/confirm", headers=own_auth).status_code == 409
            assert client.post(base + expired["id"] + "/confirm", headers=own_auth).status_code == 409
            assert client.post(base + legacy["id"] + "/confirm", headers=own_auth).status_code == 409
            assert len(client.get("/api/v1/me/action-history", headers=own_auth).json()) >= 16
        with engine.connect() as db:
            assert db.execute(select(func.count()).select_from(action_results).where(
                action_results.c.traveler_id == owner)).scalar_one() == 8
            assert db.execute(select(hotel_orders.c.status).where(
                hotel_orders.c.id == order_ids[2])).scalar_one() == "confirmed"
        state["actions"] = [dict(kind=kind, id=card["id"], order_id=result["order_id"])
                            for kind, card, result in outcomes]
        STATE.write_text(json.dumps(state), encoding="utf-8")
        print("P2.5c prepare verified: eight actions, owner isolation, refunds, "
              "replay, refusal, expiry and v3 invalidation")
        print("Restart the db container, then run --phase finish")
    finally:
        if seeded and not STATE.exists():
            _cleanup(state)


def finish():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    try:
        with TestClient(_app()) as client:
            own_auth = _auth(client, state["owner"], state["password"])
            for action in state["actions"]:
                path = (f"/api/v1/sessions/{state['session']}/actions/"
                        f"{action['id']}/confirm")
                response = client.post(path, headers=own_auth)
                assert response.status_code == 200, (action["kind"], response.text)
                assert response.json()["order_id"] == action["order_id"]
            itinerary = client.get("/api/v1/me/itinerary", headers=own_auth)
            assert itinerary.status_code == 200
            assert len(itinerary.json()["rental_orders"]) == 1
            assert itinerary.json()["rental_orders"][0]["status"] == "cancelled"
        print("P2.5c restart verified: eight results and itinerary persisted")
    finally:
        _cleanup(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "finish"), required=True)
    args = parser.parse_args()
    prepare() if args.phase == "prepare" else finish()
