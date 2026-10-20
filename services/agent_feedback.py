"""Traveler-facing explanations for guarded transaction outcomes."""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.travel_models import airports, flight_orders, flights, hotel_orders
from services.actions import _hotel_today
from services.travel import itinerary


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _flight_detail(db, flight):
    zone = db.execute(select(airports.c.timezone).where(
        airports.c.code == flight["departure_airport"])).scalar_one_or_none()
    when = _utc(flight["departure_at"])
    local = when.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M") if zone else when.isoformat()
    return f"航班 {flight['flight_no']}（当地起飞 {local}）"


def _owned_flight(db, traveler_id, order_id=None):
    rows = db.execute(select(flight_orders, flights).join(
        flights, flight_orders.c.flight_id == flights.c.id).where(
            flight_orders.c.traveler_id == traveler_id,
            flight_orders.c.status == "confirmed",
        )).mappings().all()
    return next((row for row in rows if row["id"] == order_id), None) if order_id else (
        rows[0] if len(rows) == 1 else None)


def _owned_hotel(db, traveler_id, order_id=None):
    rows = db.execute(select(hotel_orders).where(
        hotel_orders.c.traveler_id == traveler_id,
        hotel_orders.c.status == "confirmed",
    )).mappings().all()
    return next((row for row in rows if row["id"] == order_id), None) if order_id else (
        rows[0] if len(rows) == 1 else None)


def explain_action_error(db, traveler_id, plans, error):
    """Never send raw service exceptions to a traveler."""
    reason = str(error)
    first = next((item for item in plans if isinstance(item, dict)), {})
    params = first.get("parameters", {})
    params = params if isinstance(params, dict) else {}
    if reason == "flight no longer available":
        order = _owned_flight(db, traveler_id, params.get("order_id"))
        if order:
            return (f"订单 {order['id']} 的{_flight_detail(db, order)}已过起飞时间或班次不再可用，"
                    "因此不能取消或改签。你仍可在行程中查看这笔历史订单。")
        return "这笔机票已过起飞时间或班次不再可用，不能取消或改签。"
    if reason == "hotel cancellation closes on local check-in date":
        order = _owned_hotel(db, traveler_id, params.get("order_id"))
        if order:
            return (f"订单 {order['id']} 的酒店入住日期是 {order['check_in']}。"
                    "本平台仅允许在酒店当地入住日期前取消，当前已过截止日期。")
        return "酒店当地入住日期已到，不能再取消这笔订单。"
    messages = {
        "flight no longer bookable": "这班航班已过起飞时间或不再可预订，请重新查询未来班次。",
        "flight outside demo booking window": "该航班不在当前可预订日期范围，请选择未来可用班次。",
        "invalid replacement flight": "改签目标航班无效、已起飞或与原航线不一致，请重新查询同航线候选。",
        "hotel stay already started": "酒店入住日期已过，不能修改这笔住宿；请选择未来日期。",
        "order status or version changed": "订单状态已经变化，请刷新行程后重试。",
        "order unavailable": "未找到可办理的本人订单，请刷新行程并核对订单号。",
        "invalid order reference": "订单信息不完整，请刷新行程并重新选择。",
        "invalid stay dates": "退房日期必须晚于入住日期，请重新选择。",
        "hotel stay unchanged": "酒店入住安排没有变化，无需生成修改确认卡。",
    }
    if reason in messages:
        return messages[reason]
    if re.search(r"[a-z]{2,}", reason, re.I):
        return "这笔操作未通过订单、时间或产品规则校验；没有生成确认卡，也没有修改订单。请刷新行程后重试。"
    return reason[:300]


def explain_selection_needed(db, traveler_id, plans, error):
    reason = str(error)
    first = next((item for item in plans if isinstance(item, dict)), {})
    action_type = first.get("action_type")
    if reason == "请先查询并明确选择具体候选。" and action_type == "flight_change":
        params = first.get("parameters", {})
        order = _owned_flight(db, traveler_id, params.get("order_id") if isinstance(params, dict) else None)
        if order:
            return (f"我找到了机票订单 {order['id']}，当前为{_flight_detail(db, order)}。"
                    "改签还需要选择同航线的新班次；请告诉我新日期，或先查询可用航班。")
        return "改签还需要指定本人机票和同航线的新班次，请先查询行程与可用航班。"
    if reason == "请明确酒店日期或租车的取还车日期和当地时刻。":
        if action_type in ("hotel_book", "hotel_change"):
            return "预订或修改酒店还需要明确入住日期、退房日期和酒店房型；请提供日期后查询候选。"
        if action_type in ("rental_book", "rental_change"):
            return "租车还需要明确取车、还车的当地日期和时刻，并选择车辆。"
    return reason[:300]


def cancellation_overview(db, traveler_id):
    """Enumerate a request for all owned flight and hotel cancellations."""
    rows = itinerary(db, traveler_id)
    now = datetime.now(timezone.utc)
    lines, eligible = [], []
    for order in rows["flight_orders"]:
        if order["status"] != "confirmed":
            continue
        detail = _flight_detail(db, order)
        if _utc(order["departure_at"]) <= now:
            lines.append(f"机票订单 {order['id']}：{detail}已过起飞时间，不能取消。")
        else:
            lines.append(f"机票订单 {order['id']}：{detail}可申请取消，需逐笔确认。")
            eligible.append({"action_type": "flight_cancel",
                             "parameters": {"order_id": order["id"]}})
    for order in rows["hotel_orders"]:
        if order["status"] != "confirmed":
            continue
        if order["check_in"] <= _hotel_today(db, order["product_id"], now):
            lines.append(f"酒店订单 {order['id']}：{order['check_in']} 入住，已过取消截止日期。")
        else:
            lines.append(f"酒店订单 {order['id']}：{order['check_in']} 入住，可申请取消，需逐笔确认。")
            eligible.append({"action_type": "hotel_cancel",
                             "parameters": {"order_id": order["id"]}})
    if not lines:
        return "你目前没有已确认的机票或酒店订单可供核对。", []
    tail = ("本轮最多生成两张待确认卡；其他可办理订单请下一轮单独提出。"
            if len(eligible) > 2 else "订单尚未改变；可办理的项目仍须逐张确认。")
    return "\n".join([*lines, tail]), eligible[:2]
