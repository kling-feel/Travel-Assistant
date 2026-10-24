"""Validate the fictional full-network fixture without touching PostgreSQL."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "fixtures" / "network_source.sqlite"
AIRPORT_COLUMNS = ("airport_code", "airport_name", "city", "latitude", "longitude", "timezone")
FLIGHT_COLUMNS = (
    "flight_id", "flight_no", "scheduled_departure", "scheduled_arrival",
    "actual_departure", "actual_arrival", "departure_airport", "arrival_airport", "status",
)
EXPECTED_STATUSES = {
    "Arrived": 16707, "Cancelled": 414, "Delayed": 41,
    "Departed": 58, "On Time": 518, "Scheduled": 15383,
}
ANCHOR_ID = 8144
ALTERNATIVE_ID = 8152


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def reachable_cities(path: Path = DEFAULT_SOURCE) -> list[str]:
    """Read the scheduled arrival cities from the delivered fixture."""
    with closing(_connect(path)) as db:
        rows = db.execute(
            "SELECT DISTINCT a.city FROM flights f "
            "JOIN airports_data a ON a.airport_code=f.arrival_airport "
            "WHERE f.status='Scheduled' ORDER BY a.city"
        ).fetchall()
    cities = [row["city"] for row in rows]
    if len(cities) != 95:
        raise RuntimeError("expected 95 reachable fixture cities")
    return cities


def _columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError(f"Timestamp lacks UTC offset: {value}")
    return parsed


def _shift(db: sqlite3.Connection, as_of: date) -> tuple[timedelta, dict[str, str]]:
    rows = db.execute(
        "SELECT flight_id, scheduled_departure, departure_airport, arrival_airport, status "
        "FROM flights WHERE flight_id IN (?, ?)", (ANCHOR_ID, ALTERNATIVE_ID),
    ).fetchall()
    flights = {row["flight_id"]: row for row in rows}
    if set(flights) != {ANCHOR_ID, ALTERNATIVE_ID}:
        raise ValueError("The booked flight or its alternative is missing")
    anchor, alternative = flights[ANCHOR_ID], flights[ALTERNATIVE_ID]
    if any(row["status"] != "Scheduled" or row["departure_airport"] != "HAM"
           or row["arrival_airport"] != "BSL" for row in rows):
        raise ValueError("The afternoon demonstration flights changed")
    departure = _datetime(anchor["scheduled_departure"])
    other = _datetime(alternative["scheduled_departure"])
    if (departure.date() != other.date() or not 12 <= departure.hour < 18
            or not departure < other or other - departure != timedelta(minutes=15)):
        raise ValueError("The demonstration alternative is not 15 minutes later")
    target = datetime.combine(as_of + timedelta(days=1), departure.time().replace(tzinfo=None),
                              ZoneInfo("Europe/Berlin"))
    shift = target.astimezone(timezone.utc) - departure.astimezone(timezone.utc)
    moved = {
        "anchor": (departure.astimezone(timezone.utc) + shift).astimezone(ZoneInfo("Europe/Berlin")).isoformat(),
        "alternative": (other.astimezone(timezone.utc) + shift).astimezone(ZoneInfo("Europe/Berlin")).isoformat(),
    }
    return shift, moved


def _available_ids(db: sqlite3.Connection, origin: str, destination: str,
                   travel_date: date, as_of: date, shift: timedelta,
                   zones: dict[str, ZoneInfo]) -> list[int]:
    if not as_of + timedelta(days=1) <= travel_date <= as_of + timedelta(days=30):
        return []
    if origin not in zones or destination not in zones:
        return []
    rows = db.execute(
        "SELECT flight_id, scheduled_departure FROM flights "
        "WHERE departure_airport=? AND arrival_airport=? AND status='Scheduled'",
        (origin, destination),
    )
    return [row["flight_id"] for row in rows if
            (_datetime(row["scheduled_departure"]).astimezone(timezone.utc) + shift)
            .astimezone(zones[origin]).date() == travel_date]


def verify_fixture(path: Path, as_of_dates: tuple[date, ...]) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(_connect(path)) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if tables != {"airports_data", "flights"}:
            raise ValueError(f"Unexpected fixture tables: {sorted(tables)}")
        if _columns(db, "airports_data") != AIRPORT_COLUMNS or _columns(db, "flights") != FLIGHT_COLUMNS:
            raise ValueError("Fixture contains an unexpected or missing column")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Flight references a missing airport")

        zones: dict[str, ZoneInfo] = {}
        for row in db.execute("SELECT * FROM airports_data"):
            lat, lon = row["latitude"], row["longitude"]
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"Invalid airport coordinates: {row['airport_code']}")
            zones[row["airport_code"]] = ZoneInfo(row["timezone"])
        if len(zones) != 115:
            raise ValueError(f"Expected 115 airports, got {len(zones)}")

        shifts = {as_of: _shift(db, as_of) for as_of in as_of_dates}
        for as_of, (shift, _) in shifts.items():
            tomorrow = as_of + timedelta(days=1)
            choices = _available_ids(db, "HAM", "BSL", tomorrow, as_of, shift, zones)
            if not {ANCHOR_ID, ALTERNATIVE_ID}.issubset(choices):
                raise ValueError(f"Demonstration alternatives are not available on {tomorrow}")
            if (_available_ids(db, "ZZZ", "BSL", tomorrow, as_of, shift, zones)
                    or _available_ids(db, "HAM", "BSL", as_of + timedelta(days=31),
                                      as_of, shift, zones)):
                raise ValueError("A missing route or out-of-range date returned flights")
        statuses: Counter[str] = Counter()
        all_scheduled_routes: set[tuple[str, str]] = set()
        windows: dict[date, dict[str, object]] = {
            as_of: {"routes": set(), "flights": 0, "first": None, "last": None}
            for as_of in as_of_dates
        }
        count = 0
        for row in db.execute("SELECT * FROM flights ORDER BY flight_id"):
            count += 1
            if row["flight_no"] != f"SIM-{row['flight_id']}":
                raise ValueError(f"Non-fictional flight number: {row['flight_id']}")
            if row["departure_airport"] not in zones or row["arrival_airport"] not in zones:
                raise ValueError(f"Unknown airport on flight {row['flight_id']}")
            departure = _datetime(row["scheduled_departure"])
            arrival = _datetime(row["scheduled_arrival"])
            if arrival <= departure:
                raise ValueError(f"Invalid flight duration: {row['flight_id']}")
            actual_departure = _datetime(row["actual_departure"])
            actual_arrival = _datetime(row["actual_arrival"])
            statuses[row["status"]] += 1
            route = (row["departure_airport"], row["arrival_airport"])
            if row["status"] == "Scheduled":
                all_scheduled_routes.add(route)
            for as_of, (shift, _) in shifts.items():
                moved_departure = departure.astimezone(timezone.utc) + shift
                moved_arrival = arrival.astimezone(timezone.utc) + shift
                if moved_arrival - moved_departure != arrival - departure:
                    raise ValueError(f"Flight interval changed: {row['flight_id']}")
                for actual in (actual_departure, actual_arrival):
                    if actual is not None and ((actual.astimezone(timezone.utc) + shift)
                                               - actual.astimezone(timezone.utc) != shift):
                        raise ValueError(f"Actual time shift changed: {row['flight_id']}")
                if row["status"] != "Scheduled":
                    continue
                local_date = moved_departure.astimezone(
                    zones[row["departure_airport"]]
                ).date()
                if as_of + timedelta(days=1) <= local_date <= as_of + timedelta(days=30):
                    window = windows[as_of]
                    window["routes"].add(route)
                    window["flights"] += 1
                    window["first"] = min(window["first"] or local_date, local_date)
                    window["last"] = max(window["last"] or local_date, local_date)
        if count != 33121 or dict(statuses) != EXPECTED_STATUSES or len(all_scheduled_routes) != 618:
            raise ValueError("Flight count, status distribution, or route coverage changed")
        for as_of, window in windows.items():
            if len(window["routes"]) != 618:
                raise ValueError(f"Not every route has a future scheduled flight on {as_of}")

        return {
            "airports": len(zones), "flights": count,
            "statuses": dict(sorted(statuses.items())),
            "scheduled_routes": len(all_scheduled_routes),
            "windows": {
                as_of.isoformat(): {
                    "scheduled_flights": window["flights"],
                    "scheduled_routes": len(window["routes"]),
                    "first_local_date": window["first"].isoformat(),
                    "last_local_date": window["last"].isoformat(),
                    **shifts[as_of][1],
                }
                for as_of, window in windows.items()
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--as-of", type=date.fromisoformat, action="append")
    args = parser.parse_args()
    dates = tuple(args.as_of or (date.today(),))
    print(json.dumps(verify_fixture(args.source, dates), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
