"""Verify P2.1 sessions against PostgreSQL without keeping test conversations."""

from pathlib import Path
import os
import sys

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from travel_assistant.db.travel_models import chat_messages, chat_sessions, flight_orders, hotel_orders
from main import Server


def app():
    server = Server()
    server.init_app()
    paths = {route.path for route in server.app.routes}
    assert "/api/graph/" not in paths
    return server.app


def auth(client, number):
    response = client.post("/api/v1/login", json={
        "username": f"demo{number}",
        "password": os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD", ""),
    })
    assert response.status_code == 200, response.status_code
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def order_counts():
    with engine.connect() as connection:
        return (
            connection.execute(select(func.count()).select_from(flight_orders)).scalar_one(),
            connection.execute(select(func.count()).select_from(hotel_orders)).scalar_one(),
        )


def main():
    before = order_counts()
    created_ids = []
    try:
        with TestClient(app()) as client:
            one, two = auth(client, 1), auth(client, 2)
            assert client.post("/api/v1/sessions", headers=one,
                               json={"traveler_id": "demo_traveler_2"}).status_code == 422
            first = client.post("/api/v1/sessions", headers=one, json={})
            second = client.post("/api/v1/sessions", headers=two, json={})
            for response in (first, second):
                if response.status_code == 201:
                    created_ids.append(response.json()["id"])
            assert first.status_code == second.status_code == 201
            first_id, second_id = first.json()["id"], second.json()["id"]
            assert [item["id"] for item in client.get(
                "/api/v1/sessions", headers=one).json()] == [first_id]
            assert client.get(f"/api/v1/sessions/{first_id}", headers=two).status_code == 404
            assert client.post(f"/api/v1/sessions/{first_id}/messages", headers=two,
                               json={"content": "forbidden"}).status_code == 404
            for content in ("P2.1 first", "P2.1 second"):
                assert client.post(f"/api/v1/sessions/{first_id}/messages",
                                   headers=one, json={"content": content}).status_code == 201
        with TestClient(app()) as restarted:
            one = auth(restarted, 1)
            history = restarted.get(f"/api/v1/sessions/{first_id}", headers=one)
            assert history.status_code == 200
            assert [(message["sequence"], message["role"], message["content"])
                    for message in history.json()["messages"]] == [
                        (1, "user", "P2.1 first"), (2, "user", "P2.1 second")]
            assert restarted.get(f"/api/v1/sessions/{second_id}",
                                 headers=one).status_code == 404
        assert order_counts() == before
        print("P2.1 PostgreSQL sessions verified: ownership, order, app rebuild, unchanged orders")
    finally:
        if created_ids:
            with engine.begin() as connection:
                connection.execute(delete(chat_messages).where(
                    chat_messages.c.session_id.in_(created_ids)))
                connection.execute(delete(chat_sessions).where(
                    chat_sessions.c.id.in_(created_ids)))


if __name__ == "__main__":
    main()
