"""Derive a small, anonymized SQLite fixture from the large local source."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build(source: Path, output: Path) -> None:
    source_db = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    source_db.row_factory = sqlite3.Row
    try:
        route = ("HAM", "BSL")
        latest = source_db.execute(
            "SELECT MAX(date(scheduled_departure)) FROM flights "
            "WHERE departure_airport = ? AND arrival_airport = ? AND status = 'Scheduled'",
            route,
        ).fetchone()[0]
        if not latest:
            raise ValueError("Source lacks the Hamburg-to-Basel route")
        start = date.fromisoformat(latest) - timedelta(days=3)
        flights = source_db.execute(
            "SELECT flight_id, flight_no, scheduled_departure, scheduled_arrival, "
            "departure_airport, arrival_airport, status FROM flights "
            "WHERE departure_airport = ? AND arrival_airport = ? AND status = 'Scheduled' "
            "AND date(scheduled_departure) >= ? ORDER BY scheduled_departure, flight_id",
            (*route, start.isoformat()),
        ).fetchall()
        airports = source_db.execute(
            "SELECT airport_code, airport_name, city, timezone FROM airports_data "
            "WHERE airport_code IN (?, ?) ORDER BY airport_code", route
        ).fetchall()
        hotels = source_db.execute(
            "SELECT id, name, location, price_tier FROM hotels "
            "WHERE location = 'Basel' ORDER BY id"
        ).fetchall()
        if len(flights) < 4 or len(airports) != 2 or not hotels:
            raise ValueError("Source lacks enough coherent demo records")

        tickets = []
        for flight in flights:
            candidates = source_db.execute(
                "SELECT t.passenger_id, tf.fare_conditions, tf.amount "
                "FROM ticket_flights tf JOIN tickets t ON t.ticket_no = tf.ticket_no "
                "WHERE tf.flight_id = ? ORDER BY t.ticket_no LIMIT 1",
                (flight["flight_id"],),
            ).fetchall()
            if candidates:
                tickets.append((flight["flight_id"], candidates[0]))
            if len(tickets) == 2:
                break
        if len(tickets) != 2:
            raise ValueError("Source lacks two ticketed flights")
    finally:
        source_db.close()

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Fixture already exists; choose a new --output path: {output}")
    fixture = sqlite3.connect(output)
    try:
        fixture.executescript(
            "CREATE TABLE airports_data (airport_code text, airport_name text, city text, timezone text);"
            "CREATE TABLE flights (flight_id integer, flight_no text, scheduled_departure text, "
            "scheduled_arrival text, departure_airport text, arrival_airport text, status text);"
            "CREATE TABLE hotels (id integer, name text, location text, price_tier text);"
            "CREATE TABLE tickets (ticket_no text, book_ref text, passenger_id text);"
            "CREATE TABLE ticket_flights (ticket_no text, flight_id integer, fare_conditions text, amount integer);"
        )
        fixture.executemany("INSERT INTO airports_data VALUES (?, ?, ?, ?)", [tuple(row) for row in airports])
        fixture.executemany("INSERT INTO flights VALUES (?, ?, ?, ?, ?, ?, ?)", [tuple(row) for row in flights])
        fixture.executemany("INSERT INTO hotels VALUES (?, ?, ?, ?)", [tuple(row) for row in hotels])
        for number, (flight_id, ticket) in enumerate(tickets, start=1):
            ticket_no = f"DEMO-TKT-{number}"
            fixture.execute(
                "INSERT INTO tickets VALUES (?, ?, ?)",
                (ticket_no, f"DEMO-BOOK-{number}", f"DEMO-PAX-{number}"),
            )
            fixture.execute(
                "INSERT INTO ticket_flights VALUES (?, ?, ?, ?)",
                (ticket_no, flight_id, ticket["fare_conditions"], ticket["amount"]),
            )
        fixture.commit()
    finally:
        fixture.close()
    print(f"Created {output} with {len(flights)} flights, {len(hotels)} hotels and 2 demo tickets")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=ROOT / "data" / "source" / "travel_new.sqlite")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "fixtures" / "demo_source.sqlite")
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"Source SQLite does not exist: {args.source}")
    build(args.source, args.output)


if __name__ == "__main__":
    main()
