"""Bounded, traveler-scoped evidence for the read-only conversation graph."""

from datetime import date

from sqlalchemy import select

from db.travel_models import chat_turns, demo_state
from services.actions import ActionNotFound, get_proposal
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
        f"{card['action_type']} 卡 {card['id']}（当前状态 {card['status']}，"
        f"目标订单 {card.get('target_order_id') or '新订单'}，"
        f"模拟应补 {card['price'].get('payable', 0)}、应退 "
        f"{card['price'].get('refundable', 0)}）" for card in current)
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
    return _result(f"最近生成的模拟交易卡：{details}。{advice}", references)


def read_for_assistant(db, traveler_id, intent, params):
    """Never accept owner, price, or policy content from model parameters."""
    if intent == "itinerary":
        rows = itinerary(db, traveler_id)
        parts = []
        references = []
        for kind, label in (("flight_orders", "机票"), ("hotel_orders", "酒店"),
                            ("rental_orders", "租车")):
            orders = rows[kind][:5]
            if not orders:
                parts.append(f"{label}：暂无订单")
                continue
            parts.append(f"{label}：" + "；".join(
                f"{item['id']}（{item['status']}）" for item in orders))
            references.extend({"type": "order", "id": item["id"], "kind": kind,
                               "version": item["version"], "status": item["status"]}
                              for item in orders)
        return _result("本人模拟行程。" + "；".join(parts), references)

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
            return _result("请提供出发机场代码、到达机场代码和出行日期，才能查模拟航班。")
        sort = "price" if params.get("sort") == "price" else "departure"
        try:
            rows = find_flights(db, departure.upper(), arrival.upper(), travel_date,
                                limit=3, sort=sort)
        except OrderError:
            return _result("航班查询条件无效，请核对机场代码和日期。")
        if not rows:
            return _result("该日期和航线没有可预订的模拟航班。")
        return _result(
            "找到以下模拟航班（金额为演示计价单位）：" + "；".join(
                f"{row['flight_no']}（ID {row['id']}，{row['departure_at'].isoformat()} 起飞，"
                f"模拟报价 {row['demo_fare_amount']}）" for row in rows),
            [{"type": "flight", "id": row["id"], "offer_version": row["offer_version"],
              "price": row["demo_fare_amount"], "travel_date": travel_date.isoformat()}
             for row in rows],
        )

    if intent == "hotel_search":
        city = _as_text(params.get("city"))
        check_in = _as_date(params.get("check_in"))
        check_out = _as_date(params.get("check_out"))
        if not city or not check_in or not check_out:
            return _result("请提供城市、入住日期和退房日期，才能查模拟酒店。")
        airport = _as_text(params.get("airport"), 8)
        sort = "distance" if airport and params.get("sort") == "distance" else "price"
        try:
            rows = find_hotels(db, city, check_in, check_out, airport=airport,
                               sort=sort, limit=3)
        except OrderError:
            return _result("酒店查询条件无效，请核对城市和日期。")
        if not rows:
            return _result("该城市和日期没有可用的模拟酒店产品。")
        return _result(
            "找到以下虚构酒店产品（每晚为演示计价单位）：" + "；".join(
                f"{row['name']} {row['room_type']}（产品 {row['product_id']}，"
                f"每晚 {row['nightly_price']}）" for row in rows),
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
            return _result("请提供取车城市或机场，以及取车和还车日期，才能查模拟租车。")
        try:
            rows = find_rentals(db, pickup, dropoff, city=city,
                                airport=airport.upper() if airport else None,
                                sort="price", limit=3)
        except OrderError:
            return _result("租车查询条件无效，请核对地点和日期。")
        if not rows:
            return _result("该地点和日期没有可用的模拟租车产品。")
        return _result(
            "找到以下虚构租车产品（每日为演示计价单位）：" + "；".join(
                f"{row['name']}（产品 {row['id']}，每日 {row['daily_price']}）"
                for row in rows),
            [{"type": "rental_product", "id": row["id"],
              "price": row["daily_price"], "pickup_date": pickup.isoformat(),
              "drop_off_date": dropoff.isoformat()} for row in rows],
        )

    return _result("该查询暂不可用。")
