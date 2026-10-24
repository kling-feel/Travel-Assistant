"""Session ownership, migration order and durable API history."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, insert, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.travel import router
from travel_assistant.core.config import settings
from travel_assistant.db.travel_models import chat_messages, chat_sessions, travelers
from migrations import v001_travel, v002_sessions
from services.sessions import append_message, get_session_history
from travel_assistant.core.dependencies import get_db
from travel_assistant.core.password_hash import get_hashed_password


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
                                    connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        with self.engine.begin() as connection:
            v001_travel.upgrade(connection)
            v002_sessions.upgrade(connection)
            connection.execute(insert(travelers), [
                {"id": "t1", "username": "demo1", "password_hash": get_hashed_password("password1"),
                 "display_name": "One", "active": 1},
                {"id": "t2", "username": "demo2", "password_hash": get_hashed_password("password2"),
                 "display_name": "Two", "active": 1},
            ])
        self.original_secret = settings.JWT_SECRET_KEY
        settings.JWT_SECRET_KEY = "test-only-secret-which-is-long-enough"

    def tearDown(self):
        settings.JWT_SECRET_KEY = self.original_secret
        self.engine.dispose()

    def app(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        return app

    @staticmethod
    def auth(client, username, password):
        response = client.post("/api/v1/login", json={
            "username": username, "password": password,
        })
        assert response.status_code == 200
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def test_migrations_have_separate_versions_and_are_repeatable(self):
        fresh = create_engine("sqlite+pysqlite:///:memory:")
        with fresh.begin() as connection:
            v001_travel.upgrade(connection)
            self.assertNotIn("chat_sessions", inspect(connection).get_table_names())
            v002_sessions.upgrade(connection)
            v002_sessions.upgrade(connection)
            self.assertIn("chat_sessions", inspect(connection).get_table_names())
            self.assertIn("chat_messages", inspect(connection).get_table_names())
        fresh.dispose()

    def test_accounts_are_isolated_and_history_survives_app_rebuild(self):
        with TestClient(self.app()) as client:
            one = self.auth(client, "demo1", "password1")
            two = self.auth(client, "demo2", "password2")
            self.assertEqual(client.post("/api/v1/sessions", headers=one,
                                         json={"traveler_id": "t2"}).status_code, 422)
            first = client.post("/api/v1/sessions", headers=one, json={})
            second = client.post("/api/v1/sessions", headers=two, json={})
            self.assertEqual((first.status_code, second.status_code), (201, 201))
            first_id, second_id = first.json()["id"], second.json()["id"]
            self.assertNotEqual(first_id, second_id)
            self.assertEqual([row["id"] for row in client.get(
                "/api/v1/sessions", headers=one).json()], [first_id])
            self.assertEqual(client.get(f"/api/v1/sessions/{first_id}",
                                        headers=two).status_code, 404)
            self.assertEqual(client.post(f"/api/v1/sessions/{first_id}/messages",
                                         headers=two, json={"content": "intrusion"}).status_code,
                             404)
            self.assertEqual(client.get("/api/v1/sessions").status_code, 401)
            self.assertEqual(client.post(f"/api/v1/sessions/{first_id}/messages",
                                         headers=one,
                                         json={"content": "Hello", "role": "assistant"}).status_code,
                             422)
            self.assertEqual(client.post(f"/api/v1/sessions/{first_id}/messages",
                                         headers=one, json={"content": "  "}).status_code,
                             422)
            for content in ("First", "Second"):
                self.assertEqual(client.post(f"/api/v1/sessions/{first_id}/messages",
                                             headers=one,
                                             json={"content": content}).status_code, 201)

        # A new app and DB session sees the same committed history.
        with TestClient(self.app()) as restarted:
            one = self.auth(restarted, "demo1", "password1")
            history = restarted.get(f"/api/v1/sessions/{first_id}", headers=one)
            self.assertEqual(history.status_code, 200)
            self.assertEqual([(row["sequence"], row["role"], row["content"])
                              for row in history.json()["messages"]],
                             [(1, "user", "First"), (2, "user", "Second")])
            self.assertEqual(restarted.get(f"/api/v1/sessions/{second_id}",
                                           headers=one).status_code, 404)

    def test_message_write_is_transactional(self):
        with TestClient(self.app()) as client:
            one = self.auth(client, "demo1", "password1")
            session_id = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
        with Session(self.engine) as db:
            append_message(db, "t1", session_id, "assistant", "Internal reply")
            db.rollback()
            self.assertEqual(get_session_history(db, "t1", session_id)["messages"], [])
            self.assertEqual(db.execute(select(chat_messages)).all(), [])
            self.assertEqual(db.execute(select(chat_sessions.c.id)).scalar_one(), session_id)


if __name__ == "__main__":
    unittest.main()
