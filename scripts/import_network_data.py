"""Explicitly replace the fictional PostgreSQL demo snapshot from the safe fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import engine
from db.travel_models import (action_results, airports, audit_events, chat_messages,
                              chat_turns,
                              chat_sessions, demo_state, flight_offers, flight_orders, flights,
                              hotel_orders, hotel_products, hotels, rental_orders,
                              rental_products, pending_actions,
                              policy_documents, travelers)
from scripts.verify_network_fixture import _shift, verify_fixture
from services.city_products import generate_products
from services.demo_pricing import (NETWORK_OFFER_RULE_ID, OFFER_VERSION,
                                   network_fare)
from services.policy_catalog import SEED_POLICIES
from utils.password_hash import get_hashed_password


FIXTURE = ROOT / "data" / "fixtures" / "network_source.sqlite"
FLIGHT_BATCH = 1000


def _utc_shift(value: str | None, shift: timedelta):
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc) + shift


def prepare_snapshot(source: Path, as_of: date):
    """Validate source and build one consistent network snapshot without DB writes."""
    report = verify_fixture(source, (as_of,))
    with closing(sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        shift, _ = _shift(db, as_of)
        airport_rows = [dict(code=row["airport_code"], name=row["airport_name"],
                             city=row["city"], timezone=row["timezone"],
                             latitude=row["latitude"], longitude=row["longitude"])
                        for row in db.execute("SELECT * FROM airports_data ORDER BY airport_code")]
        zones = {row["code"]: ZoneInfo(row["timezone"]) for row in airport_rows}
        flight_rows, offer_rows = [], []
        statuses: Counter[str] = Counter()
        for row in db.execute("SELECT * FROM flights ORDER BY flight_id"):
            departure = _utc_shift(row["scheduled_departure"], shift)
            flight_rows.append(dict(
                id=row["flight_id"], flight_no=row["flight_no"],
                departure_at=departure,
                arrival_at=_utc_shift(row["scheduled_arrival"], shift),
                actual_departure_at=_utc_shift(row["actual_departure"], shift),
                actual_arrival_at=_utc_shift(row["actual_arrival"], shift),
                departure_airport=row["departure_airport"],
                arrival_airport=row["arrival_airport"], status=row["status"],
            ))
            statuses[row["status"]] += 1
            local_date = departure.astimezone(zones[row["departure_airport"]]).date()
            if row["status"] == "Scheduled" and (
                    as_of + timedelta(days=1) <= local_date <= as_of + timedelta(days=30)):
                offer_rows.append(dict(
                    flight_id=row["flight_id"],
                    amount=network_fare(row["flight_id"], row["departure_airport"],
                                        row["arrival_airport"]),
                    version=OFFER_VERSION, rule_id=NETWORK_OFFER_RULE_ID,
                ))
    expected_offers = report["windows"][as_of.isoformat()]["scheduled_flights"]
    if (len(airport_rows), len(flight_rows), len(offer_rows)) != (
            115, 33121, expected_offers):
        raise ValueError("Unexpected network snapshot counts")
    offered_ids = {offer["flight_id"] for offer in offer_rows}
    if len({(row["departure_airport"], row["arrival_airport"])
            for row in flight_rows if row["id"] in offered_ids}) != 618:
        raise ValueError("Not all bookable routes have an offer")
    if statuses["Scheduled"] != 15383 or report["scheduled_routes"] != 618:
        raise ValueError("Unexpected flight states or route coverage")
    by_id = {row["id"]: row for row in flight_rows}
    fares = {row["flight_id"]: row["amount"] for row in offer_rows}
    for flight_id in (8144, 8152):
        if flight_id not in fares or by_id[flight_id]["departure_airport"] != "HAM":
            raise ValueError("Demo flight or alternative is not available")
    return airport_rows, flight_rows, offer_rows, report


def _batches(rows, size=FLIGHT_BATCH):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _stage(connection, airport_rows, flight_rows, offer_rows):
    connection.execute(text(
        "CREATE TEMP TABLE import_airports (code varchar(8) PRIMARY KEY, "
        "name varchar(120) NOT NULL, city varchar(80) NOT NULL, "
        "timezone varchar(80) NOT NULL, latitude double precision NOT NULL, "
        "longitude double precision NOT NULL) ON COMMIT DROP"
    ))
    connection.execute(text(
        "CREATE TEMP TABLE import_flights (id integer PRIMARY KEY, "
        "flight_no varchar(30) NOT NULL UNIQUE, departure_at timestamptz NOT NULL, "
        "arrival_at timestamptz NOT NULL, actual_departure_at timestamptz, "
        "actual_arrival_at timestamptz, departure_airport varchar(8) NOT NULL "
        "REFERENCES import_airports(code), arrival_airport varchar(8) NOT NULL "
        "REFERENCES import_airports(code), status varchar(30) NOT NULL, "
        "CHECK (arrival_at > departure_at)) ON COMMIT DROP"
    ))
    connection.execute(text(
        "CREATE TEMP TABLE import_offers (flight_id integer PRIMARY KEY "
        "REFERENCES import_flights(id), amount integer NOT NULL CHECK (amount >= 0), "
        "version integer NOT NULL, rule_id varchar(80) NOT NULL) ON COMMIT DROP"
    ))
    statements = (
        ("INSERT INTO import_airports (code, name, city, timezone, latitude, longitude) "
         "VALUES (:code, :name, :city, :timezone, :latitude, :longitude)", airport_rows),
        ("INSERT INTO import_flights (id, flight_no, departure_at, arrival_at, "
         "actual_departure_at, actual_arrival_at, departure_airport, arrival_airport, status) "
         "VALUES (:id, :flight_no, :departure_at, :arrival_at, :actual_departure_at, "
         ":actual_arrival_at, :departure_airport, :arrival_airport, :status)", flight_rows),
        ("INSERT INTO import_offers (flight_id, amount, version, rule_id) "
         "VALUES (:flight_id, :amount, :version, :rule_id)", offer_rows),
    )
    for sql, rows in statements:
        for batch in _batches(rows):
            connection.execute(text(sql), batch)
    counts = connection.execute(text(
        "SELECT (SELECT count(*) FROM import_airports), "
        "(SELECT count(*) FROM import_flights), "
        "(SELECT count(*) FROM import_offers), "
        "(SELECT count(*) FROM import_flights WHERE status = 'Scheduled')"
    )).one()
    if tuple(counts) != (115, 33121, len(offer_rows), 15383):
        raise ValueError(f"Staged network counts changed: {counts}")
    routes = connection.execute(text(
        "SELECT count(*) FROM (SELECT DISTINCT f.departure_airport, f.arrival_airport "
        "FROM import_flights f JOIN import_offers o ON o.flight_id = f.id) routes"
    )).scalar_one()
    if routes != 618:
        raise ValueError("Staged bookable routes changed")


def _formal_reset(connection, as_of, passwords, fixture_sha256, products):
    users = set(connection.execute(select(travelers.c.username)).scalars())
    if users - {"demo1", "demo2"}:
        raise RuntimeError("Non-demo account exists; refusing to reset")
    if connection.execute(select(func.count()).select_from(pending_actions).where(
            pending_actions.c.status == "pending")).scalar_one():
        raise RuntimeError("Pending actions exist; resolve them before resetting")
    if connection.execute(text("SELECT to_regclass('public.checkpoints')")).scalar_one() is not None:
        for table_name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            connection.execute(text(f"DELETE FROM {table_name}"))
    for table in (action_results, audit_events, pending_actions, chat_turns,
                  chat_messages,
                  chat_sessions, flight_offers, rental_orders, hotel_orders, flight_orders,
                  hotel_products, rental_products, policy_documents, hotels, flights, airports,
                  travelers):
        connection.execute(delete(table))
    connection.execute(delete(demo_state))
    connection.execute(text(
        "INSERT INTO airports (code, name, city, timezone, latitude, longitude) "
        "SELECT code, name, city, timezone, latitude, longitude FROM import_airports"
    ))
    connection.execute(text(
        "INSERT INTO flights (id, flight_no, departure_at, arrival_at, "
        "actual_departure_at, actual_arrival_at, departure_airport, arrival_airport, status) "
        "SELECT id, flight_no, departure_at, arrival_at, actual_departure_at, "
        "actual_arrival_at, departure_airport, arrival_airport, status FROM import_flights"
    ))
    connection.execute(text(
        "INSERT INTO flight_offers (flight_id, amount, version, rule_id) "
        "SELECT flight_id, amount, version, rule_id FROM import_offers"
    ))
    connection.execute(insert(demo_state).values(
        id=1, as_of=as_of, fixture_sha256=fixture_sha256,
    ))
    connection.execute(insert(travelers), [dict(
        id=f"demo_traveler_{number}", username=f"demo{number}",
        password_hash=get_hashed_password(passwords[number]),
        display_name=f"演示旅客 {number}", active=1,
    ) for number in (1, 2)])
    hotel_rows, room_rows, rental_rows = products
    connection.execute(insert(hotels), hotel_rows)
    connection.execute(insert(hotel_products), room_rows)
    connection.execute(insert(rental_products), rental_rows)
    fares = dict(connection.execute(select(flight_offers.c.flight_id,
                                          flight_offers.c.amount).where(
        flight_offers.c.flight_id.in_((8144, 8152)))).all())
    if len(fares) != 2:
        raise ValueError("Demo flights have no candidate offers")
    connection.execute(insert(flight_orders), [dict(
        id=f"demo-flight-order-{number}", traveler_id=f"demo_traveler_{number}",
        flight_id=flight_id, fare_conditions="模拟经济舱",
        amount=fares[flight_id], status="confirmed", version=1,
    ) for number, flight_id in ((1, 8144), (2, 8152))])
    connection.execute(insert(hotel_orders), [dict(
        id=f"demo-hotel-order-{number}", traveler_id=f"demo_traveler_{number}",
        product_id=f"room-{number}", check_in=as_of + timedelta(days=1),
        check_out=as_of + timedelta(days=2), total_price=90 + number * 30,
        status="confirmed" if number == 1 else "cancelled", version=1,
    ) for number in (1, 2)])
    connection.execute(insert(policy_documents), list(SEED_POLICIES))


def apply_snapshot(airport_rows, flight_rows, offer_rows, as_of, passwords,
                   fixture_sha256):
    products = generate_products(airport_rows, flight_rows, offer_rows, as_of)
    with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("Network import requires PostgreSQL")
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('travel_demo_reset'))"))
        _stage(connection, airport_rows, flight_rows, offer_rows)
        _formal_reset(connection, as_of, passwords, fixture_sha256, products)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=FIXTURE)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reset-demo", action="store_true",
                        help="Acknowledge that existing demo orders and sessions are replaced")
    parser.add_argument("--backup", type=Path,
                        help="Existing verified local pg_dump file; required for --apply")
    args = parser.parse_args()
    airports_data, flights_data, offers_data, report = prepare_snapshot(args.source, args.as_of)
    with engine.connect() as connection:
        current = {table.name: connection.execute(
            select(func.count()).select_from(table)).scalar_one()
            for table in (travelers, flights, flight_orders, hotel_orders, rental_orders,
                          chat_sessions, pending_actions, action_results, audit_events)}
        users = connection.execute(select(travelers.c.username)).scalars().all()
    print(json.dumps({"as_of": args.as_of.isoformat(), "current": current,
                      "accounts": users, "incoming": {
                          "airports": len(airports_data), "flights": len(flights_data),
                          "offers": len(offers_data), "routes": report["scheduled_routes"]},
                      "reset_clears": ["demo orders", "sessions", "actions", "results", "audit"]},
                     ensure_ascii=False, indent=2))
    if not args.apply:
        return
    if not args.reset_demo or not args.backup or not args.backup.is_file():
        parser.error("--apply requires --reset-demo and a verified --backup file")
    with args.backup.open("rb") as backup:
        if backup.read(5) != b"PGDMP":
            parser.error("--backup is not a PostgreSQL custom-format dump")
    passwords = {number: os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD", "")
                 for number in (1, 2)}
    if any(len(value) < 12 or value.startswith("replace-with") for value in passwords.values()):
        parser.error("Configure the two local demo passwords first")
    fixture_sha256 = hashlib.sha256(args.source.read_bytes()).hexdigest()
    apply_snapshot(airports_data, flights_data, offers_data, args.as_of, passwords,
                   fixture_sha256)
    print("Network snapshot applied and committed")


if __name__ == "__main__":
    main()
