"""Small, bounded conversation context for traveler read requests."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from services.location_labels import AIRPORT_NAMES, CITY_ALIASES, EXTRA_CITY_ALIASES


_ALIASES = {**CITY_ALIASES, **EXTRA_CITY_ALIASES}
_RELATIVE = re.compile(r"大后天|后天|明天|今天")
_OFFSETS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_ABSOLUTE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def local_today() -> date:
    """Traveler-facing relative dates use the current China calendar day."""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def mentioned_cities(text: str) -> list[str]:
    """Return canonical cities in message order, including known airport names."""
    found = []
    for alias, city in _ALIASES.items():
        for match in re.finditer(re.escape(alias), text, re.I):
            found.append((match.start(), -len(alias), city))
    for code, name in AIRPORT_NAMES.items():
        if name in text:
            found.append((text.index(name), -len(name), code))
    for match in re.finditer(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", text):
        found.append((match.start(), -3, match.group()))
    for city in set(_ALIASES.values()):
        for match in re.finditer(rf"(?<![A-Za-z]){re.escape(city)}(?![A-Za-z])", text, re.I):
            found.append((match.start(), -len(city), city))
    found.sort()
    result = []
    occupied_until = -1
    for start, negative_length, city in found:
        if start < occupied_until:
            continue
        occupied_until = start - negative_length
        if not result or result[-1] != city:
            result.append(city)
    return result


def mentioned_dates(text: str, base: date) -> list[date]:
    found = []
    for match in _ABSOLUTE.finditer(text):
        try:
            found.append((match.start(), date(*map(int, match.groups()))))
        except ValueError:
            continue
    for match in _MONTH_DAY.finditer(text):
        try:
            candidate = date(base.year, int(match.group(1)), int(match.group(2)))
            if candidate < base - timedelta(days=2):
                candidate = candidate.replace(year=base.year + 1)
            found.append((match.start(), candidate))
        except ValueError:
            continue
    for match in _RELATIVE.finditer(text):
        found.append((match.start(), base + timedelta(days=_OFFSETS[match.group()])))
    return [value for _, value in sorted(found)]


def read_request(text: str, intent: str, params: dict, previous: dict,
                 base: date) -> tuple[str, dict, dict]:
    """Normalize only read paths. Transaction parameters remain guarded elsewhere."""
    previous = previous if isinstance(previous, dict) else {}
    places, dates = mentioned_cities(text), mentioned_dates(text, base)
    direct_route = bool(re.search(r"(?:从|由).{1,35}(?:到|去|飞往).{1,35}", text)) and len(places) >= 2
    weather = bool(re.search(r"天气|气温|降雨|下雨|晴天", text))
    capability = bool(re.search(r"联网|实时查询|能上网|能搜索", text)) and not weather
    hotel_coverage = bool(re.search(r"(?:哪[个些边]|哪些).{0,12}机场.{0,8}酒店|机场.{0,8}(?:有|附近).{0,5}酒店", text))
    hotel = bool(re.search(r"酒店|住宿|住哪|订房", text))
    flight = bool(re.search(r"(?:查|找|搜索|有没有).{0,40}(?:航班|机票)|"
                            r"(?:航班|机票).{0,8}(?:查询|搜索)", text))
    recommendation = bool(re.search(r"推荐|旅游|旅行|好看|好玩|去哪", text))
    if weather:
        city = places[-1] if places else previous.get("city")
        return "weather", {"city": city, "target_date": (dates[0].isoformat() if dates else None)}, previous
    if capability:
        return "capabilities", {}, previous
    if hotel_coverage:
        known_city = previous.get("city") or previous.get("source_city") or previous.get("arrival")
        return "hotel_coverage", {"city": places[-1] if places else known_city}, previous
    place_question = bool(re.search(r"景点|经典地点|游览|地点", text))
    if (recommendation and (not hotel or place_question) and not flight and not direct_route
            or intent == "recommendation" and place_question):
        source = places[0] if places and re.search(r"我(?:人)?(?:现在)?(?:在|住在)|我从", text) else previous.get("source_city")
        context = {"intent": "recommendation", "source_city": source}
        return "recommendation", {**params, "source_city": source}, context
    combined = (hotel and (flight or direct_route) and not re.search(
        r"预订|取消|改签|改期|订酒店|订房|订票", text))
    continuing_multi = (previous.get("intent") == "multi_read" and (places or dates)
                        and not (hotel or flight or direct_route or recommendation)
                        and not re.search(r"天气|租车|订单|行程|政策|取消|预订|改签|改期", text))
    if combined or continuing_multi:
        context = dict(previous) if previous.get("intent") == "multi_read" else {"intent": "multi_read"}
        if len(places) >= 2:
            context["departure"], context["arrival"] = places[0], places[1]
            context["city"] = places[-1]
        elif places:
            context["city"] = places[-1]
        if dates:
            context["travel_date"] = dates[0].isoformat()
        if len(dates) >= 2:
            context["check_in"], context["check_out"] = (day.isoformat() for day in dates[:2])
        return "multi_read", {"flight": {key: context.get(key) for key in
                                ("departure", "arrival", "travel_date")},
                              "hotel": {key: context.get(key) for key in
                                ("city", "check_in", "check_out")}}, context
    if hotel or (previous.get("intent") == "hotel_search" and (places or dates)
                 and not flight and not direct_route):
        context = dict(previous) if previous.get("intent") == "hotel_search" else {"intent": "hotel_search"}
        if places:
            context["city"] = places[-1]
        if len(dates) >= 2:
            context["check_in"], context["check_out"] = (value.isoformat() for value in dates[:2])
        elif dates and "check_in" not in context:
            context["check_in"] = dates[0].isoformat()
        return "hotel_search", context, context
    if direct_route or flight or (previous.get("intent") == "flight_search" and dates):
        context = dict(previous) if previous.get("intent") == "flight_search" else {"intent": "flight_search"}
        if len(places) >= 2:
            context["departure"], context["arrival"] = places[0], places[-1]
        elif places and "departure" not in context:
            context["departure"] = places[0]
        if dates:
            context["travel_date"] = dates[0].isoformat()
        return "flight_search", context, context
    if intent in ("flight_search", "hotel_search"):
        return intent, params, {"intent": intent, **params}
    return intent, params, previous
