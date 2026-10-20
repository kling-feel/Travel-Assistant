"""Disposable browser preview with fictional SQLite data and a fixed model.

Run only for local page checks. It binds to loopback and never uses paid APIs.
"""

import json
import os
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config import settings
from db.travel_models import (airports, demo_state, flight_offers, flight_orders,
                              flights, hotel_products, hotels, metadata,
                              policy_documents, rental_products, travelers)
from main import Server
from services.model_config import ModelUnavailable
from services.policy_catalog import CURRENT_POLICIES
from utils.dependencies import get_db
from utils.password_hash import get_hashed_password


class FixedModel:
    def __init__(self, tomorrow):
        self.tomorrow = tomorrow

    def invoke(self, messages):
        day = self.tomorrow.isoformat()
        next_day = (self.tomorrow + timedelta(days=1)).isoformat()
        decision = {"intent": "transaction", "params": {
            "queries": [
                {"intent": "flight_search", "params": {
                    "departure": "HAM", "arrival": "BSL", "travel_date": day}},
                {"intent": "hotel_search", "params": {
                    "city": "Basel", "check_in": day, "check_out": next_day}},
            ],
            "actions": [
                {"action_type": "flight_change", "parameters": {"new_flight_id": 2}},
                {"action_type": "hotel_book", "parameters": {
                    "product_id": "room-1", "check_in": day, "check_out": next_day}},
            ],
        }, "answer": "请逐笔确认"}
        return AIMessage(content=json.dumps(decision, ensure_ascii=False))


def preview_app():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    today = date.today()
    tomorrow = today + timedelta(days=1)
    departure = datetime.combine(tomorrow, time(12), timezone.utc)
    with engine.begin() as connection:
        connection.execute(insert(travelers), [
            {"id": "preview-1", "username": "demo1",
             "password_hash": get_hashed_password("password1"),
             "display_name": "旅客一", "active": 1},
            {"id": "preview-2", "username": "demo2",
             "password_hash": get_hashed_password("password2"),
             "display_name": "旅客二", "active": 1},
        ])
        connection.execute(insert(demo_state).values(
            id=1, as_of=today, fixture_sha256="0" * 64))
        connection.execute(insert(airports), [
            {"code": "HAM", "name": "Fictional Hamburg", "city": "Hamburg",
             "timezone": "Europe/Berlin"},
            {"code": "BSL", "name": "Fictional Basel", "city": "Basel",
             "timezone": "Europe/Zurich"},
        ])
        connection.execute(insert(flights), [
            {"id": number, "flight_no": f"SIM-{number}",
             "departure_airport": "HAM", "arrival_airport": "BSL",
             "departure_at": departure + timedelta(hours=number),
             "arrival_at": departure + timedelta(hours=number + 2),
            "status": "Scheduled"} for number in (1, 2)
        ])
        connection.execute(insert(flight_offers), [
            {"flight_id": 1, "amount": 100, "version": 1, "rule_id": "network-fare-v1"},
            {"flight_id": 2, "amount": 130, "version": 1, "rule_id": "network-fare-v1"},
        ])
        connection.execute(insert(flight_orders).values(
            id="preview-flight-1", traveler_id="preview-1", flight_id=1,
            amount=100, status="confirmed", version=1))
        connection.execute(insert(hotels).values(
            id=1, name="虚构·Basel机场周边酒店 1", city="Basel", price_tier="Budget",
            nearby_airport="BSL"))
        connection.execute(insert(hotel_products).values(
            id="room-1", hotel_id=1, room_type="演示标准间", nightly_price=80,
            available_from=tomorrow, available_until=tomorrow + timedelta(days=30)))
        connection.execute(insert(rental_products).values(
            id="car-1", name="虚构·BSL机场租车 1", airport_code="BSL", city="Basel",
            vehicle_type="Compact", daily_price=50, latitude=47.59,
            longitude=7.61, available_from=tomorrow,
            available_until=tomorrow + timedelta(days=30), generation_rule="preview-v1"))
        connection.execute(insert(policy_documents), list(CURRENT_POLICIES.values()))

    server = Server()
    server.init_app()
    app: FastAPI = server.app

    def test_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    settings.JWT_SECRET_KEY = "preview-only-fixed-secret-never-use-in-production"
    saver = InMemorySaver()
    model_patch = patch("api.travel.get_chat_model", return_value=FixedModel(tomorrow))
    saver_patch = patch("api.travel.open_checkpointer", return_value=nullcontext(saver))
    return app, engine, model_patch, saver_patch, tomorrow


if __name__ == "__main__":
    import uvicorn

    preview, _, model, saver, _ = preview_app()
    if os.getenv("P31_NO_MODEL") == "1":
        model = patch("api.travel.get_chat_model",
                      side_effect=ModelUnavailable("preview model disabled"))
    with model, saver:
        uvicorn.run(preview, host="127.0.0.1", port=8001)
