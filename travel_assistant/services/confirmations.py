"""Apply one saved traveler action in the caller's database transaction."""

from datetime import datetime, timezone

from sqlalchemy import insert, select, update

from travel_assistant.db.travel_models import (action_results, audit_events, flight_offers,
                              flight_orders, flights, hotel_orders,
                              hotel_products, pending_actions, policy_documents,
                              rental_orders, rental_products,
                              travelers)
from travel_assistant.services.actions import (ActionError, ActionNotFound, FIELDS, _date,
                              _policy, _quote, _utc)
from travel_assistant.services.sessions import _owned_session
from travel_assistant.services.travel import (OrderError, book_flight, book_hotel, cancel_flight,
                             cancel_hotel, change_flight, change_hotel)
from travel_assistant.services.rentals import (book_rental, cancel_rental, change_rental,
                              normalize_book_params, normalize_change_params)


def _event(db, row, kind, summary, now):
    db.execute(insert(audit_events).values(
        action_id=row["id"], traveler_id=row["traveler_id"],
        session_id=row["session_id"], event_type=kind,
        summary=summary, created_at=now,
    ))


def _locked_action(db, traveler_id, session_id, action_id):
    session = _owned_session(db, traveler_id, session_id, lock=True)
    row = db.execute(select(pending_actions).where(
        pending_actions.c.id == action_id,
        pending_actions.c.traveler_id == traveler_id,
        pending_actions.c.session_id == session_id,
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        raise ActionNotFound("action not found")
    return session, row


def _terminal(db, row, status, reason, now):
    db.execute(update(pending_actions).where(
        pending_actions.c.id == row["id"], pending_actions.c.status == "pending",
    ).values(status=status))
    _event(db, row, status, {"reason": reason}, now)
    return {"action_id": row["id"], "status": status, "reason": reason}


def _lock_quote_inputs(db, row):
    """Hold rows that determine the quote through the final order update."""
    params = row["parameters"]
    kind = row["action_type"]
    db.execute(select(policy_documents.c.id).where(
        policy_documents.c.id == row["policy_id"]).with_for_update(read=True)).all()
    if kind.startswith("flight_"):
        db.execute(select(travelers.c.id).where(
            travelers.c.id == row["traveler_id"]).with_for_update()).all()
        if kind == "flight_book":
            flight_id = params.get("flight_id")
            if type(flight_id) is int:
                db.execute(select(flights.c.id).where(
                    flights.c.id == flight_id).with_for_update(read=True)).all()
                db.execute(select(flight_offers.c.flight_id).where(
                    flight_offers.c.flight_id == flight_id).with_for_update(read=True)).all()
            return
        order = db.execute(select(flight_orders.c.flight_id).where(
            flight_orders.c.id == row["target_order_id"]).with_for_update()
        ).scalar_one_or_none()
        if order is not None:
            db.execute(select(flights.c.id).where(flights.c.id == order).with_for_update(read=True)).all()
        if kind == "flight_change" and type(params.get("new_flight_id")) is int:
            flight_id = params["new_flight_id"]
            db.execute(select(flights.c.id).where(flights.c.id == flight_id).with_for_update(read=True)).all()
            db.execute(select(flight_offers.c.flight_id).where(
                flight_offers.c.flight_id == flight_id).with_for_update(read=True)).all()
    elif kind.startswith("rental_"):
        db.execute(select(travelers.c.id).where(
            travelers.c.id == row["traveler_id"]).with_for_update()).all()
        if kind in ("rental_cancel", "rental_change"):
            product_id = db.execute(select(rental_orders.c.product_id).where(
                rental_orders.c.id == row["target_order_id"]
            ).with_for_update()).scalar_one_or_none()
        else:
            product_id = params.get("product_id")
        if product_id:
            db.execute(select(rental_products.c.id).where(
                rental_products.c.id == product_id).with_for_update(read=True)).all()
    else:
        if kind != "hotel_book":
            db.execute(select(hotel_orders.c.id).where(
                hotel_orders.c.id == row["target_order_id"]).with_for_update()).all()
        product_id = params.get("product_id") if kind != "hotel_cancel" else None
        if product_id:
            db.execute(select(hotel_products.c.id).where(
                hotel_products.c.id == product_id).with_for_update(read=True)).all()
        if kind != "hotel_book":
            old_product = db.execute(select(hotel_orders.c.product_id).where(
                hotel_orders.c.id == row["target_order_id"])).scalar_one_or_none()
            if old_product:
                db.execute(select(hotel_products.c.id).where(
                    hotel_products.c.id == old_product).with_for_update(read=True)).all()


def _fresh_quote(db, row, now):
    params, kind = row["parameters"], row["action_type"]
    if kind not in FIELDS or not isinstance(params, dict) or set(params) != FIELDS[kind]:
        raise ActionError("action parameters changed")
    if kind in ("hotel_book", "hotel_change"):
        for key in ("check_in", "check_out"):
            if type(params[key]) is not str or _date(params[key]).isoformat() != params[key]:
                raise ActionError("action dates changed")
    if kind in ("rental_book", "rental_change"):
        try:
            normalized = (normalize_book_params(db, params)
                          if kind == "rental_book" else
                          normalize_change_params(db, row["traveler_id"], params))
            if normalized != params:
                raise ActionError("action rental times changed")
        except OrderError as error:
            raise ActionError(str(error)) from error
    policy = _policy(db, kind)
    order, before, after, price, offer_version = _quote(
        db, row["traveler_id"], kind, params, now)
    if (policy["id"] != row["policy_id"] or policy["version"] != row["policy_version"]
            or (order["id"] if order else None) != row["target_order_id"]
            or (order["version"] if order else None) != row["target_order_version"]
            or before != row["before_snapshot"] or after != row["after_snapshot"]
            or {"unit": "demo", **price} != row["price_summary"]
            or offer_version != row["offer_version"]):
        raise ActionError("proposal content or quote changed")
    return price


def _order_snapshot(db, table, order_id):
    row = db.execute(select(table).where(table.c.id == order_id)).mappings().one()
    return {key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in row.items()}


def confirm_action(db, traveler_id, session_id, action_id, *, now=None):
    """Return result or conflict; caller commits both success and terminal conflicts."""
    session, row = _locked_action(db, traveler_id, session_id, action_id)
    now = _utc(now or datetime.now(timezone.utc))
    if row["status"] == "confirmed":
        result = db.execute(select(action_results).where(
            action_results.c.action_id == action_id,
            action_results.c.traveler_id == traveler_id,
        )).mappings().one()
        return {"action_id": action_id, "status": "confirmed",
                "order_id": result["order_id"], "order": result["order_snapshot"],
                "price": result["price_summary"],
                "simulation_notice": "模拟核算，未发生支付或退款"}
    if row["status"] != "pending":
        return {"action_id": action_id, "status": row["status"],
                "reason": "action is no longer pending"}
    if _utc(row["expires_at"]) <= now:
        return _terminal(db, row, "expired", "confirmation window expired", now)
    if session["status"] != "active":
        return _terminal(db, row, "invalidated", "session is closed", now)
    try:
        _lock_quote_inputs(db, row)
        price = _fresh_quote(db, row, now)
    except (ActionError, ValueError) as error:
        return _terminal(db, row, "invalidated", str(error), now)

    params, kind = row["parameters"], row["action_type"]
    order_id = row["target_order_id"]
    version = row["target_order_version"]
    try:
        if kind == "flight_book":
            order_id = book_flight(db, traveler_id, params["flight_id"],
                                   price["new_amount"], now=now)
        elif kind == "flight_change":
            change_flight(db, traveler_id, order_id, version,
                          params["new_flight_id"], price["new_amount"])
        elif kind == "flight_cancel":
            cancel_flight(db, traveler_id, order_id, version)
        elif kind == "hotel_book":
            order_id = book_hotel(db, traveler_id, params["product_id"],
                                  _date(params["check_in"]), _date(params["check_out"]))
        elif kind == "hotel_change":
            change_hotel(db, traveler_id, order_id, version, params["product_id"],
                         _date(params["check_in"]), _date(params["check_out"]))
        elif kind == "hotel_cancel":
            cancel_hotel(db, traveler_id, order_id, version)
        elif kind == "rental_book":
            order_id = book_rental(db, traveler_id, params, price["new_amount"], now=now)
        elif kind == "rental_change":
            change_rental(db, traveler_id, order_id, version,
                          row["after_snapshot"], now=now)
        elif kind == "rental_cancel":
            cancel_rental(db, traveler_id, order_id, version, now=now)
    except OrderError as error:
        return _terminal(db, row, "invalidated", str(error), now)
    table = (flight_orders if kind.startswith("flight_") else
             rental_orders if kind.startswith("rental_") else hotel_orders)
    snapshot = _order_snapshot(db, table, order_id)
    db.execute(update(pending_actions).where(
        pending_actions.c.id == action_id, pending_actions.c.status == "pending",
    ).values(status="confirmed"))
    db.execute(insert(action_results).values(
        action_id=action_id, traveler_id=traveler_id, session_id=session_id,
        order_id=order_id, order_snapshot=snapshot,
        price_summary=row["price_summary"], created_at=now,
    ))
    _event(db, row, "confirmed", {"order_id": order_id}, now)
    return {"action_id": action_id, "status": "confirmed", "order_id": order_id,
            "order": snapshot, "price": row["price_summary"],
            "simulation_notice": "模拟核算，未发生支付或退款"}


def reject_action(db, traveler_id, session_id, action_id, *, now=None):
    session, row = _locked_action(db, traveler_id, session_id, action_id)
    now = _utc(now or datetime.now(timezone.utc))
    if row["status"] == "rejected":
        return {"action_id": action_id, "status": "rejected"}
    if row["status"] != "pending":
        return {"action_id": action_id, "status": row["status"],
                "reason": "action is no longer pending"}
    if _utc(row["expires_at"]) <= now:
        return _terminal(db, row, "expired", "confirmation window expired", now)
    if session["status"] != "active":
        return _terminal(db, row, "invalidated", "session is closed", now)
    db.execute(update(pending_actions).where(
        pending_actions.c.id == action_id, pending_actions.c.status == "pending",
    ).values(status="rejected"))
    _event(db, row, "rejected", {}, now)
    return {"action_id": action_id, "status": "rejected"}


def list_action_history(db, traveler_id):
    rows = db.execute(select(audit_events).where(
        audit_events.c.traveler_id == traveler_id,
    ).order_by(audit_events.c.created_at.desc(), audit_events.c.id.desc())).mappings()
    return [{"action_id": row["action_id"], "session_id": row["session_id"],
             "event_type": row["event_type"], "summary": row["summary"],
             "created_at": row["created_at"]} for row in rows]
