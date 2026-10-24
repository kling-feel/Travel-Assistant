"""Deterministic simulated rental time, quote and order rules."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import insert, select, update

from travel_assistant.db.travel_models import airports, demo_state, rental_orders, rental_products
from travel_assistant.services.travel import OrderError


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _product(db, product_id):
    if type(product_id) is not str or not product_id:
        raise OrderError("invalid rental product")
    row = db.execute(select(rental_products, airports.c.timezone).join(
        airports, airports.c.code == rental_products.c.airport_code
    ).where(rental_products.c.id == product_id)).mappings().one_or_none()
    if row is None:
        raise OrderError("rental product unavailable")
    try:
        zone = ZoneInfo(row["timezone"])
    except ZoneInfoNotFoundError:
        raise OrderError("rental airport timezone unavailable") from None
    return row, zone


def _local_time(value, zone):
    if type(value) is not str or len(value) < 16 or value[10] != "T":
        raise OrderError("rental pickup and dropoff times are required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise OrderError("invalid rental local time") from None
    wall = parsed.replace(tzinfo=None)
    valid = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold)
        roundtrip = candidate.astimezone(timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == wall:
            valid.append(candidate)
    if not valid:
        raise OrderError("rental local time does not exist")
    if parsed.tzinfo is None:
        if len({candidate.utcoffset() for candidate in valid}) > 1:
            raise OrderError("ambiguous rental local time needs UTC offset")
        return valid[0]
    matching = [candidate for candidate in valid
                if candidate.utcoffset() == parsed.utcoffset()]
    if not matching:
        raise OrderError("rental time offset does not match airport timezone")
    return matching[0]


def normalize_book_params(db, params):
    product, zone = _product(db, params["product_id"])
    pickup = _local_time(params["pickup_at"], zone)
    dropoff = _local_time(params["dropoff_at"], zone)
    return {"product_id": product["id"],
            "pickup_at": pickup.isoformat(), "dropoff_at": dropoff.isoformat()}


def normalize_change_params(db, traveler_id, params):
    order_id = params["order_id"]
    if type(order_id) is not str or not order_id:
        raise OrderError("invalid rental order reference")
    order = db.execute(select(rental_orders.c.product_id).where(
        rental_orders.c.id == order_id,
        rental_orders.c.traveler_id == traveler_id)).scalar_one_or_none()
    if order is None:
        raise OrderError("rental order unavailable")
    _, zone = _product(db, order)
    return {"order_id": order_id, "expected_version": params["expected_version"],
            "pickup_at": _local_time(params["pickup_at"], zone).isoformat(),
            "dropoff_at": _local_time(params["dropoff_at"], zone).isoformat()}


def _billable_days(pickup_utc, dropoff_utc):
    duration = dropoff_utc - pickup_utc
    return duration // timedelta(days=1) + (duration % timedelta(days=1) != timedelta())


def _snapshot(order_id, status, product, pickup, dropoff, days, total,
              *, daily_price=None):
    zone = ZoneInfo(product["timezone"])
    return {"order_id": order_id, "status": status,
            "product_id": product["id"], "product_name": product["name"],
            "airport_code": product["airport_code"],
            "airport_timezone": product["timezone"],
            "pickup_at": _utc(pickup).isoformat(),
            "dropoff_at": _utc(dropoff).isoformat(),
            "pickup_local": _utc(pickup).astimezone(zone).isoformat(),
            "dropoff_local": _utc(dropoff).astimezone(zone).isoformat(),
            "daily_price": product["daily_price"] if daily_price is None else daily_price,
            "billable_days": days, "total_price": total}


def rental_quote(db, traveler_id, kind, params, now):
    now = _utc(now)
    order = None
    before = None
    if kind in ("rental_cancel", "rental_change"):
        order_id, version = params["order_id"], params["expected_version"]
        if type(order_id) is not str or not order_id or type(version) is not int or version < 1:
            raise OrderError("invalid rental order reference")
        order = db.execute(select(rental_orders).where(
            rental_orders.c.id == order_id,
            rental_orders.c.traveler_id == traveler_id)).mappings().one_or_none()
        if order is None or order["status"] != "confirmed" or order["version"] != version:
            raise OrderError("rental order status or version changed")
        if _utc(order["pickup_at"]) <= now:
            raise OrderError("rental pickup already started")
        product, zone = _product(db, order["product_id"])
        before = _snapshot(order["id"], order["status"], product,
                           order["pickup_at"], order["dropoff_at"],
                           order["billable_days"], order["total_price"],
                           daily_price=order["daily_price"])
        if kind == "rental_cancel":
            after = {**before, "status": "cancelled"}
            total = order["total_price"]
            price = {"old_amount": total, "new_amount": 0, "difference": -total,
                     "payable": 0, "refundable": total, "cancellation_fee": 0}
            return order, before, after, price
    else:
        product, zone = _product(db, params["product_id"])
    pickup = _local_time(params["pickup_at"], zone)
    dropoff = _local_time(params["dropoff_at"], zone)
    pickup_utc, dropoff_utc = _utc(pickup), _utc(dropoff)
    if pickup_utc <= now or dropoff_utc <= pickup_utc:
        raise OrderError("invalid rental period")
    as_of = db.execute(select(demo_state.c.as_of).where(
        demo_state.c.id == 1)).scalar_one_or_none()
    if as_of is None:
        raise OrderError("demo booking window unavailable")
    if not (as_of + timedelta(days=1) <= pickup.date()
            <= as_of + timedelta(days=30)
            and dropoff.date() <= as_of + timedelta(days=31)):
        raise OrderError("rental outside demo booking window")
    if not (product["available_from"] <= pickup.date()
            and dropoff.date() <= product["available_until"]):
        raise OrderError("rental product unavailable for period")
    if product["daily_price"] <= 0:
        raise OrderError("rental daily price unavailable")
    if order and (_utc(order["pickup_at"]), _utc(order["dropoff_at"])) == (
            pickup_utc, dropoff_utc):
        raise OrderError("rental period unchanged")
    duplicate_query = select(rental_orders.c.id).where(
        rental_orders.c.traveler_id == traveler_id,
        rental_orders.c.product_id == product["id"],
        rental_orders.c.pickup_at == pickup_utc,
        rental_orders.c.dropoff_at == dropoff_utc,
        rental_orders.c.status == "confirmed",
    )
    if order:
        duplicate_query = duplicate_query.where(rental_orders.c.id != order["id"])
    duplicate = db.execute(duplicate_query.limit(1)).scalar_one_or_none()
    if duplicate:
        raise OrderError("traveler already holds this rental period")
    days = _billable_days(pickup_utc, dropoff_utc)
    total = days * product["daily_price"]
    old_total = order["total_price"] if order else 0
    difference = total - old_total
    after = _snapshot(order["id"] if order else None, "confirmed", product,
                      pickup_utc, dropoff_utc, days, total)
    price = {"old_amount": old_total, "new_amount": total,
             "difference": difference, "payable": max(difference, 0),
             "refundable": max(-difference, 0), "cancellation_fee": 0}
    return order, before, after, price


def book_rental(db, traveler_id, params, quoted_total, *, now):
    _, _, after, price = rental_quote(db, traveler_id, "rental_book", params, now)
    if quoted_total != price["new_amount"]:
        raise OrderError("rental price changed")
    order_id = str(uuid4())
    db.execute(insert(rental_orders).values(
        id=order_id, traveler_id=traveler_id, product_id=after["product_id"],
        pickup_at=datetime.fromisoformat(after["pickup_at"]),
        dropoff_at=datetime.fromisoformat(after["dropoff_at"]),
        daily_price=after["daily_price"], billable_days=after["billable_days"],
        total_price=after["total_price"], status="confirmed", version=1))
    return order_id


def cancel_rental(db, traveler_id, order_id, expected_version, *, now):
    result = db.execute(update(rental_orders).where(
        rental_orders.c.id == order_id,
        rental_orders.c.traveler_id == traveler_id,
        rental_orders.c.status == "confirmed",
        rental_orders.c.version == expected_version,
        rental_orders.c.pickup_at > _utc(now),
    ).values(status="cancelled", version=expected_version + 1,
             updated_at=_utc(now)))
    if result.rowcount != 1:
        raise OrderError("rental order status, version or pickup time changed")


def change_rental(db, traveler_id, order_id, expected_version, after, *, now):
    result = db.execute(update(rental_orders).where(
        rental_orders.c.id == order_id,
        rental_orders.c.traveler_id == traveler_id,
        rental_orders.c.product_id == after["product_id"],
        rental_orders.c.status == "confirmed",
        rental_orders.c.version == expected_version,
        rental_orders.c.pickup_at > _utc(now),
    ).values(pickup_at=datetime.fromisoformat(after["pickup_at"]),
             dropoff_at=datetime.fromisoformat(after["dropoff_at"]),
             daily_price=after["daily_price"],
             billable_days=after["billable_days"],
             total_price=after["total_price"], version=expected_version + 1,
             updated_at=_utc(now)))
    if result.rowcount != 1:
        raise OrderError("rental order status, version or pickup time changed")
