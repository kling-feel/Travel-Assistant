"""Bounded, traveler-scoped evidence for the read-only conversation graph."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.travel_models import airports, chat_turns, demo_state
from services.actions import ActionNotFound, _hotel_today, get_proposal
from services.location_labels import airport_zh, amount_zh, city_zh, product_zh, room_zh
from services.sessions import _owned_session
from services.travel import (OrderError, find_flights, find_hotels, find_rentals,
                             itinerary, policies)


def demo_date(db):
    return db.execute(select(demo_state.c.as_of).where(
        demo_state.c.id == 1)).scalar_one_or_none() or date.today()


def _as_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _as_text(value, maximum=80):
    return value.strip() if isinstance(value, str) and 1 <= len(value.strip()) <= maximum else None


def _result(answer, references=()):
    return {"answer": answer, "references": list(references)}


def _airport(db, code):
    row = db.execute(select(airports.c.city, airports.c.timezone).where(
        airports.c.code == code)).one_or_none()
    return (airport_zh(code, row.city), row.timezone, row.city) if row else (code, None, "")


def _local_time(value, zone):
    if not zone:
        return value.isoformat() if isinstance(value, datetime) else str(value)
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M（当地）")


_ACTION_NAMES = {"flight_book": "预订机票", "flight_change": "改签机票",
                 "flight_cancel": "取消机票", "hotel_book": "预订酒店",
                 "hotel_change": "修改酒店", "hotel_cancel": "取消酒店",
                 "rental_book": "预订租车", "rental_change": "修改租车日期",
                 "rental_cancel": "取消租车"}
_STATUS_NAMES = {"pending": "待确认", "confirmed": "已确认",
                 "rejected": "已拒绝", "expired": "已过期",
                 "cancelled": "已取消", "canceled": "已取消"}


def read_action_guidance(db, traveler_id, session_id, mode):
    """Explain the latest displayed cards without confirming or rejecting them."""
    _owned_session(db, traveler_id, session_id)
    rows = db.execute(select(chat_turns.c.action_cards).where(
        chat_turns.c.session_id == session_id,
        chat_turns.c.status == "completed",
    ).order_by(chat_turns.c.started_at.desc()).limit(20)).scalars()
    cards = next((items for items in rows if isinstance(items, list) and items), [])
    current = []
    for card in cards[:2]:
        if not isinstance(card, dict) or not isinstance(card.get("id"), str):
            continue
        try:
            current.append(get_proposal(db, traveler_id, session_id, card["id"]))
        except ActionNotFound:
            continue
    if not current:
        return _result("此会话没有可定位的近期待确认卡。请先查看本人会话的动作列表。")
    references = [{"type": "action", "id": card["id"], "status": card["status"]}
                  for card in current]
    details = "；".join(
        f"{_ACTION_NAMES.get(card['action_type'], card['action_type'])}卡 {card['id']}（当前状态 {_STATUS_NAMES.get(card['status'], card['status'])}，"
        f"目标订单 {card.get('target_order_id') or '新订单'}，"
        f"需补 {amount_zh(card['price'].get('payable', 0))}、可退 "
        f"{amount_zh(card['price'].get('refundable', 0))}）" for card in current)
    pending = [card for card in current if card["status"] == "pending"]
    if mode == "withdraw":
        advice = ("如要撤回，请对每张仍待确认的卡分别调用对应的拒绝接口；"
                  "这条对话不会取消已确认订单，也不会自动拒绝卡。"
                  if pending else "这些卡目前已非待确认状态，无需再次拒绝。")
    else:
        advice = ("如确认，请先核对每张卡的对象、时间、金额和政策，再分别调用对应的确认接口；"
                  "口头同意或付款表达不会执行订单，也不会重复生成卡。"
                  "如还想办理新的交易，请下一轮单独说明对象和条件。"
                  if pending else "这些卡目前已非待确认状态，请重新查询后再提出新交易。")
    return _result(f"最近生成的待确认卡：{details}。{advice}", references)


def read_for_assistant(db, traveler_id, intent, params):
    """Never accept owner, price, or policy content from model parameters."""
    if intent == "itinerary":
        rows = itinerary(db, traveler_id)
        question = _as_text(params.get("_user_text"), 4000) or ""
        flight_only = "起飞" in question or "航班时间" in question
        hotel_only = "退房时间" in question or "入住时间" in question
        parts = []
        references = []
        for kind, label in (("flight_orders", "机票"), ("hotel_orders", "酒店"),
                            ("rental_orders", "租车")):
            if (flight_only and kind != "flight_orders") or (hotel_only and kind != "hotel_orders"):
                continue
            orders = rows[kind][:5]
            if not orders:
                parts.append(f"{label}：暂无订单")
                continue
            if kind == "flight_orders":
                descriptions = [
                    f"{_airport(db, item['departure_airport'])[0]} → {_airport(db, item['arrival_airport'])[0]}"
                    f"（航班 {item['flight_no']}，起飞 {_local_time(item['departure_at'], _airport(db, item['departure_airport'])[1])}，"
                    f"到达 {_local_time(item['arrival_at'], _airport(db, item['arrival_airport'])[1])}，"
                    f"订单 {item['id']}，{_STATUS_NAMES.get(item['status'], item['status'])}"
                    f"{'，起飞时间已过' if item['departure_at'].replace(tzinfo=item['departure_at'].tzinfo or timezone.utc) <= datetime.now(timezone.utc) else ''}）"
                    for item in orders]
            elif kind == "hotel_orders":
                descriptions = [
                    f"{product_zh(item['name'], city=item['city'])} {room_zh(item['room_type'])}"
                    f"（{item['check_in']} 入住、{item['check_out']} 退房，"
                    f"订单 {item['id']}，{_STATUS_NAMES.get(item['status'], item['status'])}"
                    f"{'，入住日期已过' if item['check_in'] < _hotel_today(db, item['product_id'], datetime.now(timezone.utc)) else ''}）"
                    for item in orders]
            else:
                descriptions = [
                    f"{product_zh(item['name'], city=_airport(db, item['airport_code'])[2], airport=item['airport_code'])}"
                    f"（取车 {_local_time(item['pickup_at'], item['airport_timezone'])}、"
                    f"还车 {_local_time(item['dropoff_at'], item['airport_timezone'])}，"
                    f"订单 {item['id']}，{_STATUS_NAMES.get(item['status'], item['status'])}）"
                    for item in orders]
            parts.append(f"{label}：" + "；".join(descriptions))
            references.extend({"type": "order", "id": item["id"], "kind": kind,
                               "version": item["version"], "status": item["status"]}
                              for item in orders)
        return _result("我的行程：" + "；".join(parts), references)

    if intent == "policy":
        scope = _as_text(params.get("scope"), 16)
        if scope not in ("flight", "hotel", "rental"):
            return _result("请说明要查询机票、酒店还是租车的虚构平台政策。")
        try:
            records = policies(db, scope)
        except OrderError:
            return _result("当前虚构平台政策无法核实，请稍后重试。")
        if not records:
            return _result("没有可核实的当前虚构平台政策。")
        rule = records[0]
        return _result(
            f"虚构平台政策《{rule['title']}》（规则 {rule['id']}，v{rule['version']}，"
            f"适用范围 {rule['scope']}）：{rule['body']}",
            [{"type": "policy", "id": rule["id"], "version": rule["version"]}],
        )

    if intent == "flight_search":
        departure = _as_text(params.get("departure"), 8)
        arrival = _as_text(params.get("arrival"), 8)
        travel_date = _as_date(params.get("travel_date"))
        if not departure or not arrival or not travel_date:
            return _result("请提供出发地、目的地和出行日期，以便查询航班。")
        sort = "price" if params.get("sort") == "price" else "departure"
        try:
            rows = find_flights(db, departure.upper(), arrival.upper(), travel_date,
                                limit=3, sort=sort)
        except OrderError:
            return _result("航班查询条件无效，请核对出发地、目的地和日期。")
        if not rows:
            return _result("该日期和航线没有可预订的航班。")
        return _result(
            "找到以下航班：" + "；".join(
                f"{_airport(db, row['departure_airport'])[0]} → {_airport(db, row['arrival_airport'])[0]}，"
                f"航班 {row['flight_no']}（候选 {row['id']}，"
                f"当地出发 {_local_time(row['departure_at'], _airport(db, row['departure_airport'])[1])}，"
                f"参考报价 {amount_zh(row['demo_fare_amount'])}）" for row in rows),
            [{"type": "flight", "id": row["id"], "offer_version": row["offer_version"],
              "price": row["demo_fare_amount"], "travel_date": travel_date.isoformat()}
             for row in rows],
        )

    if intent == "hotel_search":
        city = _as_text(params.get("city"))
        check_in = _as_date(params.get("check_in"))
        check_out = _as_date(params.get("check_out"))
        if not city or not check_in or not check_out:
            return _result("请提供城市、入住日期和退房日期，以便查询酒店。")
        airport = _as_text(params.get("airport"), 8)
        sort = "distance" if airport and params.get("sort") == "distance" else "price"
        try:
            rows = find_hotels(db, city, check_in, check_out, airport=airport,
                               sort=sort, limit=3)
        except OrderError:
            return _result("酒店查询条件无效，请核对城市和日期。")
        if not rows:
            return _result("该城市和日期没有可用的酒店房型。")
        return _result(
            "找到以下酒店房型：" + "；".join(
                f"{product_zh(row['name'], city=row['city'])} {room_zh(row['room_type'])}"
                f"（产品 {row['product_id']}，每晚 {amount_zh(row['nightly_price'])}）" for row in rows),
            [{"type": "hotel_product", "id": row["product_id"],
              "price": row["nightly_price"], "distance_km": row.get("distance_km"),
              "check_in": check_in.isoformat(), "check_out": check_out.isoformat()}
             for row in rows],
        )

    if intent == "rental_search":
        city = _as_text(params.get("city"))
        airport = _as_text(params.get("airport"), 8)
        pickup = _as_date(params.get("pickup_date"))
        dropoff = _as_date(params.get("drop_off_date"))
        if (not city and not airport) or not pickup or not dropoff:
            return _result("请提供取车地点，以及取车和还车日期，以便查询租车。")
        try:
            rows = find_rentals(db, pickup, dropoff, city=city,
                                airport=airport.upper() if airport else None,
                                sort="price", limit=3)
        except OrderError:
            return _result("租车查询条件无效，请核对地点和日期。")
        if not rows:
            return _result("该地点和日期没有可用的车辆。")
        return _result(
            "找到以下车辆：" + "；".join(
                f"{product_zh(row['name'], city=row['city'], airport=row['airport_code'])}"
                f"（产品 {row['id']}，每日 {amount_zh(row['daily_price'])}）"
                for row in rows),
            [{"type": "rental_product", "id": row["id"],
              "price": row["daily_price"], "pickup_date": pickup.isoformat(),
              "drop_off_date": dropoff.isoformat()} for row in rows],
        )

    return _result("该查询暂不可用。")
