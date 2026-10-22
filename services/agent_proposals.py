"""Resolve model transaction suggestions against owned orders and shown candidates."""

import re
from datetime import date, timedelta

from sqlalchemy import select

from db.travel_models import (chat_turns, flight_offers, flight_orders, hotel_orders,
                              hotel_products, rental_orders, rental_products)
from services.actions import FIELDS
from services.dialogue_context import mentioned_cities, mentioned_dates
from services.order_targeting import matching_orders, order_options


class SelectionNeeded(ValueError):
    """The traveler must clarify a target before any card is created."""


ORDER_TABLES = {"flight": flight_orders, "hotel": hotel_orders,
                "rental": rental_orders}
PRODUCT_TYPES = {"flight": "flight", "hotel": "hotel_product",
                 "rental": "rental_product"}
PRODUCT_KEYS = {"flight": "flight_id", "hotel": "product_id",
                "rental": "product_id"}
ORDINALS = {"一": 0, "二": 1, "三": 2, "1": 0, "2": 1, "3": 2}


def recent_references(db, session_id, current=()):
    rows = db.execute(select(chat_turns.c.references).where(
        chat_turns.c.session_id == session_id,
        chat_turns.c.status == "completed",
    ).order_by(chat_turns.c.started_at.desc()).limit(4)).scalars().all()
    return [list(current)] + [row or [] for row in rows]


def _shown(groups, reference_type):
    for group in groups:
        candidates = [item for item in group if isinstance(item, dict)
                      and item.get("type") == reference_type]
        if candidates:
            return candidates
    return []


def _ordinal(text, kind):
    noun = {"flight": "航班|班机|班次|班", "hotel": "酒店|房间|房型|家",
            "rental": "租车|车辆|车|辆"}[kind]
    match = re.search(rf"第\s*([一二三123])\s*(?:个\s*)?(?:{noun}|个)", text)
    return ORDINALS[match.group(1)] if match else None


def _explicit_id(text, kind, candidate_id):
    if kind == "flight":
        return re.search(rf"(?:SIM-|航班\s*ID\s*[:：]?\s*){candidate_id}\b",
                         text, re.IGNORECASE) is not None
    return isinstance(candidate_id, str) and len(candidate_id) > 5 and candidate_id in text


def _product_choice(kind, proposed, groups, text):
    candidates = _shown(groups, PRODUCT_TYPES[kind])
    if not candidates:
        if proposed is not None and _explicit_id(text, kind, proposed):
            return proposed
        raise SelectionNeeded("请先查询并明确选择具体候选。")
    ids = [item["id"] for item in candidates]
    if proposed is not None and proposed not in ids:
        raise SelectionNeeded("该产品不在当前候选中，请重新查询并选择。")
    ordinal = _ordinal(text, kind)
    if ordinal is not None:
        if ordinal >= len(ids):
            raise SelectionNeeded("所选序号不在当前候选中，请重新选择。")
        choice = ids[ordinal]
    elif kind in ("hotel", "rental") and re.search(r"最贵|贵的那个|价格最高|最便宜|便宜的那个|价格最低", text):
        marked = [item for item in candidates if item.get("extreme_unique")]
        if len(marked) != 1:
            raise SelectionNeeded("最高或最低价格的产品尚未唯一确定，请选择具体房型或车辆。")
        choice = marked[0]["id"]
    elif kind == "flight" and "最早" in text:
        marked = [item for item in candidates if item.get("earliest_unique")]
        if len(marked) != 1:
            raise SelectionNeeded("最早出发班次尚未唯一确定，请重新查询并指定航班。")
        choice = marked[0]["id"]
    elif len(ids) == 1 and re.search(r"这个|这班|这家|这辆|前面|刚才|上面|那个|那班|那家|就它|就这", text):
        choice = ids[0]
    elif "最便宜" in text:
        priced = [item for item in candidates if type(item.get("price")) in (int, float)]
        if len(priced) != len(candidates):
            raise SelectionNeeded("候选价格不完整，请指定具体产品。")
        choice = min(priced, key=lambda item: (item["price"], str(item["id"])))['id']
    elif kind == "hotel" and ("最近" in text or "距离最近" in text):
        located = [item for item in candidates if item.get("distance_km") is not None]
        if len(located) != len(candidates):
            raise SelectionNeeded("酒店距离缺少可靠坐标，请指定具体产品。")
        choice = min(located, key=lambda item: (item["distance_km"], str(item["id"])))['id']
    elif proposed is not None and _explicit_id(text, kind, proposed):
        choice = proposed
    else:
        raise SelectionNeeded("有多个候选，请明确选择班次或产品。")
    if proposed is not None and str(proposed) != str(choice):
        raise SelectionNeeded("所选候选与请求不一致，请重新指定。")
    return choice


def _compatible_candidate(kind, choice, groups, text, today):
    candidates = _shown(groups, PRODUCT_TYPES[kind])
    item = next((item for item in candidates if str(item.get("id")) == str(choice)), None)
    if item is None:
        return None
    days = {day.isoformat() for day in mentioned_dates(text, today)} if today else set()
    if kind == "flight":
        if days and (item.get("travel_date") not in days and not (
                item.get("earliest_unique") and "最早" in text
                and len(days) == 1 and item.get("travel_date") >= next(iter(days)))):
            raise SelectionNeeded("所选航班与本轮日期不一致，请重新查询。")
        places = mentioned_cities(text)
        allowed = {item.get(key) for key in ("departure", "arrival", "departure_airport", "arrival_airport")}
        if any(place not in allowed for place in places):
            raise SelectionNeeded("所选航班与本轮地点不一致，请重新查询。")
    if kind == "hotel" and days and item.get("check_in") and item["check_in"] not in days:
        raise SelectionNeeded("所选酒店与本轮入住日期不一致，请重新查询。")
    if kind == "rental":
        candidate_days = {item.get("pickup_date"), item.get("drop_off_date")}
        if days and not days.issubset(candidate_days):
            raise SelectionNeeded("所选车辆与本轮取还日期不一致，请重新查询。")
        places = mentioned_cities(text)
        if any(place not in {item.get("city"), item.get("airport")} for place in places):
            raise SelectionNeeded("所选车辆与本轮地点不一致，请重新查询。")
    return item


def _owned_order(db, traveler_id, kind, proposed, text, *, all_cancellations=False):
    table = ORDER_TABLES[kind]
    rows = db.execute(select(table).where(
        table.c.traveler_id == traveler_id, table.c.status == "confirmed",
    )).mappings().all()
    if not rows:
        raise SelectionNeeded("本人没有可办理的有效订单。")
    if all_cancellations:
        selected = next((row for row in rows if row["id"] == proposed), None)
        if selected is None:
            raise SelectionNeeded("未找到可办理的本人订单，请刷新行程。")
        return selected
    matches = matching_orders(db, traveler_id, kind, text, rows)
    if not matches:
        raise SelectionNeeded("没有找到符合你描述的本人有效订单，请核对地点或订单号。")
    if len(matches) > 1:
        from services.travel import itinerary
        details = {row["id"]: row for row in itinerary(db, traveler_id)[f"{kind}_orders"]}
        raise SelectionNeeded("找到多笔符合条件的本人订单，请选择其中一笔：" +
                              order_options(kind, [details.get(row["id"], row) for row in matches]))
    chosen = matches[0]
    if proposed is not None and proposed != chosen["id"]:
        raise SelectionNeeded("模型建议的订单与你描述的不一致，请重新核对目标。")
    return next(row for row in rows if row["id"] == chosen["id"])


def _date_grounded(value, text, today):
    try:
        target = date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if value in text or f"{target.year}年{target.month}月{target.day}日" in text:
        return True
    if f"{target.month}月{target.day}日" in text:
        return True
    for word, days in (("今天", 0), ("明天", 1), ("后天", 2)):
        if word in text and target == today + timedelta(days=days):
            return True
    return False


def _dates_grounded(action_type, params, text, today, candidate=None, grounded=None):
    if action_type.startswith("hotel_") and action_type != "hotel_cancel":
        start, end = params.get("check_in"), params.get("check_out")
        if not (_date_grounded(start, text, today) or
                candidate and candidate.get("check_in") == start):
            return False
        if (_date_grounded(end, text, today) or
                candidate and candidate.get("check_out") == end):
            return True
        try:
            return ("一晚" in text and date.fromisoformat(end)
                    == date.fromisoformat(start) + timedelta(days=1))
        except (TypeError, ValueError):
            return False
    if action_type.startswith("rental_") and action_type != "rental_cancel":
        grounded = grounded if isinstance(grounded, dict) and grounded.get("intent") == "rental_search" else {}
        for field in ("pickup_at", "dropoff_at"):
            value = params.get(field)
            if not isinstance(value, str) or "T" not in value:
                return False
            day, time = value.split("T", 1)
            prefix = "pickup" if field == "pickup_at" else "dropoff"
            context_day = grounded.get("pickup_date" if prefix == "pickup" else "drop_off_date")
            context_clock = grounded.get(f"{prefix}_clock")
            if ((context_day and context_day != day)
                    or (context_clock and context_clock != time[:5])):
                return False
            from_context = context_day == day and context_clock == time[:5]
            if not _date_grounded(day, text, today) and not from_context:
                return False
            hour, minute = time[:2], time[3:5]
            if not hour.isdigit() or not minute.isdigit():
                return False
            clock = (time[:5] in text or f"{int(hour)}点{int(minute)}" in text
                     or (minute == "00" and f"{int(hour)}点" in text))
            if not clock and not from_context:
                return False
    return True


def prepare_actions(db, traveler_id, plans, references, user_text,
                    context_text=None, today=None, grounded_context=None,
                    bulk_cancel=False):
    if not isinstance(plans, list) or not 1 <= len(plans) <= 2:
        raise SelectionNeeded("请说明要办理的一到两笔具体交易。")
    if re.search(r"改签.+(?:并|同时|再).*(?:订|预订).*(?:酒店|房)", user_text):
        if {item.get("action_type") for item in plans if isinstance(item, dict)} != {
                "flight_change", "hotel_book"}:
            raise SelectionNeeded("机票改签和酒店预订需同时明确，才能一起生成两张待确认卡。")
    context_text = context_text or user_text
    resolved = []
    for item in plans:
        if not isinstance(item, dict) or set(item) != {"action_type", "parameters"}:
            raise SelectionNeeded("交易内容不完整，请重新说明需求。")
        action_type, params = item["action_type"], item["parameters"]
        if action_type not in FIELDS or not isinstance(params, dict):
            raise SelectionNeeded("该交易类型暂不可用。")
        if set(params) - FIELDS[action_type]:
            raise SelectionNeeded("交易参数包含未核实的字段，请重新说明需求。")
        params = dict(params)
        kind, operation = action_type.split("_", 1)
        if operation in ("change", "cancel"):
            order = _owned_order(db, traveler_id, kind, params.get("order_id"),
                                 user_text, all_cancellations=bulk_cancel)
            params["order_id"] = order["id"]
            params["expected_version"] = order["version"]
        if operation == "book" or (kind in ("flight", "hotel") and operation == "change"):
            key = "new_flight_id" if action_type == "flight_change" else PRODUCT_KEYS[kind]
            if action_type == "hotel_change" and params.get(key) is None:
                params[key] = order["product_id"]
            else:
                params[key] = _product_choice(kind, params.get(key), references, user_text)
            candidate = _compatible_candidate(kind, params[key], references, user_text, today)
            if kind == "hotel" and candidate:
                for date_key in ("check_in", "check_out"):
                    shown_date = candidate.get(date_key)
                    if shown_date is not None and params.get(date_key) not in (None, shown_date):
                        raise SelectionNeeded("酒店日期与当前房型候选不一致，请重新查询。")
                params.setdefault("check_in", candidate.get("check_in"))
                params.setdefault("check_out", candidate.get("check_out"))
        else:
            candidate = None
        if (kind == "rental" and operation in ("book", "change")
                and isinstance(grounded_context, dict)
                and grounded_context.get("intent") == "rental_search"):
            for field, day_key, clock_key in (("pickup_at", "pickup_date", "pickup_clock"),
                                              ("dropoff_at", "drop_off_date", "dropoff_clock")):
                day, clock = grounded_context.get(day_key), grounded_context.get(clock_key)
                if day and clock:
                    params.setdefault(field, f"{day}T{clock}")
        if today is not None and not _dates_grounded(action_type, params,
                                                     context_text, today, candidate,
                                                     grounded_context):
            raise SelectionNeeded("请明确酒店日期或租车的取还车日期和当地时刻。")
        if set(params) != FIELDS[action_type]:
            raise SelectionNeeded("交易日期、时刻或产品信息不完整，请补充后再生成确认卡。")
        resolved.append((action_type, params))
    return resolved


def verify_shown_quotes(db, resolved, references):
    """Reject a changed quote instead of silently presenting a different price."""
    tables = {"flight": (flight_offers, "flight_id", "amount"),
              "hotel": (hotel_products, "id", "nightly_price"),
              "rental": (rental_products, "id", "daily_price")}
    for action_type, params in resolved:
        kind, operation = action_type.split("_", 1)
        if operation not in ("book", "change") or action_type == "rental_change":
            continue
        key = "new_flight_id" if action_type == "flight_change" else PRODUCT_KEYS[kind]
        target = params.get(key)
        shown = next((item for item in _shown(references, PRODUCT_TYPES[kind])
                      if str(item.get("id")) == str(target)), None)
        if shown is None:
            continue  # An explicit identifier is checked by create_proposal.
        table, id_field, price_field = tables[kind]
        row = db.execute(select(table).where(table.c[id_field] == target)).mappings().one_or_none()
        if (row is None or row[price_field] != shown.get("price") or
                kind == "flight" and row["version"] != shown.get("offer_version")):
            raise SelectionNeeded("候选报价已变化，请重新查询后再确认。")
