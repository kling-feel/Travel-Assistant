"""Create and optionally load a small, repeatable PostgreSQL demo data set.

The source SQLite file is opened read-only. Generated SQL goes to an ignored
directory; no large source database is copied into the Docker image or Git.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "fixtures" / "demo_source.sqlite"
DEFAULT_OUTPUT = ROOT / "data" / "generated" / "demo_seed.sql"


def sql_value(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def insert(table: str, columns: tuple[str, ...], values: tuple[object, ...]) -> str:
    return (
        f"INSERT INTO demo_source.{table} ({', '.join(columns)}) VALUES "
        f"({', '.join(sql_value(value) for value in values)});"
    )


def moved_datetime(value: str | None, days: int) -> str | None:
    if not value or value == r"\N":
        return None
    return (datetime.fromisoformat(value) + timedelta(days=days)).isoformat(sep=" ")


def collect_data(connection: sqlite3.Connection, as_of: date) -> tuple[str, dict[str, object]]:
    connection.row_factory = sqlite3.Row
    route = ("HAM", "BSL")  # Hamburg -> Basel; the source has Basel hotels.
    latest = connection.execute(
        "SELECT MAX(date(scheduled_departure)) FROM flights "
        "WHERE departure_airport = ? AND arrival_airport = ? AND status = 'Scheduled'",
        route,
    ).fetchone()[0]
    if not latest:
        raise ValueError("Source has no scheduled Hamburg-to-Basel flights")
    start = date.fromisoformat(latest) - timedelta(days=3)
    flights = connection.execute(
        "SELECT flight_id, flight_no, scheduled_departure, scheduled_arrival, "
        "departure_airport, arrival_airport, status FROM flights "
        "WHERE departure_airport = ? AND arrival_airport = ? AND status = 'Scheduled' "
        "AND date(scheduled_departure) >= ? ORDER BY scheduled_departure, flight_id",
        (*route, start.isoformat()),
    ).fetchall()
    if len(flights) < 4:
        raise ValueError("Source has too few route flights for the demo")
    start = date.fromisoformat(flights[0]["scheduled_departure"][:10])
    shift_days = (as_of + timedelta(days=1) - start).days

    airports = connection.execute(
        "SELECT airport_code, airport_name, city, timezone FROM airports_data "
        "WHERE airport_code IN (?, ?) ORDER BY airport_code",
        route,
    ).fetchall()
    hotels = connection.execute(
        "SELECT id, name, location, price_tier FROM hotels "
        "WHERE location = 'Basel' ORDER BY id"
    ).fetchall()
    if len(airports) != 2 or not hotels:
        raise ValueError("Source lacks route airports or Basel hotels")

    # Take one ticket from each of two flights, so the two demo travelers have
    # distinct existing source bookings. Do not import unrelated passengers.
    orders = []
    passenger_ids = set()
    for flight in flights:
        candidates = connection.execute(
            "SELECT t.ticket_no, t.book_ref, t.passenger_id, tf.fare_conditions, "
            "tf.amount FROM ticket_flights tf "
            "JOIN tickets t ON t.ticket_no = tf.ticket_no "
            "WHERE tf.flight_id = ? ORDER BY t.ticket_no LIMIT 5",
            (flight["flight_id"],),
        ).fetchall()
        for candidate in candidates:
            if candidate["passenger_id"] not in passenger_ids:
                orders.append((flight["flight_id"], candidate))
                passenger_ids.add(candidate["passenger_id"])
                break
        if len(orders) == 2:
            break
    if len(orders) != 2:
        raise ValueError("Source lacks two distinct ticketed travelers on selected flights")

    lines = [
        "-- Generated from an existing fictional SQLite database. Do not edit by hand.",
        "BEGIN;",
        "CREATE SCHEMA IF NOT EXISTS demo_source;",
        "CREATE TABLE IF NOT EXISTS demo_source.airports ("
        "airport_code text PRIMARY KEY, airport_name text, city text, timezone text);",
        "CREATE TABLE IF NOT EXISTS demo_source.flights ("
        "source_flight_id integer PRIMARY KEY, flight_no text NOT NULL, "
        "scheduled_departure timestamptz NOT NULL, scheduled_arrival timestamptz NOT NULL, "
        "departure_airport text NOT NULL REFERENCES demo_source.airports(airport_code), "
        "arrival_airport text NOT NULL REFERENCES demo_source.airports(airport_code), status text NOT NULL);",
        "CREATE TABLE IF NOT EXISTS demo_source.hotels ("
        "source_hotel_id integer PRIMARY KEY, name text NOT NULL, city text NOT NULL, "
        "price_tier text, available_from date NOT NULL, available_until date NOT NULL);",
        "CREATE TABLE IF NOT EXISTS demo_source.travelers ("
        "demo_traveler_id text PRIMARY KEY, display_name text NOT NULL, "
        "source_passenger_id text NOT NULL UNIQUE);",
        "CREATE TABLE IF NOT EXISTS demo_source.flight_orders ("
        "source_ticket_no text PRIMARY KEY, demo_traveler_id text NOT NULL "
        "REFERENCES demo_source.travelers(demo_traveler_id), "
        "source_flight_id integer NOT NULL REFERENCES demo_source.flights(source_flight_id), "
        "source_book_ref text NOT NULL, fare_conditions text, amount integer, "
        "status text NOT NULL);",
        "TRUNCATE demo_source.flight_orders, demo_source.travelers, "
        "demo_source.flights, demo_source.hotels, demo_source.airports;",
    ]
    for airport in airports:
        lines.append(insert("airports", ("airport_code", "airport_name", "city", "timezone"), tuple(airport)))
    for flight in flights:
        lines.append(
            insert(
                "flights",
                (
                    "source_flight_id", "flight_no", "scheduled_departure", "scheduled_arrival",
                    "departure_airport", "arrival_airport", "status",
                ),
                (
                    flight["flight_id"], flight["flight_no"],
                    moved_datetime(flight["scheduled_departure"], shift_days),
                    moved_datetime(flight["scheduled_arrival"], shift_days),
                    flight["departure_airport"], flight["arrival_airport"], flight["status"],
                ),
            )
        )
    for hotel in hotels:
        lines.append(
            insert(
                "hotels",
                ("source_hotel_id", "name", "city", "price_tier", "available_from", "available_until"),
                (
                    hotel["id"], hotel["name"], hotel["location"], hotel["price_tier"],
                    (as_of + timedelta(days=1)).isoformat(),
                    (as_of + timedelta(days=30)).isoformat(),
                ),
            )
        )
    for number, (flight_id, ticket) in enumerate(orders, start=1):
        traveler_id = f"demo_traveler_{number}"
        lines.append(
            insert(
                "travelers", ("demo_traveler_id", "display_name", "source_passenger_id"),
                (traveler_id, f"演示旅客 {number}", ticket["passenger_id"]),
            )
        )
        lines.append(
            insert(
                "flight_orders",
                (
                    "source_ticket_no", "demo_traveler_id", "source_flight_id",
                    "source_book_ref", "fare_conditions", "amount", "status",
                ),
                (
                    ticket["ticket_no"], traveler_id, flight_id, ticket["book_ref"],
                    ticket["fare_conditions"], ticket["amount"], "confirmed",
                ),
            )
        )
    lines.append("COMMIT;")
    summary = {
        "as_of": as_of.isoformat(),
        "source": str(DEFAULT_SOURCE.name),
        "airports": len(airports),
        "flights": len(flights),
        "hotels": len(hotels),
        "travelers": len(orders),
        "flight_orders": len(orders),
    }
    return "\n".join(lines) + "\n", summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--apply", action="store_true", help="Load into the Docker PostgreSQL service")
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_file():
        parser.error(f"SQLite source does not exist: {source}")
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        sql, summary = collect_data(connection, args.as_of)
    finally:
        connection.close()
    summary["source"] = source.name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(sql, encoding="utf-8")
    summary["output"] = str(args.output.resolve())
    if args.apply:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "db", "psql", "-U", "travel_app",
             "-d", "travel_demo", "-v", "ON_ERROR_STOP=1"],
            input=sql, text=True, check=True, cwd=ROOT, encoding="utf-8",
        )
        summary["applied"] = True
    else:
        summary["applied"] = False
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
