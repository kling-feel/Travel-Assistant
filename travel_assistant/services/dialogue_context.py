"""Small, bounded conversation context for traveler read requests."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from travel_assistant.services.location_labels import AIRPORT_NAMES, CITY_ALIASES, EXTRA_CITY_ALIASES


_ALIASES = {**CITY_ALIASES, **EXTRA_CITY_ALIASES}
_RELATIVE = re.compile(r"大后天|后天|明天|今天")
_OFFSETS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_ABSOLUTE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_WRITE = re.compile(r"预订|预定|下单|订票|订.{0,10}(?:机票|航班|酒店|房|车)|购买|买票|改签|改期|取消")
_NUMBER = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}
_NIGHTS = re.compile(r"(?:住|订|预订|预定)?\s*([一二两三四五六七八九十\d]{1,2})\s*晚")
_CLOCK = re.compile(r"(凌晨|早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*(?:[:：]\s*(\d{2})|点\s*(半|\d{1,2}分?)?)")


def requested_actions(text: str) -> list[str]:
    """Recognize explicit writes without treating policy questions as orders."""
    if (not _WRITE.search(text) or re.search(r"政策|规则|条款|手续费|怎么|如何|取消过|取消记录|取消历史|已取消|历史订单|订单历史|历史行程|行程历史", text)
            or re.search(r"(?:取消|改签|改期).{0,18}(?:多少|几成|几元|费用|收费)", text)
            or re.search(r"不(?:要|想|用)?(?:预订|预定|订票|订房|订车|取消)", text)
            or re.search(r"(?:能|可以).{0,12}(?:取消|改期|改签).{0,2}吗", text)):
        return []
    domains = [("flight", r"机票|航班|班机|班次|订票|(?:最早|最便宜|那|这|前面|刚才).{0,4}班"),
               ("hotel", r"酒店|房间|房型|住宿|订房"),
               ("rental", r"租车|车辆|车子|辆车|台车|订车")]
    actions = []
    segments = re.split(r"[，,；;]|并且|同时|顺便|然后|再|和(?=订|预订|预定|取消|改期|改签)", text)
    for segment in segments:
        for kind, noun in domains:
            if not re.search(noun, segment):
                continue
            if re.search(r"取消", segment) and not re.search(r"取消.{0,8}查询", segment):
                operation = "cancel"
            elif kind == "flight" and re.search(r"改签", segment):
                operation = "change"
            elif kind != "flight" and re.search(r"改期|修改", segment):
                operation = "change"
            elif re.search(r"预订|预定|下单|订票|订.{0,10}(?:机票|航班|酒店|房|车)|购买|买票", segment):
                operation = "book"
            else:
                continue
            action = f"{kind}_{operation}"
            if action not in actions:
                actions.append(action)
    return actions


def flight_followup(text: str, previous: dict) -> bool:
    """A short flight follow-up may resume its own task after another read topic."""
    if not isinstance(previous, dict) or previous.get("intent") != "flight_search":
        return False
    if re.search(r"天气|景点|游览|推荐|酒店|住宿|租车|车辆|辆车|台车|取车|还车|订车|政策|规则|取消|改签", text):
        return False
    return bool(re.search(r"机票|航班|班次|起飞|飞往|出发|最早|这班|那班|前面|刚才", text)
                or mentioned_cities(text) and re.search(r"(?:想|要|打算)?去|(?:从|在).{0,16}出发", text))


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
        if match.group() == "SIM" and text[match.end():match.end() + 1] == "-":
            continue
        if re.search(r"(?:rental|room)-$", text[:match.start()], re.I):
            continue
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


def declared_current_city(text: str) -> str | None:
    """A location claim must be made by the traveler, not inferred from a route."""
    match = re.search(r"我(?:人)?(?:现在|目前)?(?:就)?在|我住在", text)
    if not match:
        return None
    places = mentioned_cities(text[match.end():match.end() + 35])
    return places[0] if places else None


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


def stay_nights(text: str, dates: list[date]) -> int | None:
    match = _NIGHTS.search(text)
    if match:
        raw = match.group(1)
        value = int(raw) if raw.isdigit() else _NUMBER.get(raw)
        return value if value is not None and 1 <= value <= 14 else None
    if len(dates) >= 2 and re.search(r"(?:都|全部).{0,4}住|住.{0,4}(?:都|全部)", text):
        value = (dates[1] - dates[0]).days + 1
        return value if 1 <= value <= 14 else None
    return None


def rental_clocks(text: str) -> dict[str, str]:
    """Extract only user-written wall times, never infer an airport offset."""
    result = {}
    for segment in re.split(r"[，,；;、]", text):
        found = list(_CLOCK.finditer(segment))
        for index, match in enumerate(found):
            period, raw_hour, raw_minute, after_point = match.groups()
            hour = int(raw_hour)
            minute = (int(raw_minute) if raw_minute is not None else
                      30 if after_point == "半" else
                      int(after_point.rstrip("分")) if after_point and after_point != "半" else 0)
            if not 0 <= minute < 60 or not 0 <= hour <= 23:
                continue
            if period in ("下午", "晚上") and hour < 12:
                hour += 12
            elif period == "凌晨" and hour == 12:
                hour = 0
            role = ("dropoff" if "还" in segment and "取" not in segment else
                    "pickup" if "取" in segment and "还" not in segment else
                    "pickup" if index == 0 and len(found) >= 2 else
                    "dropoff" if index == 1 and len(found) >= 2 else None)
            if role:
                result[f"{role}_clock"] = f"{hour:02d}:{minute:02d}"
    return result


def read_request(text: str, intent: str, params: dict, previous: dict,
                 base: date) -> tuple[str, dict, dict]:
    """Normalize only read paths. Transaction parameters remain guarded elsewhere."""
    previous = previous if isinstance(previous, dict) else {}
    places, dates = mentioned_cities(text), mentioned_dates(text, base)
    abandoned = re.search(r"不去(.{1,20}?)(?:了|，|,|；|;|改成)", text)
    if abandoned:
        discarded = set(mentioned_cities(abandoned.group(1)))
        if discarded:
            places = [place for place in places if place not in discarded]
    direct_route = bool(re.search(r"(?:从|由).{1,35}(?:到|去|飞往).{1,35}", text)) and len(places) >= 2
    weather = bool(re.search(r"天气|气温|降雨|下雨|晴天", text))
    capability = bool(re.search(r"联网|实时查询|能上网|能搜索", text)) and not weather
    hotel_coverage = bool(re.search(r"(?:哪[个些边]|哪些).{0,12}机场.{0,8}酒店|机场.{0,8}(?:有|附近).{0,5}酒店", text))
    hotel = bool(re.search(r"酒店|住宿|住哪|订房", text))
    nights = stay_nights(text, dates)
    rank = ("max" if re.search(r"最贵|贵的那个|价格最高", text) else
            "min" if re.search(r"最便宜|便宜的那个|价格最低", text) else None)
    flight_booking = "flight_book" in requested_actions(text)
    flight = flight_booking or bool(re.search(r"(?:查|找|搜索|有没有).{0,40}(?:航班|机票)|"
                                       r"(?:航班|机票).{0,8}(?:查询|搜索)", text))
    earliest = bool(re.search(r"最早|最近.{0,8}(?:能|可).{0,4}(?:走|飞|出发|起飞)|"
                              r"(?:什么时候|何时).{0,8}(?:能|可)?.{0,4}(?:走|飞|出发|起飞)", text))
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
    combined = (hotel and (flight or direct_route) and not requested_actions(text))
    continuing_multi = (previous.get("intent") == "multi_read" and (places or dates)
                        and not (hotel or flight or direct_route or recommendation)
                        and not re.search(r"天气|租车|订单|行程|政策|取消|预订|预定|改签|改期", text))
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
    rental = bool(re.search(r"租车|取车|还车|车辆|辆车|台车|租.{0,8}车", text))
    if previous.get("intent") == "rental_search" and rank and "车" in text:
        rental = True
    clocks = rental_clocks(text) if (rental or previous.get("intent") == "rental_search") else {}
    if rental or (previous.get("intent") == "rental_search" and (places or dates or clocks or rank)
                  and not hotel and not flight and not direct_route):
        context = dict(previous) if previous.get("intent") == "rental_search" else {"intent": "rental_search"}
        if places:
            if context.get("city") != places[-1]:
                for key in ("pickup_date", "drop_off_date", "pickup_clock", "dropoff_clock"):
                    context.pop(key, None)
            context["city"] = places[-1]
        if len(dates) >= 2:
            context["pickup_date"], context["drop_off_date"] = (value.isoformat() for value in dates[:2])
        elif dates:
            if "还" in text and "取" not in text:
                context["drop_off_date"] = dates[0].isoformat()
            elif "取" in text or "pickup_date" not in context:
                context["pickup_date"] = dates[0].isoformat()
        context.update(clocks)
        if re.search(r"(?:租|用|要)?\s*(?:两|二|2)\s*天", text):
            context["duration_hours"] = 48
        if rank:
            context["rank"] = rank
        return "rental_search", context, context
    if hotel or (previous.get("intent") == "hotel_search" and (places or dates or nights or rank)
                 and not rental and not flight and not direct_route):
        context = dict(previous) if previous.get("intent") == "hotel_search" else {"intent": "hotel_search"}
        if places:
            if context.get("city") != places[-1]:
                for key in ("check_in", "check_out", "nights"):
                    context.pop(key, None)
            context["city"] = places[-1]
        context.pop("date_conflict", None)
        if len(dates) >= 2:
            context["check_in"], context["check_out"] = (value.isoformat() for value in dates[:2])
            if nights is None:
                context["nights"] = (dates[1] - dates[0]).days
        elif dates:
            if "退房" in text and context.get("check_in"):
                context["check_out"] = dates[0].isoformat()
                if nights is None:
                    context["nights"] = (dates[0] - date.fromisoformat(context["check_in"])).days
            else:
                context["check_in"] = dates[0].isoformat()
        if nights is not None:
            context["nights"] = nights
        if context.get("nights") and context.get("check_in"):
            derived = (date.fromisoformat(context["check_in"]) +
                       timedelta(days=context["nights"])).isoformat()
            explicit_checkout = (len(dates) >= 2 and "退房" in text or
                                 len(dates) == 1 and "退房" in text)
            if explicit_checkout and context.get("check_out") != derived:
                context["date_conflict"] = True
            else:
                context["check_out"] = derived
        if rank:
            context["rank"] = rank
        return "hotel_search", context, context
    if (direct_route or flight or earliest and bool(re.search(r"航班|机票|班次|起飞", text))
            or previous.get("intent") == "flight_search"
            and (dates or earliest or flight_followup(text, previous))):
        context = dict(previous) if previous.get("intent") == "flight_search" else {"intent": "flight_search"}
        sources = dict(context.get("_sources") or {})
        old_route = (context.get("departure"), context.get("arrival"))
        if len(places) >= 2:
            if old_route != (places[0], places[-1]):
                for key in ("travel_date", "not_before", "sort", "_goal"):
                    context.pop(key, None)
                for key in ("travel_date", "not_before"):
                    sources.pop(key, None)
            context["departure"], context["arrival"] = places[0], places[-1]
            sources.update(departure="user", arrival="user")
        elif places:
            departure = bool(re.search(r"我(?:人)?(?:现在|目前)?(?:就)?在|我住在|从|由|出发", text))
            arrival = bool(re.search(r"(?:想|要|打算|准备)?去|到|飞往|目的地", text))
            field = ("departure" if departure and not arrival else
                     "arrival" if arrival and not departure else
                     "departure" if not context.get("departure") and context.get("arrival") else
                     "arrival" if not context.get("arrival") and context.get("departure") else None)
            if field:
                if context.get(field) not in (None, places[0]):
                    for key in ("travel_date", "not_before", "sort", "_goal"):
                        context.pop(key, None)
                    for key in ("travel_date", "not_before"):
                        sources.pop(key, None)
                context[field] = places[0]
                sources[field] = "user"
        if dates:
            context["travel_date"] = dates[0].isoformat()
            sources["travel_date"] = "user"
        if earliest:
            context["sort"] = "earliest"
            context["not_before"] = context.get("travel_date")
            if dates:
                sources["not_before"] = "user"
        elif dates or (len(places) >= 2 and old_route != (places[0], places[-1])):
            context.pop("sort", None)
            context.pop("not_before", None)
        if flight_booking:
            context["_goal"] = "book"
        context["_sources"] = sources
        return "flight_search", context, context
    if intent in ("flight_search", "hotel_search"):
        return intent, params, {"intent": intent, **params}
    return intent, params, previous
