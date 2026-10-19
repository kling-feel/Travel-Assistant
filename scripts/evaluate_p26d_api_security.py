"""Exercise current traveler API boundaries on the isolated P2.6d database."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import delete, func, insert, select

from config import settings
from db import engine
from db.travel_models import chat_sessions, travelers
from main import Server
from scripts.p26d_eval_guard import assert_eval_database
from utils.password_hash import get_hashed_password


REPORT = Path(__file__).resolve().parents[1] / "data/generated/p26d-api-security.json"


def main():
    assert_eval_database()
    logging.disable(logging.INFO)
    marker = uuid4().hex[:12]
    users = ["p26d_sec_a_" + marker, "p26d_sec_b_" + marker]
    password = uuid4().hex
    checks = {}
    with engine.connect() as db:
        before = db.execute(select(func.count()).select_from(chat_sessions)).scalar_one()
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=user, username=user, password_hash=get_hashed_password(password),
                display_name="P2.6d security test", active=1)
                for user in users])
        server = Server()
        server.init_app()
        routes = sorted({route.path for route in server.app.routes})
        checks["old_routes_absent"] = not any(
            path.startswith(("/api/graph", "/api/system")) for path in routes)
        with TestClient(server.app) as client:
            def auth(user):
                response = client.post("/api/v1/login", json={
                    "username": user, "password": password})
                assert response.status_code == 200
                return {"Authorization": "Bearer " + response.json()["access_token"]}

            a, b = auth(users[0]), auth(users[1])
            session_response = client.post("/api/v1/sessions", headers=a, json={})
            assert session_response.status_code == 201
            sid = session_response.json()["id"]
            checks["unauthenticated_rejected"] = all(
                client.get(path).status_code in (401, 403)
                for path in ("/api/v1/me", "/api/v1/me/itinerary",
                             "/api/v1/sessions", f"/api/v1/sessions/{sid}"))
            checks["foreign_session_hidden"] = all(
                client.get(path, headers=b).status_code == 404
                for path in (f"/api/v1/sessions/{sid}",
                             f"/api/v1/sessions/{sid}/actions",
                             f"/api/v1/sessions/{sid}/actions/{uuid4()}"))
            checks["foreign_session_write_rejected"] = all(
                client.post(path, headers=b, json=body).status_code == 404
                for path, body in (
                    (f"/api/v1/sessions/{sid}/messages", {"content": "hello"}),
                    (f"/api/v1/sessions/{sid}/turns",
                     {"content": "hello", "request_key": "foreign-123"}),
                    (f"/api/v1/sessions/{sid}/actions/{uuid4()}/confirm", {})))
            checks["mass_assignment_rejected"] = all(
                client.post(path, headers=a, json=body).status_code == 422
                for path, body in (
                    ("/api/v1/sessions", {"traveler_id": users[1]}),
                    (f"/api/v1/sessions/{sid}/messages",
                     {"content": "hello", "role": "assistant"}),
                    (f"/api/v1/sessions/{sid}/turns",
                     {"content": "hello", "request_key": "spoof-123",
                      "traveler_id": users[1]}),
                    (f"/api/v1/sessions/{sid}/actions/{uuid4()}/confirm",
                     {"amount": 0, "traveler_id": users[1]})))
            future = datetime.now(timezone.utc) + timedelta(hours=1)
            past = datetime.now(timezone.utc) - timedelta(seconds=1)
            claims = {"sub": users[0], "type": "traveler_access", "exp": future}
            bad_tokens = [
                jwt.encode({**claims, "exp": past}, settings.JWT_SECRET_KEY,
                           algorithm=settings.ALGORITHM),
                jwt.encode(claims, "wrong-signing-key", algorithm=settings.ALGORITHM),
                jwt.encode({**claims, "type": "admin"}, settings.JWT_SECRET_KEY,
                           algorithm=settings.ALGORITHM),
            ]
            checks["jwt_invalid_rejected"] = all(
                client.get("/api/v1/me", headers={
                    "Authorization": "Bearer " + token}).status_code == 401
                for token in bad_tokens)
            checks["cors_origin_rejected"] = "access-control-allow-origin" not in (
                client.options("/api/v1/me", headers={
                    "Origin": "https://untrusted.example",
                    "Access-Control-Request-Method": "GET"}).headers)
            checks["docs_public"] = client.get("/docs").status_code == 200
            checks["foreign_host_accepted"] = client.get(
                "/health", headers={"Host": "untrusted.example"}).status_code == 200
            response = client.get(f"/api/v1/sessions/{sid}", headers=b)
            checks["no_key_in_error"] = settings.AI_API_KEY not in response.text
        with engine.connect() as db:
            checks["only_expected_session_created"] = (
                db.execute(select(func.count()).select_from(chat_sessions)).scalar_one()
                == before + 1)
    finally:
        with engine.begin() as db:
            db.execute(delete(chat_sessions).where(
                chat_sessions.c.traveler_id.in_(users)))
            db.execute(delete(travelers).where(travelers.c.id.in_(users)))
    REPORT.write_text(json.dumps({"checks": checks, "routes": routes},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print("API security checks:", checks)
    if not all(value for key, value in checks.items()
               if key not in ("docs_public", "foreign_host_accepted")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
