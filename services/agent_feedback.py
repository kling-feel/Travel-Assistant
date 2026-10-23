"""Traveler-facing explanations for guarded transaction outcomes."""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.travel_models import airports, flight_orders, flights, hotel_orders
from services.actions import _hotel_today
from services.dialogue_context import mentioned_cities
from services.location_labels import city_zh
from services.travel import itinerary


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _flight_detail(db, flight):
    zone = db.execute(select(airports.c.timezone).where(
        airports.c.code == flight["departure_airport"])).scalar_one_or_none()
    when = _utc(flight["departure_at"])
    local = when.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M") if zone else when.isoformat()
    return f"航班 {flight['flight_no']}（当地起飞 {local}）"


def _local_display(value):
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M（当地）")
    except (TypeError, ValueError):
        return "时间待核对"


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


def bulk_cancellation_requested(text):
    """Only explicit bulk commands may enter the deterministic cancel overview."""
    return ("取消" in text
            and not re.search(r"政策|规则|怎么|如何|费用|取消过|已取消|不(?:要|想|用)?取消|"
                              r"(?:能|可以).{0,12}取消.{0,12}吗", text)
            and bool(re.search(r"所有|全部|全都|都取消|取消其他|取消其余|取消剩余", text))
            and bool(re.search(r"机票|航班|酒店|住宿|租车|车辆|行程|安排|其他|其余|剩余", text)))


def cancellation_overview(db, traveler_id, text=None):
    """Scope a bulk request to owned active orders, with explicit preserved items."""
    rows = itinerary(db, traveler_id)
    now = datetime.now(timezone.utc)
    text = text or "取消所有机票和酒店"
    preserved = re.search(r"(?:除了|除)(.{1,30}?)(?:以外|之外)|保留(.{1,30}?)(?:[，,；;]|，?取消|不取消)", text)
    preserved_text = next((part for part in preserved.groups() if part), "") if preserved else ""
    places = mentioned_cities(preserved_text)
    if preserved and len(places) != 1:
        return "请明确说出要保留的一座城市；我会先核对各订单，再逐笔提出取消。", []
    keep_city = places[0] if places else None
    if keep_city and len(keep_city) == 3 and keep_city.isupper():
        keep_city = db.execute(select(airports.c.city).where(
            airports.c.code == keep_city)).scalar_one_or_none()
    if preserved and not keep_city:
        return "无法核对要保留的城市，请说明城市名称。", []
    named = {kind for kind, words in (("flight", "机票|航班"), ("hotel", "酒店|住宿"),
                                      ("rental", "租车|车辆")) if re.search(words, text)}
    kinds = named or {"flight", "hotel", "rental"}
    if keep_city and re.search(r"其他|其余|剩余|以外.*都取消", text):
        kinds = {"flight", "hotel", "rental"}
    airport_city = dict(db.execute(select(airports.c.code, airports.c.city)).all())
    lines, eligible, retained, ambiguous = [], [], [], []
    for kind, label in (("flight", "机票"), ("hotel", "酒店"), ("rental", "租车")):
        if kind not in kinds:
            continue
        for order in rows[f"{kind}_orders"]:
            if order["status"] != "confirmed":
                continue
            identity = f"{label}订单 {order['id']}"
            if kind == "flight":
                detail = _flight_detail(db, order)
                destination = airport_city.get(order["arrival_airport"])
                departure = airport_city.get(order["departure_airport"])
                if keep_city and destination == keep_city:
                    retained.append(f"{identity}：目的地为{city_zh(keep_city)}，保留。")
                    continue
                if keep_city and departure == keep_city:
                    ambiguous.append(f"{identity}：从{city_zh(keep_city)}出发，是否也保留？")
                    continue
                eligible_now = _utc(order["departure_at"]) > now
                detail = f"{detail}{'可申请取消' if eligible_now else '已过起飞时间，不能取消'}"
            elif kind == "hotel":
                if keep_city and order["city"] == keep_city:
                    retained.append(f"{identity}：位于{city_zh(keep_city)}，保留。")
                    continue
                eligible_now = order["check_in"] > _hotel_today(db, order["product_id"], now)
                detail = f"{order['check_in']} 入住，{'可申请取消' if eligible_now else '已过取消截止日期'}"
            else:
                if keep_city and airport_city.get(order["airport_code"]) == keep_city:
                    retained.append(f"{identity}：在{city_zh(keep_city)}取车，保留。")
                    continue
                eligible_now = _utc(order["pickup_at"]) > now
                detail = (f"{_local_display(order['pickup_local'])} 取车，"
                          f"{'可申请取消' if eligible_now else '已过取车时间，不能取消'}")
            if eligible_now:
                eligible.append({"action_type": f"{kind}_cancel",
                                 "parameters": {"order_id": order["id"]}})
            lines.append(f"{identity}：{detail}。")
    if len(lines) + len(retained) + len(ambiguous) > 35 or sum(
            map(len, [*lines, *retained, *ambiguous])) > 3000:
        return "符合范围的订单较多，请缩小到机票、酒店或租车，并指定日期；本轮没有生成卡。", []
    if ambiguous:
        return "\n".join([*retained, *lines, *ambiguous,
                          "请先确认这些跨城订单的去留；上列可取消订单也没有生成取消卡，订单均未改变。"]), []
    if not lines and not retained:
        return "你目前没有符合这次范围的已确认订单可供核对。", []
    selected = eligible[:2]
    selected_ids = [item["parameters"]["order_id"] for item in selected]
    remaining = [item["parameters"]["order_id"] for item in eligible[2:]]
    if remaining:
        tail = ("本轮仅为 " + "、".join(selected_ids) + " 生成待确认卡；"
                "尚未生成卡的订单：" + "、".join(remaining) +
                "。这些订单不会自动继续办理；如仍要取消，请下一轮明确提出。")
    else:
        tail = "订单尚未改变；本轮可办理的项目仍须逐张确认。"
    return "\n".join([*retained, *lines, tail]), selected
