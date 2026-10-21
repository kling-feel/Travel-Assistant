"""Isolated PostgreSQL model/search failures must not mutate business orders."""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from db import engine
from db.travel_models import (audit_events, demo_state, flight_orders,
                              hotel_orders, pending_actions, rental_orders)
from main import Server
from services.model_config import ModelUnavailable
from services.recommendations import RecommendationSearchError
from services.weather_search import WeatherSearchError


class FixedModel:
    def invoke(self, _messages):
        return AIMessage(content='{"intent":"general","params":{},"answer":""}')


def counts():
    with engine.connect() as db:
        return {table.name: db.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (flight_orders, hotel_orders, rental_orders,
                              pending_actions, audit_events)}


def main():
    assert settings.POSTGRES_PORT == 5434 and not settings.DATABASE_URL
    with engine.connect() as db:
        assert db.execute(select(demo_state.c.as_of)).scalar_one() == date.today()
    before = counts()
    server = Server()
    server.init_app()
    report = {}
    with TestClient(server.app) as client:
        login = client.post("/api/v1/login", json={
            "username": "demo2", "password": os.getenv("DEMO_TRAVELER_2_PASSWORD")})
        assert login.status_code == 200, login.text
        auth = {"Authorization": "Bearer " + login.json()["access_token"]}
        session = client.post("/api/v1/sessions", headers=auth, json={})
        sid = session.json()["id"]

        def ask(key, prompt):
            return client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                               json={"request_key": "p31e4-fault-" + key,
                                     "content": prompt})

        with patch("api.travel.get_chat_model", side_effect=ModelUnavailable("offline")):
            unavailable = ask("model", "请帮我预订机票")
        assert unavailable.status_code == 503
        report["model_unavailable"] = unavailable.status_code

        with (patch("api.travel.get_chat_model", return_value=FixedModel()),
              patch("services.recommendations.CleverSeeRecommendationProvider.search",
                    side_effect=RecommendationSearchError("timeout"))):
            place = ask("place", "我在上海，推荐经典地点")
        assert place.status_code == 200 and place.json()["actions"] == []
        assert any(row.get("type") == "place_source"
                   for row in place.json()["references"])
        report["place_fallback"] = True

        tomorrow = date.today() + timedelta(days=1)
        with (patch("api.travel.get_chat_model", return_value=FixedModel()),
              patch("services.agent_reads.search_weather",
                    side_effect=WeatherSearchError("timeout"))):
            weather = ask("weather", f"{tomorrow.isoformat()}上海天气怎么样？")
        assert weather.status_code == 200 and weather.json()["actions"] == []
        assert not any(row.get("type") == "weather_source"
                       for row in weather.json()["references"])
        report["weather_degraded"] = True

    after = counts()
    assert after == before, (before, after)
    report["business_counts_unchanged"] = True
    path = ROOT / "data/generated/p31e4-fault-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
