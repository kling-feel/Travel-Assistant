"""Bounded real-model check against disposable travel data only."""

import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from config import settings
from db.travel_models import flight_orders, pending_actions
from services.answer_composer import compose_verified_answer
from services.model_config import get_chat_model
from tests.test_p31e1_transaction_journey import TransactionJourneyTests


REPORT = Path(__file__).resolve().parents[1] / "data/generated/p31e3b-live.json"
TURNS = (
    "查10月25日上海到伦敦的航班",
    "我想预订前面查到的航班",
    "我最近有什么行程安排",
    "机票取消规则和费用是什么",
    "撤回刚才那张待确认卡",
)


class CountedModel:
    def __init__(self):
        self.backend = get_chat_model()
        self.calls = []

    def invoke(self, messages):
        if len(self.calls) >= 20:
            raise RuntimeError("live model call cap reached")
        try:
            reply = self.backend.invoke(messages)
        except Exception as error:
            print("model_error_type=" + type(error).__name__)
            raise
        usage = getattr(reply, "usage_metadata", {}) or {}
        self.calls.append({"input_tokens": usage.get("input_tokens", 0),
                           "output_tokens": usage.get("output_tokens", 0)})
        return reply

    def compose(self, intent, user_text, answer, references):
        return compose_verified_answer(self, intent, user_text, answer, references)


def main():
    if not (settings.AI_API_KEY and settings.AI_MODEL and settings.AI_BASE_URL):
        raise RuntimeError("model configuration is incomplete")
    logging.disable(logging.INFO)
    case = TransactionJourneyTests("runTest")
    case.setUp()
    report = {"turns": [], "calls": 0, "input_tokens": 0, "output_tokens": 0}
    try:
        model = CountedModel()
        client, model_patch, saver_patch = case.fixture.client(model)
        with model_patch, saver_patch, patch("graph_chat.controlled.local_today",
                                            return_value=date(2026, 10, 21)), client:
            auth = case.fixture.auth(client, "demo1", "password1")
            sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
            for index, prompt in enumerate(TURNS, 1):
                response = case.fixture.turn(client, sid, auth, f"e3b-live-{index}", prompt)
                body = response.json()
                report["turns"].append({"prompt": prompt, "status": response.status_code,
                                        "answer": body.get("answer", "")[:800],
                                        "actions": [row.get("action_type") for row in
                                                    body.get("actions", [])]})
                if response.status_code != 200:
                    break
        with case.fixture.engine.connect() as db:
            report["confirmed_flight_orders"] = len(db.execute(select(flight_orders.c.id)).all())
            report["pending_actions"] = len(db.execute(select(pending_actions.c.id)).all())
        report["calls"] = len(model.calls)
        report["input_tokens"] = sum(row["input_tokens"] for row in model.calls)
        report["output_tokens"] = sum(row["output_tokens"] for row in model.calls)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if len(report["turns"]) != len(TURNS) or any(
                row["status"] != 200 for row in report["turns"]):
            raise RuntimeError("isolated live dialogue did not complete")
    finally:
        case.tearDown()


if __name__ == "__main__":
    main()
