"""Verify confirmed flight booking on PostgreSQL without changing demo accounts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import sys

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import engine
from db.travel_models import (action_results, audit_events, chat_sessions,
                              flight_offers, flight_orders, flights,
                              pending_actions, travelers)
from main import Server
from services.actions import create_proposal
from services.confirmations import confirm_action, reject_action
from utils.password_hash import get_hashed_password


def _app():
    server = Server()
    server.init_app()
    assert not any(route.path.startswith("/api/graph/") for route in server.app.routes)
    return server.app


def _headers(client, username, password):
    login = client.post("/api/v1/login", json={"username": username, "password": password})
    assert login.status_code == 200, (login.status_code, login.text)
    return {"Authorization": "Bearer " + login.json()["access_token"]}


def _counts(db):
    return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (travelers, flight_orders, chat_sessions, pending_actions,
                          action_results, audit_events)}


def main():
    marker = uuid4().hex[:20]
    owner, outsider = f"p25a_owner_{marker}", f"p25a_other_{marker}"
    password = uuid4().hex
    first_session, second_session = str(uuid4()), str(uuid4())
    outsider_session = str(uuid4())
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        baseline = _counts(db)
        candidates = db.execute(select(flights.c.id, flight_offers.c.amount)
                                .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
                                .where(flights.c.status == "Scheduled",
                                       flights.c.departure_at > now + timedelta(hours=1))
                                .order_by(flights.c.departure_at).limit(2)).all()
        assert len(candidates) == 2, "Demo flights expired; reset explicitly after backup"
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=traveler_id, username=traveler_id,
                password_hash=get_hashed_password(password),
                display_name="P2.5a verifier", active=1,
            ) for traveler_id in (owner, outsider)])
            db.execute(insert(chat_sessions), [dict(
                id=session_id, traveler_id=traveler_id, status="active",
            ) for session_id, traveler_id in ((first_session, owner),
                                               (second_session, owner),
                                               (outsider_session, outsider))])
            normal = create_proposal(db, owner, first_session, "flight_book",
                                     {"flight_id": candidates[0].id})
        with engine.connect() as db:
            assert db.execute(select(func.count()).select_from(flight_orders).where(
                flight_orders.c.traveler_id == owner)).scalar_one() == 0
        with TestClient(_app()) as client:
            own_auth = _headers(client, owner, password)
            other_auth = _headers(client, outsider, password)
            path = f"/api/v1/sessions/{first_session}/actions/{normal['id']}"
            assert client.get(path, headers=other_auth).status_code == 404
            assert client.post(path + "/confirm", headers=other_auth).status_code == 404
            response = client.post(path + "/confirm", headers=own_auth)
            assert response.status_code == 200, (response.status_code, response.text)
            result = response.json()
            assert result["order"]["flight_id"] == candidates[0].id
            assert result["price"]["payable"] == candidates[0].amount
            assert len(client.get("/api/v1/me/itinerary", headers=own_auth)
                       .json()["flight_orders"]) == 1
        with TestClient(_app()) as restarted:
            own_auth = _headers(restarted, owner, password)
            replay = restarted.post(path + "/confirm", headers=own_auth)
            assert replay.status_code == 200 and replay.json() == result

        with engine.begin() as db:
            first = create_proposal(db, owner, first_session, "flight_book",
                                    {"flight_id": candidates[1].id})
            second = create_proposal(db, owner, second_session, "flight_book",
                                     {"flight_id": candidates[1].id})
        barrier = Barrier(2)

        def worker(session_id, action_id):
            with Session(engine) as db:
                barrier.wait()
                outcome = confirm_action(db, owner, session_id, action_id)
                db.commit()
                return outcome

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(worker, first_session, first["id"])
            two = pool.submit(worker, second_session, second["id"])
            statuses = {one.result(timeout=30)["status"], two.result(timeout=30)["status"]}
        assert statuses == {"confirmed", "invalidated"}, statuses
        with engine.begin() as db:
            rejected = create_proposal(db, outsider, outsider_session, "flight_book",
                                       {"flight_id": candidates[0].id})
            expired = create_proposal(db, outsider, outsider_session, "flight_book",
                                      {"flight_id": candidates[1].id})
        with engine.begin() as db:
            assert reject_action(db, outsider, outsider_session,
                                 rejected["id"])["status"] == "rejected"
            assert confirm_action(db, outsider, outsider_session, expired["id"],
                                  now=now + timedelta(minutes=16))["status"] == "expired"
        with engine.connect() as db:
            orders = db.execute(select(flight_orders.c.flight_id).where(
                flight_orders.c.traveler_id == owner,
                flight_orders.c.status == "confirmed")).scalars().all()
            assert sorted(orders) == sorted(candidate.id for candidate in candidates)
            assert db.execute(select(func.count()).select_from(flight_orders).where(
                flight_orders.c.traveler_id == outsider)).scalar_one() == 0
        print("P2.5a PostgreSQL verified: API booking, ownership, restart replay, "
              "two-card concurrency, reject and expiry")
    finally:
        with engine.begin() as db:
            ids = (owner, outsider)
            for table in (action_results, audit_events, pending_actions,
                          flight_orders, chat_sessions, travelers):
                db.execute(delete(table).where(table.c.traveler_id.in_(ids)
                                               if table is not travelers else
                                               table.c.id.in_(ids)))
        with engine.connect() as db:
            assert _counts(db) == baseline, "Verifier did not restore baseline counts"


if __name__ == "__main__":
    main()
