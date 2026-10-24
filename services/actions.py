"""Create traveler-owned proposals without changing any order."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from travel_assistant.db.travel_models import (airports, audit_events, demo_state, flight_offers, flight_orders, flights, hotel_orders,
                              hotel_products, hotels, pending_actions,
                              policy_documents)
from services.demo_pricing import (FLIGHT_POLICY, NETWORK_OFFER_RULE_ID,
                                   OFFER_RULE_ID,
                                   cancellation_fee)
from services.sessions import SessionNotFound, _owned_session
from services.policy_catalog import HOTEL_POLICY, CURRENT_RENTAL_POLICY
from services.rentals import (normalize_book_params, normalize_change_params,
                              rental_quote)
from services.travel import OrderError


class ActionError(ValueError):
    pass


class ActionNotFound(ValueError):
    pass


FIELDS = {
    "flight_book": {"flight_id"},
    "flight_change": {"order_id", "expected_version", "new_flight_id"},
    "flight_cancel": {"order_id", "expected_version"},
    "hotel_book": {"product_id", "check_in", "check_out"},
    "hotel_change": {"order_id", "expected_version", "product_id",
                     "check_in", "check_out"},
    "hotel_cancel": {"order_id", "expected_version"},
    "rental_book": {"product_id", "pickup_at", "dropoff_at"},
    "rental_change": {"order_id", "expected_version", "pickup_at", "dropoff_at"},
    "rental_cancel": {"order_id", "expected_version"},
}


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _date(value):
    try:
        return value if type(value) is date else date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ActionError("invalid date") from None


def _owned_order(db, table, traveler_id, order_id, version):
    if not isinstance(order_id, str) or not order_id or type(version) is not int or version < 1:
        raise ActionError("invalid order reference")
    row = db.execute(select(table).where(
        table.c.id == order_id, table.c.traveler_id == traveler_id,
    )).mappings().one_or_none()
    if row is None:
        raise ActionError("order unavailable")
    if row["status"] != "confirmed" or row["version"] != version:
        raise ActionError("order status or version changed")
    return row


def _flight(db, flight_id):
    if type(flight_id) is not int:
        raise ActionError("invalid flight id")
    row = db.execute(select(flights).where(flights.c.id == flight_id)).mappings().one_or_none()
    if row is None:
        raise ActionError("flight unavailable")
    return row


def _hotel_product(db, product_id, check_in, check_out):
    if not isinstance(product_id, str) or not product_id:
        raise ActionError("invalid hotel product")
    row = db.execute(select(hotel_products, hotels.c.name.label("hotel_name"),
                            hotels.c.city.label("hotel_city"))
                     .join(hotels, hotels.c.id == hotel_products.c.hotel_id)
                     .where(hotel_products.c.id == product_id,
                            hotel_products.c.available_from <= check_in,
                            hotel_products.c.available_until >= check_out)
                     ).mappings().one_or_none()
    if row is None:
        raise ActionError("hotel product unavailable")
    return row


def _hotel_today(db, product_id, now):
    """Use the associated airport's local date; legacy rooms fall back to UTC."""
    zone = db.execute(select(airports.c.timezone).select_from(
        hotel_products.join(hotels, hotels.c.id == hotel_products.c.hotel_id)
        .outerjoin(airports, airports.c.code == hotels.c.nearby_airport)
    ).where(hotel_products.c.id == product_id)).scalar_one_or_none()
    return now.astimezone(ZoneInfo(zone)).date() if zone else now.date()


def _offer(db, flight_id):
    row = db.execute(select(flight_offers).where(
        flight_offers.c.flight_id == flight_id)).mappings().one_or_none()
    if row is None or row["rule_id"] not in (OFFER_RULE_ID, NETWORK_OFFER_RULE_ID):
        raise ActionError("candidate flight has no demo fare")
    return row


def _policy(db, action_type):
    expected = (FLIGHT_POLICY if action_type.startswith("flight_") else
                CURRENT_RENTAL_POLICY if action_type.startswith("rental_") else HOTEL_POLICY)
    row = db.execute(select(policy_documents).where(
        policy_documents.c.id == expected["id"],
        policy_documents.c.version == expected["version"],
    )).mappings().one_or_none()
    if row is None or row["body"] != expected["body"] or row["scope"] != expected["scope"]:
        raise ActionError("demo policy changed")
    return row


def _flight_snapshot(flight):
    return {"flight_id": flight["id"], "flight_no": flight["flight_no"],
            "departure_airport": flight["departure_airport"],
            "arrival_airport": flight["arrival_airport"],
            "departure_at": _utc(flight["departure_at"]).isoformat(),
            "arrival_at": _utc(flight["arrival_at"]).isoformat()}


def _flight_local_snapshot(db, flight):
    zones = dict(db.execute(select(airports.c.code, airports.c.timezone).where(
        airports.c.code.in_((flight["departure_airport"], flight["arrival_airport"])))).all())
    if flight["departure_airport"] not in zones or flight["arrival_airport"] not in zones:
        raise ActionError("flight airport unavailable")
    return {
        **_flight_snapshot(flight),
        "departure_local": _utc(flight["departure_at"]).astimezone(
            ZoneInfo(zones[flight["departure_airport"]])).isoformat(),
        "arrival_local": _utc(flight["arrival_at"]).astimezone(
            ZoneInfo(zones[flight["arrival_airport"]])).isoformat(),
    }


def _active_ticket(db, traveler_id, flight_id, *, excluding=None):
    query = select(flight_orders.c.id).where(
        flight_orders.c.traveler_id == traveler_id,
        flight_orders.c.flight_id == flight_id,
        flight_orders.c.status == "confirmed",
    )
    if excluding is not None:
        query = query.where(flight_orders.c.id != excluding)
    return db.execute(query.limit(1)).scalar_one_or_none() is not None


def _hotel_snapshot(db, order):
    product = db.execute(select(hotel_products, hotels.c.name.label("hotel_name"),
                                hotels.c.city.label("hotel_city"))
                         .join(hotels, hotels.c.id == hotel_products.c.hotel_id)
                         .where(hotel_products.c.id == order["product_id"])
                         ).mappings().one()
    return {"order_id": order["id"], "status": order["status"],
            "product_id": order["product_id"], "hotel_name": product["hotel_name"],
            "hotel_city": product["hotel_city"], "room_type": product["room_type"],
            "check_in": order["check_in"].isoformat(),
            "check_out": order["check_out"].isoformat(),
            "total_price": order["total_price"]}


def _quote(db, traveler_id, action_type, params, now):
    if action_type.startswith("rental_"):
        try:
            order, before, after, price = rental_quote(
                db, traveler_id, action_type, params, now)
        except OrderError as error:
            raise ActionError(str(error)) from error
        return order, before, after, price, None
    if action_type == "flight_book":
        flight = _flight(db, params["flight_id"])
        if flight["status"] != "Scheduled" or _utc(flight["departure_at"]) <= now:
            raise ActionError("flight no longer bookable")
        as_of = db.execute(select(demo_state.c.as_of).where(
            demo_state.c.id == 1)).scalar_one_or_none()
        if as_of is None:
            raise ActionError("demo booking window unavailable")
        after = _flight_local_snapshot(db, flight)
        local_day = date.fromisoformat(after["departure_local"][:10])
        if not as_of + timedelta(days=1) <= local_day <= as_of + timedelta(days=30):
            raise ActionError("flight outside demo booking window")
        if _active_ticket(db, traveler_id, flight["id"]):
            raise ActionError("traveler already holds this flight")
        offer = _offer(db, flight["id"])
        after = {"order_id": None, "status": "confirmed",
                 "amount": offer["amount"], **after}
        price = {"old_amount": 0, "new_amount": offer["amount"],
                 "difference": offer["amount"], "payable": offer["amount"],
                 "refundable": 0, "cancellation_fee": 0}
        return None, None, after, price, offer["version"]

    if action_type.startswith("flight_"):
        order = _owned_order(db, flight_orders, traveler_id,
                             params["order_id"], params["expected_version"])
        old_flight = _flight(db, order["flight_id"])
        if old_flight["status"] != "Scheduled" or _utc(old_flight["departure_at"]) <= now:
            raise ActionError("flight no longer available")
        before = {"order_id": order["id"], "status": order["status"],
                  "amount": order["amount"], **_flight_local_snapshot(db, old_flight)}
        if action_type == "flight_cancel":
            fee = cancellation_fee(order["amount"])
            after = {**before, "status": "cancelled"}
            price = {"old_amount": order["amount"], "new_amount": 0,
                     "difference": -order["amount"], "payable": 0,
                     "refundable": order["amount"] - fee, "cancellation_fee": fee}
            return order, before, after, price, None
        new = _flight(db, params["new_flight_id"])
        if (new["status"] != "Scheduled" or new["id"] == old_flight["id"]
                or new["departure_airport"] != old_flight["departure_airport"]
                or new["arrival_airport"] != old_flight["arrival_airport"]
                or _utc(new["departure_at"]) <= now):
            raise ActionError("invalid replacement flight")
        if _active_ticket(db, traveler_id, new["id"], excluding=order["id"]):
            raise ActionError("traveler already holds replacement flight")
        offer = _offer(db, new["id"])
        difference = offer["amount"] - order["amount"]
        after = {"order_id": order["id"], "status": "confirmed",
                 "amount": offer["amount"], **_flight_local_snapshot(db, new)}
        price = {"old_amount": order["amount"], "new_amount": offer["amount"],
                 "difference": difference, "payable": max(difference, 0),
                 "refundable": max(-difference, 0), "cancellation_fee": 0}
        return order, before, after, price, offer["version"]

    if action_type == "hotel_cancel":
        order = _owned_order(db, hotel_orders, traveler_id,
                             params["order_id"], params["expected_version"])
        if order["check_in"] <= _hotel_today(db, order["product_id"], now):
            raise ActionError("hotel cancellation closes on local check-in date")
        before = _hotel_snapshot(db, order)
        after = {**before, "status": "cancelled"}
        price = {"old_amount": order["total_price"], "new_amount": 0,
                 "difference": -order["total_price"], "payable": 0,
                 "refundable": order["total_price"], "cancellation_fee": 0}
        return order, before, after, price, None

    check_in, check_out = _date(params["check_in"]), _date(params["check_out"])
    if check_out <= check_in:
        raise ActionError("invalid stay dates")
    product = _hotel_product(db, params["product_id"], check_in, check_out)
    if check_in < _hotel_today(db, product["id"], now):
        raise ActionError("hotel stay already started")
    new_total = (check_out - check_in).days * product["nightly_price"]
    order = None
    before = None
    if action_type == "hotel_change":
        order = _owned_order(db, hotel_orders, traveler_id,
                             params["order_id"], params["expected_version"])
        if order["check_in"] < _hotel_today(db, order["product_id"], now):
            raise ActionError("hotel stay already started")
        before = _hotel_snapshot(db, order)
        if (order["product_id"], order["check_in"], order["check_out"]) == (
                product["id"], check_in, check_out):
            raise ActionError("hotel stay unchanged")
    after = {"order_id": order["id"] if order else None, "status": "confirmed",
             "product_id": product["id"], "hotel_name": product["hotel_name"],
             "hotel_city": product["hotel_city"], "room_type": product["room_type"],
             "check_in": check_in.isoformat(), "check_out": check_out.isoformat(),
             "total_price": new_total}
    old_total = order["total_price"] if order else 0
    difference = new_total - old_total
    price = {"old_amount": old_total, "new_amount": new_total,
             "difference": difference, "payable": max(difference, 0),
             "refundable": max(-difference, 0), "cancellation_fee": 0}
    return order, before, after, price, None


def _public(row, now):
    created_at, expires_at = _utc(row["created_at"]), _utc(row["expires_at"])
    card = {"id": row["id"], "session_id": row["session_id"],
            "action_type": row["action_type"], "target_order_id": row["target_order_id"],
            "target_order_version": row["target_order_version"],
            "parameters": row["parameters"], "before": row["before_snapshot"],
            "after": row["after_snapshot"], "price": row["price_summary"],
            "policy": {"id": row["policy_id"], "version": row["policy_version"]},
            "offer_version": row["offer_version"],
            "status": "expired" if row["status"] == "pending" and expires_at <= now else row["status"],
            "created_at": created_at, "expires_at": expires_at,
            "simulation_notice": "模拟核算，未发生支付或退款"}
    if row["action_type"].startswith("hotel_"):
        card["cancellation_notice"] = (
            "仅可在产品机场当地入住日期前确认取消；入住当日预订或改期后也不可取消")
    if row["action_type"] in ("rental_change", "rental_cancel"):
        card["change_notice"] = "仅可在原订单取车时刻前确认改期或取消"
    return card


def create_proposal(db, traveler_id, session_id, action_type, parameters, *,
                    now=None, idempotency_key=None, reuse_pending=False):
    """Caller owns transaction; proposal and its audit event commit together."""
    now = _utc(now or datetime.now(timezone.utc))
    if action_type not in FIELDS or not isinstance(parameters, dict):
        raise ActionError("invalid action")
    if set(parameters) != FIELDS[action_type]:
        raise ActionError("invalid action parameters")
    owned = _owned_session(db, traveler_id, session_id)
    if owned["status"] != "active":
        raise ActionError("session is closed")
    normalized = dict(parameters)
    if action_type in ("rental_book", "rental_change"):
        try:
            normalized = (normalize_book_params(db, normalized)
                          if action_type == "rental_book" else
                          normalize_change_params(db, traveler_id, normalized))
        except OrderError as error:
            raise ActionError(str(error)) from error
    for key in ("check_in", "check_out"):
        if key in normalized:
            normalized[key] = _date(normalized[key]).isoformat()
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or len(idempotency_key) != 36:
            raise ActionError("invalid idempotency key")
        existing = db.execute(select(pending_actions).where(
            pending_actions.c.idempotency_key == idempotency_key,
        )).mappings().one_or_none()
        if existing:
            if (existing["traveler_id"] != traveler_id or
                    existing["session_id"] != session_id or
                    existing["action_type"] != action_type or
                    existing["parameters"] != normalized):
                raise ActionError("idempotency key conflict")
            return get_proposal(db, traveler_id, session_id, existing["id"], now=now)
    policy = _policy(db, action_type)
    order, before, after, price, offer_version = _quote(
        db, traveler_id, action_type, normalized, now)
    pending = []
    if reuse_pending:
        pending = db.execute(select(pending_actions).where(
            pending_actions.c.traveler_id == traveler_id,
            pending_actions.c.session_id == session_id,
            pending_actions.c.action_type == action_type,
            pending_actions.c.status == "pending",
            pending_actions.c.expires_at > now,
        ).order_by(pending_actions.c.created_at.desc())).mappings().all()
    for old in pending:
        if old["parameters"] != normalized or _utc(old["expires_at"]) <= now:
            continue
        same_quote = (
            old["target_order_id"] == (order["id"] if order else None)
            and old["target_order_version"] == (order["version"] if order else None)
            and old["before_snapshot"] == before
            and old["after_snapshot"] == after
            and old["price_summary"] == {"unit": "demo", **price}
            and old["policy_id"] == policy["id"]
            and old["policy_version"] == policy["version"]
            and old["offer_version"] == offer_version)
        if same_quote:
            return get_proposal(db, traveler_id, session_id, old["id"], now=now)
    action_id = idempotency_key or str(uuid4())
    db.execute(insert(pending_actions).values(
        id=action_id, traveler_id=traveler_id, session_id=session_id,
        action_type=action_type, target_order_id=order["id"] if order else None,
        target_order_version=order["version"] if order else None,
        parameters=normalized, before_snapshot=before, after_snapshot=after,
        price_summary={"unit": "demo", **price}, policy_id=policy["id"],
        policy_version=policy["version"], offer_version=offer_version,
        status="pending", idempotency_key=action_id,
        created_at=now, expires_at=now + timedelta(minutes=15),
    ))
    db.execute(insert(audit_events).values(
        action_id=action_id, traveler_id=traveler_id, session_id=session_id,
        event_type="proposed", summary={"action_type": action_type}, created_at=now,
    ))
    return get_proposal(db, traveler_id, session_id, action_id, now=now)


def list_proposals(db, traveler_id, session_id, *, now=None):
    now = _utc(now or datetime.now(timezone.utc))
    _owned_session(db, traveler_id, session_id)
    rows = db.execute(select(pending_actions).where(
        pending_actions.c.traveler_id == traveler_id,
        pending_actions.c.session_id == session_id,
    ).order_by(pending_actions.c.created_at.desc(), pending_actions.c.id)).mappings()
    return [_public(row, now) for row in rows]


def get_proposal(db, traveler_id, session_id, action_id, *, now=None):
    now = _utc(now or datetime.now(timezone.utc))
    _owned_session(db, traveler_id, session_id)
    row = db.execute(select(pending_actions).where(
        pending_actions.c.id == action_id,
        pending_actions.c.traveler_id == traveler_id,
        pending_actions.c.session_id == session_id,
    )).mappings().one_or_none()
    if row is None:
        raise ActionNotFound("action not found")
    return _public(row, now)
