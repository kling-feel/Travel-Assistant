"""Sellable direct-flight choices for a country-sized destination request."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from travel_assistant.db.travel_models import airports, demo_state, flight_offers, flights
from services.demo_pricing import NETWORK_OFFER_RULE_ID, OFFER_RULE_ID
from services.location_labels import amount_zh, city_zh
from services.recommendation_geography import airport_geography


COUNTRIES = {
    "中国": "CN", "日本": "JP", "美国": "US", "法国": "FR",
    "英国": "GB", "瑞士": "CH", "韩国": "KR", "加拿大": "CA",
    "意大利": "IT", "阿联酋": "AE",
}


def country_in_text(text: str) -> tuple[str, str] | None:
    return next(((name, code) for name, code in COUNTRIES.items() if name in text), None)


def _utc_now():
    return datetime.now(timezone.utc)


def find_country_flights(db, origin: str, country_code: str, *, not_before: str | None = None,
                         relative_offset: int | None = None,
                         origin_source: str | None = None, limit: int = 5):
    """One earliest offered flight per destination city, globally ordered."""
    if (not isinstance(origin, str) or not origin or country_code not in COUNTRIES.values()
            or type(limit) is not int or not 1 <= limit <= 5
            or relative_offset is not None and relative_offset not in (0, 1, 2, 3)):
        return {"answer": "出发地或目的地国家无法核实，请重新说明。", "options": []}
    try:
        explicit = date.fromisoformat(not_before) if not_before else None
    except (TypeError, ValueError):
        return {"answer": "最早出发日期无法核对，请重新说明日期。", "options": []}
    origins = db.execute(select(airports.c.code, airports.c.city, airports.c.timezone).where(
        (airports.c.city == origin) | (airports.c.code == origin.upper())
    ).order_by(airports.c.code).limit(11)).all()
    if not origins or len(origins) > 10:
        return {"answer": "无法唯一核实出发城市或机场，请明确出发地。", "options": []}
    zones = {row.timezone for row in origins}
    if len(zones) != 1:
        return {"answer": "该出发城市的机场使用不同当地日期，请指定出发机场。", "options": []}
    geography = airport_geography()
    destination_codes = [row.code for row in db.execute(select(
        airports.c.code, airports.c.city, airports.c.timezone)).all()
        if (entry := geography.get(row.code)) and entry["country_code"] == country_code
        and entry["city"] == row.city and entry["timezone"] == row.timezone]
    as_of = db.execute(select(demo_state.c.as_of).where(demo_state.c.id == 1)).scalar_one_or_none()
    if as_of is None:
        return {"answer": "当前演示航班日期窗口无法核实。", "options": []}
    now = _utc_now().astimezone(timezone.utc)
    zone = ZoneInfo(next(iter(zones)))
    local_today = now.astimezone(zone).date()
    first_day = max(as_of + timedelta(days=1), explicit or local_today,
                    local_today + timedelta(days=relative_offset or 0))
    last_day = as_of + timedelta(days=30)
    country_name = next(name for name, code in COUNTRIES.items() if code == country_code)
    window = f"{first_day} 至 {last_day}"
    if first_day > last_day:
        return {"answer": f"可订窗口截至 {last_day}；你要求从 {first_day} 起出发，请调整日期。",
                "options": []}
    if not destination_codes:
        return {"answer": f"当前模拟航班网络没有可核实的{country_name}目的地机场。",
                "options": []}
    start = max(datetime.combine(first_day, time.min, zone).astimezone(timezone.utc), now)
    end = datetime.combine(last_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    destination = airports.alias("country_destination")
    ranked = (select(
        flights.c.id.label("flight_id"), flights.c.flight_no,
        flights.c.departure_at, flights.c.departure_airport,
        destination.c.city.label("arrival_city"),
        flight_offers.c.amount, flight_offers.c.version,
        func.row_number().over(partition_by=destination.c.city,
                               order_by=(flights.c.departure_at, flights.c.id)).label("rank"))
        .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
        .join(destination, destination.c.code == flights.c.arrival_airport)
        .where(flights.c.departure_airport.in_([row.code for row in origins]),
               flights.c.arrival_airport.in_(destination_codes),
               flights.c.status == "Scheduled", flight_offers.c.amount > 0,
               flight_offers.c.rule_id.in_((OFFER_RULE_ID, NETWORK_OFFER_RULE_ID)),
               flights.c.departure_at >= start, flights.c.departure_at < end)
        .subquery())
    rows = db.execute(select(ranked).where(ranked.c.rank == 1)
                      .order_by(ranked.c.departure_at, ranked.c.flight_id)
                      .limit(limit + 1)).mappings().all()
    if not rows:
        return {"answer": f"已查询 {window} 从{city_zh(origins[0].city)}出发、到{country_name}的"
                "模拟直达航班；窗口内没有可预订班次。请调整日期或目的地。",
                "options": [], "not_before": first_day.isoformat()}
    lines, options = [], []
    for index, row in enumerate(rows[:limit], 1):
        departure = row["departure_at"].replace(
            tzinfo=row["departure_at"].tzinfo or timezone.utc).astimezone(zone)
        lines.append(f"{index}. {city_zh(row['arrival_city'])}：最早可售航班 "
                     f"{row['flight_no']}，{departure.strftime('%Y-%m-%d %H:%M %z')} "
                     f"当地出发，参考报价 {amount_zh(row['amount'])}。")
        options.append({"city": row["arrival_city"], "flight_id": row["flight_id"],
                        "price": row["amount"], "offer_version": row["version"]})
    more = "另有其他可售城市，可指定城市继续查询。" if len(rows) > limit else ""
    source = (f"按你先前自述在{city_zh(origins[0].city)}" if origin_source == "self_report"
              else f"按你指定从{city_zh(origins[0].city)}出发")
    return {"answer": f"{source}，我查到 {window} "
            f"到{country_name}的模拟直达航班（仅列每座城市最早一班）：\n" +
            "\n".join(lines) + f"\n{more}请先选择目的地城市；选定后我会重新核对班次，"
            "生成待确认卡之前不会创建订单。",
            "options": options, "not_before": first_day.isoformat()}
