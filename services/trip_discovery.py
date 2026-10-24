"""Evidence-backed destination discovery from a stated origin and travel day."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from travel_assistant.db.travel_models import airports, demo_state, flight_offers, flights
from services.demo_pricing import NETWORK_OFFER_RULE_ID, OFFER_RULE_ID
from services.location_labels import amount_zh, city_zh
from services.recommendation_geography import country_for_fixture_city
from services.recommendations import get_recommendation_provider
from services.reviewed_places import places_for_city
from services.weather_search import search_weather


def discover_destinations(db, origin: str, travel_day: date, preference: str | None = None,
                          *, user_text="", provider=None, weather_lookup=None, now=None):
    """Only offered direct flights on the origin's local day can be recommended."""
    if not isinstance(origin, str) or not isinstance(travel_day, date):
        return {"answer": "请说明出发城市和出行日期。", "references": [], "options": []}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    as_of = db.execute(select(demo_state.c.as_of).where(demo_state.c.id == 1)).scalar_one_or_none()
    if as_of is None:
        return {"answer": "当前演示航班日期窗口无法核实。", "references": [], "options": []}
    origins = db.execute(select(airports.c.code, airports.c.city, airports.c.timezone).where(
        (airports.c.city == origin) | (airports.c.code == origin))).all()
    if not origins or len(origins) > 10:
        return {"answer": "无法唯一核实出发城市或机场，请明确出发地。", "references": [], "options": []}
    relative = re.search(r"大后天|后天|明天|今天", user_text)
    if relative:
        offset = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[relative.group()]
        travel_day = now.astimezone(ZoneInfo(origins[0].timezone)).date() + timedelta(days=offset)
    if not as_of + timedelta(days=1) <= travel_day <= as_of + timedelta(days=30):
        return {"answer": f"当前可查询 {as_of + timedelta(days=1)} 至 {as_of + timedelta(days=30)} 出发的模拟航班，请调整日期。",
                "references": [], "options": [], "travel_date": travel_day.isoformat()}
    destination = airports.alias("destination")
    found = {}
    for code, _, zone_name in origins:
        zone = ZoneInfo(zone_name)
        start = datetime.combine(travel_day, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(travel_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        rows = db.execute(select(flights.c.id, flights.c.flight_no,
                                 flights.c.departure_airport, flights.c.arrival_airport,
                                 flights.c.departure_at, flight_offers.c.amount,
                                 flight_offers.c.version, destination.c.city)
                          .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
                          .join(destination, destination.c.code == flights.c.arrival_airport)
                          .where(flights.c.departure_airport == code,
                                 flights.c.status == "Scheduled",
                                 flights.c.departure_at >= max(start, now),
                                 flights.c.departure_at < end,
                                 flight_offers.c.amount > 0,
                                 flight_offers.c.rule_id.in_((OFFER_RULE_ID, NETWORK_OFFER_RULE_ID)))
                          .order_by(flights.c.departure_at, flights.c.id).limit(5001)).mappings().all()
        if len(rows) > 5000:
            return {"answer": "该日期符合条件的航班过多，请指定机场或缩小条件后重试。",
                    "references": [], "options": []}
        for row in rows:
            if row["city"] == origins[0].city:
                continue
            old = found.get(row["city"])
            if old is None or (row["departure_at"], row["id"]) < (old["departure_at"], old["id"]):
                found[row["city"]] = dict(row) | {"departure_timezone": zone_name}
    if not found:
        return {"answer": f"已查询 {travel_day} 从{city_zh(origins[0].city)}出发的所有机场组合；"
                "当天没有可预订的模拟直达班次。你可以调整日期或出发地。",
                "references": [], "options": [], "travel_date": travel_day.isoformat()}
    candidates = []
    for city, row in found.items():
        country = country_for_fixture_city(city)
        try:
            places = places_for_city(city, country["code"], preference) if country else []
        except (OSError, ValueError):
            places = []
        candidates.append((row, places, country))
    candidates.sort(key=lambda item: (not bool(item[1]), item[0]["departure_at"],
                                      item[0]["amount"], item[0]["city"]))
    selected = candidates[:3]
    provider = provider or get_recommendation_provider()
    weather_lookup = weather_lookup or search_weather
    lines, references, options = [], [], []
    for row, places, country in selected:
        city = row["city"]
        if places and country:
            try:
                provider.search(city, preference, country)
            except Exception:
                pass  # Live search only checks freshness; it cannot supply answer facts.
            place = places[0]
            place_text = f"{place['display_name_zh']}：{place['intro']}"
            references.append({"type": "place", "id": place["id"], "place": place["name"],
                               "display_name": place["display_name_zh"], "city": city,
                               "url": place["url"], "reviewed_on": place["reviewed_on"]})
        else:
            place_text = "该城市暂无符合偏好的近期核查地点资料"
        weather_text = "天气尚无法核实"
        if country and country["code"] == "CN":
            try:
                weather = weather_lookup(city, travel_day)
            except Exception:
                weather = None
            if weather:
                weather_text = f"已核实天气：{weather['condition']}，{weather['low']}～{weather['high']}℃"
                references.append({"type": "weather_source", "city": city,
                                   "date": travel_day.isoformat(), "url": weather["url"],
                                   "published_at": weather["published_at"],
                                   "retrieved_at": weather["retrieved_at"]})
        local = row["departure_at"].replace(
            tzinfo=row["departure_at"].tzinfo or timezone.utc).astimezone(
                ZoneInfo(row["departure_timezone"]))
        lines.append(f"{city_zh(city)}：{place_text}；{weather_text}。"
                     f"最早可查班次 {row['flight_no']}，{local.strftime('%Y-%m-%d %H:%M %z')} "
                     f"从{city_zh(origins[0].city)}出发，示例报价 {amount_zh(row['amount'])}。")
        references.append({"type": "flight", "id": row["id"], "offer_version": row["version"],
                           "price": row["amount"], "travel_date": travel_day.isoformat()})
        options.append({"city": city, "flight_id": row["id"]})
    return {"answer": f"{travel_day} 从{city_zh(origins[0].city)}出发，可考虑以下有模拟直达航班的城市：\n" +
            "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1)) +
            "\n请选定目的地，再核对具体班次；你确认机票卡前不会创建订单。",
            "references": references, "options": options,
            "travel_date": travel_day.isoformat()}
