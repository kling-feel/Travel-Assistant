"""Reproducible fictional products near reachable airport coordinates."""

from datetime import date, timedelta
from hashlib import sha256

PRICE_SEED_RULE = "reachable-products-v1"
GENERATION_RULE = "reachable-products-v2"
HOTEL_LAST_CHECKOUT_OFFSET = 34


def _amount(code: str, kind: str, variant: int, base: int, spread: int) -> int:
    key = f"{PRICE_SEED_RULE}|{code}|{kind}|{variant}".encode("ascii")
    return base + int.from_bytes(sha256(key).digest()[:4], "big") % spread


def _position(airport: dict, variant: int):
    offsets = ((0.009, 0.012), (-0.013, 0.018), (0.019, -0.014))
    lat, lon = offsets[variant - 1]
    return (round(max(-90, min(90, airport["latitude"] + lat)), 6),
            round(max(-180, min(180, airport["longitude"] + lon)), 6))


def generate_products(airport_rows: list[dict], flight_rows: list[dict],
                      offer_rows: list[dict], as_of: date):
    """Return hotel, room and rental rows for airports with a bookable arrival."""
    offered = {row["flight_id"] for row in offer_rows}
    destination_codes = {flight["arrival_airport"] for flight in flight_rows
                         if flight["id"] in offered}
    airport_map = {airport["code"]: airport for airport in airport_rows}
    if len(destination_codes) != 104 or not destination_codes <= airport_map.keys():
        raise ValueError("Destination airport coverage changed")
    if len({airport_map[code]["city"] for code in destination_codes}) != 95:
        raise ValueError("Destination city coverage changed")
    if "BSL" not in destination_codes:
        raise ValueError("Demo Basel products have no reachable airport")

    hotel_rows, room_rows, rental_rows = [], [], []
    for index, code in enumerate(sorted(destination_codes), start=1):
        airport = airport_map[code]
        for variant in (1, 2, 3):
            latitude, longitude = _position(airport, variant)
            hotel_id = variant if code == "BSL" else 1000 + index * 3 + variant
            room_id = f"room-{variant}" if code == "BSL" else f"room-{code}-{variant}"
            price = (90 + variant * 30 if code == "BSL" else
                     _amount(code, "hotel", variant, 90 + variant * 20, 81))
            start = as_of + timedelta(days=7 if variant == 3 else 1)
            end = as_of + timedelta(days=(
                14 if variant == 2 else 31 if variant == 3
                else HOTEL_LAST_CHECKOUT_OFFSET))
            hotel_rows.append(dict(
                id=hotel_id, name=f"虚构·{airport['city']}机场周边酒店 {variant}",
                city=airport["city"], price_tier="Simulated",
                nearby_airport=code, latitude=latitude, longitude=longitude,
            ))
            room_rows.append(dict(
                id=room_id, hotel_id=hotel_id,
                room_type=("演示标准间", "演示双床间", "演示家庭间")[variant - 1],
                nightly_price=price, available_from=start, available_until=end,
                generation_rule=GENERATION_RULE,
            ))
        for variant in (1, 2):
            rental_rows.append(dict(
                id=f"rental-{code}-{variant}", airport_code=code,
                city=airport["city"], name=f"虚构·{code}机场租车 {variant}",
                vehicle_type=("演示经济型", "演示舒适型")[variant - 1],
                daily_price=_amount(code, "rental", variant, 55 + variant * 20, 61),
                latitude=airport["latitude"], longitude=airport["longitude"],
                available_from=as_of + timedelta(days=1 if variant == 1 else 7),
                available_until=as_of + timedelta(days=31),
                generation_rule=GENERATION_RULE,
            ))
    if (len(hotel_rows), len(room_rows), len(rental_rows)) != (312, 312, 208):
        raise ValueError("Generated product counts changed")
    return hotel_rows, room_rows, rental_rows
