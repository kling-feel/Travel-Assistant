"""Add airport position and an indexed route/date lookup without changing rows."""

from sqlalchemy import inspect, text

VERSION = "005_network"


def upgrade(connection):
    columns = {column["name"] for column in inspect(connection).get_columns("airports")}
    for name in ("latitude", "longitude"):
        if name not in columns:
            connection.execute(text(f"ALTER TABLE airports ADD COLUMN {name} FLOAT"))
    flight_columns = {column["name"] for column in inspect(connection).get_columns("flights")}
    for name in ("actual_departure_at", "actual_arrival_at"):
        if name not in flight_columns:
            connection.execute(text(f"ALTER TABLE flights ADD COLUMN {name} TIMESTAMP WITH TIME ZONE"))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_flights_route_status_departure "
        "ON flights (departure_airport, arrival_airport, status, departure_at)"
    ))
