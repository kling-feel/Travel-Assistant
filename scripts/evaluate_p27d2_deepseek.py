"""Isolated real-model checks for the complete reviewed place catalog."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from config import settings
from db.travel_models import (airports, flight_offers, flight_orders, flights,
                              pending_actions)
from scripts import p26d_metered_model as meter
from services.model_config import get_chat_model
from services.recommendation_geography import airport_geography
from tests.test_p26a_dialogue import DialogueTests


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data/generated/p27d2-model-attempts.json"
REPORT = ROOT / "data/generated/p27d2-model-report.json"
meter.LEDGER = ROOT / "data/generated/p27d2-model-usage.json"
meter.MAX_REQUESTS = 30
meter.STOP_RMB = 7.5
meter.MAX_RMB = 8

# First turn is scripted to establish context; only the second uses DeepSeek.
SCENARIOS = (
    ("adelaide_nature", "Adelaide", "再推荐阿德莱德的自然景点", "recommend"),
    ("arusha_culture", "Arusha", "再推荐阿鲁沙的文化地点", "recommend"),
    ("atlanta_family", "Atlanta", "再推荐亚特兰大的亲子地点", "recommend"),
    ("barcelona_architecture", "Barcelona", "再推荐巴塞罗那的建筑景点", "recommend"),
    ("bogota_culture", "Bogotá", "再推荐波哥大的文化景点", "recommend"),
    ("boston_nature", "Boston", "再推荐波士顿的自然景点", "recommend"),
    ("brisbane_culture", "Brisbane", "再推荐布里斯班的文化景点", "recommend"),
    ("cancun_history", "Cancún", "再推荐坎昆的历史地点", "recommend"),
    ("caracas_culture", "Caracas", "再推荐加拉加斯的文化地点", "recommend"),
    ("cebu_history", "Cebu", "再推荐宿务的历史景点", "recommend"),
    ("dakar_history", "Dakar", "再推荐达喀尔的历史景点", "recommend"),
    ("doha_culture", "Doha", "再推荐多哈的文化景点", "recommend"),
    ("honolulu_history", "Honolulu", "再推荐檀香山的历史景点", "recommend"),
    ("johannesburg_history", "Johannesburg", "再推荐约翰内斯堡的历史景点", "recommend"),
    ("kunming_nature", "Kunming", "再推荐昆明的自然景点", "recommend"),
    ("lima_history", "Lima", "再推荐利马的历史景点", "recommend"),
    ("lisbon_family", "Lisbon", "再推荐里斯本的亲子地点", "recommend"),
    ("nagoya_history", "Nagoya", "再推荐名古屋的历史景点", "recommend"),
    ("newark_excursion", "Newark", "帮我买这个纽瓦克景点门票", "excursion"),
    ("papeete_price", "Papeete", "帕皮提这些地点门票多少钱？", "price"),
    ("phuket_change", "Phuket", "把这个普吉景点改期", "excursion"),
    ("siem_reap_history", "Siem Reap", "再推荐暹粒的历史景点", "recommend"),
    ("victoria_distance", "Victoria", "塞舌尔维多利亚景点离酒店几公里？", "distance"),
    ("washington_policy", "Washington, D.C.", "虚构平台机票取消政策是什么？", "policy"),
    ("yangon_flight", "Yangon", "明天去仰光的机票能预订吗？", "flight"),
)


def save(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(path)


class HybridModel:
    def __init__(self):
        self.first = None
        self.real = meter.BudgetedModel(get_chat_model().bind(max_tokens=512), limit=30)

    def invoke(self, messages):
        if self.first is not None:
            decision, self.first = self.first, None
            return AIMessage(content=json.dumps(decision, ensure_ascii=False))
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        if ledger["model_attempts"] >= 30:
            raise RuntimeError("P2.7d2 DeepSeek request cap reached")
        ledger["model_attempts"] += 1
        save(LEDGER, ledger)  # Reserve before the external request.
        return self.real.invoke(messages)


def main() -> None:
    if settings.AI_PROVIDER != "deepseek" or not settings.AI_API_KEY:
        raise RuntimeError("DeepSeek is not configured")
    if settings.ALIYUN_IQS_ENABLED:
        raise RuntimeError("live search must remain disabled")
    resume_network = sys.argv[1:] == ["--resume-network"]
    if resume_network:
        failed = meter.read_ledger()
        attempts = json.loads(LEDGER.read_text(encoding="utf-8"))
        if (attempts["model_attempts"] != 1 or len(failed) != 1
                or failed[0].get("error") != "APIConnectionError"
                or failed[0].get("usage_complete")
                or not REPORT.exists()):
            raise RuntimeError("unexpected preflight state; do not resume")
        save(ROOT / "data/generated/p27d2-model-preflight.json",
             json.loads(REPORT.read_text(encoding="utf-8")))
        meter.LEDGER = ROOT / "data/generated/p27d2-model-usage-retry.json"
        meter.MAX_REQUESTS = 29
        if meter.LEDGER.exists():
            raise RuntimeError("P2.7d2 retry already started")
    else:
        if LEDGER.exists() or REPORT.exists() or meter.LEDGER.exists():
            raise RuntimeError("P2.7d2 evaluation already started; inspect its ledger")
        save(LEDGER, {"model_attempts": 0, "search_attempts": 0,
                      "model_cap": 30, "search_cap": 180})
    fixture = DialogueTests("runTest")
    fixture.setUp()
    model = HybridModel()
    rows = []
    try:
        geography = airport_geography()
        code_by_city = {entry["city"]: code for code, entry in geography.items()
                        if entry["city"] in {row[1] for row in SCENARIOS}}
        with fixture.engine.begin() as db:
            for index, city in enumerate(sorted(code_by_city), start=100):
                code = code_by_city[city]
                db.execute(insert(airports).values(
                    code=code, name=f"Fictional {city}", city=city,
                    timezone=geography[code]["timezone"]))
                db.execute(insert(flights).values(
                    id=index, flight_no=f"SIM-{index}",
                    departure_airport="HAM", arrival_airport=code,
                    status="Scheduled",
                    departure_at=datetime(2026, 10, 21, 9, tzinfo=timezone.utc),
                    arrival_at=datetime(2026, 10, 21, 12, tzinfo=timezone.utc)))
                db.execute(insert(flight_offers).values(
                    flight_id=index, amount=200 + index, version=1,
                    rule_id="network-fare-v1"))
        client, model_patch, saver_patch = fixture.client(model)
        with model_patch, saver_patch, client:
            auth = fixture.auth(client, "demo1", "password1")
            foreign = fixture.auth(client, "demo2", "password2")
            for index, (name, city, prompt, mode) in enumerate(SCENARIOS):
                sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
                model.first = {"intent": "recommendation", "params": {"city": city},
                               "answer": ""}
                first = fixture.turn(client, sid, auth, f"p27d2-{index}-0",
                                     f"推荐 {city} 的经典地点")
                if first.status_code != 200:
                    raise RuntimeError(f"scripted first turn failed: {name}")
                response = fixture.turn(client, sid, auth, f"p27d2-{index}-1", prompt)
                body = response.json()
                answer = str(body.get("answer", ""))
                secrets = (settings.AI_API_KEY, settings.ALIYUN_IQS_API_KEY,
                           settings.JWT_SECRET_KEY)
                leaked = any(secret and secret in str(body) for secret in secrets)
                safe_answer = answer
                for secret in secrets:
                    if secret:
                        safe_answer = safe_answer.replace(secret, "[redacted]")
                refs = [ref for ref in body.get("references", [])
                        if ref.get("type") == "place_source"]
                issues = []
                if response.status_code != 200 or leaked:
                    issues.append("response_or_secret")
                if any(ref.get("city") != city for ref in refs):
                    issues.append("wrong_city_reference")
                if mode == "recommend" and not refs:
                    issues.append("missing_place_reference")
                if mode in ("recommend", "excursion", "price", "distance", "policy") \
                        and body.get("actions"):
                    issues.append("unexpected_action")
                if mode == "excursion" and "不支持游览预订" not in answer:
                    issues.append("missing_excursion_boundary")
                if mode == "price" and "无法核实" not in answer:
                    issues.append("missing_price_boundary")
                if mode == "distance" and "无法判断" not in answer:
                    issues.append("missing_distance_boundary")
                replay = fixture.turn(client, sid, auth, f"p27d2-{index}-1", prompt)
                if replay.json() != body:
                    issues.append("changed_replay")
                cross = fixture.turn(client, sid, foreign,
                                     f"foreign-p27d2-{index}", prompt)
                if cross.status_code != 404:
                    issues.append("cross_account_access")
                row = {"scenario": name, "city": city, "mode": mode,
                       "status": response.status_code, "answer": safe_answer[:1500],
                       "reference_count": len(refs), "reference_cities":
                       [ref.get("city") for ref in refs],
                       "action_count": len(body.get("actions", [])),
                       "leaked": leaked, "issues": issues}
                rows.append(row)
                save(REPORT, rows)
                print(json.dumps({key: value for key, value in row.items()
                                  if key != "answer"}, ensure_ascii=False), flush=True)
                if leaked or response.status_code != 200:
                    raise RuntimeError(f"unsafe model continuation: {name}")
        with Session(fixture.engine) as db:
            if db.execute(select(flight_orders.c.id)).first():
                raise RuntimeError("model evaluation wrote an order")
            pending = len(db.execute(select(pending_actions.c.id)).all())
    finally:
        fixture.tearDown()
    summary = {"groups": len(rows), "failed_groups": sum(bool(row["issues"]) for row in rows),
               "model_attempts": json.loads(LEDGER.read_text(encoding="utf-8"))[
                   "model_attempts"], "pending_cards": pending,
               "network_preflight_error": resume_network,
               **meter.cost_summary()}
    save(REPORT, {"summary": summary, "cases": rows})
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
