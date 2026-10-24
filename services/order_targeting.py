"""Resolve traveler-described order attributes without trusting a model's order ID."""

import re

from travel_assistant.db.travel_models import airports
from services.dialogue_context import mentioned_cities
from services.location_labels import city_zh, product_zh
from services.travel import itinerary
from sqlalchemy import select


def _place_codes(db, place):
    if not place:
        return set()
    rows = db.execute(select(airports.c.code).where(
        (airports.c.city == place) | (airports.c.code == place))).scalars().all()
    return set(rows)


def _place_city(db, place):
    if len(place) != 3 or not place.isupper():
        return place
    return db.execute(select(airports.c.city).where(
        airports.c.code == place)).scalar_one_or_none() or place


def _flight_place_direction(text, place):
    # Direction is grounded in the traveler's words, never a model parameter.
    words = [word for word in (place, city_zh(place)) if word]
    for word in words:
        match = re.search(re.escape(word), text, re.I)
        if not match:
            continue
        before, after = text[max(0, match.start() - 8):match.start()], text[match.end():match.end() + 8]
        if re.search(r"(?:到|去|往|飞往)\s*$", before) or re.search(r"^\s*(?:到达|降落)", after):
            return "arrival"
        if re.search(r"(?:从|由|自)\s*$", before) or re.search(r"^\s*(?:出发|起飞)", after):
            return "departure"
    return None


def matching_orders(db, traveler_id, kind, text, rows):
    """Return active owned orders matching explicit ID or mentioned location."""
    explicit_id = re.search(r"订单\s*(?:号)?\s*([A-Za-z0-9][A-Za-z0-9-]{3,})", text)
    if explicit_id:
        rows = [row for row in rows if row["id"] == explicit_id.group(1)]
    else:
        explicit = [row for row in rows if row["id"] in text]
        if explicit:
            rows = explicit
    places = mentioned_cities(text)
    if not places or not rows:
        return rows
    details = {row["id"]: row for row in itinerary(db, traveler_id)[f"{kind}_orders"]
               if row["status"] == "confirmed"}
    criteria = [(_place_codes(db, place), _place_city(db, place),
                 _flight_place_direction(text, place) if kind == "flight" else None)
                for place in places]
    matched = []
    for owned in rows:
        row = details.get(owned["id"])
        if row is None:
            continue
        checks = []
        for codes, city, direction in criteria:
            if kind == "flight":
                checks.append(row["departure_airport"] in codes if direction == "departure" else
                              row["arrival_airport"] in codes if direction == "arrival" else
                              row["departure_airport"] in codes or row["arrival_airport"] in codes)
            elif kind == "hotel":
                checks.append(row["city"] == city)
            else:
                checks.append(row["airport_code"] in codes)
        if all(checks):
            matched.append(row)
    return matched


def order_options(kind, rows):
    if kind == "flight":
        shown = "；".join(f"{row.get('flight_no', '航班')}，"
                       f"{row['departure_at'].date() if row.get('departure_at') else '日期待核对'} 出发"
                       f"（订单 {row['id']}）"
                       for row in rows[:5])
    elif kind == "hotel":
        shown = "；".join(f"{product_zh(row.get('name', '酒店'), city=row.get('city', ''))}，"
                       f"{row['check_in']} 入住（订单 {row['id']}）"
                       for row in rows[:5])
    else:
        shown = "；".join(f"{product_zh(row.get('name', '车辆'), airport=row.get('airport_code', ''))}，"
                         f"{row['pickup_at'].date()} 取车（订单 {row['id']}）"
                         for row in rows[:5])
    return shown + (f"；另有 {len(rows) - 5} 笔，请按日期或订单号进一步指定" if len(rows) > 5 else "")
