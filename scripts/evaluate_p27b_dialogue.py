"""Bounded live-source API check using isolated memory data and a fixed router."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from config import settings
from db.travel_models import airports, flight_offers, flights, pending_actions
from scripts.evaluate_p27b_sources import LEDGER, LIMIT, save
from services.recommendations import CleverSeeRecommendationProvider
from tests.test_p26a_dialogue import DialogueTests, ScriptedModel


class CountingProvider:
    def __init__(self):
        self.calls = 0
        self.inner = CleverSeeRecommendationProvider(settings.ALIYUN_IQS_API_KEY.strip())

    def search(self, city, preference, country):
        self.calls += 1
        return self.inner.search(city, preference, country)


def main() -> None:
    if not settings.ALIYUN_IQS_API_KEY.strip():
        raise RuntimeError("local search key is not configured")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    previous = ledger.setdefault("dialogue_samples", [])
    if len(previous) >= 3:
        raise RuntimeError("dialogue samples already complete")
    if any(row["status"] != 200 or row["source_calls"] != 1 for row in previous):
        raise RuntimeError("previous dialogue result needs inspection before resume")
    save(ledger)
    fixture = DialogueTests("runTest")
    fixture.setUp()
    provider = CountingProvider()
    try:
        with fixture.engine.begin() as db:
            db.execute(insert(airports), [
                {"code": "NRT", "name": "Fictional Tokyo", "city": "Tokyo",
                 "timezone": "Asia/Tokyo"},
                {"code": "CDG", "name": "Fictional Paris", "city": "Paris",
                 "timezone": "Europe/Paris"},
            ])
            db.execute(insert(flights), [
                {"id": 2, "flight_no": "SIM-2", "departure_airport": "HAM",
                 "arrival_airport": "NRT", "status": "Scheduled",
                 "departure_at": datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
                 "arrival_at": datetime(2026, 10, 21, 1, tzinfo=timezone.utc)},
                {"id": 3, "flight_no": "SIM-3", "departure_airport": "HAM",
                 "arrival_airport": "CDG", "status": "Scheduled",
                 "departure_at": datetime(2026, 10, 20, 13, tzinfo=timezone.utc),
                 "arrival_at": datetime(2026, 10, 20, 15, tzinfo=timezone.utc)},
            ])
            db.execute(insert(flight_offers), [
                {"flight_id": 2, "amount": 250, "version": 1, "rule_id": "network-fare-v1"},
                {"flight_id": 3, "amount": 170, "version": 1, "rule_id": "network-fare-v1"},
            ])
        decisions = (
            {"intent": "recommendation", "params": {"city": "Tokyo"}, "answer": ""},
            {"intent": "recommendation", "params": {"city": "Paris"}, "answer": ""},
            {"intent": "transaction", "params": {"actions": [
                {"action_type": "hotel_book", "parameters": {"product_id": "room-1"}}]},
             "answer": "景点已经预订"},
        )
        first_index = len(previous)
        model = ScriptedModel(*decisions[first_index:])
        client, model_patch, saver_patch = fixture.client(model)
        with model_patch, saver_patch, patch("api.travel.get_recommendation_provider",
                                             return_value=provider), client:
            one = fixture.auth(client, "demo1", "password1")
            two = fixture.auth(client, "demo2", "password2")
            sid = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
            scenarios = (
                ("tokyo", "推荐 Tokyo 的文化景点"),
                ("paris", "推荐 Paris 的历史景点"),
                ("excursion", "帮我预订 Tokyo 景点门票"),
            )
            for index, (name, prompt) in enumerate(scenarios[first_index:],
                                                   start=first_index):
                if ledger["search_requests"] >= LIMIT:
                    break
                ledger["search_requests"] += 1
                save(ledger)
                before = provider.calls
                response = fixture.turn(client, sid, one, f"p27b-{name}", prompt)
                data = response.json()
                outcome = {"case": name, "status": response.status_code,
                           "source_calls": provider.calls - before,
                           "references": [{"city": ref.get("city"), "place": ref.get("place"),
                                           "url": ref.get("url")}
                                          for ref in data.get("references", [])],
                           "action_count": len(data.get("actions", [])),
                           "unsupported_excursion": "不支持游览预订" in data.get("answer", "")}
                ledger["dialogue_samples"].append(outcome)
                save(ledger)
                print(json.dumps(outcome, ensure_ascii=False))
                if response.status_code != 200 or provider.calls - before != 1:
                    raise RuntimeError("dialogue/source contract failed")
                if name != "excursion" and not outcome["references"]:
                    raise RuntimeError("no live source reached the dialogue")
                if outcome["action_count"] or (name == "excursion" and
                                                not outcome["unsupported_excursion"]):
                    raise RuntimeError("excursion write boundary failed")
                if index == first_index:
                    replay = fixture.turn(client, sid, one, f"p27b-{name}", prompt)
                    if replay.json() != data or provider.calls != before + 1:
                        raise RuntimeError("replay caused a second search")
                    if fixture.turn(client, sid, two,
                                    f"foreign-place-{name}", prompt).status_code != 404:
                        raise RuntimeError("cross-account session access")
            with Session(fixture.engine) as db:
                if db.execute(select(pending_actions.c.id)).first():
                    raise RuntimeError("recommendation generated an action")
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
