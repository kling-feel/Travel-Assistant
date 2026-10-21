"""Bounded real-model read-only check using disposable traveler data."""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import insert, select

from config import settings
from db.travel_models import flight_offers, flights, pending_actions
from scripts.evaluate_p31e3b_dialogue import CountedModel
from services.recommendations import ReviewedCatalogProvider
from tests.test_p31e1_transaction_journey import TransactionJourneyTests


REPORT = Path(__file__).resolve().parents[1] / "data/generated/p31e3c-live.json"
TURNS = (
    "我在上海，推荐一些经典地点",
    "我更喜欢文化相关的，豫园适合吗？",
    "我想看看伦敦的经典地点",
)


def main():
    if not (settings.AI_API_KEY and settings.AI_MODEL and settings.AI_BASE_URL):
        raise RuntimeError("model configuration is incomplete")
    logging.disable(logging.INFO)
    case = TransactionJourneyTests("runTest")
    case.setUp()
    report = {"turns": [], "calls": 0, "input_tokens": 0,
              "output_tokens": 0, "pending_actions": 0}
    try:
        with case.fixture.engine.begin() as db:
            db.execute(insert(flights).values(
                id=3832, flight_no="SIM-3832", departure_airport="HAM",
                arrival_airport="SHA", status="Scheduled",
                departure_at=datetime(2026, 10, 25, 1, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 25, 13, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=3832, amount=6585, version=1,
                rule_id="network-fare-v1"))
        model = CountedModel()
        client, model_patch, saver_patch = case.fixture.client(model)
        with (model_patch, saver_patch,
              patch("graph_chat.controlled.local_today", return_value=date(2026, 10, 21)),
              patch("api.travel.get_recommendation_provider",
                    return_value=ReviewedCatalogProvider()), client):
            auth = case.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            for index, prompt in enumerate(TURNS, 1):
                response = case.fixture.turn(client, sid, auth,
                                             f"e3c-live-{index}", prompt)
                body = response.json()
                report["turns"].append({
                    "prompt": prompt, "status": response.status_code,
                    "answer": body.get("answer", "")[:900],
                    "references": [{key: ref.get(key) for key in
                                    ("id", "display_name", "place")}
                                   for ref in body.get("references", [])],
                    "actions": len(body.get("actions", [])),
                })
                if response.status_code != 200:
                    break
        with case.fixture.engine.connect() as db:
            report["pending_actions"] = len(db.execute(select(pending_actions.c.id)).all())
        report["calls"] = len(model.calls)
        report["input_tokens"] = sum(row["input_tokens"] for row in model.calls)
        report["output_tokens"] = sum(row["output_tokens"] for row in model.calls)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if (len(report["turns"]) != len(TURNS)
                or any(row["status"] != 200 or row["actions"] for row in report["turns"])
                or report["pending_actions"]):
            raise RuntimeError("isolated recommendation dialogue did not complete")
    finally:
        case.tearDown()


if __name__ == "__main__":
    main()
