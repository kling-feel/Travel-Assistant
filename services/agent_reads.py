"""Bounded, traveler-scoped evidence for the read-only conversation graph."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.travel_models import airports, chat_turns, demo_state, flight_offers, flights, hotel_products, hotels
from services.actions import ActionNotFound, _hotel_today, get_proposal
from services.location_labels import airport_zh, amount_zh, city_zh, product_zh, room_zh
from services.location_labels import CITY_ALIASES, EXTRA_CITY_ALIASES
from services.weather_search import WeatherSearchError, search_weather
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


def _city(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return {**CITY_ALIASES, **EXTRA_CITY_ALIASES}.get(value, value)


def _airports_for(db, value):
    value = _city(value)
    if not value:
        return []
    return db.execute(select(airports.c.code).where(
        (airports.c.city == value) | (airports.c.code == value.upper())
    ).order_by(airports.c.code).limit(5)).scalars().all()


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
        if any(word in question for word in ("现在", "还有", "要出发", "出发吗")):
            now = datetime.now(timezone.utc)
            active = [item for item in rows["flight_orders"] if item["status"] == "confirmed"
                      and item["departure_at"].replace(tzinfo=item["departure_at"].tzinfo or timezone.utc) > now]
            if not any(word in question for word in ("机票", "航班", "出发")):
                active_hotels = [item for item in rows["hotel_orders"] if item["status"] == "confirmed"
                                 and item["check_out"] > _hotel_today(db, item["product_id"], now)]
                active_rentals = [item for item in rows["rental_orders"] if item["status"] == "confirmed"
                                  and item["dropoff_at"].replace(tzinfo=item["dropoff_at"].tzinfo or timezone.utc) > now]
                details = ([f"机票 {item['flight_no']}，起飞 {_local_time(item['departure_at'], _airport(db, item['departure_airport'])[1])}"
                            for item in active[:3]] +
                           [f"酒店 {product_zh(item['name'], city=item['city'])}，{item['check_in']} 入住、{item['check_out']} 退房"
                            for item in active_hotels[:3]] +
                           [f"租车 {product_zh(item['name'], airport=item['airport_code'])}，取车 {_local_time(item['pickup_at'], item['airport_timezone'])}"
                            for item in active_rentals[:3]])
                cancelled = sum(item["status"] == "cancelled" for kind in
                                ("flight_orders", "hotel_orders", "rental_orders") for item in rows[kind])
                history_note = f"另有 {cancelled} 笔已取消订单在历史记录中。" if cancelled else ""
                answer = ("目前有效行程：" + "；".join(details) + "。") if details else "目前没有进行中或未来的已确认行程。"
                references = [{"type": "order", "id": item["id"], "kind": kind,
                               "version": item["version"], "status": item["status"]}
                              for kind, items in (("flight_orders", active),
                                                  ("hotel_orders", active_hotels),
                                                  ("rental_orders", active_rentals))
                              for item in items[:3]]
                return _result(answer + history_note, references)
            if not active:
                cancelled = sum(item["status"] == "cancelled" for item in rows["flight_orders"])
                note = f"；另有 {cancelled} 笔已取消机票留在历史记录中" if cancelled else ""
                return _result(f"目前没有未来待出发的已确认机票{note}。",
                               [{"type": "order", "id": item["id"], "kind": "flight_orders",
                                 "version": item["version"], "status": item["status"]}
                                for item in rows["flight_orders"][:5]])
            details = "；".join(
                f"{_airport(db, item['departure_airport'])[0]} → {_airport(db, item['arrival_airport'])[0]}，"
                f"航班 {item['flight_no']}，起飞 {_local_time(item['departure_at'], _airport(db, item['departure_airport'])[1])}"
                for item in active[:3])
            return _result(f"目前有 {len(active)} 笔未来待出发的已确认机票：{details}。",
                           [{"type": "order", "id": item["id"], "kind": "flight_orders",
                             "version": item["version"], "status": item["status"]}
                            for item in active[:3]])
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
        departure = _as_text(params.get("departure"))
        arrival = _as_text(params.get("arrival"))
        travel_date = _as_date(params.get("travel_date"))
        missing = [label for label, value in (("出发地", departure), ("目的地", arrival),
                                              ("出行日期", travel_date)) if not value]
        if missing:
            known = "、".join(((_airport(db, value)[0] if len(value) == 3 else city_zh(_city(value))))
                             for value in (departure, arrival) if value)
            prefix = f"已记下 {known}。" if known else ""
            return _result(prefix + "还需要" + "、".join(missing) + "，才能查询航班。")
        departures, arrivals = _airports_for(db, departure), _airports_for(db, arrival)
        if not departures or not arrivals:
            return _result("当前航班网络无法识别该出发地或目的地；请提供可达城市或机场名称。")
        today = demo_date(db)
        if not today + timedelta(days=1) <= travel_date <= today + timedelta(days=30):
            return _result(f"当前可查的出发日期为 {today + timedelta(days=1)} 至 {today + timedelta(days=30)}；"
                           f"你指定的是 {travel_date}。")
        sort = "price" if params.get("sort") == "price" else "departure"
        try:
            rows = [row for dep in departures for arr in arrivals
                    for row in find_flights(db, dep, arr, travel_date, limit=3, sort=sort)]
        except OrderError:
            return _result("航班查询条件无效，请核对出发地、目的地和日期。")
        if not rows:
            route = db.execute(select(flights.c.id).join(
                flight_offers, flight_offers.c.flight_id == flights.c.id).where(
                flights.c.departure_airport.in_(departures),
                flights.c.arrival_airport.in_(arrivals), flights.c.status == "Scheduled"
            ).limit(1)).first()
            if not route:
                return _result(f"当前模拟航班网络没有 {city_zh(_city(departure))} 至 {city_zh(_city(arrival))} 的直达航线。")
            future = db.execute(select(flights.c.departure_airport, flights.c.departure_at).join(
                flight_offers, flight_offers.c.flight_id == flights.c.id).where(
                flights.c.departure_airport.in_(departures),
                flights.c.arrival_airport.in_(arrivals), flights.c.status == "Scheduled",
                flights.c.departure_at >= datetime.combine(
                    travel_date - timedelta(days=1), time.min, timezone.utc)
            ).order_by(flights.c.departure_at).limit(100)).all()
            nearest = []
            for code, stamp in future:
                zone = _airport(db, code)[1]
                local_day = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(
                    ZoneInfo(zone)).date()
                if travel_date < local_day <= today + timedelta(days=30) and local_day not in nearest:
                    nearest.append(local_day)
                if len(nearest) == 2:
                    break
            suggestion = f"；最近可查日期为 {'、'.join(map(str, nearest))}" if nearest else ""
            return _result(f"已查询 {travel_date} 从 {city_zh(_city(departure))} 到 {city_zh(_city(arrival))}"
                           f"的机场组合，该日没有可预订的直达班次{suggestion}。")
        rows.sort(key=(lambda row: (row["demo_fare_amount"], row["departure_at"]))
                  if sort == "price" else (lambda row: row["departure_at"]))
        rows = rows[:3]
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
        airport = _as_text(params.get("airport"), 8)
        city = _city(_as_text(params.get("city")))
        if not city and airport:
            city = _airport(db, airport.upper())[2] or None
        check_in = _as_date(params.get("check_in"))
        check_out = _as_date(params.get("check_out"))
        missing = [label for label, value in (("城市", city), ("入住日期", check_in),
                                              ("退房日期", check_out)) if not value]
        if missing:
            known = f"已记下 {city_zh(city)}。" if city else ""
            missing_text = "和".join(missing) if len(missing) == 2 else "、".join(missing)
            return _result(known + "还需要" + missing_text + "，才能查询酒店。")
        sort = "distance" if airport and params.get("sort") == "distance" else "price"
        try:
            rows = find_hotels(db, city, check_in, check_out, airport=airport,
                               sort=sort, limit=3)
        except OrderError:
            return _result("酒店查询条件无效，请核对城市和日期。")
        if not rows:
            count = db.execute(select(hotels.c.id).join(
                hotel_products, hotel_products.c.hotel_id == hotels.c.id).where(
                hotels.c.city == city).limit(1)).first()
            reason = "该日期没有可用房型" if count else "当前模拟网络未提供该城市的酒店产品"
            return _result(f"已查询 {city_zh(city)} {check_in} 入住、{check_out} 退房：{reason}。")
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
        city = _city(_as_text(params.get("city")))
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
                for row in rows) + "。如需预订，请选择车辆并提供取车、还车的当地时刻。",
            [{"type": "rental_product", "id": row["id"],
              "price": row["daily_price"], "pickup_date": pickup.isoformat(),
              "drop_off_date": dropoff.isoformat()} for row in rows],
        )

    if intent == "hotel_coverage":
        city = _city(_as_text(params.get("city")))
        query = select(hotels.c.nearby_airport).distinct()
        if city:
            query = query.where(hotels.c.city == city)
        codes = db.execute(query.order_by(hotels.c.nearby_airport)).scalars().all()
        if not codes:
            return _result(f"当前没有查到 {city_zh(city)} 机场周边的酒店产品。" if city else
                           "当前没有可查询的机场周边酒店产品。")
        preferred = ("PVG", "SHA", "LHR", "LGW", "BSL")
        shown = codes[:6] if city else [code for code in preferred if code in codes][:5]
        names = "、".join(_airport(db, code)[0] for code in shown)
        area = f"{city_zh(city)}的" if city else "部分"
        count = f"共 {len(codes)} 个机场；" if not city else ""
        return _result(f"当前模拟酒店覆盖{area}机场，{count}例如：{names}。"
                       "告诉我入住和退房日期，我可以查询具体房型。")

    if intent == "capabilities":
        return _result("我可以联网检索有来源的经典地点和天气预报；"
                       "航班、酒店、租车候选与订单来自本平台的模拟数据库，"
                       "不能把联网网页当作实时机票或酒店库存。")

    if intent == "weather":
        city = _city(_as_text(params.get("city")))
        target = _as_date(params.get("target_date"))
        if not city or not target:
            return _result("请告诉我天气城市和日期，例如“明天上海天气”；我会尝试联网核对预报。")
        try:
            forecast = search_weather(city, target)
        except WeatherSearchError as exc:
            reasons = {"unconfigured": "联网搜索尚未配置", "coverage": "目前仅支持中国可达城市的天气核验",
                       "date": "目前只能核验未来七天内的天气", "timeout": "联网搜索超时",
                       "limited": "联网搜索已限流", "authentication": "联网搜索认证失败"}
            return _result(f"暂时无法核实 {city_zh(city)} {target} 的天气："
                           f"{reasons.get(str(exc), '搜索服务暂不可用')}。")
        if not forecast:
            return _result(f"已联网搜索，但未找到城市、日期和发布时间均可核对的"
                           f"{city_zh(city)} {target} 预报；我不猜测天气。")
        return _result(f"国家气象中心对 {city_zh(city)} {target} 的预报："
                       f"{forecast['condition']}，约 {forecast['low']}～{forecast['high']}℃。"
                       f"页面发布时间 {_local_time(forecast['published_at'], 'Asia/Shanghai')}；预报可能更新。",
                       [{"type": "weather_source", "city": city, "date": target.isoformat(),
                         **forecast}])

    return _result("该查询暂不可用。")
