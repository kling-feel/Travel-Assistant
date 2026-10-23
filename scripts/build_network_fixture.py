"""Export only fictional airport and flight fields from the original SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_network_fixture import verify_fixture


DEFAULT_SOURCE = ROOT / "data" / "source" / "travel_new.sqlite"
DEFAULT_OUTPUT = ROOT / "data" / "fixtures" / "network_source.sqlite"
AIRPORT_QUERY = (
    "SELECT airport_code, airport_name, city, coordinates, timezone "
    "FROM airports_data ORDER BY airport_code"
)
FLIGHT_QUERY = (
    "SELECT flight_id, scheduled_departure, scheduled_arrival, actual_departure, "
    "actual_arrival, departure_airport, arrival_airport, status "
    "FROM flights ORDER BY flight_id"
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _airports(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    for code, name, city, raw_coordinates, timezone_name in source.execute(AIRPORT_QUERY):
        coordinates = json.loads(raw_coordinates)
        if not isinstance(coordinates, list) or len(coordinates) != 2:
            raise ValueError(f"Invalid coordinates for {code}")
        lat, lon = coordinates
        if not all(isinstance(value, (int, float)) for value in coordinates):
            raise ValueError(f"Non-numeric coordinates for {code}")
        ZoneInfo(timezone_name)
        target.execute(
            "INSERT INTO airports_data VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, city, lat, lon, timezone_name),
        )


def _flights(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    cursor = source.execute(FLIGHT_QUERY)
    while batch := cursor.fetchmany(1000):
        target.executemany(
            "INSERT INTO flights VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (flight_id, f"SIM-{flight_id}", departure, arrival,
                 actual_departure, actual_arrival, origin, destination, status)
                for (flight_id, departure, arrival, actual_departure, actual_arrival,
                     origin, destination, status) in batch
            ],
        )


def build(source: Path, output: Path) -> dict[str, object]:
    source, output = source.resolve(), output.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if output.exists():
        raise FileExistsError(f"Fixture already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_sha = file_hash(source)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".network_", suffix=".sqlite", dir=output.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as fixture:
                fixture.execute("PRAGMA foreign_keys=ON")
                fixture.executescript(
                    "CREATE TABLE airports_data ("
                    "airport_code TEXT PRIMARY KEY, airport_name TEXT NOT NULL, "
                    "city TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, "
                    "timezone TEXT NOT NULL);"
                    "CREATE TABLE flights ("
                    "flight_id INTEGER PRIMARY KEY, flight_no TEXT NOT NULL UNIQUE, "
                    "scheduled_departure TEXT NOT NULL, scheduled_arrival TEXT NOT NULL, "
                    "actual_departure TEXT, actual_arrival TEXT, "
                    "departure_airport TEXT NOT NULL REFERENCES airports_data(airport_code), "
                    "arrival_airport TEXT NOT NULL REFERENCES airports_data(airport_code), "
                    "status TEXT NOT NULL);"
                )
                with fixture:
                    _airports(original, fixture)
                    _flights(original, fixture)
        report = verify_fixture(temporary, (date(2026, 10, 17), date(2026, 11, 1)))
        if file_hash(source) != source_sha:
            raise RuntimeError("The source SQLite changed while exporting")
        os.rename(temporary, output)
        return {
            "source_sha256": source_sha,
            "output": str(output),
            "output_bytes": output.stat().st_size,
            "output_sha256": file_hash(output),
            **report,
        }
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
