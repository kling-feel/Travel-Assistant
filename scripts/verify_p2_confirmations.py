"""Exercise P2.3 on PostgreSQL with an isolated temporary traveler."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import sys

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import engine
from db.travel_models import (action_results, audit_events, chat_sessions, demo_state,
                              flight_offers, flight_orders, flights, hotel_orders,
                              hotel_products, pending_actions, travelers)
from main import Server
from services.actions import create_proposal
from services.confirmations import confirm_action, reject_action
from utils.password_hash import get_hashed_password


def app():
    server = Server()
    server.init_app()
    assert "/api/graph/" not in {route.path for route in server.app.routes}
    return server.app


def main():
    traveler_id = "p23_" + uuid4().hex[:24]
    username = traveler_id
    password = uuid4().hex
    session_id = str(uuid4())
    flight_order_ids = [str(uuid4()), str(uuid4())]
    hotel_order_ids = [str(uuid4()), str(uuid4())]
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        demo_day = db.execute(select(demo_state.c.as_of).where(
            demo_state.c.id == 1)).scalar_one_or_none()
        check_in = max(now.date() + timedelta(days=1),
                       (demo_day + timedelta(days=1)) if demo_day else now.date())
        check_out = check_in + timedelta(days=1)
        flight = db.execute(select(flights).where(
            flights.c.status == "Scheduled", flights.c.departure_at > now + timedelta(hours=2)
        ).order_by(flights.c.departure_at)).mappings().first()
        assert flight is not None, "Demo flights expired; reimport and reseed for today"
        candidate = db.execute(select(flights.c.id, flight_offers.c.amount)
                               .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
                               .where(flights.c.id != flight["id"],
                                      flights.c.departure_airport == flight["departure_airport"],
                                      flights.c.arrival_airport == flight["arrival_airport"],
                                      flights.c.status == "Scheduled", flights.c.departure_at > now)
                               .order_by(flights.c.departure_at)).first()
        assert candidate is not None, "No future candidate with a demo fare"
        other = db.execute(select(flights.c.id).where(
            flights.c.id.not_in((flight["id"], candidate.id)),
            flights.c.status == "Scheduled", flights.c.departure_at > now,
        ).order_by(flights.c.departure_at).limit(1)).scalar_one_or_none()
        assert other is not None, "No second future flight for cancellation check"
        product = db.execute(select(hotel_products).where(
            hotel_products.c.available_from <= check_in,
            hotel_products.c.available_until >= check_in + timedelta(days=3)
        )).mappings().first()
        assert product is not None, "Demo hotel dates expired; reimport and reseed for today"
    try:
        with engine.begin() as db:
            db.execute(insert(travelers).values(
                id=traveler_id, username=username,
                password_hash=get_hashed_password(password),
                display_name="P2.3 verifier", active=1,
            ))
            db.execute(insert(chat_sessions).values(
                id=session_id, traveler_id=traveler_id, status="active"))
            for order_id, flight_id in zip(flight_order_ids, (flight["id"], other)):
                db.execute(insert(flight_orders).values(
                    id=order_id, traveler_id=traveler_id, flight_id=flight_id,
                    amount=6000, status="confirmed", version=1,
                ))
            for order_id in hotel_order_ids:
                db.execute(insert(hotel_orders).values(
                    id=order_id, traveler_id=traveler_id, product_id=product["id"],
                    check_in=check_in, check_out=check_out,
                    total_price=product["nightly_price"], status="confirmed", version=1,
                ))
        with engine.begin() as db:
            cases = [
                ("flight_change", {"order_id": flight_order_ids[0], "expected_version": 1,
                                   "new_flight_id": candidate.id}),
                ("flight_cancel", {"order_id": flight_order_ids[1], "expected_version": 1}),
                ("hotel_book", {"product_id": product["id"],
                                "check_in": check_in.isoformat(), "check_out": check_out.isoformat()}),
                ("hotel_change", {"order_id": hotel_order_ids[0], "expected_version": 1,
                                  "product_id": product["id"], "check_in": check_in.isoformat(),
                                  "check_out": (check_out + timedelta(days=1)).isoformat()}),
                ("hotel_cancel", {"order_id": hotel_order_ids[1], "expected_version": 1}),
            ]
            actions = [create_proposal(db, traveler_id, session_id, kind, params)
                       for kind, params in cases]
        with TestClient(app()) as client:
            login = client.post("/api/v1/login", json={"username": username,
                                                        "password": password})
            assert login.status_code == 200
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            outcomes = []
            for action in actions:
                response = client.post(f"/api/v1/sessions/{session_id}/actions/{action['id']}/confirm",
                                       headers=headers)
                assert response.status_code == 200, (response.status_code, response.text)
                outcomes.append(response.json())
            assert outcomes[0]["order"]["amount"] == candidate.amount
            assert outcomes[0]["price"]["difference"] == candidate.amount - 6000
            assert outcomes[1]["price"]["cancellation_fee"] == 1800
            assert outcomes[1]["price"]["refundable"] == 4200
            assert outcomes[4]["order"]["status"] == "cancelled"
            assert outcomes[4]["price"]["refundable"] == product["nightly_price"]
            assert client.get("/api/v1/me/action-history", headers=headers).status_code == 200
        with TestClient(app()) as restarted:
            login = restarted.post("/api/v1/login", json={"username": username,
                                                           "password": password})
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            retry = restarted.post(
                f"/api/v1/sessions/{session_id}/actions/{actions[2]['id']}/confirm", headers=headers)
            assert retry.status_code == 200 and retry.json() == outcomes[2]
        with engine.begin() as db:
            concurrent = create_proposal(db, traveler_id, session_id, "hotel_book", {
                "product_id": product["id"], "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
            })
        barrier = Barrier(2)

        def worker():
            with Session(engine) as db:
                barrier.wait()
                result = confirm_action(db, traveler_id, session_id, concurrent["id"])
                db.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            both = [future.result(timeout=20) for future in futures]
        assert both[0] == both[1] and both[0]["status"] == "confirmed"
        with engine.begin() as db:
            stale = create_proposal(db, traveler_id, session_id, "flight_cancel", {
                "order_id": flight_order_ids[0], "expected_version": 2})
            refused = create_proposal(db, traveler_id, session_id, "hotel_book", {
                "product_id": product["id"], "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat()})
            expired = create_proposal(db, traveler_id, session_id, "hotel_book", {
                "product_id": product["id"], "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat()})
            db.execute(update(flight_orders).where(
                flight_orders.c.id == flight_order_ids[0]).values(version=3))
            db.execute(update(pending_actions).where(
                pending_actions.c.id == expired["id"]).values(
                    created_at=now - timedelta(minutes=20),
                    expires_at=now - timedelta(minutes=5)))
        with engine.begin() as db:
            assert confirm_action(db, traveler_id, session_id, stale["id"])["status"] == "invalidated"
            assert reject_action(db, traveler_id, session_id, refused["id"])["status"] == "rejected"
            assert confirm_action(db, traveler_id, session_id, expired["id"])["status"] == "expired"
        with engine.connect() as db:
            assert db.execute(select(func.count()).select_from(action_results).where(
                action_results.c.traveler_id == traveler_id)).scalar_one() == 6
            assert db.execute(select(func.count()).select_from(hotel_orders).where(
                hotel_orders.c.traveler_id == traveler_id)).scalar_one() == 4
        print("P2.3 PostgreSQL verified: five writes, retry, concurrency, stale/reject/expiry")
    finally:
        with engine.begin() as db:
            db.execute(delete(action_results).where(action_results.c.traveler_id == traveler_id))
            db.execute(delete(audit_events).where(audit_events.c.traveler_id == traveler_id))
            db.execute(delete(pending_actions).where(pending_actions.c.traveler_id == traveler_id))
            db.execute(delete(hotel_orders).where(hotel_orders.c.traveler_id == traveler_id))
            db.execute(delete(flight_orders).where(flight_orders.c.traveler_id == traveler_id))
            db.execute(delete(chat_sessions).where(chat_sessions.c.traveler_id == traveler_id))
            db.execute(delete(travelers).where(travelers.c.id == traveler_id))


if __name__ == "__main__":
    main()
