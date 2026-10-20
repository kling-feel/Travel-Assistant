"""Traveler-scoped reads and internal order writes.

P1 writes are deliberately not routed to AI or HTTP. P2 will call them only
after a durable, traveler-bound confirmation has been checked.
"""

from datetime import date, datetime, time, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, insert, or_, select, update

from db.travel_models import (airports, demo_state, flight_offers, flight_orders, flights, hotel_orders,
                              hotel_products, hotels, policy_documents, rental_orders,
                              rental_products)
from services.policy_catalog import CURRENT_POLICIES
from services.demo_pricing import NETWORK_OFFER_RULE_ID, OFFER_RULE_ID


class OrderError(ValueError):
    pass


def itinerary(session, traveler_id):
    flight_rows = session.execute(
        select(flight_orders.c.id, flight_orders.c.status, flight_orders.c.version,
               flight_orders.c.amount, flights.c.flight_no, flights.c.departure_at,
               flights.c.arrival_at, flights.c.departure_airport,
               flights.c.arrival_airport)
        .join(flights, flight_orders.c.flight_id == flights.c.id)
        .where(flight_orders.c.traveler_id == traveler_id)
        .order_by(flights.c.departure_at)
    ).mappings().all()
    hotel_rows = session.execute(
        select(hotel_orders.c.id, hotel_orders.c.status, hotel_orders.c.version,
               hotel_orders.c.product_id, hotel_orders.c.check_in, hotel_orders.c.check_out,
               hotel_orders.c.total_price, hotels.c.name, hotels.c.city,
               hotel_products.c.room_type)
        .join(hotel_products, hotel_orders.c.product_id == hotel_products.c.id)
        .join(hotels, hotel_products.c.hotel_id == hotels.c.id)
        .where(hotel_orders.c.traveler_id == traveler_id)
        .order_by(hotel_orders.c.check_in)
    ).mappings().all()
    rental_rows = session.execute(
        select(rental_orders.c.id, rental_orders.c.status, rental_orders.c.version,
               rental_orders.c.product_id, rental_orders.c.pickup_at,
               rental_orders.c.dropoff_at, rental_orders.c.daily_price,
               rental_orders.c.billable_days, rental_orders.c.total_price,
               rental_products.c.name, rental_products.c.airport_code,
               airports.c.timezone.label("airport_timezone"))
        .join(rental_products, rental_products.c.id == rental_orders.c.product_id)
        .join(airports, airports.c.code == rental_products.c.airport_code)
        .where(rental_orders.c.traveler_id == traveler_id)
        .order_by(rental_orders.c.pickup_at)
    ).mappings().all()
    rentals = []
    for row in rental_rows:
        item = dict(row)
        zone = ZoneInfo(item["airport_timezone"])
        pickup = (item["pickup_at"] if item["pickup_at"].tzinfo else
                  item["pickup_at"].replace(tzinfo=timezone.utc))
        dropoff = (item["dropoff_at"] if item["dropoff_at"].tzinfo else
                   item["dropoff_at"].replace(tzinfo=timezone.utc))
        item["pickup_local"] = pickup.astimezone(zone).isoformat()
        item["dropoff_local"] = dropoff.astimezone(zone).isoformat()
        item["simulation_notice"] = "虚构租车与模拟金额；没有真实付款或退款"
        rentals.append(item)
    return {"flight_orders": [dict(row) for row in flight_rows],
            "hotel_orders": [dict(row) for row in hotel_rows],
            "rental_orders": rentals}


def find_flights(session, departure, arrival, travel_date, *, limit=20, offset=0,
                 sort="departure", today=None):
    if (not isinstance(travel_date, date) or type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 10000
            or sort not in ("departure", "price")):
        raise OrderError("invalid flight search")
    if today is None:
        today = session.execute(select(demo_state.c.as_of).where(
            demo_state.c.id == 1
        )).scalar_one_or_none() or date.today()
    if not today + timedelta(days=1) <= travel_date <= today + timedelta(days=30):
        return []
    airport_timezone = session.execute(select(airports.c.timezone).where(
        airports.c.code == departure
    )).scalar_one_or_none()
    if airport_timezone is None:
        return []
    local_zone = ZoneInfo(airport_timezone)
    start = datetime.combine(travel_date, time.min, local_zone).astimezone(timezone.utc)
    end = datetime.combine(travel_date + timedelta(days=1), time.min,
                           local_zone).astimezone(timezone.utc)
    ordering = ((flight_offers.c.amount, flights.c.departure_at, flights.c.id)
                if sort == "price" else (flights.c.departure_at, flights.c.id))
    candidates = session.execute(
        select(flights, flight_offers.c.amount.label("demo_fare_amount"),
               flight_offers.c.version.label("offer_version"))
        .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
        .where(flights.c.departure_airport == departure,
               flights.c.arrival_airport == arrival,
               flights.c.status == "Scheduled",
               flights.c.departure_at >= start,
               flights.c.departure_at < end)
        .order_by(*ordering).limit(limit).offset(offset)
    ).mappings()
    return [dict(row) for row in candidates]


def find_earliest_flights(session, departures, arrivals, *, not_before=None,
                          now=None, limit=3):
    """Globally sort all eligible airport pairs before limiting the answer."""
    if (not departures or not arrivals or len(departures) > 10 or len(arrivals) > 10
            or type(limit) is not int or not 1 <= limit <= 10
            or not_before is not None and type(not_before) is not date):
        raise OrderError("invalid earliest flight search")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise OrderError("current time must include timezone")
    now = now.astimezone(timezone.utc)
    as_of = session.execute(select(demo_state.c.as_of).where(
        demo_state.c.id == 1)).scalar_one_or_none()
    if as_of is None:
        raise OrderError("demo booking window unavailable")
    last_day = as_of + timedelta(days=30)
    if not_before and not_before > last_day:
        return {"rows": [], "tie_count": 0, "last_day": last_day}
    zones = dict(session.execute(select(airports.c.code, airports.c.timezone).where(
        airports.c.code.in_(departures))).all())
    scopes = []
    for code in departures:
        if code not in zones:
            continue
        zone = ZoneInfo(zones[code])
        first_day = max(as_of + timedelta(days=1),
                        not_before or now.astimezone(zone).date())
        start = datetime.combine(first_day, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(last_day + timedelta(days=1), time.min,
                               zone).astimezone(timezone.utc)
        start = max(start, now)
        if start < end:
            scopes.append(and_(flights.c.departure_airport == code,
                               flights.c.departure_at >= start,
                               flights.c.departure_at < end))
    if not scopes:
        return {"rows": [], "tie_count": 0, "last_day": last_day}
    eligible = (flights.c.arrival_airport.in_(arrivals),
                flights.c.status == "Scheduled",
                flight_offers.c.rule_id.in_((OFFER_RULE_ID, NETWORK_OFFER_RULE_ID)),
                or_(*scopes))
    query = (select(flights, flight_offers.c.amount.label("demo_fare_amount"),
                    flight_offers.c.version.label("offer_version"))
             .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
             .where(*eligible).order_by(flights.c.departure_at, flights.c.id)
             .limit(limit))
    rows = [dict(row) for row in session.execute(query).mappings()]
    ties = 0
    if rows:
        ties = session.execute(select(func.count()).select_from(
            flights.join(flight_offers, flight_offers.c.flight_id == flights.c.id)
        ).where(*eligible, flights.c.departure_at == rows[0]["departure_at"])).scalar_one()
    return {"rows": rows, "tie_count": ties, "last_day": last_day}


def _product_dates(session, start, end):
    if not isinstance(start, date) or not isinstance(end, date) or end <= start:
        raise OrderError("invalid product dates")
    as_of = session.execute(select(demo_state.c.as_of).where(
        demo_state.c.id == 1)).scalar_one_or_none()
    return as_of is None or (as_of + timedelta(days=1) <= start
                             and end <= as_of + timedelta(days=31))


def _distance_km(source, target):
    lat1, lon1 = map(radians, source)
    lat2, lon2 = map(radians, target)
    value = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return round(6371.0 * 2 * asin(min(1.0, sqrt(value))), 2)


def _search_options(limit, offset, sort, allowed):
    if (type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 10000
            or sort not in allowed):
        raise OrderError("invalid product search options")


def _reference_airport(session, airport_code, city):
    if not airport_code:
        return None
    row = session.execute(select(airports).where(
        airports.c.code == airport_code)).mappings().one_or_none()
    if row is None or (city and row["city"] != city):
        return None
    return row


def find_hotels(session, city, check_in, check_out, *, airport=None,
                limit=20, offset=0, sort="price"):
    _search_options(limit, offset, sort, ("price", "distance"))
    if not city or (sort == "distance" and not airport):
        raise OrderError("city and distance reference are required")
    if not _product_dates(session, check_in, check_out):
        return []
    reference = _reference_airport(session, airport, city)
    if airport and reference is None:
        return []
    query = (
        select(hotel_products.c.id.label("product_id"), hotel_products.c.room_type,
               hotel_products.c.nightly_price, hotel_products.c.available_from,
               hotel_products.c.available_until, hotel_products.c.generation_rule,
               hotels.c.id.label("hotel_id"), hotels.c.name, hotels.c.city,
               hotels.c.nearby_airport, hotels.c.latitude, hotels.c.longitude)
        .join(hotels, hotel_products.c.hotel_id == hotels.c.id)
        .where(hotels.c.city == city,
               hotel_products.c.available_from <= check_in,
               hotel_products.c.available_until >= check_out)
    )
    if airport:
        query = query.where(hotels.c.nearby_airport == airport)
    rows = [dict(row) for row in session.execute(query.limit(501)).mappings()]
    if len(rows) > 500:
        raise OrderError("too many hotel candidates")
    for row in rows:
        row["distance_km"] = (_distance_km(
            (reference["latitude"], reference["longitude"]),
            (row["latitude"], row["longitude"]))
            if reference and all(value is not None for value in
                                 (reference["latitude"], reference["longitude"],
                                  row["latitude"], row["longitude"])) else None)
        row["distance_basis"] = ("机场至模拟酒店坐标的近似直线距离；非道路距离或通行时间"
                                 if row["distance_km"] is not None else None)
        row["simulation_notice"] = "虚构酒店与模拟价格；没有真实库存承诺"
    if sort == "distance":
        rows.sort(key=lambda row: (row["distance_km"] is None,
                                   row["distance_km"] or 0,
                                   row["nightly_price"], row["product_id"]))
    else:
        rows.sort(key=lambda row: (row["nightly_price"], row["product_id"]))
    return rows[offset:offset + limit]


def find_rentals(session, pickup_date, drop_off_date, *, airport=None, city=None,
                 max_daily_price=None, limit=20, offset=0, sort="price"):
    _search_options(limit, offset, sort, ("price", "distance"))
    if not airport and not city:
        raise OrderError("airport or city is required")
    if sort == "distance" and not airport:
        raise OrderError("airport is required for distance sort")
    if (max_daily_price is not None and
            (type(max_daily_price) is not int or max_daily_price < 0)):
        raise OrderError("invalid maximum price")
    if not _product_dates(session, pickup_date, drop_off_date):
        return []
    reference = _reference_airport(session, airport, city)
    if airport and reference is None:
        return []
    query = select(rental_products).where(
        rental_products.c.available_from <= pickup_date,
        rental_products.c.available_until >= drop_off_date)
    if airport:
        query = query.where(rental_products.c.airport_code == airport)
    if city:
        query = query.where(rental_products.c.city == city)
    if max_daily_price is not None:
        query = query.where(rental_products.c.daily_price <= max_daily_price)
    rows = [dict(row) for row in session.execute(query.limit(501)).mappings()]
    if len(rows) > 500:
        raise OrderError("too many rental candidates")
    for row in rows:
        row["distance_km"] = (_distance_km(
            (reference["latitude"], reference["longitude"]),
            (row["latitude"], row["longitude"]))
            if reference and reference["latitude"] is not None and
            reference["longitude"] is not None else None)
        row["distance_basis"] = ("机场至模拟取车点坐标的近似直线距离；非道路距离或通行时间"
                                 if row["distance_km"] is not None else None)
        row["simulation_notice"] = "虚构租车与模拟价格；预订须逐笔确认，无真实库存或付款"
    if sort == "distance":
        rows.sort(key=lambda row: (row["distance_km"] is None,
                                   row["distance_km"] or 0,
                                   row["daily_price"], row["id"]))
    else:
        rows.sort(key=lambda row: (row["daily_price"], row["id"]))
    return rows[offset:offset + limit]


def policies(session, scope):
    current = CURRENT_POLICIES.get(scope)
    if current is None:
        return []
    rows = [dict(row) for row in session.execute(
        select(policy_documents).where(policy_documents.c.id == current["id"],
                                       policy_documents.c.version == current["version"])
    ).mappings()]
    if any(any(row[field] != current[field] for field in ("scope", "title", "body"))
           for row in rows):
        raise OrderError("current demo policy changed")
    return rows


def _owned_order(session, table, traveler_id, order_id, expected_version):
    row = session.execute(select(table).where(
        table.c.id == order_id, table.c.traveler_id == traveler_id
    )).mappings().one_or_none()
    if row is None:
        raise OrderError("order not found")
    if row["status"] != "confirmed" or row["version"] != expected_version:
        raise OrderError("order status or version changed")
    return row


def _change_order(session, table, traveler_id, order_id, expected_version, values):
    result = session.execute(update(table).where(
        table.c.id == order_id, table.c.traveler_id == traveler_id,
        table.c.status == "confirmed", table.c.version == expected_version
    ).values(**values, version=expected_version + 1,
             updated_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        raise OrderError("order status or version changed")


def _active_flight_order(session, traveler_id, flight_id, *, excluding=None):
    query = select(flight_orders.c.id).where(
        flight_orders.c.traveler_id == traveler_id,
        flight_orders.c.flight_id == flight_id,
        flight_orders.c.status == "confirmed",
    )
    if excluding is not None:
        query = query.where(flight_orders.c.id != excluding)
    return session.execute(query.limit(1)).scalar_one_or_none() is not None


def book_flight(session, traveler_id, flight_id, amount, *, now=None):
    if type(flight_id) is not int or type(amount) is not int or amount < 0:
        raise OrderError("invalid flight booking")
    now = now or datetime.now(timezone.utc)
    flight = session.execute(select(flights).where(
        flights.c.id == flight_id, flights.c.status == "Scheduled"
    )).mappings().one_or_none()
    offer = session.execute(select(flight_offers).where(
        flight_offers.c.flight_id == flight_id
    )).mappings().one_or_none()
    departure_at = flight["departure_at"] if flight else None
    if departure_at is not None and departure_at.tzinfo is None:
        departure_at = departure_at.replace(tzinfo=timezone.utc)
    if (flight is None or offer is None or departure_at <= now
            or offer["amount"] != amount):
        raise OrderError("flight or demo fare unavailable")
    if _active_flight_order(session, traveler_id, flight_id):
        raise OrderError("traveler already holds this flight")
    order_id = str(uuid4())
    session.execute(insert(flight_orders).values(
        id=order_id, traveler_id=traveler_id, flight_id=flight_id,
        fare_conditions="模拟经济舱", amount=amount,
        status="confirmed", version=1,
    ))
    return order_id


def change_flight(session, traveler_id, order_id, expected_version, new_flight_id,
                  new_amount):
    if type(new_amount) is not int or new_amount < 0:
        raise OrderError("invalid new fare")
    old = _owned_order(session, flight_orders, traveler_id, order_id, expected_version)
    old_flight = session.execute(select(flights).where(
        flights.c.id == old["flight_id"]
    )).mappings().one()
    new = session.execute(select(flights).where(
        flights.c.id == new_flight_id, flights.c.status == "Scheduled"
    )).mappings().one_or_none()
    if (new is None or new_flight_id == old["flight_id"]
            or new["departure_airport"] != old_flight["departure_airport"]
            or new["arrival_airport"] != old_flight["arrival_airport"]):
        raise OrderError("invalid replacement flight")
    if _active_flight_order(session, traveler_id, new_flight_id, excluding=order_id):
        raise OrderError("traveler already holds replacement flight")
    _change_order(session, flight_orders, traveler_id, order_id, expected_version,
                  {"flight_id": new_flight_id, "amount": new_amount})


def cancel_flight(session, traveler_id, order_id, expected_version):
    _owned_order(session, flight_orders, traveler_id, order_id, expected_version)
    _change_order(session, flight_orders, traveler_id, order_id, expected_version,
                  {"status": "cancelled"})


def _hotel_product(session, product_id, check_in, check_out):
    if check_out <= check_in:
        raise OrderError("check_out must be after check_in")
    product = session.execute(select(hotel_products).where(
        hotel_products.c.id == product_id,
        hotel_products.c.available_from <= check_in,
        hotel_products.c.available_until >= check_out,
    )).mappings().one_or_none()
    if product is None:
        raise OrderError("hotel product unavailable")
    return product


def book_hotel(session, traveler_id, product_id, check_in, check_out):
    product = _hotel_product(session, product_id, check_in, check_out)
    order_id = str(uuid4())
    session.execute(insert(hotel_orders).values(
        id=order_id, traveler_id=traveler_id, product_id=product_id,
        check_in=check_in, check_out=check_out,
        total_price=(check_out - check_in).days * product["nightly_price"],
        status="confirmed", version=1,
    ))
    return order_id


def change_hotel(session, traveler_id, order_id, expected_version,
                 product_id, check_in, check_out):
    _owned_order(session, hotel_orders, traveler_id, order_id, expected_version)
    product = _hotel_product(session, product_id, check_in, check_out)
    _change_order(session, hotel_orders, traveler_id, order_id, expected_version, {
        "product_id": product_id, "check_in": check_in, "check_out": check_out,
        "total_price": (check_out - check_in).days * product["nightly_price"],
    })


def cancel_hotel(session, traveler_id, order_id, expected_version):
    _owned_order(session, hotel_orders, traveler_id, order_id, expected_version)
    _change_order(session, hotel_orders, traveler_id, order_id, expected_version,
                  {"status": "cancelled"})
