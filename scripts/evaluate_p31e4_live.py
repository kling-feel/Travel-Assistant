"""Bounded real-model journey on the isolated P3.1e4 PostgreSQL database."""

import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from db import engine
from db.travel_models import airports, demo_state, flights
from main import Server
from services.answer_composer import compose_verified_answer
from services.model_config import get_chat_model
from services.recommendations import CleverSeeRecommendationProvider
from services.weather_search import search_weather


REPORT = ROOT / "data/generated/p31e4-live-report.json"
LEDGER = ROOT / "data/generated/p31e4-live-ledger.json"
MODEL_CAP = 40
SEARCH_CAP = 25
MODEL_RMB_CAP = 8.0


def ledger():
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def append(row):
    rows = ledger()
    rows.append(row)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


class CountedModel:
    def __init__(self):
        self.backend = get_chat_model().backend

    def invoke(self, messages):
        rows = ledger()
        models = [row for row in rows if row["kind"] == "model"]
        cost = sum(row.get("upper_rmb", 0) for row in models)
        if len(models) >= MODEL_CAP or cost + 0.2 > MODEL_RMB_CAP:
            raise RuntimeError("P3.1e4 model cap reached")
        row = {"kind": "model", "seconds": 0, "error": None}
        start = perf_counter()
        try:
            reply = self.backend.invoke(messages)
            usage = (getattr(reply, "response_metadata", {}) or {}).get("token_usage", {}) or {}
            fallback = getattr(reply, "usage_metadata", {}) or {}
            incoming = usage.get("prompt_tokens", fallback.get("input_tokens"))
            outgoing = usage.get("completion_tokens", fallback.get("output_tokens"))
            if not isinstance(incoming, int) or not isinstance(outgoing, int):
                raise RuntimeError("Model usage missing; stop paid requests")
            row.update(input_tokens=incoming, output_tokens=outgoing,
                       upper_rmb=round((incoming * 2 + outgoing * 8) / 1_000_000, 6))
            return reply
        except Exception as error:
            row["error"] = type(error).__name__
            raise
        finally:
            row["seconds"] = round(perf_counter() - start, 3)
            append(row)

    def compose(self, intent, user_text, answer, references):
        return compose_verified_answer(self, intent, user_text, answer, references)


def counted_search(method, label):
    def wrapper(*args, **kwargs):
        rows = ledger()
        if sum(row["kind"] == "search" for row in rows) >= SEARCH_CAP:
            raise RuntimeError("P3.1e4 search cap reached")
        row = {"kind": "search", "path": label, "seconds": 0, "error": None}
        start = perf_counter()
        try:
            return method(*args, **kwargs)
        except Exception as error:
            row["error"] = type(error).__name__
            raise
        finally:
            row["seconds"] = round(perf_counter() - start, 3)
            append(row)
    return wrapper


def app():
    server = Server()
    server.init_app()
    return server.app


def main():
    assert settings.POSTGRES_PORT == 5434 and not settings.DATABASE_URL
    assert settings.AI_API_KEY and settings.ALIYUN_IQS_API_KEY
    assert settings.ALIYUN_IQS_ENABLED
    with engine.connect() as db:
        assert db.execute(select(demo_state.c.as_of)).scalar_one() == date.today()
    logging.disable(logging.INFO)
    report = {"date": date.today().isoformat(), "turns": []}
    model = CountedModel()
    original_search = CleverSeeRecommendationProvider.search
    with (patch("api.travel.get_chat_model", return_value=model),
          patch.object(CleverSeeRecommendationProvider, "search",
                       counted_search(original_search, "place")),
          patch("services.agent_reads.search_weather",
                side_effect=counted_search(search_weather, "weather")),
          TestClient(app()) as client):
        password = os.getenv("DEMO_TRAVELER_2_PASSWORD")
        login = client.post("/api/v1/login", json={"username": "demo2",
                                                   "password": password})
        assert login.status_code == 200, login.text
        auth = {"Authorization": "Bearer " + login.json()["access_token"]}
        created = client.post("/api/v1/sessions", headers=auth, json={})
        assert created.status_code == 201, created.text
        sid = created.json()["id"]

        def turn(label, content):
            response = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                                   json={"request_key": "p31e4-live-" + label,
                                         "content": content})
            body = response.json()
            entry = {"label": label, "status": response.status_code,
                     "answer": body.get("answer", "")[:550],
                     "actions": [row["action_type"] for row in body.get("actions", [])],
                     "references": [row.get("type") for row in body.get("references", [])]}
            report["turns"].append(entry)
            print(json.dumps(entry, ensure_ascii=False))
            assert response.status_code == 200, response.text
            return body

        try:
            turn("itinerary", "我最近有什么行程安排吗")
            turn("flight-empty", "我明天要从上海到伦敦，帮我查航班")
            earliest = turn("earliest", "那最早什么时候能走")
            assert earliest["references"] and "flight" in report["turns"][-1]["references"]
            booked = turn("flight-book", "就预订最早那班，顺便找落地附近酒店")
            assert [row["action_type"] for row in booked["actions"]] == ["flight_book"]
            flight_id = booked["actions"][0]["parameters"]["flight_id"]
            with engine.connect() as db:
                flight = db.execute(select(flights.c.arrival_at, airports.c.timezone).join(
                    airports, flights.c.arrival_airport == airports.c.code).where(
                    flights.c.id == flight_id)).one()
            arrival = flight.arrival_at.astimezone(ZoneInfo(flight.timezone)).date()
            turn("hotel-search", f"伦敦落地当天 {arrival.isoformat()} 入住，住两晚，找附近酒店")
            hotel = turn("hotel-book", "预订刚才最贵的酒店房间，住两晚")
            assert [row["action_type"] for row in hotel["actions"]] == ["hotel_book"]
            turn("rental-search", "我在上海租车，明天10点取，大后天18点还")
            rental = turn("rental-book", "我要租最贵的那辆车")
            assert [row["action_type"] for row in rental["actions"]] == ["rental_book"]
            turn("places", "我在上海附近有什么经典地点？")
            turn("weather", "明天上海天气怎么样？")
            policy = turn("policy", "机票取消要收多少费用？")
            assert policy["actions"] == [] and "policy" in report["turns"][-1]["references"]
        finally:
            rows = ledger()
            report["usage"] = {"model_attempts": sum(row["kind"] == "model" for row in rows),
                               "search_attempts": sum(row["kind"] == "search" for row in rows),
                               "input_tokens": sum(row.get("input_tokens", 0) for row in rows),
                               "output_tokens": sum(row.get("output_tokens", 0) for row in rows),
                               "model_upper_rmb": round(sum(row.get("upper_rmb", 0) for row in rows), 6)}
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report["usage"], ensure_ascii=False))


if __name__ == "__main__":
    main()
