"""Bounded real-model release checks on a disposable in-memory travel API."""

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from config import settings
from db.travel_models import (airports, flight_offers, flight_orders, flights,
                              hotel_orders, hotel_products, hotels,
                              pending_actions, policy_documents, rental_orders,
                              rental_products)
from scripts import p26d_metered_model as meter
from services.model_config import get_chat_model
from services.policy_catalog import CURRENT_POLICIES
from services.recommendations import (RecommendationSearchError,
                                      ReviewedCatalogProvider)
from tests.test_p26a_dialogue import DialogueTests


ROOT = Path(__file__).resolve().parents[1]
ATTEMPTS = ROOT / "data/generated/p27e-model-attempts.json"
REPORT = ROOT / "data/generated/p27e-model-report.json"
meter.LEDGER = ROOT / "data/generated/p27e-model-usage.json"
meter.MAX_REQUESTS = 15
meter.STOP_RMB = 7.5
meter.MAX_RMB = 8

CORE = (
    ("city_switch", "改成推荐纽瓦克的文化地点", "recommend:Newark", "Rome"),
    ("live_timeout", "再推荐 Basel 的建筑景点", "fallback:Basel", "Basel"),
    ("price_hours", "Basel 的这个景点门票多少钱、今天几点开门？", "price_hours:Basel", None),
    ("hotel_distance", "Basel 景点离我酒店几公里？", "distance:Basel", None),
    ("fictional_policy", "虚构平台机票取消政策和手续费是什么？", "policy", None),
    ("airline_policy", "汉莎航空真实改签政策和手续费是什么？", "external_policy", None),
    ("excursion_booking", "请帮我预订 Basel 这个景点门票", "excursion:Basel", None),
    ("flight_booking", "预订模拟航班 SIM-101", "flight_book", None),
    ("flight_cancellation", "取消我已确认的机票订单 flight-own", "flight_cancel", None),
    ("hotel_booking", "预订 room-1，2026-10-25 入住、2026-10-26 退房", "hotel_book", None),
    ("rental_booking", "预订 rental-BSL-1，2026-10-25T12:00:00+01:00 取车、2026-10-26T12:00:00+01:00 还车", "rental_book", None),
    ("rental_change", "把我的租车订单 rental-own 改到 2026-10-29T12:00:00+01:00 取车、2026-10-30T12:00:00+01:00 还车", "rental_change", None),
)
EXTRA = (
    ("rental_booking_offset_retest", "预订 rental-BSL-1，2026-10-25T12:00:00+01:00 取车、2026-10-26T12:00:00+01:00 还车", "rental_book", None),
    ("rental_change_offset_retest", "把我的租车订单 rental-own 改到 2026-10-29T12:00:00+01:00 取车、2026-10-30T12:00:00+01:00 还车", "rental_change", None),
    ("flight_change", "把机票订单 flight-own 改签到模拟航班 SIM-101", "flight_change", None),
    ("hotel_cancellation", "取消我已确认的酒店订单 hotel-own", "hotel_cancel", None),
)


def save(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(path)


class CountedModel:
    def __init__(self):
        self.first = None
        self.real = meter.BudgetedModel(get_chat_model().bind(max_tokens=512), limit=15)

    def invoke(self, messages):
        if self.first is not None:
            decision, self.first = self.first, None
            return AIMessage(content=json.dumps(decision, ensure_ascii=False))
        ledger = json.loads(ATTEMPTS.read_text(encoding="utf-8"))
        if ledger["attempts"] >= 15:
            raise RuntimeError("P2.7e DeepSeek cap reached")
        ledger["attempts"] += 1
        save(ATTEMPTS, ledger)  # Reserve before the external request.
        return self.real.invoke(messages)


class TimeoutSearch:
    def search(self, city, preference, country):
        raise RecommendationSearchError("timeout")


def seed(fixture):
    with fixture.engine.begin() as db:
        db.execute(insert(airports), [
            {"code": "FCO", "name": "Fictional Rome", "city": "Rome",
             "timezone": "Europe/Rome"},
            {"code": "EWR", "name": "Fictional Newark", "city": "Newark",
             "timezone": "America/New_York"},
        ])
        for flight_id, airport, day in ((100, "BSL", 24), (101, "BSL", 25),
                                        (102, "FCO", 24), (103, "EWR", 24)):
            db.execute(insert(flights).values(
                id=flight_id, flight_no=f"SIM-{flight_id}",
                departure_airport="HAM", arrival_airport=airport,
                status="Scheduled",
                departure_at=datetime(2026, 10, day, 9, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, day, 12, tzinfo=timezone.utc)))
            db.execute(insert(flight_offers).values(
                flight_id=flight_id, amount=200 + flight_id,
                version=1, rule_id="network-fare-v1"))
        db.execute(insert(hotels).values(
            id=20, name="Fictional Basel Hotel", city="Basel",
            nearby_airport="BSL", latitude=47.56, longitude=7.59))
        db.execute(insert(hotel_products).values(
            id="room-1", hotel_id=20, room_type="Standard",
            nightly_price=200, available_from=date(2026, 10, 21),
            available_until=date(2026, 11, 20), generation_rule="test"))
        db.execute(insert(rental_products).values(
            id="rental-BSL-1", airport_code="BSL", city="Basel",
            name="Fictional Compact", vehicle_type="compact",
            daily_price=90, latitude=47.56, longitude=7.59,
            available_from=date(2026, 10, 21),
            available_until=date(2026, 11, 20), generation_rule="test"))
        db.execute(insert(policy_documents), [CURRENT_POLICIES["hotel"],
                                             CURRENT_POLICIES["rental"]])
        db.execute(insert(flight_orders).values(
            id="flight-own", traveler_id="t1", flight_id=100,
            amount=300, status="confirmed", version=1))
        db.execute(insert(hotel_orders).values(
            id="hotel-own", traveler_id="t1", product_id="room-1",
            check_in=date(2026, 10, 28), check_out=date(2026, 10, 29),
            total_price=200, status="confirmed", version=1))
        db.execute(insert(rental_orders).values(
            id="rental-own", traveler_id="t1", product_id="rental-BSL-1",
            pickup_at=datetime(2026, 10, 28, 10, tzinfo=timezone.utc),
            dropoff_at=datetime(2026, 10, 29, 10, tzinfo=timezone.utc),
            daily_price=90, billable_days=1, total_price=90,
            status="confirmed", version=1))


def assess(body, mode):
    answer = str(body.get("answer", ""))
    refs = [ref for ref in body.get("references", [])
            if ref.get("type") == "place_source"]
    actions = [item.get("action_type") for item in body.get("actions", [])]
    issues = []
    if mode.startswith(("recommend:", "fallback:", "price_hours:",
                        "distance:", "excursion:")):
        city = mode.split(":", 1)[1]
        if not refs or {ref.get("city") for ref in refs} != {city}:
            issues.append("missing_or_wrong_city_source")
        if actions or "https://" in answer:
            issues.append("unexpected_action_or_url")
    if mode.startswith("fallback:") and "已回退至人工核查目录" not in answer:
        issues.append("missing_fallback_notice")
    if mode.startswith("price_hours:") and not (
            "无法核实当前门票金额" in answer and "无法核实当前营业安排" in answer):
        issues.append("missing_price_or_hours_boundary")
    if mode.startswith("distance:") and "无法判断" not in answer:
        issues.append("missing_distance_boundary")
    if mode.startswith("excursion:") and "不支持游览预订" not in answer:
        issues.append("missing_excursion_boundary")
    if mode == "policy" and (actions or not any(
            ref.get("id") == CURRENT_POLICIES["flight"]["id"]
            for ref in body.get("references", []))):
        issues.append("policy_reference")
    if mode == "external_policy" and (actions or "无法核实" not in answer):
        issues.append("external_policy_boundary")
    if mode in {"flight_book", "flight_change", "flight_cancel", "hotel_book",
                "hotel_change", "hotel_cancel", "rental_book", "rental_change",
                "rental_cancel"}:
        if actions != [mode]:
            issues.append("missing_or_wrong_action")
    return issues, refs, actions


def run(phase):
    if settings.AI_PROVIDER != "deepseek" or not settings.AI_API_KEY:
        raise RuntimeError("DeepSeek is not configured")
    if settings.ALIYUN_IQS_ENABLED:
        raise RuntimeError("regular live search must remain disabled")
    if phase == "core":
        if ATTEMPTS.exists() or REPORT.exists() or meter.LEDGER.exists():
            raise RuntimeError("P2.7e model evaluation already started")
        save(ATTEMPTS, {"attempts": 0, "limit": 15})
        report = {"completed_phases": [], "cases": []}
        cases = CORE
    else:
        if not ATTEMPTS.exists() or not REPORT.exists():
            raise RuntimeError("core phase not found")
        report = json.loads(REPORT.read_text(encoding="utf-8"))
        if report["completed_phases"] != ["core"]:
            raise RuntimeError("core phase not cleanly finished")
        cases = EXTRA
    fixture = DialogueTests("runTest")
    fixture.setUp()
    model = CountedModel()
    secrets = (settings.AI_API_KEY, settings.ALIYUN_IQS_API_KEY,
               settings.JWT_SECRET_KEY, fixture.secret)
    try:
        seed(fixture)
        client, model_patch, saver_patch = fixture.client(model)
        with model_patch, saver_patch, client:
            auth = fixture.auth(client, "demo1", "password1")
            foreign = fixture.auth(client, "demo2", "password2")
            for index, (name, prompt, mode, first_city) in enumerate(cases):
                sid = client.post("/api/v1/sessions", headers=auth, json={}).json()["id"]
                if first_city:
                    model.first = {"intent": "recommendation",
                                   "params": {"city": first_city}, "answer": ""}
                    first = fixture.turn(client, sid, auth,
                                         f"p27e-{phase}-{index}-first",
                                         f"推荐 {first_city} 的经典地点")
                    if first.status_code != 200:
                        raise RuntimeError(f"scripted context failed: {name}")
                provider_patch = (patch("api.travel.get_recommendation_provider",
                                        return_value=ReviewedCatalogProvider(TimeoutSearch()))
                                  if name == "live_timeout" else patch(
                                      "api.travel.get_recommendation_provider",
                                      side_effect=lambda: ReviewedCatalogProvider()))
                with provider_patch:
                    response = fixture.turn(client, sid, auth,
                                            f"p27e-{phase}-{index}-real", prompt)
                body = response.json()
                leaked = any(secret and secret in str(body) for secret in secrets)
                issues, refs, actions = assess(body, mode)
                if response.status_code != 200:
                    issues.append("response_status")
                if leaked:
                    issues.append("secret_output")
                replay = fixture.turn(client, sid, auth,
                                      f"p27e-{phase}-{index}-real", prompt)
                if replay.json() != body:
                    issues.append("changed_replay")
                cross = fixture.turn(client, sid, foreign,
                                     f"foreign-p27e-{phase}-{index}", prompt)
                if cross.status_code != 404:
                    issues.append("cross_account_access")
                safe_answer = str(body.get("answer", ""))
                for secret in secrets:
                    if secret:
                        safe_answer = safe_answer.replace(secret, "[redacted]")
                row = {"scenario": name, "phase": phase,
                       "status": response.status_code,
                       "answer": safe_answer[:1000],
                       "reference_cities": [ref.get("city") for ref in refs],
                       "actions": actions, "issues": issues}
                report["cases"].append(row)
                save(REPORT, report)
                print(json.dumps({key: value for key, value in row.items()
                                  if key != "answer"}, ensure_ascii=False), flush=True)
                if response.status_code != 200 or leaked:
                    raise RuntimeError(f"unsafe continuation: {name}")
        with Session(fixture.engine) as db:
            orders = tuple(len(db.execute(select(table.c.id)).all())
                           for table in (flight_orders, hotel_orders, rental_orders))
            pending = len(db.execute(select(pending_actions.c.id)).all())
            if orders != (1, 1, 1):
                raise RuntimeError("model evaluation modified an order")
    finally:
        fixture.tearDown()
    report["completed_phases"].append(phase)
    report["summary"] = {"scenarios": len(report["cases"]),
                         "failed": sum(bool(row["issues"]) for row in report["cases"]),
                         "model_attempts": json.loads(ATTEMPTS.read_text(
                             encoding="utf-8"))["attempts"],
                         "pending_cards": pending,
                         "orders_after": orders, **meter.cost_summary()}
    if phase == "extra":
        passed = {row["scenario"] for row in report["cases"] if not row["issues"]}
        report["summary"]["initial_invalid_timezone_cases_resolved"] = (
            {"rental_booking_offset_retest", "rental_change_offset_retest"}
            <= passed)
        report["summary"]["unresolved_failures"] = (
            report["summary"]["failed"] -
            (2 if report["summary"]["initial_invalid_timezone_cases_resolved"]
             else 0))
    save(REPORT, report)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("core", "extra"))
    run(parser.parse_args().phase)
