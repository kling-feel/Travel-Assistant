"""Bounded P2.6e live regressions on the disposable travel-eval database."""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from db import engine
from db.travel_models import flight_orders, hotel_orders, rental_orders, travelers
from config import settings
from scripts import evaluate_p26c_deepseek as base
from scripts import evaluate_p26d_variants as variants
from scripts import p26d_metered_model as meter
from scripts.p26d_eval_guard import assert_eval_database
from scripts.verify_p25_all_actions import _inputs
from services.model_config import get_chat_model
from utils.password_hash import get_hashed_password


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data/generated/p26e-live-journeys.json"
VARIANT_REPORT = ROOT / "data/generated/p26e-live-variants.json"
ORDINAL_REPORT = ROOT / "data/generated/p26e-exact-ordinal.json"


def _budget():
    meter.LEDGER = ROOT / "data/generated/p26e-budget-ledger.json"
    meter.MAX_REQUESTS = 25
    meter.STOP_RMB = 0.9
    meter.MAX_RMB = 1
    meter.NEXT_REQUEST_RESERVE_RMB = 0.1


def run_variants():
    variants.REPORT = VARIANT_REPORT
    labels = [f"alt-{kind}-{operation}" for kind in ("flight", "hotel", "rental")
              for operation in ("book", "change", "cancel")]
    labels += ["multi-search", "ordinal-short", "ordinal-digit", "ordinal-long",
               "ordinal-ge", "real-delta", "real-emirates", "real-lufthansa",
               "real-united", "real-compare"]
    with patch.object(sys, "argv", ["evaluate_p26d_variants", "--only", *labels]):
        variants.main()


def run_journeys():
    assert_eval_database()
    logging.disable(logging.INFO)
    baseline = base._counts()
    if baseline["chat_sessions"] or baseline["pending_actions"]:
        raise RuntimeError("P2.6e journeys require the clean evaluation baseline")
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        first, replacement, extras, hotel, rental, day, pickup, dropoff = _inputs(db, now)
        origin, destination, flight_day = base._multiple_flight_query(db, now)
    marker, password = uuid4().hex[:12], uuid4().hex
    owner, outsider = "p26e_journey_" + marker, "p26e_other_" + marker
    state = {"owner": owner, "outsider": outsider, "password": password,
             "baseline": baseline}
    report = {"scenarios": [], "baseline_restored": False}
    flight_order_id = str(uuid4())
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=user, username=user, password_hash=get_hashed_password(password),
                display_name="P2.6e journey", active=1)
                for user in (owner, outsider)])
            db.execute(insert(flight_orders).values(
                id=flight_order_id, traveler_id=owner, flight_id=first["id"],
                amount=6000, status="confirmed", version=1))
            db.execute(insert(hotel_orders).values(
                id=str(uuid4()), traveler_id=owner, product_id=hotel["id"],
                check_in=day, check_out=day + timedelta(days=1),
                total_price=hotel["nightly_price"], status="confirmed", version=1))
            db.execute(insert(rental_orders).values(
                id=str(uuid4()), traveler_id=owner, product_id=rental["id"],
                pickup_at=pickup, dropoff_at=dropoff,
                daily_price=rental["daily_price"], billable_days=2,
                total_price=rental["daily_price"] * 2,
                status="confirmed", version=1))
        model = meter.BudgetedModel(get_chat_model(), limit=25)
        with patch("api.travel.get_chat_model", return_value=model):
            with TestClient(base._app()) as client:
                auth = base._login(client, owner, password)
                foreign = base._login(client, outsider, password)
                sessions = {}

                def ask(journey, label, prompt, expected=(), no_card=False):
                    if journey not in sessions:
                        sessions[journey] = base._session(client, auth)
                    sid = sessions[journey]
                    response = client.post(
                        f"/api/v1/sessions/{sid}/turns", headers=auth,
                        json={"request_key": "p26e-journey-" + label,
                              "content": prompt})
                    body = response.json()
                    cards = body.get("actions", []) if response.status_code == 200 else []
                    kinds = [card["action_type"] for card in cards]
                    passed = (response.status_code == 200 and
                              (not expected or kinds == list(expected) or
                               all(item in [ref.get("type") for ref in
                                            body.get("references", [])] for item in expected)) and
                              (not no_card or not cards) and
                              settings.AI_API_KEY not in str(body))
                    report["scenarios"].append({
                        "journey": journey, "label": label, "passed": passed,
                        "status": response.status_code, "actions": kinds,
                        "card_ids": [card["id"] for card in cards],
                        "answer_excerpt": str(body.get("answer", body.get("detail", "")))[:300]})
                    print(f"{label}: {'PASS' if passed else 'FAIL'} {kinds}")
                    return sid, body

                ask("flight", "flight-search", f"查 {flight_day} {origin} 到 {destination} 的航班。",
                    ("flight",))
                sid, selected = ask("flight", "flight-second", "预订刚才的第二班。",
                                    ("flight_book",))
                if selected.get("actions"):
                    card = selected["actions"][0]
                    other = client.post(f"/api/v1/sessions/{sid}/actions/{card['id']}/confirm",
                                        headers=foreign, json={})
                    own = client.post(f"/api/v1/sessions/{sid}/actions/{card['id']}/confirm",
                                      headers=auth, json={})
                    report["confirmations"] = {"foreign_status": other.status_code,
                                               "owner_status": own.status_code}
                ask("flight", "flight-itinerary", "查看我现在的行程。", ("order",))

                d1, d2 = day.isoformat(), (day + timedelta(days=1)).isoformat()
                ask("hotel", "hotel-book", f"订 {hotel['id']}，{d1} 入住、{d2} 退房。",
                    ("hotel_book",))
                ask("hotel", "hotel-withdraw", "撤回刚才未确认的订房卡，不是取消已确认酒店订单。",
                    no_card=True)
                ask("hotel", "hotel-cancel", "取消我已确认的酒店订单。", ("hotel_cancel",))

                ask("rental", "rental-book",
                    f"订 {rental['id']}，{(pickup + timedelta(days=2)).isoformat()} 取车、"
                    f"{(dropoff + timedelta(days=2)).isoformat()} 还车。", ("rental_book",))
                ask("rental", "rental-approval", "我同意了，直接付款并完成租车。", no_card=True)
                ask("rental", "rental-change",
                    f"把我已确认的租车改到 {(pickup + timedelta(days=1)).isoformat()} 取车、"
                    f"{(dropoff + timedelta(days=1)).isoformat()} 还车。", ("rental_change",))

                ask("dual", "dual-proposal",
                    f"把我的订单 {flight_order_id} 改签成 SIM-{replacement.id}，同时订 {hotel['id']}，"
                    f"{d1} 入住、{d2} 退房。", ("flight_change", "hotel_book"))
                ask("dual", "dual-withdraw", "刚才两张待确认卡都不要执行。", no_card=True)
        during = base._counts()
        report["orders_expected"] = (
            during["flight_orders"] == baseline["flight_orders"] + 2 and
            during["hotel_orders"] == baseline["hotel_orders"] + 1 and
            during["rental_orders"] == baseline["rental_orders"] + 1)
    finally:
        base._cleanup(state)
        report["baseline_restored"] = True
        report["budget"] = meter.cost_summary()
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("P2.6e journeys:", sum(row["passed"] for row in report["scenarios"]),
          "/", len(report["scenarios"]), "budget:", report["budget"])


def run_exact_ordinal():
    assert_eval_database()
    logging.disable(logging.INFO)
    baseline = base._counts()
    if baseline["chat_sessions"] or baseline["pending_actions"]:
        raise RuntimeError("Exact ordinal check requires a clean baseline")
    with engine.connect() as db:
        origin, destination, day = base._multiple_flight_query(
            db, datetime.now(timezone.utc))
    marker, password = uuid4().hex[:12], uuid4().hex
    owner, outsider = "p26e_ordinal_" + marker, "p26e_other_" + marker
    state = {"owner": owner, "outsider": outsider, "password": password,
             "baseline": baseline}
    report = {"passed": False, "baseline_restored": False}
    try:
        with engine.begin() as db:
            db.execute(insert(travelers), [dict(
                id=user, username=user, password_hash=get_hashed_password(password),
                display_name="P2.6e ordinal", active=1)
                for user in (owner, outsider)])
        model = meter.BudgetedModel(get_chat_model(), limit=2)
        with patch("api.travel.get_chat_model", return_value=model):
            with TestClient(base._app()) as client:
                auth = base._login(client, owner, password)
                sid = base._session(client, auth)
                search = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                                     json={"request_key": "p26e-exact-search",
                                           "content": f"查 {day} {origin} 到 {destination} 的航班。"})
                refs = [ref for ref in search.json().get("references", [])
                        if ref.get("type") == "flight"]
                chosen = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                                     json={"request_key": "p26e-exact-second",
                                           "content": "预订刚才的第二班。"})
                cards = chosen.json().get("actions", [])
                target = cards[0]["parameters"].get("flight_id") if len(cards) == 1 else None
                report.update(search_status=search.status_code,
                              choose_status=chosen.status_code,
                              shown_ids=[ref["id"] for ref in refs],
                              proposed_flight_id=target,
                              passed=(search.status_code == chosen.status_code == 200
                                      and len(refs) >= 2 and len(cards) == 1
                                      and cards[0]["action_type"] == "flight_book"
                                      and target == refs[1]["id"]),
                              no_order_write=(base._counts()["flight_orders"]
                                              == baseline["flight_orders"]))
    finally:
        base._cleanup(state)
        report["baseline_restored"] = True
        report["budget"] = meter.cost_summary()
        ORDINAL_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print("P2.6e exact ordinal:", "PASS" if report["passed"] else "FAIL",
          "budget:", report["budget"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("variants", "journeys", "ordinal"))
    args = parser.parse_args()
    _budget()
    assert_eval_database()
    if args.phase == "variants":
        run_variants()
    elif args.phase == "journeys":
        run_journeys()
    else:
        run_exact_ordinal()


if __name__ == "__main__":
    main()
